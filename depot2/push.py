"""The depot's push to its own git remote: every `backup.git.every_commits` commits, detached.

- The remote: any git remote the owner added once, by name (`backup.git.remote`, default `backup`). The tool never
  stores or invents a URL. No such remote: silence, and no attempt.
- The check: after every command, `after_command` counts the commits HEAD has that the remote lacks, locally; at the
  threshold it starts `depot2 backup --push --auto` detached, with its own lock and a retry pause.
- The scans (`backup.git.scan`): what the push carries (its blobs and commit messages, exported) and the files on disk;
  the last line of each must be exactly CLEAN, and only that verdict is shown. Null refuses to push; false pushes
  without a scan.
- gate_raw: an executor's raw stream is scanned the same way before the depot's git takes it; one that is not CLEAN
  goes to `backup.git.quarantine`, outside the depot, and a marker takes its place.
"""

import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time

from . import backup
from .i18n import t
from .store import atomic_write, now_iso, read_json, write_json

STAMP = "git.json"  # all four in .backup/, which git ignores and the Drive archive leaves out
LOG = "git.log"
LOCK = "git.lock"
SPAWNED = "git.spawned"

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")  # no ':', '+', '^', '~', '@', space
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
# user[:password]@ in an http(s) URL: a token may sit there, so no line ever shows it.
USERINFO_RE = re.compile(r"(?i)(https?://)[^/@\s]*@")
# A rejection that only a force would get past: the remote has commits HEAD lacks.
DIVERGED = ("non-fast-forward", "fetch first", "stale info", "already exists", "needs force")
HOSTKEY_HINTS = ("host key verification failed", "remote host identification has changed")
AUTH_HINTS = ("permission denied", "authentication failed", "terminal prompts disabled",
              "could not read username", "could not read password", "invalid username or password",
              "returned error: 401", "returned error: 403")
NET_HINTS = ("could not resolve host", "temporary failure in name resolution", "name or service not known",
             "network is unreachable", "no route to host", "connection timed out", "operation timed out",
             "connection refused", "failed to connect", "connection reset", "couldn't connect")
MISSING_HINTS = ("does not appear to be a git repository", "repository not found")
BACKOFF_MAX_MIN = 360


class PushError(Exception):
    """A push that cannot go on. `reason` is the short Russian line of the ⚠ warning; `person`:
    only a person can fix it, so no background retry."""

    def __init__(self, reason, step, detail="", person=False):
        Exception.__init__(self, reason)
        self.reason, self.step, self.detail, self.person = reason, step, detail, person


def redact(text, secrets=None):
    text = text if isinstance(text, str) else str(text)
    if secrets is not None:
        text = secrets.redact(text)
    return USERINFO_RE.sub(r"\1***@", text)


# --- the stamp, the config -------------------------------------------------------------------

def log_path(state):
    return state.path(backup.DIR, LOG)


def load_stamp(state):
    return read_json(state.path(backup.DIR, STAMP), {}) or {}


def save_stamp(state, st):
    backup.bdir(state)
    write_json(state.path(backup.DIR, STAMP), st)


def check_cfg(g):
    remote, branch = g.get("remote"), g.get("branch")
    if not isinstance(remote, str) or not NAME_RE.match(remote) or ".." in remote:
        raise PushError(t("push.bad_remote", remote=repr(remote)), "config", person=True)
    if branch is not None and (not isinstance(branch, str) or not BRANCH_RE.match(branch) or ".." in branch
                               or "//" in branch or branch.endswith(("/", ".", ".lock"))):
        raise PushError(t("push.bad_branch", branch=repr(branch)), "config", person=True)
    for k in ("every_commits", "retry_min", "timeout_s", "scan_timeout_s"):
        v = g.get(k)
        if isinstance(v, bool) or not isinstance(v, int) or v < 1:
            raise PushError(t("push.not_int", item=k), "config", person=True)
    if g.get("scan") is not False and (not isinstance(g.get("scan"), str) or not g["scan"]):
        raise PushError(t("push.no_scan_path"), "config", person=True)
    for k in ("scan_args", "scan_pushed_args"):
        args = g.get(k)
        if not isinstance(args, list) or not all(isinstance(x, str) for x in args):
            raise PushError(t("push.not_list", item=k), "config", person=True)
    if not any("{dir}" in x for x in g["scan_pushed_args"]):
        raise PushError(t("push.no_dir"), "config", person=True)


def scan_argv(state, g, key="scan_args", d=""):
    return [os.path.expanduser(g["scan"] or "")] + [x.replace("{state}", state.root).replace("{dir}", d) for x in g[key]]


# --- git, read-only ---------------------------------------------------------------------------

def _git(state, *args):
    try:
        return subprocess.run(["git", "-C", state.root, *list(args)], stdin=subprocess.DEVNULL,
                              capture_output=True, text=True)
    except FileNotFoundError:
        raise PushError(t("push.no_git"), "git")


def remote_url(state, remote):
    """The URL a push goes to (pushurl or url), or None: no such remote."""
    p = _git(state, "remote", "get-url", "--push", remote)
    url = p.stdout.strip()
    return url if p.returncode == 0 and url else None


def branch_of(state, g):
    if g.get("branch"):
        return g["branch"]
    p = _git(state, "symbolic-ref", "-q", "--short", "HEAD")
    b = p.stdout.strip()
    return b if p.returncode == 0 and b and BRANCH_RE.match(b) else None


def rev(state, ref):
    p = _git(state, "rev-parse", "--verify", "-q", ref + "^{commit}")
    sha = p.stdout.strip()
    return sha if p.returncode == 0 and SHA_RE.match(sha) else None


def tracking(remote, branch):
    return f"refs/remotes/{remote}/{branch}"


def _count(state, base, tip="HEAD"):
    p = _git(state, "rev-list", "--count", (base + ".." + tip) if base else tip)
    out = p.stdout.strip()
    return int(out) if p.returncode == 0 and out.isdigit() else None


def base_of(state, remote, branch, st):
    """The remote's commit as this repo knows it, without the network: the remote-tracking ref, else
    the last push's commit; None: never pushed (the push carries all of HEAD's history)."""
    b = rev(state, tracking(remote, branch))
    if b:
        return b
    c = st.get("commit") or ""
    return c if SHA_RE.match(c) and rev(state, c) else None


def pending(state, remote, branch, st, tip="HEAD"):
    """The commits `tip` has that the remote lacks, counted locally. 0 when there is no commit."""
    return _count(state, base_of(state, remote, branch, st), tip) or 0


# --- the check after every command ------------------------------------------------------------

def wait_min(g, st):
    """Minutes between two background starts: retry_min, doubled after each failure in a row."""
    fails = int(st.get("fails") or 0)
    return min(float(g["retry_min"]) * (2 ** max(0, fails - 1)), BACKOFF_MAX_MIN)


def _needs_person(st):
    err, ls = st.get("last_error"), st.get("last_success") or ""
    return bool(err and err.get("person") and err.get("ts", "") >= ls)


def warning(state, st, remote, n, now):
    """The one ⚠ line, or None."""
    ls, err = st.get("last_success"), st.get("last_error")
    if st.get("source") and st["source"] != state.root:
        return t("push.w_foreign", source=st["source"])
    if not ls:
        return t("push.w_never_err", remote=remote, reason=err["reason"]) if err else t("push.w_never", remote=remote)
    if err and err.get("ts", "") >= ls:
        return t("push.w_failed", remote=remote, n=n, reason=err["reason"])
    att = st.get("last_attempt")
    if att and att > ls and backup._age_h(now, att) > 1:
        return t("push.w_hung", remote=remote, log=log_path(state))
    return None


def due(state, st, g, n, now):
    """Start a background push? Never before the first success: that push is a person's."""
    if not st.get("last_success"):
        return False
    if st.get("source") and st["source"] != state.root:
        return False  # a copied state: its pushes are a person's
    if n < int(g["every_commits"]) or _needs_person(st):
        return False
    sp = state.path(backup.DIR, SPAWNED)
    if os.path.exists(sp) and now - os.path.getmtime(sp) < wait_min(g, st) * 60:
        return False  # one start per window; a push still going holds its lock anyway
    return True


def check(state, a, b, now=None):
    """after_command's part for the push: the ⚠ or · line, or None. Starts a detached push when due."""
    now = time.time() if now is None else now
    g = b["git"]
    try:
        check_cfg(g)
    except PushError as e:
        return t("push.w_not_started", reason=e.reason)
    remote = g["remote"]
    if remote_url(state, remote) is None:
        return None  # no remote: silence
    branch = branch_of(state, g)
    if not branch:
        return t("push.w_no_branch", remote=remote)
    st = load_stamp(state)
    n = pending(state, remote, branch, st)
    line = warning(state, st, remote, n, now)
    if not getattr(a, "dry_run", False) and due(state, st, g, n, now):
        backup.spawn(state, getattr(a, "set", None), ("--push", "--auto"), SPAWNED, LOG)
        if not line:
            line = t("push.started", remote=remote, n=n)
    return line


# --- one push ---------------------------------------------------------------------------------

def run_group(argv, env, timeout, cwd=None):
    """argv in its own process group, so a timeout kills ssh too. → (exit code or None, out, err)."""
    p = subprocess.Popen(argv, env=env, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.PIPE, encoding="utf-8", errors="replace", start_new_session=True)
    try:
        out, err = p.communicate(timeout=timeout)
        return p.returncode, out, err
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return None, "", ""


def is_clean(out):
    """The scan's verdict: its last line is exactly CLEAN (whatever its exit code)."""
    lines = (out or "").split("\n")
    while lines and lines[-1] == "":
        lines.pop()
    return bool(lines) and lines[-1].rstrip("\r") == "CLEAN"



RAW_OFF = ("0", "off", "no", "false")


def gate_raw(state, cfg, path, out=print):
    if os.environ.get("DEPOT2_RAW_SCAN", "").lower() in RAW_OFF or not os.path.isfile(path):
        return None
    g = cfg["backup"]["git"]
    why = raw_verdict(state, g, path)
    if why is None:
        return None
    rel = os.path.relpath(path, state.root)
    qdir = os.path.expanduser(g["quarantine"])
    dest = os.path.join(qdir, _safe_rel(rel))
    base, n = dest, 2
    while os.path.exists(dest):
        dest, n = "%s.%d" % (base, n), n + 1  # noqa: UP031
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    shutil.move(path, dest)
    atomic_write(path, json.dumps({"type": "quarantined", "at": now_iso(), "to": dest, "why": why},
                                  ensure_ascii=False) + "\n")
    out(t("push.quarantined", rel=rel, why=why, dest=dest))
    return {"file": rel, "to": dest, "why": why}


def raw_verdict(state, g, path):
    """One scan of one raw stream, copied alone into a temp dir. None: CLEAN; else why not, naming no pattern."""
    if g.get("scan") is False:  # no scan, by the project's choice
        return None
    d = tempfile.mkdtemp(prefix="team-raw-scan-")
    try:
        shutil.copyfile(path, os.path.join(d, os.path.basename(path)))
        argv = scan_argv(state, g, "scan_pushed_args", d)
        if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
            return t("push.raw_no_scan", path=argv[0])
        code, text, _ = run_group(backup._nice(argv), None, int(g["scan_timeout_s"]), cwd=tempfile.gettempdir())
        if code is None:
            return t("push.raw_timeout", s=int(g["scan_timeout_s"]))
        return None if is_clean(text) else t("push.raw_not_clean")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _safe_rel(path):
    parts = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    return os.path.join(*parts) if parts else "blob"


def export_range(root, base, head, dest, spec=None):
    """What a push of `head` carries beyond `base` (None: all of head's history), as files for the
    scan: each blob at <dest>/blobs/<sha12>/<its path>, the commit messages in <dest>/commits.txt.
    spec: the revisions instead (the merge queue's trial: [head, "--not", its parents]). → (blobs, bytes)."""
    spec = list(spec) if spec else [base + ".." + head] if base else [head]
    p = subprocess.run(["git", "-C", root, "rev-list", "--objects", *spec], stdin=subprocess.DEVNULL,
                       capture_output=True)
    if p.returncode != 0:
        raise PushError(t("push.rev_list", code=p.returncode), "scan", os.fsdecode(p.stderr).strip()[-300:])
    paths = {}
    for ln in p.stdout.split(b"\n"):
        sha, _, path = ln.partition(b" ")
        if sha and path and sha not in paths:
            paths[sha] = os.fsdecode(path)
    blobs, size = 0, 0
    lst = os.path.join(dest, "objects.txt")
    with open(lst, "wb") as f:
        f.write(b"".join(s + b"\n" for s in paths))
    with open(lst, "rb") as inp:
        cat = subprocess.Popen(["git", "-C", root, "cat-file", "--batch"], stdin=inp, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL)
        try:
            while True:
                header = cat.stdout.readline()
                if not header:
                    break
                parts = header.split()
                if len(parts) != 3:
                    continue
                n = int(parts[2])
                body = cat.stdout.read(n)
                cat.stdout.read(1)
                if parts[1] != b"blob":
                    continue
                out = os.path.join(dest, "blobs", parts[0][:12].decode(), _safe_rel(paths.get(parts[0], "blob")))
                os.makedirs(os.path.dirname(out), exist_ok=True)
                with open(out, "wb") as f:
                    f.write(body)
                blobs += 1
                size += n
        finally:
            cat.stdout.close()
            cat.wait()
    os.remove(lst)
    p = subprocess.run(["git", "-C", root, "log", "--format=commit %H%n%B", *spec], stdin=subprocess.DEVNULL,
                       capture_output=True)
    if p.returncode != 0:
        raise PushError(t("push.git_log", code=p.returncode), "scan")
    with open(os.path.join(dest, "commits.txt"), "wb") as f:
        f.write(p.stdout)
    return blobs, size


def push_argv(remote, branch, sha):
    """The only push there is: the scanned commit onto the branch. No force, no mirror, no delete,
    no tag (a global push.followTags would add tags, so it is off for this call)."""
    if not NAME_RE.match(remote or "") or not BRANCH_RE.match(branch or "") or ".." in remote + branch:
        raise PushError(t("push.bad_names"), "config", person=True)
    if not SHA_RE.match(sha or ""):
        raise PushError(t("push.bad_sha"), "config", person=True)
    return ["git", "-c", "push.followTags=false", "push", "--porcelain", remote, f"{sha}:refs/heads/{branch}"]


def push_env(state):
    env = dict(os.environ)
    ssh = env.get("GIT_SSH_COMMAND") or _git(state, "config", "--get", "core.sshCommand").stdout.strip() or "ssh"
    env.update(GIT_TERMINAL_PROMPT="0", GIT_ASKPASS="", SSH_ASKPASS="", SSH_ASKPASS_REQUIRE="never",
               LC_ALL="C", LANGUAGE="C",
               GIT_SSH_COMMAND=ssh + " -o BatchMode=yes -o ConnectTimeout=30 -o ServerAliveInterval=15"
                                     " -o ServerAliveCountMax=4")
    return env


def parse_porcelain(out):
    """`git push --porcelain`'s ref lines: (flag, from:to, summary)."""
    rows = []
    for ln in (out or "").split("\n"):
        parts = ln.split("\t")
        if len(parts) >= 3 and len(parts[0]) == 1 and parts[0] in " +-*!=":
            rows.append((parts[0], parts[1], "\t".join(parts[2:]).strip()))
    return rows


def classify(code, err, tail):
    low = err.lower()
    if any(h in low for h in HOSTKEY_HINTS):
        reason = t("push.r_hostkey")
    elif any(h in low for h in AUTH_HINTS):
        reason = t("push.r_auth")
    elif any(h in low for h in NET_HINTS):
        reason = t("push.r_net")
    elif any(h in low for h in MISSING_HINTS):
        reason = t("push.r_missing")
    else:
        reason = t("push.r_code", code=code)
    return PushError(reason, "push", tail)


class Pusher:
    def __init__(self, state, cfg, auto=False):
        self.state, self.g, self.auto = state, cfg["backup"]["git"], auto
        self.secrets = backup.Secrets()

    # output: in --auto stdout IS the log; in the foreground print and append to the log
    def say(self, msg):
        msg = redact(msg, self.secrets)
        stamp = "%s [%d] " % (backup.utc_iso(), os.getpid())  # noqa: UP031
        if self.auto:
            print(stamp + msg, flush=True)
            return
        print(msg, flush=True)
        try:
            backup.bdir(self.state)
            with open(log_path(self.state), "a", encoding="utf-8") as f:
                f.write(stamp + msg + "\n")
        except OSError:
            pass

    def url_secrets(self, url):
        m = re.search(r"(?i)https?://([^/@\s]*)@", url)
        if not m:
            return
        info = m.group(1)
        pw = info.split(":", 1)[1] if ":" in info else info
        if ":" in info or len(info) >= 16:
            self.secrets.add(info)
        if len(pw) >= 4 and (":" in info or len(info) >= 16):
            self.secrets.add(pw)

    def run_scan(self, argv, what):
        """One run of the private scan. Only its verdict is shown: its lines name private things."""
        g = self.g
        if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
            raise PushError(t("push.scan_missing", path=argv[0]), "scan", person=True)
        t0 = time.time()
        code, out, _ = run_group(backup._nice(argv), None, int(g["scan_timeout_s"]), cwd=tempfile.gettempdir())
        if code is None:
            raise PushError(t("push.scan_timeout", s=int(g["scan_timeout_s"])), "scan")
        clean = is_clean(out)
        self.say("scan of %s: %s in %.0f s (exit %d; its output is not shown here)" % (  # noqa: UP031
            what, "CLEAN" if clean else "NOT CLEAN", time.time() - t0, code))
        if not clean:
            raise PushError(t("push.scan_dirty_push" if what == "the push" else "push.scan_dirty_files"),
                            "scan", "the scan, by hand: " + " ".join(argv), person=True)

    def scan(self, base, head):
        """Both scans of one push: what it carries (base..head, exported to a temp dir), then the files."""
        if self.g.get("scan") is False:  # no scan, by the project's choice
            self.say("no scan before the push (backup.git.scan is false)")
            return
        d = tempfile.mkdtemp(prefix="team-push-scan-")
        try:
            blobs, size = export_range(self.state.root, base, head, d)
            self.say("the push carries %s: %d blobs, %.1f MB, and the commit messages"  # noqa: UP031
                     % (f"{base[:12]}..{head[:12]}" if base else "all the history up to " + head[:12],
                        blobs, size / 1e6))
            self.run_scan(scan_argv(self.state, self.g, "scan_pushed_args", d), "the push")
        finally:
            shutil.rmtree(d, ignore_errors=True)
        self.run_scan(scan_argv(self.state, self.g), "the files")

    def git_push(self, remote, branch, sha):
        timeout = int(self.g["timeout_s"])
        try:
            code, out, err = run_group(push_argv(remote, branch, sha), push_env(self.state), timeout,
                                       cwd=self.state.root)
        except FileNotFoundError:
            raise PushError(t("push.no_git"), "push")
        if code is None:
            raise PushError(t("push.push_timeout", s=timeout), "push")
        err = redact(err, self.secrets)
        tail = "\n".join([ln for ln in err.strip().split("\n") if ln.strip()][-5:])
        rows = parse_porcelain(out)
        for flag, ref, summary in rows:
            summary = redact(summary, self.secrets)
            if flag == "!":
                if any(h in summary.lower() for h in DIVERGED):
                    raise PushError(t("push.diverged"), "push", (tail + "\n" + summary).strip(), person=True)
                raise PushError(t("push.refused", summary=summary[:160]), "push", tail, person=True)
            if flag in "+-":  # cannot happen without a force or a delete
                raise PushError(t("push.odd_flag", flag=repr(flag), ref=ref), "push", tail, person=True)
        if code == 0 and rows:
            return rows
        raise classify(code, err, tail)

    def push(self):
        st = load_stamp(self.state)
        t0 = time.time()
        st["last_attempt"] = backup.utc_iso(t0)
        save_stamp(self.state, st)
        try:
            check_cfg(self.g)
            remote = self.g["remote"]
            url = remote_url(self.state, remote)
            if url is None:
                raise PushError(t("push.no_remote", remote=remote, root=self.state.root), "config", person=True)
            self.url_secrets(url)
            if _git(self.state, "config", "--bool", "--get", f"remote.{remote}.mirror").stdout.strip() == "true":
                raise PushError(t("push.mirror", remote=remote), "config", person=True)
            branch = branch_of(self.state, self.g)
            if not branch:
                raise PushError(t("push.detached"), "config", person=True)
            head = rev(self.state, "HEAD")
            if not head:
                raise PushError(t("push.no_commits"), "config", person=True)
            n = pending(self.state, remote, branch, st)
            n = pending(self.state, remote, branch, st, head)
            self.say("push %s → %s (%s), %s:refs/heads/%s; commits the remote lacks (by the local refs): %d"  # noqa: UP031
                     % (self.state.root, remote, url, head[:12], branch, n))
            if n == 0:
                self.say(f"nothing to push: {tracking(remote, branch)} already holds HEAD {head}")
            else:
                # The scans cover base..head, and the push sends exactly head: a commit made
                # meanwhile waits for the next push.
                self.scan(base_of(self.state, remote, branch, st), head)
                rows = self.git_push(remote, branch, head)
                self.say("git push: {}".format("; ".join(f"{r[1]} {redact(r[2], self.secrets)}" for r in rows)))
            st.update(last_success=backup.utc_iso(), commit=head, pushed=n, remote=remote, branch=branch,
                      source=self.state.root, duration_s=round(time.time() - t0, 1), last_error=None, fails=0)
            save_stamp(self.state, st)
            self.say("ok: %s/%s at %s, %d commits pushed, in %.1f s" % (remote, branch, head, n, time.time() - t0))  # noqa: UP031
            return 0
        except PushError as e:
            st["last_error"] = {"ts": backup.utc_iso(), "reason": e.reason, "step": e.step, "person": e.person}
            st["fails"] = int(st.get("fails") or 0) + 1
            save_stamp(self.state, st)
            self.say("FAILED ({}): {}{}".format(e.step, e.reason, ("\n" + e.detail) if e.detail else ""))
            return 1
        except Exception as e:
            st["last_error"] = {"ts": backup.utc_iso(), "reason": t("push.crash", error=redact(e, self.secrets)[:200]),
                                "step": "internal", "person": False}
            st["fails"] = int(st.get("fails") or 0) + 1
            save_stamp(self.state, st)
            raise


# --- `depot2 backup --push` and the status ---------------------------------------------------

def command(state, cfg, a):
    p = Pusher(state, cfg, auto=a.auto)
    try:
        with backup.backup_lock(state, LOCK):
            return p.push()
    except backup.Busy:
        if a.auto:
            p.say(f"another push holds {state.path(backup.DIR, LOCK)}: nothing to do")
            return 0
        print(f"error: a push is running already ({state.path(backup.DIR, LOCK)}); see depot2 backup --status",
              file=sys.stderr)
        return 1


def quarantine_line(g):
    qdir = os.path.expanduser(g["quarantine"])
    n = sum(len(files) for _, _, files in os.walk(qdir))
    return "quarantine: {} in {} (outside the depot: the private scan did not say CLEAN; a marker in the depot in its " \
           "place)".format("%d raw stream%s" % (n, "" if n == 1 else "s") if n else "empty", qdir)  # noqa: UP031


def status(state, cfg):
    g = cfg["backup"]["git"]
    try:
        check_cfg(g)
    except PushError as e:
        print(f"git push: {e.reason}")
        return
    remote = g["remote"]
    url = remote_url(state, remote)
    if url is None:
        print(t("push.status_no_remote", remote=remote, root=state.root))
        print(quarantine_line(g))
        return
    now = time.time()
    st = load_stamp(state)
    branch = branch_of(state, g)
    print("git push of %s → remote %s (%s), <the scanned commit>:refs/heads/%s, every %d commits; never a force"  # noqa: UP031
          % (state.root, remote, redact(url), branch or "? (HEAD is on no branch)", int(g["every_commits"])))
    ls = st.get("last_success")
    if ls:
        print("last push: {} ({:.1f} h ago): commit {}, {} commits, {} s".format(ls, backup._age_h(now, ls), st.get("commit"), st.get("pushed"), st.get("duration_s")))
    else:
        print("last push: never. The first push is a person's: depot2 backup --push "
              "(no background push starts before it)")
    n = pending(state, remote, branch, st) if branch else 0
    print("waiting: %d commits (by the local refs, no network)%s" % (  # noqa: UP031
        n, "; the next depot2 command starts a push" if branch and due(state, st, g, n, now) else ""))
    err = st.get("last_error")
    if err and (not ls or err.get("ts", "") >= ls):
        print("last error: {}, step {}: {}{}".format(
            err.get("ts"), err.get("step"), err.get("reason"),
            " (needs a person: no background push until depot2 backup --push succeeds)" if err.get("person")
            else " (retried in the background, at most every %d min)" % wait_min(g, st)))  # noqa: UP031
    if st.get("last_attempt"):
        print("last attempt: {}".format(st["last_attempt"]))
    print("running now: %s" % ("yes" if backup.is_running(state, LOCK) else "no"))
    print("the scans before each push: {}; then {}".format(" ".join(scan_argv(state, g, "scan_pushed_args", "<tmp>")),
                                                         " ".join(scan_argv(state, g))))
    print(f"log: {log_path(state)}")
    print(quarantine_line(g))
    line = warning(state, st, remote, n, now) if branch else t("push.w_no_branch", remote=remote)
    if line:
        print(line)
