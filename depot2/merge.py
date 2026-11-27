"""The merge queue, `depot2 merge`.

A PR goes in only on the owner's word, kept verbatim (`merge add <PR> --word "…"`). One runner (`merge run`, its own
lock) takes the PRs one at a time, in the order they were added, so each PR is tested on top of the one before:
  1. sync: the branch's worktree (merge.worktrees/<branch>) gets the fresh base as a merge commit, made in a throwaway
     worktree and fast-forwarded; conflicts: a prepared resolution (`merge resolve add`) or the queue's own rules
     (mergeauto.py, merge.auto); then merge.after_resolve, the scan, the push;
  2. ci: the PR's run of merge.ci_workflow on its new head, green;
  3. merge: `gh pr merge`, the car and its stack object done;
  4. deploy: the base's run of the merge commit, its deploy job (merge.deploy_job, or none), the site
     (merge.site_check, or none);
  5. smoke: the main clone fast-forwarded, merge.smoke run in it; its verdict: the plugins' smoke_verdict, else the
     exit code (only when something was deployed);
  6. usage: the plugins' after_merge lines.
While a PR waits for its CI or the deploy, the next ones are tried on top of it (mergetrials.py). A runner pool that is
offline can be swapped through a repository variable (merge.cd_fallback, merge.ci_fallback). A network failure of git
or gh is tried again (merge.net_retries); a CI run that failed only on the network is rerun (merge.ci_net_patterns).
A failure stops the queue until `merge resume` or `merge drop`; GitHub not starting a job is no failure.

merge-queue.json         {"next": n, "entries": [...]}: in the order added (merged and dropped ones stay)
merge-history.jsonl      every change
merge-resolutions.json   the prepared resolutions' index; merge-resolutions/<ours>-<theirs> their files
.merge/                  git-ignored: the runner's lock (run.lock) and its log (run.log, `--ensure`)
"""

import contextlib
import datetime
import fcntl
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import types

from . import hooks, i18n, mergeauto
from .mergetrials import TrialsMixin, trial_brief, trial_cell
from .mergewt import WorktreesMixin, size_text, wt_text
from .store import append_jsonl, read_json, write_json

FILE = "merge-queue.json"
HISTORY = "merge-history.jsonl"
RES_INDEX = "merge-resolutions.json"
RES_DIR = "merge-resolutions"
RUN_DIR = ".merge"
RUN_LOCK = "run.lock"
RUN_LOCK_TRIES = (10, 0.1)
RUN_LOG = "run.log"  # `merge run --ensure`'s runner writes here (.merge/: git ignores it)
RUN_LOG_MAX = 4 * 1024 * 1024  # past it, run.log becomes run.log.1 at the next start
ENSURE_WAIT_S = 10.0  # `--ensure` watches its runner start this long: the lock taken, or its exit and the log's tail

OPEN = ("waiting", "riding", "github", "stopped")
SHOWN = ("riding", "github", "stopped", "waiting")  # the counts' order: what rides first
STATUSES = ("waiting", "riding", "github", "stopped", "merged", "dropped")
STATUS_RU = i18n.Labels("merge.status", STATUSES)
STEPS = ("sync", "ci", "merge", "deploy", "smoke", "usage")
STEP_RU = i18n.Labels("merge.step", STEPS)
WHERE_RU = i18n.Labels("merge.where", ("slot", "post", "queue"))  # a stack object's place, in the dry run
SMOKE_RU = i18n.Labels("merge.smoke", ("ok", "skipped", "bad"))
OK_CONCLUSIONS = ("success", "skipped", "neutral")
BAD_CONCLUSIONS = ("failure", "timed_out", "cancelled", "startup_failure", "action_required", "stale")
NOT_STARTED = ("queued", "waiting", "pending", "requested")
BLOB_RE = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
RE_LOG_MERGED = re.compile(i18n.rx("in.merge.log_merged"))  # a log line of a car marked merged, in any language


def means(cap=False):
    """What the merge queue is, in one line (cap: its first letter a capital)."""
    m = i18n.t("merge.means")
    return m[:1].upper() + m[1:] if cap else m


CLOCK = time.time
SLEEP = time.sleep


def stamp(t=None):
    """The queue clock's time as the depot writes it (store.now_iso's format: local, with its offset)."""
    lt = time.localtime(CLOCK() if t is None else t)
    off = time.strftime("%z", lt)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + off[:3] + ":" + off[3:]


class MergeError(ValueError):
    """A refused command: nothing changed."""


class Stop(Exception):
    """The PR's ride failed: the queue stops here. files: the conflicting files no rule resolved (a conflict)."""

    def __init__(self, reason, files=None):
        Exception.__init__(self, reason)
        self.reason = reason
        self.files = list(files or [])


class GithubExit(Exception):
    """GitHub has not started the job for merge.github_wait_max_min: the runner leaves it waiting."""


class Dropped(Exception):
    """`merge drop` took the entry out while it rode."""


# --- small helpers -------------------------------------------------------------------------------------

def epoch(iso):
    try:
        return datetime.datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except (AttributeError, ValueError):
        return None


def hhmm(iso):
    return (iso or "?")[:16].replace("T", " ")


def age(iso, now=None):
    from .stack import age_ru
    return age_ru(iso, now)


def clip(text, n=160):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def expand(p):
    return os.path.expanduser(p) if isinstance(p, str) else p


def argv_of(cmd, **subs):
    out = []
    for x in cmd:
        x = expand(str(x))
        for k, v in subs.items():
            x = x.replace(f"{{{k}}}", v)
        out.append(x)
    return out


class Sh:

    def __init__(self, env=None):
        self.env = env

    def run(self, argv, cwd=None, timeout=None, env_extra=None, input_text=None):
        """→ (exit code or None on a timeout, stdout, stderr)."""
        from .push import run_group
        env = dict(os.environ if self.env is None else self.env)
        env.update(env_extra or {})
        try:
            return run_group(list(argv), env, timeout, cwd=cwd)
        except OSError as e:
            return 127, "", f"{argv[0]}: {e}"


# --- the queue's file ----------------------------------------------------------------------------------

class MergeQueue:
    def __init__(self, state):
        self.state = state
        self.path = state.path(FILE)
        self.history_path = state.path(HISTORY)
        self.data = read_json(self.path) or {"next": 1, "entries": []}

    def entries(self, open_only=False):
        return [e for e in self.data["entries"] if not open_only or e["status"] in OPEN]

    def get(self, ref):
        """mq-NNNN, or a PR (#N or N): its open entry, else its latest."""
        ref = str(ref).strip()
        if ref.startswith("mq-"):
            for e in self.data["entries"]:
                if e["id"] == ref:
                    return e
            raise MergeError(f"no {ref} in the merge queue")
        try:
            n = int(ref.lstrip("#"))
        except ValueError:
            raise MergeError(f"a merge queue entry is mq-NNNN or a PR number, got {ref!r}")  # noqa: B904
        mine = [e for e in self.data["entries"] if e["pr"] == n]
        if not mine:
            raise MergeError("PR #%d is not in the merge queue" % n)  # noqa: UP031
        live = [e for e in mine if e["status"] in OPEN]
        return (live or mine)[-1]

    def stopped(self):
        for e in self.data["entries"]:
            if e["status"] == "stopped":
                return e
        return None

    def head(self):
        """The entry the runner takes next: the first open one; None when the queue is stopped or empty."""
        for e in self.data["entries"]:
            if e["status"] == "stopped":
                return None
            if e["status"] in ("riding", "github", "waiting"):
                return e
        return None

    def save(self, event, eid, **fields):
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": stamp(), "event": event, "entry": eid}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def new_id(self):
        eid = "mq-%04d" % self.data["next"]  # noqa: UP031
        self.data["next"] += 1
        return eid

    def history(self):
        from .store import read_jsonl
        return read_jsonl(self.history_path)


def log_line(e, text):
    e.setdefault("log", []).append(f"{stamp()[:19]} {clip(text, 400)}")


# --- prepared conflict resolutions ---------------------------------------------------------------------

class Resolutions:
    """A conflict is resolved only from a resolution worked out and tested beforehand, keyed by the two
    sides' blobs (ours = the branch's, theirs = main's) and checked against the file's path."""

    def __init__(self, state):
        self.state = state
        self.path = state.path(RES_INDEX)
        self.dir = state.path(RES_DIR)
        self.data = read_json(self.path) or {"items": []}

    def items(self):
        return list(self.data["items"])

    def find(self, ours, theirs):
        key = f"{ours}-{theirs}"
        for it in self.data["items"]:
            if it["key"] == key:
                return it
        return None

    def file_of(self, it):
        return os.path.join(self.dir, it["key"])

    def _save(self):
        write_json(self.path, self.data)
        self.state.touch(self.path)

    def add(self, file, ours, theirs, src, note=None, replace=False):
        file = (file or "").strip()
        while file.startswith("./"):
            file = file[2:]
        if not file or file.startswith("/") or ".." in file.split("/"):
            raise MergeError("the file is the conflicting path inside the repo (as `git diff --diff-filter=U` "
                             f"names it), got {file!r}")
        for name, b in (("--ours", ours), ("--theirs", theirs)):
            if not BLOB_RE.match(b or ""):
                raise MergeError("{} is the full blob id (`git rev-parse :{}:{}`), got {!r}".format(name, "2" if name == "--ours" else "3", file, b))
        src = expand(src)
        if not os.path.isfile(src):
            raise MergeError(f"no such file: {src} (--from is the resolved file)")
        with open(src, "rb") as f:
            body = f.read()
        if b"<<<<<<<" in body or b">>>>>>>" in body:
            raise MergeError(f"{src} still has conflict markers: a resolution is the resolved file")
        old = self.find(ours, theirs)
        if old is not None and not replace:
            raise MergeError("a resolution for {} ({} / {}) is there already ({}): --replace to change it".format(old["file"], ours[:12], theirs[:12], old["added"][:16]))
        import hashlib
        it = {"key": f"{ours}-{theirs}", "file": file, "ours": ours, "theirs": theirs,
              "sha256": hashlib.sha256(body).hexdigest(), "bytes": len(body), "from": os.path.abspath(src),
              "note": (note or "").strip() or None, "added": stamp()}
        os.makedirs(self.dir, exist_ok=True)
        dest = self.file_of(it)
        tmp = "%s.tmp-%d" % (dest, os.getpid())  # noqa: UP031
        with open(tmp, "wb") as f:
            f.write(body)
        os.replace(tmp, dest)
        with open(dest, "rb") as f:
            if f.read() != body:
                raise MergeError(f"the resolution did not read back: {dest}")
        self.state.touch(dest)
        self.data["items"] = [x for x in self.data["items"] if x["key"] != it["key"]] + [it]
        self._save()
        return it, old is not None

    def drop(self, key):
        found = [x for x in self.data["items"] if x["key"] == key or (len(key) >= 12 and x["key"].startswith(key))]
        if not found:
            raise MergeError(f"no prepared resolution {key}")
        if len(found) > 1:
            raise MergeError("%s names %d resolutions: give more of the key" % (key, len(found)))  # noqa: UP031
        it = found[0]
        self.data["items"] = [x for x in self.data["items"] if x is not it]
        self._save()
        p = self.file_of(it)
        if os.path.exists(p):
            os.remove(p)
            self.state.touch(p)
        return it


# --- the CI runs: what a set of runs says ----------------------------------------------------------------

def _ran(j):
    return bool(j.get("runner_name")) or bool(j.get("steps"))


def classify(runs, jobs_of, now, queued_max_min):
    """The CI/CD runs of one sha → (kind, text, run id, rerunnable). kind: none (no run yet), pending, green,
    failed, github (GitHub did not start a job: a job left queued in a finished run, a job that failed or
    was cancelled without ever running, a startup failure, or a job queued with no runner for
    queued_max_min). jobs_of(run id) gives a run's jobs (the REST API's: runner_name, steps, created_at)."""
    if not runs:
        return "none", i18n.t("merge.ci_no_run"), None, False
    verdicts = []
    for r in runs:
        rid = r.get("databaseId")
        jobs = jobs_of(rid) or []
        status, concl = r.get("status"), r.get("conclusion") or ""
        if status != "completed":
            stuck = [j for j in jobs if j.get("status") in NOT_STARTED and not _ran(j)
                     and (epoch(j.get("created_at") or "") or now) <= now - queued_max_min * 60]
            if stuck:
                verdicts.append(("github", i18n.t("merge.ci_stuck", job=stuck[0].get("name"),
                                                  min=int((now - epoch(stuck[0]["created_at"])) // 60)), rid, False))
            else:
                verdicts.append(("pending", i18n.t("merge.ci_running", status=status), rid, False))
            continue
        if concl in OK_CONCLUSIONS:
            verdicts.append(("green", concl, rid, False))
            continue
        ran_bad = [j for j in jobs if j.get("status") == "completed" and j.get("conclusion") in BAD_CONCLUSIONS
                   and _ran(j)]
        never = [j for j in jobs if j.get("status") in NOT_STARTED or
                 (j.get("conclusion") in BAD_CONCLUSIONS and not _ran(j))]
        if ran_bad:
            verdicts.append(("failed", "{}: {}".format(concl or "?", ", ".join(
                "{} {}".format(j.get("name"), j.get("conclusion")) for j in ran_bad)), rid, False))
        elif never or concl == "startup_failure":
            what = ", ".join("«{}» ({})".format(j.get("name"), j.get("status") if j.get("status") != "completed"
                                             else j.get("conclusion")) for j in never) or i18n.t("merge.ci_the_run")
            verdicts.append(("github", i18n.t("merge.ci_not_started", what=what, conclusion=concl), rid, True))
        else:
            verdicts.append(("failed", i18n.t("merge.ci_failed_no_job", conclusion=concl or "?"), rid, False))
    for kind in ("failed", "github", "pending"):
        for v in verdicts:
            if v[0] == kind:
                return v
    return "green", ", ".join(sorted({v[1] for v in verdicts})), verdicts[0][2], False


def labels_json(labels):
    return json.dumps(list(labels), separators=(",", ":"))


def label_names(labels):
    return "+".join(x for x in labels or [] if x.lower() != "self-hosted") or "+".join(labels or [])


def back_command(fb):
    return "gh variable set {} --body '{}'{}".format(fb.get("var"), labels_json(fb.get("back_to") or []),
                                                 " -R {}".format(fb["repo"]) if fb.get("repo") else "")


FALLBACKS = {"ci": ("ci_fallback", "CI"),  # the config's key, the English name; the shown words: merge.fb_what.<key>
             "cd": ("cd_fallback", "the deploy")}


def fb_reason(fb, key):
    return i18n.t("merge.fb_reason", main=label_names(fb.get("back_to")), what=i18n.t("merge.fb_what." + key),
                  spare=label_names(fb.get("to")), run=fb.get("run"))


def runner_line(fb, key):
    var, main, spare = fb.get("var"), label_names(fb.get("back_to")), label_names(fb.get("to"))
    what, waits = i18n.t("merge.fb_what." + key), i18n.t("merge.fb_waits." + key)
    if fb.get("var_refused"):
        return i18n.t("merge.fb_var_refused", var=var, main=main, job=fb.get("job"), waited=fb.get("waited_min"),
                      to=labels_json(fb.get("to") or []), error=fb["var_refused"], waits=waits)
    if fb.get("cancel_refused"):
        head = i18n.t("merge.fb_cancel_refused", var=var, main=main, job=fb.get("job"), waited=fb.get("waited_min"),
                      run=fb.get("run"), error=fb["cancel_refused"], waits=waits)
    else:
        head = i18n.t("merge.fb_switched", var=var, main=main, job=fb.get("job"), waited=fb.get("waited_min"),
                      what=what, spare=spare, run=fb.get("run"))
    if fb.get("back"):
        tail = i18n.t("merge.fb_back", var=var, back_to=labels_json(fb.get("back_to") or []), at=hhmm(fb["back"]))
    elif fb.get("back_error"):
        tail = i18n.t("merge.fb_back_error", var=var, error=fb["back_error"], cmd=back_command(fb))
    else:
        tail = i18n.t("merge.fb_back_later", var=var, to=labels_json(fb.get("to") or []),
                      back_to=labels_json(fb.get("back_to") or []))
    return head + "; " + tail


def json_objects(text):
    """Every JSON object in a text (one per line or pretty-printed), in order."""
    dec, i, out = json.JSONDecoder(), 0, []
    while True:
        j = text.find("{", i)
        if j < 0:
            return out
        try:
            obj, end = dec.raw_decode(text, j)
            if isinstance(obj, dict):
                out.append(obj)
            i = end
        except ValueError:
            i = j + 1


def smoke_verdict(cfg, code, out):
    """The smoke check's verdict → (ok, summary): the first plugin's smoke_verdict that answers, else its exit code
    (0 passes) with its last line of output."""
    got = hooks.call_first(cfg, "smoke_verdict", code, out)
    if got is not None:
        return bool(got[0]), str(got[1])
    last = ([ln.strip() for ln in (out or "").splitlines() if ln.strip()] or [""])[-1]
    if code is None:
        return False, i18n.t("merge.smoke_timeout") + (": " + clip(last, 120) if last else "")
    return code == 0, ("exit %d" % code) + (": " + clip(last, 120) if last else "")  # noqa: UP031


# --- the runner: one PR's ride ---------------------------------------------------------------------------

def git_subcommand(args):
    """git's subcommand among its arguments (past -c NAME=V, -C DIR and the other leading options)."""
    i = 0
    while i < len(args) and str(args[i]).startswith("-"):
        i += 2 if args[i] in ("-c", "-C") else 1
    return args[i] if i < len(args) else None


NET_GIT = ("fetch", "push", "ls-remote")


class Rider(TrialsMixin, WorktreesMixin):
    def __init__(self, state, cfg, sh=None, out=None, clock=None, sleep=None):
        self.state, self.cfg = state, cfg
        self.m = cfg["merge"]
        self.repo = cfg.get("repo") or ""
        self.base = self.m["base"]
        self.remote = self.m["remote"]
        self.clone = expand(self.m["clone"]) if self.m.get("clone") else ""
        self.sh = sh or Sh()
        self.out = out or (lambda line: print(line, flush=True))
        self.clock, self.sleep = clock or CLOCK, sleep or SLEEP

    # --- outside commands ---
    def net_retry(self, what, fn):
        tries, pause = int(self.m.get("net_retries") or 0), float(self.m.get("net_retry_s") or 0)
        for i in range(tries + 1):
            code, out, err = fn()
            if code == 0 or i == tries or not mergeauto.is_net(code, "{}\n{}".format(err or "", out or "")):
                return code, out, err
            self.out("network: %s failed (%s): again in %d s (%d of %d)" % (  # noqa: UP031
                what, clip(err or out, 140) if code is not None else "no answer in time", pause, i + 1, tries))
            self.sleep(pause)
        return code, out, err

    def git(self, cwd, *args, timeout=600, env=None):
        extra = {"GIT_EDITOR": "true", "GIT_MERGE_AUTOEDIT": "no", "GIT_TERMINAL_PROMPT": "0"}
        extra.update(env or {})
        sub = git_subcommand(args)

        def run():
            return self.sh.run(["git", *list(args)], cwd=cwd, timeout=timeout, env_extra=extra)
        return self.net_retry(f"git {sub}", run) if sub in NET_GIT else run()

    def unmerged(self, cwd):
        return self.git(cwd, "diff", "--name-only", "--diff-filter=U")[1].split()

    def rev(self, cwd, ref):
        code, out, _ = self.git(cwd, "rev-parse", "--verify", "-q", ref + "^{commit}" if not ref.startswith(":")
                                else ref)
        return out.strip() if code == 0 and out.strip() else None

    def is_ancestor(self, cwd, a, b):
        return self.git(cwd, "merge-base", "--is-ancestor", a, b)[0] == 0

    def gh(self, *args, timeout=120, net=True):
        """gh; a network failure is tried again (net_retry), but never `pr merge` (step_merge looks first)."""
        def run():
            return self.sh.run(["gh", *list(args)], timeout=timeout)
        if not net or tuple(args[:2]) == ("pr", "merge"):
            return run()
        return self.net_retry("gh {}".format(" ".join(args[:2])), run)

    def gh_json(self, *args):
        code, out, err = self.gh(*args)
        if code != 0:
            raise MergeError("gh {}: exit {}: {}".format(" ".join(args[:2]), code, clip(err, 200)))
        try:
            return json.loads(out or "null")
        except ValueError:
            raise MergeError("gh {}: not JSON: {}".format(" ".join(args[:2]), clip(out, 120)))  # noqa: B904

    def pr_view(self, n):
        return self.gh_json("pr", "view", str(n), "-R", self.repo, "--json",
                            "number,title,state,headRefName,headRefOid,baseRefName,isDraft,mergeCommit,url")

    def runs(self, branch, sha):
        return self.gh_json("run", "list", "-R", self.repo, "--workflow", self.m["ci_workflow"], "--branch", branch,
                            "--commit", sha, "--limit", "20", "--json",
                            "databaseId,status,conclusion,headSha,event,createdAt,attempt,url") or []

    def jobs(self, run_id):
        data = self.gh_json("api", "--paginate", "--slurp", f"repos/{self.repo}/actions/runs/{run_id}/jobs?per_page=100")
        pages = data if isinstance(data, list) else [data]
        return [j for p in pages for j in (p or {}).get("jobs") or []]

    def runners(self):
        """The repository's self-hosted runners → [(name, status, {its labels, lower-case})]."""
        data = self.gh_json("api", "--paginate", "--slurp", f"repos/{self.repo}/actions/runners?per_page=100")
        pages = data if isinstance(data, list) else [data]
        return [(r.get("name"), r.get("status"), {str(x.get("name") if isinstance(x, dict) else x).lower()
                                                  for x in r.get("labels") or []})
                for p in pages for r in (p or {}).get("runners") or []]

    # --- the state ---
    @contextlib.contextmanager
    def edit(self):
        """A short write under the state's lock: the file read fresh (another command may have added to it)."""
        with self.state.lock():
            q = MergeQueue(self.state)
            yield q

    def entry(self, eid):
        return MergeQueue(self.state).get(eid)

    COMMIT_ON = ("start", "stop", "github", "github_over", "rerun", "merged_pr", "merged", "cd_runner",
                 "cd_runner_back", "ci_runner", "ci_runner_back")

    def update(self, eid, event, say=None, **fields):
        """Every change is written and in the history at once; the state's git commits at the status changes
        (COMMIT_ON) and when the run ends."""
        with self.edit() as q:
            e = q.get(eid)
            if e["status"] == "dropped":
                raise Dropped()
            e.update(fields)
            if say:
                log_line(e, say)
            q.save(event, eid, **{k: v for k, v in fields.items() if k in ("status", "step", "head", "reason", "sha", "wt")})
            if event in self.COMMIT_ON:
                self.state.commit("team: merge %s #%d %s" % (eid, e["pr"], event))  # noqa: UP031
        if say:
            self.out(say)
        return e

    def say(self, eid, line):
        """A line printed and kept in the entry's log (no status change; the next change commits it)."""
        with self.edit() as q:
            e = q.get(eid)
            log_line(e, line)
            write_json(q.path, q.data)
            self.state.touch(q.path)
        self.out(line)

    def alive(self, eid):
        if self.entry(eid)["status"] == "dropped":
            raise Dropped()

    # --- the runner's lock ---
    def lock_held(self):
        return lock_held(self.state)

    def lock_path(self):
        return os.path.join(run_dir(self.state), RUN_LOCK)

    @contextlib.contextmanager
    def run_lock(self):
        fd = os.open(self.lock_path(), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            tries, pause = RUN_LOCK_TRIES
            for left in range(tries, 0, -1):
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if left == 1:
                        pid = (os.pread(fd, 32, 0) or b"?").decode(errors="replace").strip()
                        raise MergeError(f"another merge run holds the queue (pid {pid}): one runner at a time")  # noqa: B904
                    time.sleep(pause)
            os.ftruncate(fd, 0)
            os.write(fd, ("%d\n" % os.getpid()).encode())  # noqa: UP031
            yield
        finally:
            os.close(fd)

    # --- the loop ---
    def run(self, once=False):
        """0: the queue is empty (or `--once` rode one); 1: stopped; 3: waiting for GitHub, the runner left."""
        with self.run_lock():
            try:
                return self._run(once)
            finally:
                with self.state.lock():
                    self.state.commit("team: merge run: the log")

    def _run(self, once):
        while True:
            q = MergeQueue(self.state)
            st = q.stopped()
            if st is not None:
                self.out("STOP: the merge queue is stopped at %s #%d: %s" % (st["id"], st["pr"],  # noqa: UP031
                                                                           (st.get("stop") or {}).get("reason")))
                self.out("  after the fix: depot2 merge resume %d; or: depot2 merge drop %d --reason \"…\""  # noqa: UP031
                         % (st["pr"], st["pr"]))
                return 1
            e = q.head()
            if e is None:
                self.out("the merge queue is empty")
                return 0
            rc = self.ride(e["id"])
            if rc == "stopped":
                return 1
            if rc == "github":
                return 3
            if once:
                return 0

    def ride(self, eid):
        e = self.entry(eid)
        if e["status"] == "waiting":
            start = e.get("step") or ("deploy" if e.get("merged") else "sync")
            e = self.update(eid, "start", status="riding", step=start, step_since=stamp(self.clock()), stop=None,
                            say="== #%d (%s)%s" % (e["pr"], e["branch"], f", from {start}" if start != "sync" else ""))  # noqa: UP031
        try:
            while True:
                e = self.entry(eid)
                if e["status"] == "dropped":
                    raise Dropped()
                step = e.get("step")
                if step is None:
                    break
                nxt = getattr(self, "step_" + step)(e)
                if nxt is None:
                    break
                self.update(eid, "step", step=nxt, step_since=stamp(self.clock()))
        except Stop as s:
            self.stop(eid, s.reason)
            return "stopped"
        except GithubExit:
            g = self.entry(eid).get("github") or {}
            self.out("waiting for GitHub since %s (%s): the runner leaves %s waiting; `depot2 merge run` goes on, "  # noqa: UP031
                     "`depot2 merge rerun %d` reruns its run" % (hhmm(g.get("since")), g.get("reason"), eid,
                                                                  self.entry(eid)["pr"]))
            return "github"
        except Dropped:
            self.out(f"{eid} was dropped from the merge queue while it rode: not merged here")
            return "dropped"
        e = self.update(eid, "merged", status="merged", step=None, say="== done: #%d" % e["pr"])  # noqa: UP031
        for line in self.arrive(e):
            self.say(eid, line)
        try:  # its own trial's branch, and any the queue left behind
            for line in self.sweep_trials():
                self.say(eid, line)
        except Exception as ex:
            self.out(f"trials: not swept: {type(ex).__name__}: {clip(ex, 160)}")
        for line in report_entry(e):
            self.out(line)
        return "merged"

    def arrive(self, e):
        return self.arrive_own(e) + self.arrive_earlier(e)

    def arrive_own(self, e):
        from .stages import Car, StageError
        ref = e.get("car") or e.get("st")
        if not ref:
            return ["no stack object, no car: no stage to move"]
        with self.state.lock():
            try:
                car = Car(self.state, self.cfg, ref)
                if car.obj is not None:
                    return ["{} is still in the stack: its stage moves with its stack done".format(car.obj["id"])]
                was, rows = car.move("arrived", why=i18n.t("merge.why_arrived", pr=e["pr"], outcome=outcome_text(e) or
                                                           i18n.t("merge.deploy_passed")),
                                     by="merge run ({})".format(e["id"]), force=car.stage() != "rolling")
            except Exception as ex:  # the stage is a record: it never stops the queue after a merge
                return ["stage: {}{}".format("" if isinstance(ex, StageError) else type(ex).__name__ + ": ", ex)]
            self.state.commit("team: merge %s #%d: arrived" % (e["id"], e["pr"]))  # noqa: UP031
        from .stages import ru
        return ["{}: {} → {}".format(car.label, ru(was), ru("arrived"))] if rows else []

    def arrive_earlier(self, e):
        from .stages import Car, StageError, hand_why, rolled_before, ru
        out = []
        with self.state.lock():
            q = MergeQueue(self.state)
            mine = q.get(e["id"])
            for h in q.entries():
                if h is mine or h["status"] != "merged" or not h.get("handoff") or h["handoff"].get("arrived"):
                    continue
                if q.data["entries"].index(h) > q.data["entries"].index(mine):  # it merged after this one
                    continue
                h["handoff"]["arrived"] = {"with": e["id"], "pr": e["pr"], "ts": stamp(self.clock())}
                log_line(h, "rode out with #%d (%s)" % (e["pr"], e["id"]))  # noqa: UP031
                q.save("handoff_arrived", h["id"], with_entry=e["id"])
                ref = h.get("car") or h.get("st")
                if not ref:
                    out.append("#%d (%s) rode out with #%d" % (h["pr"], h["id"], e["pr"]))  # noqa: UP031
                    continue
                try:
                    car = Car(self.state, self.cfg, ref)
                    if car.obj is None and car.stage() == "rolling":
                        car.move("arrived", why=i18n.t("merge.why_arrived_next", pr=e["pr"], id=e["id"]),
                                 by="merge run ({}, handoff {})".format(e["id"], h["id"]))
                        out.append("%s: %s → %s (with #%d)" % (car.label, ru("rolling"), ru("arrived"), e["pr"]))  # noqa: UP031
                except Exception as ex:  # a record: it never stops the queue
                    out.append("stage: {}{}".format("" if isinstance(ex, StageError) else type(ex).__name__ + ": ", ex))
            try:
                hand = rolled_before(self.state, self.cfg, (e.get("merged") or {}).get("ts"))
            except Exception as ex:
                hand = []
                out.append(f"stage: merged by hand: {type(ex).__name__}: {clip(ex, 160)}")
            for car, at in hand:
                try:
                    car.move("arrived", why=hand_why(e), by=i18n.t("merge.by_hand_merged", id=e["id"]))
                    out.append(i18n.t("merge.arrived_by_hand", car=car.label, was=ru("rolling"), now=ru("arrived"),
                                      at=hhmm(at), pr=e["pr"]))
                except Exception as ex:  # a record: it never stops the queue
                    out.append("stage: {}{}".format("" if isinstance(ex, StageError) else type(ex).__name__ + ": ", ex))
            self.state.commit("team: merge %s #%d: the earlier merges arrived" % (e["id"], e["pr"]))  # noqa: UP031
        return out

    def stop(self, eid, reason):
        e = self.entry(eid)
        self.update(eid, "stop", status="stopped", stop={"ts": stamp(self.clock()), "reason": reason, "step": e.get("step")},
                    github=None, reason=reason, say=f"STOP: {reason}")
        self.out("  the queue is stopped: after the fix `depot2 merge resume %d`, or `depot2 merge drop %d --reason \"…\""  # noqa: UP031
                 % (e["pr"], e["pr"]))
        try:  # the trials stood on it: thrown away (mergetrials.py)
            self.discard_trials("#%d stopped" % e["pr"])  # noqa: UP031
        except Exception as ex:
            self.out(f"trials: not thrown away: {type(ex).__name__}: {clip(ex, 160)}")

    # --- the gates: a lock, a car's `after` ---
    def gates(self, e):
        from .stack import Stack
        from .tracks import Tracks
        stk = Stack(self.state, self.cfg)
        if e.get("st") and stk._dep_open(e["st"]):
            o = stk.get(e["st"])
            if o.get("lock"):
                raise Stop("{} is locked ({}: {}): a locked object does not merge".format(
                    e["st"], o["lock"]["id"], o["lock"]["condition"]))
        tr = Tracks(self.state, self.cfg)
        t, c = tr.car_by_stack(e["st"]) if e.get("st") else (None, None)
        if c is None and e.get("car"):
            t, c = tr.car(e["car"])
        if c is not None:
            b = tr.car_blockers(t, c, lock=False)
            if b:
                raise Stop("{}:{} waits for {}: its PR is blocked, not ready to merge".format(t["id"], c["n"], ", ".join(b)))

    def check_pr(self, e, pr):
        if pr["state"] == "MERGED":
            return
        if pr["state"] != "OPEN":
            raise Stop("PR #%d is %s" % (e["pr"], pr["state"]))  # noqa: UP031
        if pr.get("isDraft"):
            raise Stop("PR #%d is a draft" % e["pr"])  # noqa: UP031
        if pr.get("baseRefName") != self.base:
            raise Stop("PR #%d goes into %s, not %s" % (e["pr"], pr.get("baseRefName"), self.base))  # noqa: UP031
        if pr.get("headRefName") != e["branch"]:
            raise Stop("PR #%d's branch is %s, the queue has %s" % (e["pr"], pr.get("headRefName"), e["branch"]))  # noqa: UP031

    # --- step 1: main into the branch ---
    def merge_in(self, wt, base_ref, br, label, mode="real", env=None):
        pre = self.rev(wt, "HEAD")
        none = {"head": pre, "push": False, "resolved": [], "auto": [], "golden": False}
        if self.is_ancestor(wt, base_ref, "HEAD"):
            return dict(none, note=f"{label} is in {br} already: its head stays")
        m7 = (self.rev(wt, base_ref) or base_ref)[:7]
        code, out, err = self.git(wt, "merge", "--no-ff", "--no-commit", base_ref, env=env)
        files = self.unmerged(wt)
        if code != 0 and not files:
            self.git(wt, "merge", "--abort")
            raise Stop(f"git merge of {label} into {br} failed: {clip(err or out, 200)}")
        try:
            got = self.resolve(wt, files, mode)
            got["auto"] += self.regenerate(wt, got, mode)
            if got["prepared"] and mode == "real":
                self.after_resolve(wt, br)
            left = self.unmerged(wt)
            if left:
                raise Stop("conflicts left: {}".format(", ".join(left)), files=left)
            msg = f"Merge {label} ({m7}) into {br}"
            if got["auto"]:
                msg += "\n\nResolved by the merge queue:\n" + "\n".join(f"- {x}" for x in got["auto"])
            fd, mfile = tempfile.mkstemp(prefix="merge-msg-")  # (a file: the message has lines of its own)
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as fh:
                    fh.write(msg + "\n")
                code, out, err = self.git(wt, "commit", "-q", "-F", mfile, env=env)
            finally:
                os.remove(mfile)
            if code != 0:
                raise Stop(f"the merge commit failed: {clip(err or out, 200)}")
        except Stop:
            self.git(wt, "merge", "--abort")
            raise
        if self.git(wt, "diff", "--quiet", pre, "HEAD")[0] == 0:  # a chain: the branch holds main's content already
            self.git(wt, "reset", "-q", "--hard", pre)
            what = "main" if label == f"{self.remote}/{self.base}" else label
            return dict(none, note=f"{what} adds nothing to {br}: its head stays", resolved=got["prepared"])
        return {"head": self.rev(wt, "HEAD"), "push": True, "note": f"merged {label} ({m7}) into {br}",
                "resolved": got["prepared"], "auto": got["auto"], "golden": got["golden"]}

    def resolve(self, wt, files, mode="real"):
        res = Resolutions(self.state)
        auto = self.m.get("auto") or {}
        got = {"prepared": [], "auto": [], "golden": False, "snapshots": [], "generated": False}
        stops = []
        for f in files:
            o, t = self.rev(wt, ":2:" + f), self.rev(wt, ":3:" + f)
            if not o or not t:
                stops.append((f, "conflict in {}: one side deleted it ({} / {}): no prepared resolution covers that".format(f, (o or "—")[:12], (t or "—")[:12])))
                continue
            it = res.find(o, t)
            if it is not None:
                if it["file"] != f:
                    stops.append((f, "the prepared resolution {}… is for {}, not {}".format(it["key"][:25], it["file"], f)))
                    continue
                shutil.copyfile(res.file_of(it), os.path.join(wt, f))
                if self.git(wt, "add", "--", f)[0] != 0:
                    raise Stop(f"git add {f} failed after the resolution", files=[f])
                got["prepared"].append(f)
                continue
            rule = mergeauto.rule_of(f, auto, self.cfg)
            why = None
            if rule == "union":
                why, part = self.union_file(wt, f)
                if why is None:
                    got["auto"].append(part)
            elif rule in ("generated", "snapshot", "golden"):
                test = mergeauto.snapshot_test(f, self.cfg) if rule == "snapshot" else None
                if rule == "snapshot" and not os.path.isfile(os.path.join(wt, test)):
                    why = f"no {test} beside the snapshot to update it with"
                elif self.git(wt, "checkout", "-q", "--theirs", "--", f)[0] != 0 or self.git(wt, "add", "--", f)[0] != 0:
                    why = f"git checkout --theirs {f} failed"
                elif rule == "generated":
                    got["generated"] = True
                elif rule == "snapshot":
                    got["snapshots"].append((f, test))
                else:
                    got["golden"] = True
                    g = auto["golden"]
                    got["auto"].append("{}: main's side{}".format(f, "; re-recorded after the push (`{}`)".format(" ".join(
                        argv_of(g["cmd"], remote=self.remote, branch="<branch>", out="<dir>")))
                        if mode != "trial" else "; re-recorded at the PR's own turn"))
            else:
                why = ""
            if why is not None:
                stops.append((f, "no prepared resolution for {} ({} / {}): depot2 merge resolve add {} --ours {} "
                                 "--theirs {} --from <the resolved file>{}".format(f, o[:12], t[:12], f, o, t,
                                                                               f" ({why})" if why else "")))
        if stops:
            more = "; also: {}".format("; ".join(r for _, r in stops[1:])) if len(stops) > 1 else ""
            raise Stop(stops[0][1] + more, files=[f for f, _ in stops])
        return got

    def union_file(self, wt, f):
        """A doc both sides only added to (mergeauto.union): joined, written, added. → (None, the journal's part) or
        (why not, None)."""
        code, out, err = self.git(wt, "checkout-index", "--stage=all", "--temp", "--", f)
        names = (out.split("\t")[0].split() if code == 0 else [])
        tmps = [os.path.join(wt, x) for x in names if x != "."]
        try:
            if code != 0:
                return f"git checkout-index failed: {clip(err, 120)}", None
            if len(names) != 3 or "." in names:
                return "a side has no such file (both added it, or one took it away)", None
            b, o, t = (os.path.join(wt, x) for x in names)
            code, out, err = self.git(wt, "merge-file", "--diff3", "--marker-size=%d" % mergeauto.MARK,  # noqa: UP031
                                      "-L", mergeauto.TAG + "ours", "-L", mergeauto.TAG + "base",
                                      "-L", mergeauto.TAG + "theirs", o, b, t)
            if code is None or code >= 128:
                return f"git merge-file failed: {clip(err or out, 120)}", None
            with open(o, "rb") as fh:
                raw = fh.read()
            try:
                text = raw.decode("utf-8")
            except UnicodeDecodeError:
                return "not UTF-8 text", None
            joined, n = mergeauto.union(text)
            if joined is None:
                return n, None
            if mergeauto.TAG in joined or (code < 127 and n != code):  # markers it did not read (CRLF, …): never kept
                return "its conflict markers were not all read (%d of %d)" % (n, code), None  # noqa: UP031
            with open(os.path.join(wt, f), "wb") as fh:
                fh.write(joined.encode("utf-8"))
            if self.git(wt, "add", "--", f)[0] != 0:
                return f"git add {f} failed", None
            return None, "%s: both sides only added (%d place%s): the branch's lines, then main's" % (  # noqa: UP031
                f, n, "" if n == 1 else "s")
        finally:
            for p in tmps:
                if os.path.exists(p):
                    os.remove(p)

    def regenerate(self, wt, got, mode):
        auto = self.m.get("auto") or {}
        parts = []
        s = auto.get("snapshots") or {}
        for snap, test in got["snapshots"]:
            argv = argv_of(s.get("cmd") or [], test=test)
            if mode == "plan":
                parts.append("{}: main's side, then `{}`".format(snap, " ".join(argv)))
                continue
            code, out, err = self.sh.run(argv, cwd=wt, timeout=int(self.m.get("generate_timeout_s") or 1800))
            if code != 0:
                raise Stop("{}: main's side, then `{}` is red ({}): {}".format(snap, " ".join(argv), "timeout" if code is None
                                                                            else "exit %d" % code,  # noqa: UP031
                                                                            clip((out + err)[-300:], 200)), files=[snap])
            self.git(wt, "add", "--", snap)
            parts.append("{}: main's side, then `{}`: green".format(snap, " ".join(argv)))
        c = auto.get("generated") or {}
        if c.get("file") and os.path.isfile(os.path.join(wt, c["file"])):
            argv = argv_of(c.get("cmd") or [])
            if mode == "plan":
                if got["generated"]:
                    parts.append("{}: main's side, rebuilt (`{}`)".format(c["file"], " ".join(argv)))
                return parts
            code, out, err = self.sh.run(argv, cwd=wt, timeout=int(self.m.get("generate_timeout_s") or 1800))
            if code != 0:
                if got["generated"]:
                    raise Stop("{}: main's side, then `{}` fails ({}): {}".format(
                        c["file"], " ".join(argv), "timeout" if code is None else "exit %d" % code,  # noqa: UP031
                        clip((out + err)[-300:], 200)), files=[c["file"]])
                self.git(wt, "checkout", "-q", "--", c["file"])
                parts.append("{}: `{}` failed ({}): git's merge of it kept".format(c["file"], " ".join(argv), clip(err or out, 80)))
                return parts
            stale = self.git(wt, "diff", "--quiet", "--", c["file"])[0] != 0
            self.git(wt, "add", "--", c["file"])
            if got["generated"]:
                parts.append("{}: main's side, rebuilt (`{}`)".format(c["file"], " ".join(argv)))
            elif stale:
                parts.append("{}: git's merge of it was stale, rebuilt (`{}`)".format(c["file"], " ".join(argv)))
        return parts

    @contextlib.contextmanager
    def scratch(self, repo, sha, what="merge"):
        tmp = tempfile.mkdtemp(prefix=f"merge-{what}-")
        tw = os.path.join(tmp, "wt")
        code, out, err = self.git(repo, "worktree", "add", "-q", "--detach", tw, sha)
        if code != 0:
            shutil.rmtree(tmp, ignore_errors=True)
            raise Stop("no throwaway worktree for the {} at {}: {}".format(what, (sha or "?")[:7], clip(err or out, 160)))
        nm = next((p for p in (os.path.join(self.clone, "node_modules"), os.path.join(repo, "node_modules"))
                   if os.path.isdir(p)), None)
        link = os.path.join(tw, "node_modules")
        if nm and not os.path.exists(link):
            os.symlink(os.path.realpath(nm), link)
        try:
            yield tw
        finally:
            if os.path.islink(link):
                os.unlink(link)
            self.git(repo, "worktree", "remove", "--force", tw)
            shutil.rmtree(tmp, ignore_errors=True)

    def sync(self, wt, br, main_ref):
        """Main into the branch checked out in wt: merged in a throwaway worktree of its head (merge_in), then
        fast-forwarded into wt. → merge_in's result."""
        pre = self.rev(wt, "HEAD")
        if self.is_ancestor(wt, main_ref, "HEAD"):
            return {"head": pre, "push": False, "note": f"{main_ref} is in {br} already: its head stays",
                    "resolved": [], "auto": [], "golden": False}
        with self.scratch(wt, pre) as tw:
            res = self.merge_in(tw, main_ref, br, main_ref, "real")
        if res["push"]:
            code, out, err = self.git(wt, "merge", "-q", "--ff-only", res["head"])
            if code != 0:
                raise Stop("{}: the fast-forward to the merge {} failed: {}".format(br, res["head"][:7], clip(err or out, 200)))
        return res

    def after_resolve(self, wt, br):
        for cmd in self.m.get("after_resolve") or []:
            argv = argv_of(cmd)
            code, out, err = self.sh.run(argv, cwd=wt, timeout=int(self.m["after_resolve_timeout_s"]))
            if code != 0:
                raise Stop("after the prepared resolution on {}, `{}` fails ({}): {}".format(
                    br, " ".join(argv), "timeout" if code is None else "exit %d" % code, clip((err or out)[-300:], 200)))  # noqa: UP031

    def scan(self, wt, br):
        from .backup import _nice
        from .push import is_clean
        if self.m.get("scan") is False:  # the project chose to push without a scan
            return True
        if not self.m.get("scan"):
            raise Stop("merge.scan is not set: a scan command (its last line CLEAN), or false to push without one")
        argv = argv_of([self.m["scan"], *list(self.m["scan_args"])], worktree=wt)
        if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):
            raise Stop(f"no private scan {argv[0]} (merge.scan): no push")
        code, out, _ = self.sh.run(_nice(argv), cwd=tempfile.gettempdir(), timeout=int(self.m["scan_timeout_s"]))
        if code is None:
            raise Stop("the private scan of %s took over %d s: no push" % (br, int(self.m["scan_timeout_s"])))  # noqa: UP031
        if not is_clean(out):  # its lines name private things: only the verdict is shown
            raise Stop("the private scan of {} is not CLEAN: no push (run it by hand: {})".format(br, " ".join(argv)))
        return True

    def step_sync(self, e):
        eid, br, wt = e["id"], e["branch"], e["worktree"]
        pr = self.pr_view(e["pr"])
        self.check_pr(e, pr)
        if pr["state"] == "MERGED":
            self.say(eid, "#%d is merged already (%s): the queue takes its merge commit" % (  # noqa: UP031
                e["pr"], ((pr.get("mergeCommit") or {}).get("oid") or "?")[:7]))
            return "merge"
        self.gates(e)
        if not os.path.isdir(wt):
            raise Stop(f"no worktree {wt} (git -C {self.clone} worktree add {wt} {br})")
        cur = self.git(wt, "symbolic-ref", "--short", "-q", "HEAD")[1].strip()
        if cur != br:
            raise Stop("the worktree {} is on {}, not {}".format(wt, cur or "a detached HEAD", br))
        dirty = [ln for ln in self.git(wt, "status", "--porcelain")[1].splitlines() if ln and not ln.startswith("??")]
        if dirty:
            raise Stop("the worktree of {} is dirty: {}".format(br, clip("; ".join(dirty), 160)))
        code, _, err = self.git(wt, "fetch", "-q", self.remote, timeout=300)
        if code != 0:
            raise Stop(f"git fetch in {wt} failed: {clip(err, 160)}")
        head, ob = self.rev(wt, "HEAD"), self.rev(wt, f"{self.remote}/{br}")
        if not ob:
            raise Stop(f"no {self.remote}/{br}")
        if pr.get("headRefOid") and not self.pr_head_is(e["pr"], ob, pr):
            raise Stop("PR #%d's head on GitHub is %s, %s/%s is %s" % (e["pr"], pr["headRefOid"][:7], self.remote, br,  # noqa: UP031
                                                                      ob[:7]))
        ahead = []
        if head != ob:
            if self.is_ancestor(wt, head, ob):
                if self.git(wt, "merge", "-q", "--ff-only", f"{self.remote}/{br}")[0] != 0:
                    raise Stop(f"git merge --ff-only {self.remote}/{br} failed in {wt}")
                self.say(eid, f"{br}: fast-forwarded to {self.remote}/{br} ({ob[:7]})")
            elif self.is_ancestor(wt, ob, head):
                ahead = self.git(wt, "log", "--first-parent", "--format=%h %s", f"{self.remote}/{br}..HEAD")[1] \
                    .splitlines()
                self.say(eid, "%s: the local head %s is %d commit(s) ahead of %s/%s (%s): pushed after the scan, not "  # noqa: UP031
                              "waited for" % (br, head[:7], len(ahead), self.remote, br, clip("; ".join(ahead), 200)))
            else:
                raise Stop(f"{br} and {self.remote}/{br} have diverged ({head[:7]} / {ob[:7]})")
        main_ref = f"{self.remote}/{self.base}"
        res = self.sync(wt, br, main_ref)
        for f in res["resolved"]:
            self.say(eid, f"resolved {f} from the prepared resolution")
        if res["auto"]:  # the journal's line: what the queue resolved by itself, and how (one merge commit)
            self.say(eid, "the queue resolved in the merge of {} ({}) into {}: {}".format(
                main_ref, (self.rev(wt, main_ref) or "?")[:7], br, "; ".join(res["auto"])))
        line = self.trial_tree_line(e, wt, res["head"])
        if line:
            self.say(eid, line)
        if res.get("golden"):  # kept before the push: a stop at the scan or the push re-records it on the resume
            self.update(eid, "golden", golden_pending=res["head"])
        if res["push"] or ahead:
            self.push(eid, wt, br, res["head"], "%s, CI anew" % (
                "pushed {} ({}) into {}: {}".format(main_ref, (self.rev(wt, main_ref) or "?")[:7], br, res["head"][:7])
                if res["push"] else "pushed the local head of {}: {} ({})".format(br, res["head"][:7], res["note"])))
        else:
            self.update(eid, "head", head=res["head"], pushed=False, say=res["note"])
        g = (self.m.get("auto") or {}).get("golden") or {}
        pend = self.entry(eid).get("golden_pending")
        if res.get("golden"):
            self.golden(eid, wt, br, res["head"])
        elif pend:
            if self.is_ancestor(wt, pend, res["head"]) and \
                    self.git(wt, "diff", "--quiet", pend, res["head"], "--", g.get("file") or ".")[0] == 0:
                self.golden(eid, wt, br, res["head"])
            else:
                self.update(eid, "golden_done", golden_pending=None, say="{} has a {} of its own since {}: no re-record".format(br, g.get("file"), pend[:7]))
        return "ci"

    def golden(self, eid, wt, br, head):
        g = (self.m.get("auto") or {}).get("golden") or {}
        out_dir = tempfile.mkdtemp(prefix="merge-golden-")
        try:
            argv = argv_of(g.get("cmd") or [], remote=self.remote, branch=br, out=out_dir)
            self.update(eid, "golden", golden_pending=head, say="{}: main's {} is in {}: re-recorded on it: `{}`".format(
                br, g.get("file"), head[:7], " ".join(argv)))
            code, out, err = self.sh.run(argv, cwd=wt, timeout=int(self.m.get("golden_timeout_s") or 5400))
            name = os.path.basename(g.get("file") or "golden.json")
            got = sorted(os.path.join(d, f) for d, _, fs in os.walk(out_dir) for f in fs if f == name)
            if code != 0 or len(got) != 1:
                raise Stop("the re-record of %s on %s @ %s failed (%s, %d %s): %s — main's %s is in the branch: re-record "  # noqa: UP031
                           "it, commit, push, then `depot2 merge resume %d` (on the resume the queue tries it again "
                           "while the branch has no %s of its own)" % (
                               g.get("file"), br, head[:7], "timeout" if code is None else "exit %d" % code, len(got),  # noqa: UP031
                               name, clip((err or out)[-300:], 160), g.get("file"), self.entry(eid)["pr"], name))
            shutil.copyfile(got[0], os.path.join(wt, g["file"]))
            if self.git(wt, "status", "--porcelain", "--", g["file"])[1].strip():
                if self.git(wt, "add", "--", g["file"])[0] != 0 or \
                        self.git(wt, "commit", "-q", "-m", "Re-record {} on the merge".format(g["file"]))[0] != 0:
                    raise Stop("the commit of the re-recorded {} on {} failed".format(g["file"], br))
                new = self.rev(wt, "HEAD")
                self.push(eid, wt, br, new, "re-recorded {} on the merge: {}, CI anew".format(g["file"], new[:7]))
            else:
                self.say(eid, "the re-recorded {} is main's: nothing to commit".format(g["file"]))
            self.update(eid, "golden_done", golden_pending=None)
        finally:
            shutil.rmtree(out_dir, ignore_errors=True)

    def push(self, eid, wt, br, head, say):
        """The scan (CLEAN), then a plain push of the branch (never forced); GitHub then has the head CI runs on."""
        self.scan(wt, br)
        code, _, err = self.git(wt, "push", "-q", self.remote, br, timeout=300)
        if code != 0:
            raise Stop(f"git push of {br} failed: {clip(err, 200)}")
        self.update(eid, "pushed", head=head, pushed=True, say=say)

    def pr_head_is(self, n, sha, pr=None):
        """GitHub's PR head is sha (it may lag a push by a few seconds: asked again a few times)."""
        for i in range(6):
            if pr is None or i:
                pr = self.pr_view(n)
            if pr.get("headRefOid") == sha:
                return True
            self.sleep(min(5.0, float(self.m["poll_s"])))
        return False

    def remote_head(self, wt, br):
        """The branch's head on the remote itself (git ls-remote), not a local ref."""
        code, out, _ = self.git(wt, "ls-remote", self.remote, f"refs/heads/{br}", timeout=120)
        return out.split()[0] if code == 0 and out.split() else None

    # --- waiting for the runs of one sha ---
    def wait_runs(self, eid, branch, sha, timeout_min, what, deploy=False):
        m = self.m
        poll = float(m["poll_s"])
        t0 = self.clock()
        pending_since = t0
        while True:
            self.alive(eid)
            now = self.clock()
            runs = self.runs(branch, sha)
            kind, text, rid, rerunnable = classify(runs, self.jobs, now, float(m["queued_max_min"]))
            # the runners' fallbacks: CI's on every run, the deploy's on main's; a cancel or a rerun in flight: it waits
            if self.fallbacks(eid, runs, kind, now, ("ci", "cd") if deploy else ("ci",)):
                pending_since = now
                self.sleep(poll)
                continue
            if kind == "none":
                if now - t0 < float(m["run_appear_min"]) * 60:
                    kind = "pending"
                else:
                    kind, text = "github", "no %s run for %s @ %s after %d min" % (m["ci_workflow"], branch, sha[:7],  # noqa: UP031
                                                                                 int((now - t0) // 60))
            if kind == "green":
                self.github_over(eid)
                return runs
            if kind == "failed":
                if self.ci_net_rerun(eid, rid, what):
                    pending_since = self.clock()
                    self.sleep(poll)
                    continue
                raise Stop(f"{what}: {text}")
            if kind == "github":
                g = self.github(eid, text, rid, rerunnable)
                last = epoch(g.get("last_rerun") or g["since"]) or now
                after = m.get("github_rerun_after_min")
                if rerunnable and after is not None and g.get("reruns", 0) < int(m["github_reruns_max"]) and \
                        now - last >= float(after) * 60:
                    self.do_rerun(eid, rid, f"the timer ({after} min)")
                elif now - (epoch(g["since"]) or now) >= float(m["github_wait_max_min"]) * 60:
                    raise GithubExit()
                pending_since = now
            else:
                if self.github_over(eid):
                    pending_since = now
                if now - pending_since > float(timeout_min) * 60:
                    raise Stop("%s still not done after %d min" % (what, int(timeout_min)))  # noqa: UP031
            self.sleep(0 if self.presync(eid) else poll)  # the wait builds the next PRs' trials (mergetrials.py)

    def ci_net_rerun(self, eid, rid, what):
        mx, pats = int(self.m.get("ci_net_reruns") or 0), self.m.get("ci_net_patterns") or []
        if not mx or not pats or rid is None:
            return False
        done = dict(self.entry(eid).get("ci_net") or {})
        k = str(rid)
        if int(done.get(k) or 0) >= mx:
            return False
        code, out, _ = self.gh("run", "view", k, "--log-failed", "-R", self.repo, timeout=120)
        hit = mergeauto.net_hit(out, pats) if code == 0 else None
        if not hit:
            return False
        code, out, err = self.gh("run", "rerun", k, "--failed", "-R", self.repo)
        if code != 0:
            self.say(eid, f"gh run rerun {k} --failed (the network failed it: «{hit}») refused: {clip(err or out, 160)}")
            return False
        done[k] = int(done.get(k) or 0) + 1
        self.update(eid, "ci_net_rerun", ci_net=done, say="%s failed only on the network («%s» in its failed log): "  # noqa: UP031
                    "gh run rerun %s --failed (%d of %d)" % (what, hit, k, done[k], mx))
        return True

    def github(self, eid, text, rid, rerunnable):
        e = self.entry(eid)
        g = e.get("github")
        if e["status"] == "github" and g and g.get("reason") == text and g.get("run") == rid:
            return g
        g = dict(g or {}, reason=text, run=rid, rerunnable=bool(rerunnable))
        g.setdefault("since", stamp(self.clock()))
        g.setdefault("reruns", 0)
        st = self.githubstatus() if e["status"] != "github" else g.get("githubstatus")
        if st:
            g["githubstatus"] = st
        self.update(eid, "github", status="github", github=g,
                    say="waiting for GitHub: {}{} (not a failure)".format(text, f"; githubstatus: {st}" if st else ""))
        return g

    def github_over(self, eid):
        e = self.entry(eid)
        if e["status"] != "github":
            return False
        self.update(eid, "github_over", status="riding", github=None,
                    say="GitHub is back: {}".format((e.get("github") or {}).get("reason")))
        return True

    def githubstatus(self):
        cmd = self.m.get("githubstatus")
        if not cmd:
            return None
        code, out, _ = self.sh.run(argv_of(cmd), timeout=20)
        if code != 0:
            return None
        try:
            comps = json.loads(out).get("components") or []
        except ValueError:
            return None
        for c in comps:
            if c.get("name") == "Actions":
                return "Actions {}".format(c.get("status"))
        return None

    def do_rerun(self, eid, rid, why):
        code, out, err = self.gh("run", "rerun", str(rid), "--failed", "-R", self.repo)
        e = self.entry(eid)
        g = dict(e.get("github") or {})
        if code != 0:
            self.say(eid, f"gh run rerun {rid} --failed ({why}) refused: {clip(err or out, 160)}")
            g["last_rerun"] = stamp(self.clock())  # not again at once
            self.update(eid, "rerun_refused", github=g)
            return False
        g.update(reruns=int(g.get("reruns", 0)) + 1, last_rerun=stamp(self.clock()))
        self.update(eid, "rerun", github=g, say="gh run rerun %s --failed (%s, %d of %s)" % (  # noqa: UP031
            rid, why, g["reruns"], self.m["github_reruns_max"]))
        return True

    def fallbacks(self, eid, runs, kind, now, keys):
        """One poll of the runs for the fallbacks `keys` → True while one holds the poll."""
        return any(self.fallback(eid, key, runs, kind, now) for key in keys)

    def fallback(self, eid, key, runs, kind, now):
        """One poll of the runs for one fallback → True while it holds the poll (its cancel not landed, its rerun
        not seen yet): the runs' verdict waits for it."""
        c = self.m.get(FALLBACKS[key][0])
        if not c:
            return False
        fb = self.entry(eid).get(key + "_runner")
        if fb and not fb.get("back"):
            return self.fb_follow(eid, key, fb, runs, now)
        if kind != "github":  # only a job GitHub leaves queued starts one
            return False
        for r in runs:
            if r.get("status") != "completed" and not (fb and fb.get("run") == r.get("databaseId")):  # one per run
                job = self.fb_stuck(c, r, now)
                if job:
                    return self.fb_switch(eid, key, c, r, job, now)
        return False

    def fb_stuck(self, c, r, now):
        names = c.get("jobs") or [self.m["deploy_job"]]
        want = {x.lower() for x in c["main"]}
        limit = now - float(self.m["queued_max_min"]) * 60
        for j in self.jobs(r.get("databaseId")) or []:
            if j.get("name") in names and j.get("status") in NOT_STARTED and not _ran(j) and \
                    (epoch(j.get("created_at") or "") or now) <= limit and \
                    want <= {str(x).lower() for x in j.get("labels") or []}:
                return j
        return None

    def fb_note(self, eid, key, line):
        """A line said once (a wait the next polls would repeat)."""
        if self.entry(eid).get(key + "_note") != line:
            self.update(eid, key + "_note", say=line, **{key + "_note": line})

    def set_var(self, var, labels):
        return self.gh("variable", "set", var, "--body", labels_json(labels), "-R", self.repo)

    def fb_switch(self, eid, key, c, r, dep, now):
        what, field = FALLBACKS[key][1], key + "_runner"
        try:
            rs = self.runners()
        except MergeError as ex:
            self.fb_note(eid, key, "the runners unreadable ({}): {} waits for {}".format(clip(ex, 120), what,
                                                                                    label_names(c["main"])))
            return False

        def online(labels):
            want = {x.lower() for x in labels}
            return sorted(str(n) for n, st, have in rs if st == "online" and want <= have)
        if online(c["main"]):
            return False
        spare = online(c["fallback"])
        if not spare:
            self.fb_note(eid, key, "no {} runner online, and no {} one either: {} waits".format(
                label_names(c["main"]), label_names(c["fallback"]), what))
            return False
        rid = r.get("databaseId")
        waited = int((now - (epoch(dep.get("created_at") or "") or now)) // 60)
        fb = {"var": c["var"], "run": rid, "attempt": int(r.get("attempt") or 1), "job": dep.get("name"),
              "waited_min": waited, "to": list(c["fallback"]), "back_to": list(c["main"]), "spare": spare,
              "since": stamp(self.clock()), "back": None, "repo": self.repo}
        self.update(eid, field, **{field: fb})  # kept first: a runner that dies after the change still sets it back
        code, out, err = self.set_var(c["var"], c["fallback"])
        if code != 0:
            fb = dict(fb, var_refused=clip(err or out, 120), back=stamp(self.clock()))
            self.update(eid, field + "_refused", say="gh variable set {} refused ({}): {} waits for {}".format(
                c["var"], fb["var_refused"], what, label_names(c["main"])), **{field: fb})
            return False
        self.say(eid, "no %s runner online, «%s» queued %d+ min: %s → %s (%s), gh run cancel %s, then rerun --failed" % (  # noqa: UP031
            label_names(c["main"]), dep.get("name"), waited, c["var"], labels_json(c["fallback"]), ", ".join(spare), rid))
        code, out, err = self.gh("run", "cancel", str(rid), "-R", self.repo)
        if code != 0:
            fb = dict(self.entry(eid)[field], cancel_refused=clip(err or out, 120))
            self.update(eid, key + "_cancel_refused", say="gh run cancel {} refused: {}".format(
                rid, fb["cancel_refused"]), **{field: fb})
            self.fb_back(eid, key, fb, "the cancel was refused")
            return False
        self.github(eid, fb_reason(fb, key), rid, False)
        return True

    def fb_follow(self, eid, key, fb, runs, now):
        r = next((x for x in runs if x.get("databaseId") == fb["run"]), None)
        if r is None:
            return False
        done = r.get("status") == "completed"
        if int(r.get("attempt") or 1) > int(fb["attempt"]):  # the rerun is GitHub's now: back once it is over
            if done:
                self.fb_back(eid, key, fb, "run {}, attempt {}: {}".format(fb["run"], r.get("attempt"), r.get("conclusion")))
            return False
        acted = False
        if done and not fb.get("rerun"):
            tries = int(fb.get("rerun_refused") or 0)
            if tries >= int(self.m["github_reruns_max"]):
                return False  # refused every time: the wait for GitHub decides (`merge rerun` by hand)
            acted = self.do_rerun(eid, fb["run"], "{} to {}".format(FALLBACKS[key][1], label_names(fb["to"])))
            fb = dict(fb, **({"rerun": stamp(self.clock())} if acted else {"rerun_refused": tries + 1}))
            self.update(eid, key + ("_rerun" if acted else "_rerun_refused"), **{key + "_runner": fb})
        self.github(eid, fb_reason(fb, key), fb["run"], False)  # (said once: the same reason is no change)
        if not acted and now - (epoch(fb["since"]) or now) >= float(self.m["github_wait_max_min"]) * 60:
            raise GithubExit()  # the variable stays: the next `merge run` follows the run and sets it back
        return True

    def fb_back(self, eid, key, fb, why):
        field = key + "_runner"
        code, out, err = self.set_var(fb["var"], fb["back_to"])
        if code != 0:
            fb = dict(fb, back_error=clip(err or out, 120))
            self.update(eid, field + "_back_refused", say="gh variable set {} refused ({}): set it back by hand: {}".format(
                fb["var"], fb["back_error"], back_command(fb)), **{field: fb})
            return
        fb = dict(fb, back=stamp(self.clock()), back_error=None, back_why=why)
        self.update(eid, field + "_back", say="{} → {} again ({})".format(fb["var"], labels_json(fb["back_to"]), why),
                    **{field: fb})

    # --- step 2: the branch's CI ---
    def step_ci(self, e):
        head = e["head"]
        wt = e["worktree"]
        if os.path.isdir(wt):  # CI runs only on a head GitHub has: one it lacks goes back to the sync (and its push)
            remote = self.remote_head(wt, e["branch"])
            if remote and remote != head:
                self.say(e["id"], "{}/{} is {}, not the head {}: back to the sync, which pushes it".format(
                    self.remote, e["branch"], remote[:7], head[:7]))
                return "sync"
        self.wait_runs(e["id"], e["branch"], head, float(self.m["ci_timeout_min"]), "CI of {} @ {}".format(e["branch"], head[:7]))
        self.say(e["id"], "CI green on {} @ {}".format(e["branch"], head[:7]))
        return "merge"

    # --- step 3: the merge, then the stack ---
    def step_merge(self, e):
        eid, n = e["id"], e["pr"]
        pr = self.pr_view(n)
        if pr["state"] != "MERGED":
            self.check_pr(e, pr)
            if not e.get("head"):
                raise Stop("#%d has no tested head" % n)  # noqa: UP031
            if pr.get("headRefOid") != e["head"]:
                raise Stop("PR #%d's head moved: %s, not the %s CI tested" % (n, (pr.get("headRefOid") or "?")[:7],  # noqa: UP031
                                                                              e["head"][:7]))
            self.gates(e)
            self.alive(eid)
            code, out, err = self.pr_merge(n, e["head"])
            if code != 0:
                raise Stop("the merge of #%d was refused: %s" % (n, clip(err or out, 200)))  # noqa: UP031
            for _ in range(12):
                pr = self.pr_view(n)
                if pr["state"] == "MERGED" and (pr.get("mergeCommit") or {}).get("oid"):
                    break
                self.sleep(min(5.0, float(self.m["poll_s"])))
        sha = (pr.get("mergeCommit") or {}).get("oid")
        if pr["state"] != "MERGED" or not sha:
            raise Stop("#%d: no merge commit after the merge (%s)" % (n, pr["state"]))  # noqa: UP031
        e = self.update(eid, "merged_pr", merged={"sha": sha, "ts": stamp(self.clock())}, sha=sha,
                        say="#%d merged as %s" % (n, sha[:7]))  # noqa: UP031
        for line in self.stack_done(e, sha):
            self.say(eid, line)
        self.after_merge_worktree(eid)
        return "deploy"

    def pr_merge(self, n, head):
        """gh pr merge; when the network fails it, the PR is looked at first (merged after all: done), then the merge
        is tried again (net_retry's count and pause)."""
        tries, pause = int(self.m.get("net_retries") or 0), float(self.m.get("net_retry_s") or 0)
        for i in range(tries + 1):
            code, out, err = self.gh("pr", "merge", str(n), "-R", self.repo, "--merge", "--match-head-commit", head)
            if code == 0 or i == tries or not mergeauto.is_net(code, "{}\n{}".format(err or "", out or "")):
                return code, out, err
            self.out("network: gh pr merge %d failed (%s): the PR looked at, again in %d s (%d of %d)" % (  # noqa: UP031
                n, clip(err or out, 140) if code is not None else "no answer in time", pause, i + 1, tries))
            self.sleep(pause)
            try:
                if self.pr_view(n)["state"] == "MERGED":
                    return 0, "", ""
            except MergeError:
                pass
        return code, out, err

    def stack_done(self, e, sha):
        """`stack done --car merged` by itself, under the state's lock; a parked car is marked merged."""
        from .stack import Stack
        from .tracks import Tracks
        from .trains import freed_slot_lines
        reason = i18n.t("merge.done_reason", pr=e["pr"], sha=sha[:7], id=e["id"], word=clip(e.get("word"), 120))
        out = []
        with self.state.lock():
            stk = Stack(self.state, self.cfg)
            oid = e.get("st")
            if oid and stk._dep_open(oid):
                o = stk.get(oid)
                if o.get("lock"):
                    return ["{} is locked ({}): stack done by hand once it is lifted".format(oid, o["lock"]["id"])]
                by = "merge run ({})".format(e["id"])
                stk.done(oid, reason, outcome="merged", by=by)
                out.append(f"stack done {oid} --car merged ({reason})")
                out += Tracks(self.state, self.cfg).on_stack("done", oid, reason=reason, car_status="merged", by=by)
                if o["where"] == "slot":
                    out += freed_slot_lines(self.state, self.cfg)
            elif e.get("car"):
                tr = Tracks(self.state, self.cfg)
                _t, c = tr.car(e["car"])
                if c["status"] not in ("merged", "dropped"):
                    tr.car_set(e["car"], status="merged", note=reason, by="merge run ({})".format(e["id"]))
                    out.append(i18n.t("merge.car_merged", car=e["car"], reason=reason))
                    out += tr.stage_lines
            elif oid:
                out.append(f"{oid} left the stack already: no stack done")
            else:
                out.append("no stack object: no stack done")
            self.state.commit("team: merge %s #%d: stack done" % (e["id"], e["pr"]))  # noqa: UP031
        return out

    # --- step 4: main's CI/CD, the deploy, the site ---
    def step_deploy(self, e):
        eid, n = e["id"], e["pr"]
        sha = e["merged"]["sha"]
        runs = self.wait_runs(eid, self.base, sha, float(self.m["deploy_timeout_min"]), "CI/CD of %s @ %s (#%d merged)"  # noqa: UP031
                              % (self.base, sha[:7], n), deploy=True)
        run = ([r for r in runs if r.get("event") == "push"] or runs)[0]
        jobs = self.jobs(run["databaseId"])
        if self.m.get("deploy_job"):
            dep = [j for j in jobs if j.get("name") == self.m["deploy_job"]]
            dep_c = (dep[0].get("conclusion") or dep[0].get("status")) if dep else "not in the run"
        else:  # no deploy job: main's run itself is the deploy
            dep_c = run.get("conclusion") or run.get("status")
        site = self.site()
        d = {"run": run["databaseId"], "conclusion": run.get("conclusion"), "deploy_job": dep_c, "site": site}
        self.update(eid, "deploy", deploy=d, say="CI/CD %s %s; #%d deploy %s; #%d site %s" % (  # noqa: UP031
            sha[:7], run.get("conclusion"), n, dep_c, n, site))
        if site not in [str(x) for x in self.m["site_ok"]]:
            raise Stop("after #%d's deploy the site answers %s" % (n, site))  # noqa: UP031
        return "smoke"

    def site(self):
        cmd = self.m.get("site_check")
        if not cmd:
            return "not checked"
        code, out, _err = self.sh.run(argv_of(cmd), timeout=60)
        return out.strip() or (f"exit {code}")

    def step_smoke(self, e):
        eid, n = e["id"], e["pr"]
        self.ff_clone(eid)
        dep = (e.get("deploy") or {}).get("deploy_job")
        cmd = self.m.get("smoke")
        if dep != "success" or not cmd:
            why = f"no deploy (deploy {dep}): nothing new to see" if dep != "success" else "merge.smoke is off"
            self.update(eid, "smoke", smoke={"ok": None, "summary": why}, say="#%d smoke skipped: %s" % (n, why))  # noqa: UP031
            return "usage"
        code, out, _ = self.sh.run(argv_of(cmd), cwd=self.clone if os.path.isdir(self.clone) else None,
                                   timeout=int(self.m["smoke_timeout_s"]))
        ok, summary = smoke_verdict(self.cfg, code, out)
        self.update(eid, "smoke", smoke={"ok": ok, "summary": summary}, say="#%d smoke: %s" % (n, summary))  # noqa: UP031
        if not ok:
            raise Stop("smoke after #%d's deploy: %s" % (n, summary))  # noqa: UP031
        return "usage"

    def ff_clone(self, eid):
        c = self.clone
        if not os.path.isdir(c):
            return
        self.git(c, "fetch", "-q", self.remote, timeout=300)
        cur = self.git(c, "symbolic-ref", "--short", "-q", "HEAD")[1].strip()
        dirty = [ln for ln in self.git(c, "status", "--porcelain")[1].splitlines() if ln and not ln.startswith("??")]
        if cur != self.base or dirty:
            self.say(eid, "the main clone {} is {}: not fast-forwarded".format(c, "dirty" if dirty else "on " + (cur or "?")))
            return
        if self.git(c, "merge", "-q", "--ff-only", f"{self.remote}/{self.base}")[0] == 0:
            self.say(eid, "the main clone → {}".format((self.rev(c, "HEAD") or "?")[:7]))
        else:
            self.say(eid, f"the main clone {c}: no fast-forward to {self.remote}/{self.base}")

    # --- step 6: the plugins' after_merge lines ---
    def step_usage(self, e):
        if not self.m.get("usage"):
            return
        u = {}
        try:
            got = hooks.call_all(self.cfg, "after_merge", self.cfg, e)
        except Exception as ex:  # the report must never stop the queue
            got = []
            u["error"] = f"{type(ex).__name__}: {clip(ex, 160)}"
        if not got and not u.get("error"):
            return
        u["lines"] = [str(g.get("line")) for g in got if isinstance(g, dict) and g.get("line")]
        u["plugins"] = [g for g in got if isinstance(g, dict)]
        u["ts"] = stamp(self.clock())
        self.update(e["id"], "usage", usage=u, say="after the merge: {}{}".format(
            "; ".join(u["lines"]) or "nothing", " ({})".format(u["error"]) if u.get("error") else ""))
        return

    # --- the dry run ---
    def plan(self):
        """`merge run --dry-run`: the plan, nothing pushed, merged or written. Each PR is tried on top of the ones
        before it in a throwaway worktree (main + the PRs ahead, as the real run would see it)."""
        q = MergeQueue(self.state)
        items = q.entries(open_only=True)
        L = ["DRY RUN: the merge queue's plan (no push, no merge, the depot unchanged)"]
        if not items:
            return [*L, "the merge queue is empty"]
        L.append(f"runner: {runner_cell(self.state)}")
        st = q.stopped()
        if st is not None:
            L.append("STOPPED at %s #%d: %s — the runner rides nothing until `merge resume %d` or `merge drop %d`; "  # noqa: UP031
                     "below, the plan after that" % (st["id"], st["pr"], (st.get("stop") or {}).get("reason"),
                                                      st["pr"], st["pr"]))
        if not os.path.isdir(self.clone):
            return [*L, f"no main clone {self.clone} (merge.clone)"]
        if self.git(self.clone, "fetch", "-q", self.remote, timeout=300)[0] != 0:
            L.append("warning: git fetch failed: the plan reads the refs as they were")
        main_ref = f"{self.remote}/{self.base}"
        sim_main = self.rev(self.clone, main_ref)
        tmp = tempfile.mkdtemp(prefix="merge-plan-")
        sim = os.path.join(tmp, "sim")
        ident = {"GIT_AUTHOR_NAME": "merge-plan", "GIT_AUTHOR_EMAIL": "merge-plan@localhost",
                 "GIT_COMMITTER_NAME": "merge-plan", "GIT_COMMITTER_EMAIL": "merge-plan@localhost"}
        try:
            if self.git(self.clone, "worktree", "add", "-q", "--detach", sim, sim_main)[0] != 0:
                return [*L, f"could not make a throwaway worktree in {tmp}"]
            label, ahead, bases = main_ref, [], [("main", sim_main)]
            if self.depth() > 0:
                L.append("trials: the %d PR(s) after the one riding are tried on top of it on %s<PR> while it rides "  # noqa: UP031
                         "(merge.presync_depth); a stop of it throws them away" % (self.depth(), self.m.get("trial_prefix")))
            for i, e in enumerate(items, 1):
                L.append("")
                L += self.plan_one(i, e, sim, sim_main, label, ident, bases=bases, prev=ahead[-1] if ahead else None)
                if L[-1].startswith("  → simulated merge "):
                    sim_main = L[-1].split()[-1]
                    ahead.append("#%d" % e["pr"])  # noqa: UP031
                    label = "{} + {}".format(main_ref, " + ".join(ahead))
                    bases.append(("#%d" % e["pr"], sim_main))  # noqa: UP031
        finally:
            self.git(self.clone, "worktree", "remove", "--force", sim)  # (no `worktree prune`: others' entries stay)
            shutil.rmtree(tmp, ignore_errors=True)
        return L

    def plan_one(self, i, e, sim, sim_main, label, ident, bases=None, prev=None):
        from .stack import Stack
        from .tracks import Tracks
        n, br, wt = e["pr"], e["branch"], e["worktree"]
        L = [i18n.t("merge.plan_entry", i=i, id=e["id"], pr=n, title=clip(e.get("title"), 70), branch=br,
                    status=STATUS_RU[e["status"]],
                    step=" ({})".format(STEP_RU.get(e.get("step"), e.get("step"))) if e.get("step") else "",
                    word=clip(e.get("word"), 80))]
        try:
            pr = self.pr_view(n)
        except MergeError as ex:
            return [*L, f"  gh: {ex}"]
        L.append("  PR: {}{}, head {}, into {}".format(pr["state"], ", draft" if pr.get("isDraft") else "",
                                                  (pr.get("headRefOid") or "?")[:7], pr.get("baseRefName")))
        stk, tr = Stack(self.state, self.cfg), Tracks(self.state, self.cfg)
        if e.get("st") and stk._dep_open(e["st"]):
            o = stk.get(e["st"])
            t, c = tr.car_by_stack(e["st"])
            L.append("  stack: {} ({}){}{}".format(e["st"], WHERE_RU.get(o["where"], o["where"]),
                                               ", car {}:{}".format(t["id"], c["n"]) if c else "",
                ", LOCKED {}: {}".format(o["lock"]["id"], o["lock"]["condition"]) if o.get("lock") else ""))
        elif e.get("car"):
            L.append("  stack: no object; car {} (parked)".format(e["car"]))
        else:
            L.append("  stack: %s" % ("{} has left the stack".format(e["st"]) if e.get("st") else "no object"))
        try:
            self.gates(e)
        except Stop as s:
            L.append(f"  STOP at the gate: {s.reason}")
        if e.get("merged"):
            return [*L, "  merged already as {}: the runner goes on with the deploy, smoke and usage".format(e["merged"]["sha"][:7])]
        if pr["state"] == "MERGED":
            return [*L, "  merged on GitHub already ({}): the runner takes its merge commit".format(((pr.get("mergeCommit") or {}).get("oid") or "?")[:7])]
        head0 = self.rev(self.clone, f"{self.remote}/{br}") or pr.get("headRefOid")
        ahead = False
        if not os.path.isdir(wt):
            L.append(f"  worktree: none at {wt} — the real run stops here")
        else:
            cur = self.git(wt, "symbolic-ref", "--short", "-q", "HEAD")[1].strip()
            dirty = [ln for ln in self.git(wt, "status", "--porcelain")[1].splitlines() if ln and not ln.startswith("??")]
            local, ob = self.rev(wt, "HEAD"), self.rev(wt, f"{self.remote}/{br}")
            if local == ob:
                rel = f"= {self.remote}/{br}"
            elif local and ob and self.is_ancestor(wt, local, ob):
                rel = f"behind {self.remote}/{br} (fast-forward)"
            elif local and ob and self.is_ancestor(wt, ob, local):
                ahead, head0 = True, local
                rel = "{} ahead of {}/{} by {} commit(s): the real run pushes it after the scan".format(
                    local[:7], self.remote, br, self.git(wt, "rev-list", "--count", f"{ob}..{local}")[1].strip())
            else:
                rel = "diverged from {}/{} ({} / {}) — the real run stops here".format(self.remote, br, (local or "?")[:7],
                                                                                (ob or "?")[:7])
            L.append("  worktree: {}, on {}, {}, {}".format(wt, cur or "?", "DIRTY — the real run stops here" if dirty else "clean",
                                                        rel))
        if not head0 or self.git(sim, "checkout", "-q", "--detach", head0)[0] != 0:
            return [*L, f"  no {self.remote}/{br} to try"]
        trial = self.plan_trial(e, label, prev)
        try:
            res = self.merge_in(sim, sim_main, br, label, "plan", env=ident)
        except Stop as s:
            who = self.attribute(sim, s.files, bases or [("main", sim_main)], head0) if s.files else []
            conflicts = ", ".join(i18n.t("merge.conflict_with", other=c["with"], file=c["file"]) for c in who)
            return L + [f"  STOP: {s.reason}"] + (["  " + i18n.t("merge.conflict_n", n=n, conflicts=conflicts)]
                                                    if who else []) + trial + [
                "  (a failure stops the queue: the PRs after it would not ride; below, without this PR)"]
        try:
            kind, text, _, _ = classify(self.runs(br, head0), self.jobs, self.clock(), float(self.m["queued_max_min"]))
        except MergeError as ex:
            kind, text = "?", str(ex)
        ci = "CI on {}: {}{}".format(head0[:7], {"green": "green", "failed": "FAILED", "github": "waiting for GitHub",
                                              "pending": "running", "none": "no run"}.get(kind, kind),
                                 "" if kind == "green" else f" ({text})")
        if res["push"]:
            L.append("  sync: would merge {} into {} with a merge commit{}{}; private scan; git push {} {}; CI anew on "
                     "the new head (now: {})".format(label, br, "; resolved from the prepared resolutions: " + ", ".join(
                         res["resolved"]) + " (then merge.after_resolve)" if res["resolved"] else "",
                         "; the queue resolves by itself: " + "; ".join(res["auto"]) if res["auto"] else "", self.remote,
                         br, ci))
        elif ahead:
            L.append("  sync: {}; the local head is not on GitHub: private scan, git push {} {}, CI on it".format(
                res["note"], self.remote, br))
        else:
            L.append("  sync: {} — no push; {}".format(res["note"], ci))
        L += trial
        L.append("  merge: gh pr merge %d --merge --match-head-commit <the head CI tested>" % n)  # noqa: UP031
        after = []
        if e.get("st") and stk._dep_open(e["st"]):
            t, c = tr.car_by_stack(e["st"])
            after.append("stack done {} --car merged{}".format(e["st"], i18n.t("merge.plan_car_merged", car="{}:{}".format(
                t["id"], c["n"])) if c else ""))
        elif e.get("car"):
            after.append(i18n.t("merge.plan_parked_merged", car=e["car"]))
        after.append(f"CI/CD of the merge commit on {self.base}, the deploy job, the site, smoke; the plugins' lines")
        L.append("  after: " + "; ".join(after))
        # the PR merged on top of the simulated main: what the next PR would see
        if self.git(sim, "checkout", "-q", "--detach", sim_main)[0] != 0 or \
                self.git(sim, "merge", "-q", "--no-ff", res["head"], "-m", "simulated merge of #%d" % n, env=ident)[0] != 0:  # noqa: UP031
            self.git(sim, "merge", "--abort")
            return L + ["  (the simulated merge of #%d into main failed: the next PRs are tried without it)" % n]  # noqa: RUF005, UP031
        return L + ["  → simulated merge of #%d: %s" % (n, self.rev(sim, "HEAD"))]  # noqa: RUF005, UP031

    def plan_trial(self, e, label, prev):
        """The dry run's line for e's trial: built while the PR ahead rides; what the entry holds now."""
        if self.depth() <= 0 or not prev:
            return []
        name = self.trial_branch(e["pr"])
        now = trial_brief(e["pr"], e["trial"]) if e.get("trial") else "none yet"
        return ["  trial: %s = #%d + (%s), built while %s rides (the merge as above, the private scan of what it "  # noqa: UP031
                "carries, git push %s %s; CI on it); now: %s" % (name, e["pr"], label, prev, self.remote, name, now)]


# --- commands --------------------------------------------------------------------------------------------

def find_object(state, cfg, n):
    """The stack object with PR n, or the parked car with it: → (object or None, car ref or None)."""
    from .stack import Stack
    from .tracks import Tracks
    stk = Stack(state, cfg)
    objs = [o for o in stk.objects() if o.get("pr") == n]
    tr = Tracks(state, cfg)
    if len(objs) > 1:
        raise MergeError("PR #%d is the PR of %s: one object per PR (stack set … --pr)" % (n, ", ".join(o["id"] for o in objs)))  # noqa: UP031
    if objs:
        t, c = tr.car_by_stack(objs[0]["id"])
        return objs[0], ("{}:{}".format(t["id"], c["n"]) if c else None)
    cars = ["{}:{}".format(t["id"], c["n"]) for t in tr.tracks() for c in t["cars"] if c.get("pr") == n
            and c["status"] not in ("merged", "dropped")]
    return None, (cars[0] if cars else None)


def place(q, e, at):
    """Put entry e at place `at` (from 1) among the open entries, in memory: never ahead of one that rides or
    waits for GitHub (the runner holds it), ahead of a stopped one (a fix rides first; the stopped one waits for
    its `merge resume`). → its place among the open entries."""
    rest = [x for x in q.data["entries"] if x is not e]
    op = [x for x in rest if x["status"] in OPEN]
    held = 0
    while held < len(op) and op[held]["status"] in ("riding", "github"):
        held += 1
    pos = max(held, min(len(op), int(at) - 1))
    if pos >= len(op):
        q.data["entries"] = [*rest, e]
    else:
        i = rest.index(op[pos])
        q.data["entries"] = [*rest[:i], e, *rest[i:]]
    return [x for x in q.data["entries"] if x["status"] in OPEN].index(e) + 1


def move(state, cfg, ref, at):
    q = MergeQueue(state)
    e = q.get(ref)
    if e["status"] not in ("waiting", "stopped"):
        raise MergeError("%s #%d is %s: only a waiting or stopped entry moves" % (e["id"], e["pr"], STATUS_RU[e["status"]]))  # noqa: UP031
    if int(at) < 1:
        raise MergeError("--at is a place from 1")
    was = [x["id"] for x in q.entries(open_only=True)].index(e["id"]) + 1
    n = place(q, e, at)
    log_line(e, "moved: place %d → %d" % (was, n))  # noqa: UP031
    q.save("move", e["id"], at=n, was=was)
    lines = ["%s #%d: place %d → %d in the merge queue" % (e["id"], e["pr"], was, n)]  # noqa: UP031
    if n > int(at):
        lines.append("note: ahead of it the runner holds what rides or waits for GitHub: place %d is the first free" % n)  # noqa: UP031
    return e, lines


def fallback_notes(e):
    """An entry no longer watched while a runner fallback of its (FALLBACKS) is not set back: what to do by hand."""
    lines = []
    for key, (_, what) in FALLBACKS.items():
        fb = e.get(key + "_runner") or {}
        if fb and not fb.get("back"):
            lines.append("note: {} is still {} ({}'s fallback, run {}): once that run is over, {}".format(
                fb.get("var"), labels_json(fb.get("to") or []), what, fb.get("run"), back_command(fb)))
    return lines


def handoff(state, cfg, ref, reason):
    if not (reason or "").strip():
        raise MergeError("handoff needs --reason")
    q = MergeQueue(state)
    e = q.get(ref)
    if not e.get("merged"):
        raise MergeError("%s #%d is not merged: a handoff is for a merged PR whose ride did not end (an open one "  # noqa: UP031
                         "waits, `merge drop` takes it out)" % (e["id"], e["pr"]))
    if e["status"] not in OPEN:
        raise MergeError("%s #%d is %s: only an open entry is handed off" % (e["id"], e["pr"], STATUS_RU[e["status"]]))  # noqa: UP031
    lines = []
    if e["status"] in ("riding", "github") and Rider(state, cfg).lock_held():
        lines.append("note: the runner rides it now: it stops watching it at its next step")
    lines += fallback_notes(e)
    e.update(status="merged", step=None, stop=None, github=None,
             handoff={"ts": stamp(), "reason": reason.strip(), "step": (e.get("stop") or {}).get("step") or e.get("step")})
    log_line(e, f"handed off: its deploy rides with the next merge ({reason})")
    q.save("handoff", e["id"], reason=reason)
    from .stages import ru
    return e, ["%s #%d (merged as %s) is no longer watched: its deploy rides with the next merge; «%s» until "  # noqa: RUF005, UP031
               "that one arrives (%s)" % (e["id"], e["pr"], e["merged"]["sha"][:7], ru("rolling"), reason)] + lines


def check(state, cfg, pr_n, word, sh=None, force=False, why=None):
    from .tracks import Tracks
    if word is None or not str(word).strip():
        raise MergeError(i18n.t("merge.no_word"))
    n = int(pr_n)
    q = MergeQueue(state)
    live = [e for e in q.entries(open_only=True) if e["pr"] == n]
    if live:
        raise MergeError("PR #%d is in the merge queue already (%s, %s)" % (n, live[0]["id"], STATUS_RU[live[0]["status"]]))  # noqa: UP031
    r = Rider(state, cfg, sh=sh)
    pr = r.pr_view(n)
    if pr["state"] != "OPEN":
        raise MergeError("PR #%d is %s" % (n, pr["state"]))  # noqa: UP031
    if pr.get("isDraft"):
        raise MergeError("PR #%d is a draft" % n)  # noqa: UP031
    if pr.get("baseRefName") != r.base:
        raise MergeError("PR #%d goes into %s, not %s" % (n, pr.get("baseRefName"), r.base))  # noqa: UP031
    br = pr["headRefName"]
    o, car = find_object(state, cfg, n)
    lines = []
    if o is not None:
        if o.get("branch") and o["branch"] != br:
            raise MergeError("%s's branch is %s, PR #%d's is %s" % (o["id"], o["branch"], n, br))  # noqa: UP031
        if o.get("lock"):
            raise MergeError("{} is locked ({}: {}): a locked object does not merge".format(o["id"], o["lock"]["id"],
                                                                                         o["lock"]["condition"]))
    tr = Tracks(state, cfg)
    t, c = tr.car(car) if car else (None, None)
    if c is not None:
        b = tr.car_blockers(t, c, lock=False)
        if b:
            raise MergeError("{} waits for {}: its PR is blocked, not ready to merge".format(car, ", ".join(b)))
    from . import gates, marks
    reading, forced = None, None
    if marks.wanted(cfg):
        body = r.gh_json("pr", "view", str(n), "-R", r.repo, "--json", "body") or {}
        reading = marks.Reading(state, cfg, car or (o["id"] if o is not None else None), pr=n,
                                body=str(body.get("body") or ""))
        lines += reading.lines()
        unmet = {"DoD": [i18n.t("merge.dod_principles", unmet="; ".join(reading.verdict["unmet"]))]
                 if reading.verdict["unmet"] else []}
        from .stages import ru
        try:
            forced = gates.check(unmet, force, why, "PR #%d" % n, ru("queued"),  # noqa: UP031
                                 "depot2 merge add %d --word «…»" % n, error=MergeError)  # noqa: UP031
        except MergeError as e:  # what the PR's section gave, under the refusal
            raise MergeError("\n".join([str(e), *lines]))  # noqa: B904
    return types.SimpleNamespace(n=n, q=q, pr=pr, br=br, o=o, car=car, tr=tr, c=c, reading=reading, forced=forced,
                                 lines=lines)


def add(state, cfg, pr_n, word, sh=None, at=None, force=False, why=None):
    """The owner's word puts a PR into the queue (at: its place among the open entries, from 1; default the
    end), once `check` passed it: the DoD's principles read into its car's marks, `force` with `why` recorded on the
    entry and its stage rows. → (entry, lines)."""
    from . import gates, marks
    from .stack import Stack
    if at is not None and int(at) < 1:
        raise MergeError("--at is a place from 1")
    k = check(state, cfg, pr_n, word, sh=sh, force=force, why=why)
    n, q, pr, br, o, car, tr, c = k.n, k.q, k.pr, k.br, k.o, k.car, k.tr, k.c
    reading, forced, lines = k.reading, k.forced, k.lines
    wt = os.path.join(expand(cfg["merge"]["worktrees"]), br)
    eid = q.new_id()
    e = {"id": eid, "pr": n, "title": pr.get("title"), "branch": br, "worktree": wt, "url": pr.get("url"),
         "st": o["id"] if o else None, "car": car, "word": str(word), "added": stamp(), "status": "waiting",
         "step": None, "head": None, "pushed": None, "github": None, "stop": None, "merged": None, "deploy": None,
         "smoke": None, "usage": None, "prev_status": o.get("status") if o else None, "log": []}
    if reading is not None:
        e["principles"] = marks.compact(reading.verdict)
    if forced:
        e["forced"] = {"why": why, "gates": forced}
    log_line(e, f"added on the owner's word: «{word}»")
    e["prev_stage"] = (Stack(state, cfg).stage_of(o) if o is not None else tr.stage_of(c) if c is not None else None)
    q.data["entries"].append(e)
    where = place(q, e, at) if at is not None else len(q.entries(open_only=True))
    q.save("add", eid, pr=n, word=str(word), branch=br, st=e["st"], car=car, at=where if at is not None else None,
           forced=True if forced else None, why=why if forced else None, gates=forced)
    lines.append("%s: PR #%d (%s) is in the merge queue, place %d; the owner's word: «%s»" % (  # noqa: UP031
        eid, n, br, where, word))
    if forced:
        lines.append(i18n.t("merge.forced", id=eid, why=why, gates=gates.gates_text(forced)))
    lines += on_the_tracks(state, cfg, e, "queued", i18n.t("merge.why_queued", word=word, id=eid, force=f"; --force: {why}" if forced else ""), "merge add", forced=bool(forced))
    if o is not None:
        status = i18n.t("merge.st_status", id=eid)
        Stack(state, cfg).set(o["id"], status=status)
        lines.append("{} → {}{}".format(o["id"], status, f" — car {car}" if car else ""))
        if o["where"] == "slot":
            lines.append("note: {} is in a slot: its agent must be done with {} before the ride (a dirty worktree "
                         "stops it)".format(o["id"], wt))
    elif car:
        lines.append(f"no stack object: car {car} (parked) is marked merged after the merge")
    else:
        lines.append("no stack object with PR #%d: no stack done after the merge" % n)  # noqa: UP031
    if reading is not None:
        reading.save("merge add")  # read fresh from the disk: after the stage rows and the object's status wrote
    if not os.path.isdir(wt):
        lines.append(f"warning: no worktree {wt} yet: the ride stops there")
    lines.append("ride: depot2 merge run (one runner; in the background) — dry run first: depot2 merge run --dry-run")
    return e, lines


def on_the_tracks(state, cfg, e, stage, why, by, forced=False):
    from . import stages
    from .stack import Stack
    from .tracks import Tracks
    out = []
    stk = Stack(state, cfg)
    if e.get("st") and stk._dep_open(e["st"]):
        stk.set_stage(e["st"], stage, by, why, forced=forced)
        out += stk.notes
    tr = Tracks(state, cfg)
    t, c = tr.car_by_stack(e["st"]) if e.get("st") else (None, None)
    ref = "{}:{}".format(t["id"], c["n"]) if c is not None else e.get("car")
    if ref:
        row = tr.car_stage(ref, stage, by, why, forced=forced)
        line = stages.say(ref, row)
        if line:
            out.append(line)
    return out


def restore_status(state, cfg, e):
    from .stack import Stack
    stk = Stack(state, cfg)
    if e.get("st") and stk._dep_open(e["st"]):
        o = stk.get(e["st"])
        if i18n.matches(o.get("status"), "merge.st_status"):  # the status `add` gave it, in any language
            stk.set(e["st"], status=e.get("prev_status") or "PR #%d" % e["pr"])  # noqa: UP031
            return ["{}: its status is back: {}".format(e["st"], e.get("prev_status") or "PR #%d" % e["pr"])]  # noqa: UP031
    return []


def resume(state, cfg, ref=None, reason=None):
    q = MergeQueue(state)
    e = q.get(ref) if ref else q.stopped()
    if e is None:
        raise MergeError("the merge queue is not stopped")
    if e["status"] != "stopped":
        raise MergeError("%s #%d is %s, not stopped" % (e["id"], e["pr"], STATUS_RU[e["status"]]))  # noqa: UP031
    stop = e.get("stop") or {}
    step = "deploy" if e.get("merged") else None
    e.update(status="waiting", stop=None, step=step, step_since=None)
    log_line(e, "resumed{} (it stopped at {}: {})".format(": " + reason if reason else "", stop.get("step"), stop.get("reason")))
    q.save("resume", e["id"], reason=reason, step=step)
    return e, ["%s #%d waits again, %s; the queue goes on: depot2 merge run" % (  # noqa: UP031
        e["id"], e["pr"], "from the deploy (merged as {})".format(e["merged"]["sha"][:7]) if step else "from the start")]


def drop(state, cfg, ref, reason):
    if not (reason or "").strip():
        raise MergeError("drop needs --reason")
    q = MergeQueue(state)
    e = q.get(ref)
    if e["status"] not in OPEN:
        raise MergeError("%s #%d is %s: only an open entry is dropped" % (e["id"], e["pr"], STATUS_RU[e["status"]]))  # noqa: UP031
    lines = []
    if e.get("merged"):
        lines.append("note: #%d is merged already (%s): drop only stops watching it (its deploy riding with the next "  # noqa: UP031
                     "merge: `merge handoff %d --reason …`)" % (e["pr"], e["merged"]["sha"][:7], e["pr"]))
    elif e["status"] in ("riding", "github") and Rider(state, cfg).lock_held():
        lines.append("note: the runner rides it now: it stops before the merge")
    lines += fallback_notes(e)
    e.update(status="dropped", dropped={"ts": stamp(), "reason": reason.strip()})
    log_line(e, f"dropped: {reason}")
    q.save("drop", e["id"], reason=reason)
    if not e.get("merged"):
        lines += restore_status(state, cfg, e)
        lines += on_the_tracks(state, cfg, e, back_stage(state, cfg, e),
                               i18n.t("merge.why_dropped", reason=reason.strip()), "merge drop")
    return e, ["%s #%d left the merge queue: %s" % (e["id"], e["pr"], reason)] + lines  # noqa: RUF005, UP031


def rerun(state, cfg, ref, sh=None):
    q = MergeQueue(state)
    e = q.get(ref)
    g = e.get("github") or {}
    if e["status"] != "github" or not g.get("run"):
        raise MergeError("%s #%d is %s: nothing GitHub left unstarted to rerun" % (e["id"], e["pr"], STATUS_RU[e["status"]]))  # noqa: UP031
    r = Rider(state, cfg, sh=sh)
    code, out, err = r.gh("run", "rerun", str(g["run"]), "--failed", "-R", r.repo)
    if code != 0:
        raise MergeError("gh run rerun {} --failed: {} (a run still queued has nothing to rerun: wait, or "
                         "gh run cancel {} first)".format(g["run"], clip(err or out, 200), g["run"]))
    g = dict(g, reruns=int(g.get("reruns", 0)) + 1, last_rerun=stamp())
    e["github"] = g
    log_line(e, "gh run rerun {} --failed (by hand)".format(g["run"]))
    q.save("rerun", e["id"], run=g["run"])
    return e, ["gh run rerun %s --failed: %s #%d (rerun %d)%s" % (  # noqa: UP031
        g["run"], e["id"], e["pr"], g["reruns"], "" if r.lock_held() else "; the runner is not running: depot2 merge run")]


def back_stage(state, cfg, e):
    from .stack import Stack
    if e.get("prev_stage") and e["prev_stage"] != "queued":
        return e["prev_stage"]
    stk = Stack(state, cfg)
    if e.get("st") and stk._dep_open(e["st"]) and stk.get(e["st"])["where"] == "post":
        return "acceptance"
    return "build"


def run_dir(state):
    """.merge/: the runner's lock and log; its own .gitignore says *."""
    d = state.path(RUN_DIR)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    return d


def lock_held(state):
    p = state.path(RUN_DIR, RUN_LOCK)
    if not os.path.exists(p):
        return None
    fd = os.open(p, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return None
    except OSError:
        return (os.pread(fd, 32, 0) or b"?").decode(errors="replace").strip() or "?"
    finally:
        os.close(fd)


def lock_pid(state):
    """The pid the last runner wrote into its lock (it may be gone): only read, never locked — a probe's flock in
    the very moment a runner starts would refuse that runner."""
    try:
        with open(state.path(RUN_DIR, RUN_LOCK)) as f:
            return f.read(32).strip() or None
    except OSError:
        return None


def runner_status(state, q=None):
    q = q or MergeQueue(state)
    return {"pid": lock_held(state), "open": len(q.entries(open_only=True)), "stopped": q.stopped()}


def idle_how(st):
    """(why, what starts it again) when no runner rides the queue: why is None but for a stopped queue."""
    if st is not None:
        return i18n.t("merge.stopped_at", pr=st["pr"]), i18n.t("merge.resume_or_drop", pr=st["pr"])
    return None, "`depot2 merge run --ensure`"


def runner_cell(state, s=None):
    s = s or runner_status(state)
    if s["pid"]:
        return i18n.t("merge.runner_working", pid=s["pid"])
    if not s["open"]:
        return i18n.t("merge.runner_idle_empty")
    why, how = idle_how(s["stopped"])
    return i18n.t("merge.runner_idle", n=s["open"], why=" " + why if why else "", how=how)


def spawn_runner(argv, log, cwd):
    """The runner, detached: its own session, no tty, its output appended to the log."""
    with open(log, "a") as out:
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out, cwd=cwd,
                                start_new_session=True, close_fds=True)


SPAWN = spawn_runner  # the tests swap it for a stand-in that takes the lock like a runner


def log_tail(path, n=6):
    try:
        with open(path, errors="replace") as f:
            return [ln.rstrip("\n") for ln in f.readlines()[-n:]]
    except OSError:
        return []


def ensure(state, cfg, sets=None, wait_s=None):
    s = runner_status(state)
    if s["pid"]:
        return 0, [i18n.t("merge.ensure_riding", pid=s["pid"])]
    if not s["open"]:
        return 0, [i18n.t("merge.ensure_empty")]
    st = s["stopped"]
    if st is not None:
        return 1, [i18n.t("merge.ensure_stopped", id=st["id"], pr=st["pr"],
                          reason=clip((st.get("stop") or {}).get("reason"), 200)),
                   i18n.t("merge.ensure_after_fix", pr=st["pr"])]
    was = lock_pid(state)
    lines = [i18n.t("merge.ensure_start", was=i18n.t("merge.ensure_stale_lock", pid=was) if was else "", n=s["open"])]
    log = os.path.join(run_dir(state), RUN_LOG)
    if os.path.exists(log) and os.path.getsize(log) > RUN_LOG_MAX:
        os.replace(log, log + ".1")
    team = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
    argv = [sys.executable, team, "--state", state.root]
    for x in sets or []:
        argv += ["--set", x]
    argv += ["merge", "run"]
    with open(log, "a") as f:
        f.write("== %s merge run --ensure: start, %d in the queue\n" % (stamp(), s["open"]))  # noqa: UP031
    clone = expand((cfg.get("merge") or {}).get("clone") or "")
    p = SPAWN(argv, log, clone if clone and os.path.isdir(clone) else state.root)
    end = time.monotonic() + (ENSURE_WAIT_S if wait_s is None else wait_s)
    while True:
        # the runner writes its pid only once it holds the lock; then the probe cannot refuse it
        if lock_pid(state) == str(p.pid) and lock_held(state) == str(p.pid):
            return 0, [*lines, i18n.t("merge.ensure_started", pid=p.pid, log=log)]
        code = p.poll()
        if code is not None:  # killed by a signal (a negative code): a failure all the same
            return code if code >= 0 else 1, lines + [i18n.t("merge.ensure_exited", pid=p.pid, code=code, log=log)] + [
                "  " + ln for ln in log_tail(log)]
        if time.monotonic() >= end:
            return 0, [*lines, i18n.t("merge.ensure_no_lock", pid=p.pid, log=log)]
        time.sleep(0.1)


def prune_worktrees(state, cfg, dry_run=False, sh=None):
    return Rider(state, cfg, sh=sh).prune_worktrees(dry_run=dry_run)


# --- the tables and the report ----------------------------------------------------------------------------

def state_cell(e, now=None):
    s = e["status"]
    if s == "waiting":
        return STATUS_RU["waiting"] + (i18n.t("merge.cell_from_step", step=STEP_RU.get(e["step"], e["step"]))
                                       if e.get("step") else "") + (
            " · {}".format(trial_cell(e["trial"])) if e.get("trial") else "")
    if s == "riding":
        out = i18n.t("merge.cell_riding", step=STEP_RU.get(e.get("step"), e.get("step")),
                     since=hhmm(e.get("step_since"))[11:], age=age(e.get("step_since"), now))
        if e.get("merged"):
            out += i18n.t("merge.cell_merged_sha", sha=e["merged"]["sha"][:7])
        elif e.get("head"):
            out += i18n.t("merge.cell_head", sha=e["head"][:7])
        return out
    if s == "github":
        g = e.get("github") or {}
        return i18n.t("merge.cell_github", step=STEP_RU.get(e.get("step"), e.get("step")),
                      reason=clip(g.get("reason"), 120), since=hhmm(g.get("since"))[11:], age=age(g.get("since"), now),
                      reruns=int(g.get("reruns") or 0),
                      status="; githubstatus: {}".format(g["githubstatus"]) if g.get("githubstatus") else "")
    if s == "stopped":
        st = e.get("stop") or {}
        return i18n.t("merge.cell_stopped", step=STEP_RU.get(st.get("step"), st.get("step")), at=hhmm(st.get("ts")),
                      reason=clip(st.get("reason"), 200), pr=e["pr"])
    if s == "merged":
        h = e.get("handoff")
        tail = i18n.t("merge.cell_handoff", reason=clip(h.get("reason"), 120), arrived=i18n.t(
            "merge.cell_handoff_arrived", pr=h["arrived"]["pr"]) if h.get("arrived") else "") if h else ""
        return i18n.t("merge.cell_merged", sha=((e.get("merged") or {}).get("sha") or "?")[:7],
                      at=hhmm((e.get("merged") or {}).get("ts")),
                      outcome="; " + outcome_text(e) if outcome_text(e) else "", handoff=tail,
                      wt="; " + wt_text(e.get("wt")) if e.get("wt") else "")
    return i18n.t("merge.cell_dropped", reason=clip((e.get("dropped") or {}).get("reason"), 120))


def smoke_word(ok):
    """The smoke check's verdict in a word: True ok, None skipped, anything else not ok."""
    return SMOKE_RU["ok" if ok else "skipped" if ok is None else "bad"]


def outcome_text(e):
    parts = []
    d = e.get("deploy")
    if d:
        parts.append(i18n.t("merge.outcome_deploy", job=d.get("deploy_job"), site=d.get("site")))
    ls = e.get("smoke")
    if ls:
        parts.append("smoke {}".format(smoke_word(ls.get("ok")))
                     + (" ({})".format(ls.get("summary")) if ls.get("ok") is not True else ""))
    return ", ".join(parts)


def _object_cell(e):
    return "{}{}".format(e.get("st") or "—", " ({})".format(e["car"]) if e.get("car") else "")


def md(state, cfg, all_=False, now=None, heading=True):
    from .stack import _cell
    q = MergeQueue(state)
    items = q.entries() if all_ else q.entries(open_only=True)
    L = [i18n.t("merge.md_heading"), ""] if heading else []
    from .stages import entry_stage, ru
    L.append(i18n.t("merge.md_header"))
    L.append("|---|---|---|---|---|---|---|---|")
    for i, e in enumerate(items, 1):
        L.append("| %d | %s | %s | %s | %s | %s | %s | %s |" % (  # noqa: UP031
            i, _cell("#%d %s" % (e["pr"], clip(e.get("title"), 60))), _cell(e["branch"]), _cell(_object_cell(e)),  # noqa: UP031
            _cell(state_cell(e, now)), _cell("«{}»".format(clip(e.get("word"), 80))), hhmm(e.get("added")),
            ru(entry_stage(e))))
    if not items:
        L.append(i18n.t("merge.md_empty"))
    L.append("")
    r = runner_status(state, q)
    if r["pid"] or not r["open"]:
        L.append(i18n.t("merge.md_runner", runner=runner_cell(state, r), means=means(cap=True)))
    else:
        why, how = idle_how(r["stopped"])
        L.append(i18n.t("merge.md_idle", n=r["open"], why=": " + why if why else "", how=how, means=means(cap=True)))
    tried = [e for e in items if e["status"] == "waiting" and e.get("trial")]
    if tried:
        m = cfg.get("merge") or {}
        L.append(i18n.t("merge.md_trials", prefix=m.get("trial_prefix") or "mq/", depth=m.get("presync_depth"),
                        trials="; ".join(trial_brief(e["pr"], e["trial"]) for e in tried)))
    last = last_merged(q)
    if last:
        L.append(i18n.t("merge.md_last", last=merged_brief(last)))
    return "\n".join(L) + "\n"


def merges(q):
    """The one rule for what the views call the merges: `merge report`, `merge report --json` (the web view's merge
    tab) and the last merge of `merge`, `merge --json` and `queues`. The entries the queue merged, in its order, the
    newest last; one dropped after its merge is not among them (the drop stops watching it), one that waits again for
    its deploy after `merge resume` is."""
    return [e for e in q.entries() if e.get("merged") and e["status"] != "dropped"]


def last_merged(q):
    done = [(e["merged"].get("ts") or "", i, e) for i, e in enumerate(merges(q))]
    return max(done, key=lambda x: x[:2])[2] if done else None


def merged_brief(e):
    m = e.get("merged") or {}
    tail = outcome_text(e) if e["status"] == "merged" else i18n.t("merge.now", state=STATUS_RU[e["status"]])
    tail = "; ".join(x for x in (tail, wt_text(e.get("wt"))) if x)
    return "#%d %s (%s)%s" % (e["pr"], (m.get("sha") or "?")[:7], hhmm(m.get("ts")), "; " + tail if tail else "")  # noqa: UP031


def stack_part(state, cfg, now=None, places=None):
    from . import priority as pri
    from .stack import Stack, _cell, lock_cell, place_cell
    q = MergeQueue(state)
    items = q.entries(open_only=True)
    stk = Stack(state, cfg)
    from .stages import entry_stage, ru
    blank = " | |" if places is not None else ""
    L = [i18n.t("merge.stack_heading", n=len(items)) + blank]
    if not items:
        L.append(i18n.t("merge.stack_empty") + blank)
    for i, e in enumerate(items, 1):
        o = stk.get(e["st"]) if e.get("st") and stk._dep_open(e["st"]) else None
        what = "{} {}".format(e["st"], o["title"]) if o else "PR #%d %s" % (e["pr"], e.get("title") or "")  # noqa: UP031
        pl = (places or {}).get(e.get("st")) or {}
        cells = " {} | {} |".format(_cell(place_cell(pl, "train")), _cell(place_cell(pl, "track"))) if places is not None else ""
        L.append("| %s | %s | %s | %s | %s / PR #%d | %s | — | — | — | %s | %s |%s" % (  # noqa: UP031
            i18n.t("merge.stack_row", n=i), i18n.t("merge.stack_kind"), _cell(clip(what, 160)),
            pri.cell(o) if o else "—", _cell(e["branch"]), e["pr"], _cell(state_cell(e, now)),
            _cell(lock_cell(o.get("lock")) if o else "—"), ru(entry_stage(e)), cells))
    return L


def legend(state):
    q = MergeQueue(state)
    last = last_merged(q)
    return i18n.t("merge.legend", means=means(), runner=runner_cell(state),
                  last=i18n.t("merge.legend_last", last=merged_brief(last)) if last else "")


def to_json(state, all_=False):
    from .stages import entry_stage
    q = MergeQueue(state)
    r = runner_status(state, q)
    last = last_merged(q)
    return {"entries": [dict(e, stage=entry_stage(e), status_ru=STATUS_RU.get(e["status"], e["status"]))
                        for e in q.entries(open_only=not all_)], "runner_pid": r["pid"],
            "runner_idle": None if r["pid"] or not r["open"] else {
                "open": r["open"], "stopped": r["stopped"]["pr"] if r["stopped"] else None},
            "last_merged": {"id": last["id"], "pr": last["pr"], "sha": (last.get("merged") or {}).get("sha"),
                            "ts": (last.get("merged") or {}).get("ts"), "branch": last.get("branch"),
                            "wt": last.get("wt")} if last else None}


def report_json(state, last=1):
    """`merge report --json` and the web view's merge tab: the entries of the last `last` merges (`merges`, the ones
    `merge report` shows) as the queue keeps them, with their deploy, smoke and the plugins' after_merge lines."""
    return merges(MergeQueue(state))[-max(1, int(last)):]


def counts(state):
    q = MergeQueue(state)
    c = {}
    for e in q.entries(open_only=True):
        c[e["status"]] = c.get(e["status"], 0) + 1
    return c, q


def overview_row(state):
    """`queues --md`'s row."""
    c, q = counts(state)
    count = ", ".join("%s %d" % (STATUS_RU[k], c[k]) for k in SHOWN if k in c) or "0"  # noqa: UP031
    head = ["#%d %s" % (e["pr"], state_cell(e).split(" — ")[0]) for e in q.entries(open_only=True)[:3]]  # noqa: UP031
    if not head:
        last = last_merged(q)
        head = [i18n.t("merge.overview_last", pr=last["pr"], sha=((last.get("merged") or {}).get("sha") or "?")[:7])] \
            if last else []
    return (i18n.t("merge.overview_title"), count, head, "merge", None)


def report_entry(e):
    """The owner's lines for one merged PR."""
    m = e.get("merged") or {}
    L = [i18n.t("merge.report_entry", pr=e["pr"], title=clip(e.get("title"), 80), sha=(m.get("sha") or "?")[:7],
                at=hhmm(m.get("ts")), word=clip(e.get("word"), 80))]
    d = e.get("deploy") or {}
    if d:
        L.append(i18n.t("merge.report_deploy", conclusion=d.get("conclusion"), job=d.get("deploy_job"),
                        site=d.get("site")))
    ls = e.get("smoke") or {}
    if ls:
        L.append("  smoke: %s" % (SMOKE_RU[{True: "ok", False: "bad", None: "skipped"}[ls.get("ok")]] + " — " +
                                  (ls.get("summary") or "")))
    so = [x for x in e.get("log") or [] if " stack done " in x or RE_LOG_MERGED.search(x) or "no stack" in x]
    if so:
        L.append(i18n.t("merge.report_stack", line=so[-1].split(" ", 1)[1]))
    w = e.get("wt")
    if w:
        L.append(i18n.t("merge.report_wt_removed", size=size_text(w.get("freed"))) if w.get("removed") else
                 i18n.t("merge.report_wt_kept", reason=clip(w.get("reason"), 200)))
    for key in FALLBACKS:  # CI's first, then the deploy's: as they happen
        if e.get(key + "_runner"):
            L.append("  " + runner_line(e[key + "_runner"], key))
    u = e.get("usage") or {}
    for line in u.get("lines") or []:  # the plugins' after_merge lines
        L.append(i18n.t("merge.report_after", line=line))
    if u.get("error"):
        L.append(i18n.t("merge.report_after_error", error=u["error"]))
    return L


def report(state, cfg, last=1, now=None):
    q = MergeQueue(state)
    done = merges(q)
    L = [i18n.t("merge.report_heading", at=stamp()[:16].replace("T", " ")), ""]
    for e in done[-max(1, int(last)):]:
        L += report_entry(e)
        if e["status"] != "merged":
            L.append("  " + i18n.t("merge.now", state=state_cell(e, now)))
    if not done:
        L.append(i18n.t("merge.report_none"))
    c, _ = counts(state)
    L.append("")
    L.append(i18n.t("merge.report_queue", counts=", ".join("%s %d" % (STATUS_RU[k], c[k]) for k in SHOWN if k in c) or  # noqa: UP031
                    i18n.t("merge.report_queue_empty"), runner=runner_cell(state)))
    for e in q.entries(open_only=True):
        if e["status"] in ("stopped", "github"):
            L.append("- #%d %s" % (e["pr"], state_cell(e, now)))  # noqa: UP031
    return "\n".join(L) + "\n"
