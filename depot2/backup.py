"""The depot's backup: `depot2 backup`, and the check after every command that starts it.

- git (push.py): every `backup.git.every_commits` commits a detached push to a remote the owner added by name, after
  the scan; no such remote, nothing happens.
- the cloud (here, off until `backup.remote` is set): a tar.gz of the depot made under its lock, encrypted by an
  rclone crypt remote defined only in the child's environment (its keys from pass), uploaded into its own top-level
  folder; daily when `backup.drive` is true, by hand with `depot2 backup --drive`. The keys never reach the disk or the
  log; old archives are pruned (`backup.keep_daily`, plus the first of every month).
"""

import calendar
import contextlib
import fcntl
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time

from .i18n import t
from .store import read_json, write_json

DIR = ".backup"  # inside the state; its own .gitignore says *, so git never sees it
STAMP = "backup.json"
LOG = "backup.log"
LOCK = "lock"
SPAWNED = "spawned"
LOG_MAX = 1 << 20

NAME_RE = re.compile(r"^team-(?P<host>[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?)-(?P<ts>\d{8}T\d{6}Z)\.tar\.gz$")
FORBIDDEN_FOLDERS = ("backups",)  # plus the config's backup.forbidden_folders
# Network flags for every rclone call that talks to the cloud.
NET = ["--retries", "3", "--low-level-retries", "10", "--contimeout", "30s", "--timeout", "5m"]
NET_HINTS = ("dial tcp", "no such host", "network is unreachable", "i/o timeout", "connection refused",
             "tls handshake timeout", "temporary failure in name resolution", "connection reset",
             "context deadline exceeded")
AUTH_HINTS = ("invalid_grant", "couldn't fetch token", "oauth2: ", "token expired", "unauthorized")


class BackupError(Exception):
    """A run that cannot go on. `reason` is the short Russian line of the ⚠ warning."""

    def __init__(self, reason, step, detail=""):
        Exception.__init__(self, reason)
        self.reason, self.step, self.detail = reason, step, detail


class Busy(Exception):
    """Another backup run holds the backup lock."""


class Secrets:
    """The keys and their obscured forms of one run: never printed, logged or passed in argv."""

    def __init__(self):
        self.values = []

    def add(self, value):
        if value and value not in self.values:
            self.values.append(value)
        return value

    def redact(self, text):
        text = text if isinstance(text, str) else str(text)
        for v in self.values:
            text = text.replace(v, "***")
        return text

    def check_argv(self, argv):
        if any(v in a for a in argv for v in self.values):
            raise BackupError(t("backup.key_in_argv"), "internal")


# --- time, paths, the stamp ------------------------------------------------------------------

def utc_iso(t=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() if t is None else t))


def parse_utc(s):
    return calendar.timegm(time.strptime(s, "%Y-%m-%dT%H:%M:%SZ"))


def host_name(b):
    h = b.get("host") or socket.gethostname().split(".")[0]
    h = re.sub(r"[^A-Za-z0-9-]+", "-", h).strip("-")
    return h or "host"


def archive_name(host, t=None):
    return "team-{}-{}.tar.gz".format(host, time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if t is None else t)))


def bdir(state):
    d = state.path(DIR)
    os.makedirs(d, exist_ok=True)
    gi = os.path.join(d, ".gitignore")
    if not os.path.exists(gi):
        with open(gi, "w") as f:
            f.write("*\n")
    return d


def log_path(state, name=LOG):
    return state.path(DIR, name)


def load_stamp(state):
    return read_json(state.path(DIR, STAMP), {}) or {}


def save_stamp(state, st):
    bdir(state)
    write_json(state.path(DIR, STAMP), st)


def rotate_log(state, name=LOG):
    p = log_path(state, name)
    if os.path.exists(p) and os.path.getsize(p) > LOG_MAX:
        os.replace(p, p + ".1")


@contextlib.contextmanager
def backup_lock(state, name=LOCK):
    """The Drive run's lock (`lock`) or the push's (`git.lock`): a second run of the same kind
    finds it held and exits.
    """
    fd = os.open(os.path.join(bdir(state), name), os.O_RDWR | os.O_CREAT, 0o644)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Busy  # noqa: B904
        os.ftruncate(fd, 0)
        os.write(fd, ("%d\n" % os.getpid()).encode())  # noqa: UP031
        yield
    finally:
        os.close(fd)


def is_running(state, name=LOCK):
    """For --status only: a probe takes the lock for an instant."""
    p = state.path(DIR, name)
    if not os.path.exists(p):
        return False
    fd = os.open(p, os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        fcntl.flock(fd, fcntl.LOCK_UN)
        return False
    except OSError:
        return True
    finally:
        os.close(fd)


def check_cfg(b):
    folder, crypt, remote = b["folder"], b["crypt_name"], b["remote"]
    if not re.match(r"^[A-Za-z0-9][A-Za-z0-9._-]*$", folder or "") or ".." in folder:
        raise BackupError(t("backup.bad_folder", folder=repr(folder)), "config")
    if folder.lower() in FORBIDDEN_FOLDERS + tuple(x.lower() for x in b.get("forbidden_folders") or ()):
        raise BackupError(t("backup.folder_taken", folder=repr(folder)), "config")
    if remote is None:
        raise BackupError(t("backup.no_remote"), "config")
    if not b.get("pass_password") or not b.get("pass_salt"):
        raise BackupError(t("backup.no_pass"), "config")
    forbidden = tuple(x.rstrip(":").lower() for x in b.get("forbidden_remotes") or ())
    if not re.match(r"^[a-z][a-z0-9]*$", crypt or "") or crypt in forbidden:
        raise BackupError(t("backup.bad_crypt", crypt=repr(crypt)), "config")
    if not (re.match(r"^[A-Za-z][A-Za-z0-9_-]*:$", remote or "") or (remote.startswith("/") and remote.endswith("/"))):
        raise BackupError(t("backup.bad_remote", remote=repr(remote)), "config")
    if remote.split(":")[0].lower() in forbidden:
        raise BackupError(t("backup.forbidden", remote=repr(remote)), "config")
    for k in ("every_h", "retry_min", "keep_daily"):
        if int(b[k]) < 1:
            raise BackupError(t("backup.not_ge1", item=k), "config")


# --- retention -------------------------------------------------------------------------------

def plan_retention(names, host, keep_daily=14):
    """Our archives (this host, the exact name pattern) → (keep, delete), both sorted by time.

    Kept: the newest archive of each of the `keep_daily` latest UTC days that have one, and the
    first archive of every UTC month. Every other archive of this host is deleted; other hosts'
    archives and names that don't match are never touched.
    """
    mine = {}
    for n in names:
        m = NAME_RE.match(n)
        if m and m.group("host") == host:
            mine[n] = m.group("ts")
    by_ts = sorted(mine, key=lambda n: mine[n])
    newest_of_day, first_of_month = {}, {}
    for n in by_ts:
        ts = mine[n]
        newest_of_day[ts[:8]] = n
        first_of_month.setdefault(ts[:6], n)
    days = sorted(newest_of_day)[-int(keep_daily):]
    keep = {newest_of_day[d] for d in days} | set(first_of_month.values())
    return [n for n in by_ts if n in keep], [n for n in by_ts if n not in keep]


# --- one run ---------------------------------------------------------------------------------

def _nice(argv):
    return (["nice", "-n", "10"] if shutil.which("nice") else []) + argv


class Runner:
    def __init__(self, state, cfg, auto=False):
        self.state, self.b, self.auto = state, cfg["backup"], auto
        self.secrets = Secrets()
        self.crypt = self.b["crypt_name"]
        self.host = host_name(self.b)
        self.env = None
        self.dest = self.b["remote"] + self.b["folder"]

    # output: in --auto stdout IS the log; in the foreground print and append to the log
    def say(self, msg):
        msg = self.secrets.redact(msg)
        stamp = "%s [%d] " % (utc_iso(), os.getpid())  # noqa: UP031
        if self.auto:
            print(stamp + msg, flush=True)
            return
        print(msg, flush=True)
        try:
            bdir(self.state)
            with open(log_path(self.state), "a", encoding="utf-8") as f:
                f.write(stamp + msg + "\n")
        except OSError:
            pass

    # --- pass and the crypt remote ---------------------------------------------------------
    def read_key(self, entry):
        env = dict(os.environ)
        if self.auto:  # the background never prompts: no cached passphrase → gpg fails at once
            env["PASSWORD_STORE_GPG_OPTS"] = (env.get("PASSWORD_STORE_GPG_OPTS", "") +
                                              " --batch --pinentry-mode=error").strip()
        try:
            p = subprocess.run([self.b["pass_bin"], "show", entry], env=env, text=True,
                               stdin=subprocess.DEVNULL if self.auto else None,
                               capture_output=True, timeout=60 if self.auto else 300)
        except FileNotFoundError:
            raise BackupError(t("backup.no_pass_bin"), "pass")  # noqa: B904
        except subprocess.TimeoutExpired:
            raise BackupError(t("backup.pass_silent"), "pass")  # noqa: B904
        key = self.secrets.add((p.stdout or "").split("\n")[0].rstrip("\r"))
        err = self.secrets.redact((p.stderr or "").strip())
        if p.returncode == 0 and len(key) >= 16:
            return key
        if p.returncode == 0:
            raise BackupError(t("backup.pass_short", entry=entry), "pass")
        if "not in the password store" in err:
            raise BackupError(t("backup.pass_missing", entry=entry), "pass", err)
        raise BackupError(t("backup.pass_locked"), "pass", err)

    def obscure(self, key):
        try:
            p = subprocess.run([self.b["rclone_bin"], "obscure", "-"], input=key + "\n", text=True,
                               capture_output=True, timeout=60)
        except FileNotFoundError:
            raise BackupError(t("backup.no_rclone"), "rclone")  # noqa: B904
        out = self.secrets.add((p.stdout or "").strip())
        if p.returncode != 0 or not out:
            raise BackupError(t("backup.obscure_failed"), "rclone", self.secrets.redact(p.stderr or ""))
        return out

    def keys(self):
        k1 = self.read_key(self.b["pass_password"])
        k2 = self.read_key(self.b["pass_salt"])
        if k1 == k2:
            raise BackupError(t("backup.same_secrets"), "pass")
        n = f"RCLONE_CONFIG_{self.crypt.upper()}_"
        env = {k: v for k, v in os.environ.items() if not k.startswith(n)}
        env.update({n + "TYPE": "crypt", n + "REMOTE": self.dest, n + "PASSWORD": self.obscure(k1),
                    n + "PASSWORD2": self.obscure(k2), n + "FILENAME_ENCRYPTION": "standard",
                    n + "DIRECTORY_NAME_ENCRYPTION": "true", "TZ": "UTC"})
        self.env = env

    # --- rclone ----------------------------------------------------------------------------
    def obj(self, name):
        if not NAME_RE.match(name):
            raise BackupError(t("backup.bad_name", name=repr(name)), "internal")
        return f"{self.crypt}:{name}"

    def rclone(self, step, *args):
        argv = [self.b["rclone_bin"], *list(args)]
        self.secrets.check_argv(argv)
        try:
            return subprocess.run(argv, env=self.env, stdin=subprocess.DEVNULL, text=True,
                                  capture_output=True, timeout=int(self.b["timeout_s"]))
        except FileNotFoundError:
            raise BackupError(t("backup.no_rclone"), step)  # noqa: B904
        except subprocess.TimeoutExpired:
            raise BackupError(t("backup.rclone_timeout", step=step, s=int(self.b["timeout_s"])), step)  # noqa: B904

    def rclone_error(self, step, p):
        err = self.secrets.redact(p.stderr or "")
        low = err.lower()
        remote = self.b["remote"]
        if any(h in low for h in NET_HINTS):
            reason = t("backup.r_net")
        elif any(h in low for h in AUTH_HINTS):
            reason = t("backup.r_auth", remote=remote)
        elif "didn't find section in config file" in low:
            reason = t("backup.r_no_remote", remote=remote)
        else:
            reason = t("backup.r_code", step=step, code=p.returncode)
        tail = "\n".join([ln for ln in err.strip().split("\n") if ln.strip()][-5:])
        return BackupError(reason, step, tail)

    def listing(self):
        """{name: size} of the folder, through the crypt layer (names decrypted)."""
        p = self.rclone("lsjson", "lsjson", "--files-only", "--no-mimetype", self.crypt + ":", *NET)
        if p.returncode == 3 or (p.returncode != 0 and "directory not found" in (p.stderr or "").lower()):
            return {}  # the folder doesn't exist yet: the first copyto creates it
        if p.returncode != 0:
            raise self.rclone_error("lsjson", p)
        try:
            items = json.loads(p.stdout or "[]")
        except ValueError:
            raise BackupError(t("backup.not_json"), "lsjson")  # noqa: B904
        return {it["Path"]: int(it["Size"]) for it in items if not it.get("IsDir")}

    # --- the steps -------------------------------------------------------------------------
    def snapshot(self, tmp, name):
        parent, base = os.path.split(self.state.root)
        tar_path = os.path.join(tmp, name[:-len(".gz")])
        t0 = time.time()
        try:
            with self.state.lock(wait_s=float(self.b["lock_wait_s"])):
                held = time.time()
                # Not niced: it is short (~0.4 s for 100 MB) and holds the lock.
                p = subprocess.run(["tar", "-C", parent, "-cf", tar_path, f"--exclude={base}/.lock",
                                    f"--exclude={base}/{DIR}", "--exclude=*.tmp-*", base],
                                   stdin=subprocess.DEVNULL, capture_output=True, text=True)
                held = time.time() - held
        except RuntimeError:
            w = int(self.b["lock_wait_s"])
            wait = t("backup.wait_min", m=w // 60) if w >= 60 else t("backup.wait_s", s=w)
            raise BackupError(t("backup.busy", wait=wait), "snapshot")  # noqa: B904
        if p.returncode != 0:
            raise BackupError(t("backup.tar", code=p.returncode), "snapshot", p.stderr.strip()[-500:])
        self.say(f"snapshot: {os.path.basename(tar_path)}, {os.path.getsize(tar_path) / 1e6:.1f} MB, the state lock held {held:.1f} s (waited {time.time() - t0 - held:.1f} s)")
        p = subprocess.run(_nice(["gzip", "-6", "-n", tar_path]), stdin=subprocess.DEVNULL,
                           capture_output=True, text=True)
        local = os.path.join(tmp, name)
        if p.returncode != 0 or not os.path.isfile(local):
            raise BackupError(t("backup.gzip", code=p.returncode), "snapshot", p.stderr.strip()[-500:])
        h = hashlib.sha256()
        with open(local, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        return local, os.path.getsize(local), h.hexdigest()

    def upload(self, local, name):
        p = self.rclone("copyto", "copyto", "--immutable", local, self.obj(name), *NET)
        if p.returncode != 0:
            raise self.rclone_error("copyto", p)

    def verify_upload(self, tmp, name, size):
        files = self.listing()
        if name not in files:
            raise BackupError(t("backup.not_uploaded", name=name), "verify")
        if files[name] != size:
            raise BackupError(t("backup.bad_size", name=name, got=files[name], want=size), "verify")
        # cryptcheck: encrypts the local file with the remote's nonce and compares Drive's MD5;
        # it reads only the remote file's header.
        p = self.rclone("cryptcheck", "cryptcheck", "--one-way", "--include", name, tmp, self.crypt + ":", *NET)
        if p.returncode != 0:
            raise self.rclone_error("cryptcheck", p)
        return files

    def retention(self, name, files):
        keep, drop = plan_retention(list(files), self.host, self.b["keep_daily"])
        if name not in keep:
            raise BackupError(t("backup.retention_refused"), "retention")
        flags = [] if self.b["drive_trash"] else ["--drive-use-trash=false"]
        deleted = []
        for n in drop:
            p = self.rclone("deletefile", "deletefile", self.obj(n), *(flags + NET))
            if p.returncode != 0:
                raise self.rclone_error("deletefile", p)
            deleted.append(n)
            self.say(f"retention: deleted {n}")
        return keep, deleted

    # --- the runs --------------------------------------------------------------------------
    def backup(self, dry_run=False):
        st = load_stamp(self.state)
        t0 = time.time()
        if not dry_run:
            st["last_attempt"] = utc_iso(t0)
            save_stamp(self.state, st)
        self.say("backup {}{} → {} (crypt {}:)".format("(dry run) " if dry_run else "", self.state.root, self.dest,
                                                  self.crypt))
        tmp = None
        try:
            check_cfg(self.b)
            self.keys()
            self.say("keys: 2 pass entries read (never shown)")
            files = self.listing()
            mine = [n for n in files if NAME_RE.match(n)]
            self.say("folder: %d files, %d archives" % (len(files), len(mine)))  # noqa: UP031
            name = archive_name(self.host, t0)
            if dry_run:
                return self._dry(name, files)
            tmp = tempfile.mkdtemp(prefix="team-backup-")
            local, size, sha = self.snapshot(tmp, name)
            self.say("archive: %s, %d bytes, sha256 %s" % (name, size, sha))  # noqa: UP031
            self.upload(local, name)
            files = self.verify_upload(tmp, name, size)
            self.say("uploaded and verified: lsjson size %d, cryptcheck ok" % size)  # noqa: UP031
            st.update(last_success=utc_iso(), archive=name, size=size, sha256=sha, remote=self.dest,
                      source=self.state.root, duration_s=round(time.time() - t0, 1), last_error=None)
            save_stamp(self.state, st)
            try:
                keep, deleted = self.retention(name, files)
                st["retention"] = {"ts": utc_iso(), "kept": len(keep), "deleted": deleted, "error": None}
            except BackupError as e:
                st["retention"] = {"ts": utc_iso(), "error": e.reason}
                self.say(f"retention FAILED: {e.reason} {e.detail}")
            save_stamp(self.state, st)
            self.say("ok in %.1f s" % (time.time() - t0))
            return 0
        except BackupError as e:
            if not dry_run:
                st["last_error"] = {"ts": utc_iso(), "reason": e.reason, "step": e.step}
                save_stamp(self.state, st)
            self.say("FAILED ({}): {}{}".format(e.step, e.reason, ("\n" + e.detail) if e.detail else ""))
            return 1
        except Exception as e:
            if not dry_run:
                st["last_error"] = {"ts": utc_iso(), "reason": t("backup.crash", error=self.secrets.redact(e)[:200]),
                                    "step": "internal"}
                save_stamp(self.state, st)
            raise
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)

    def _dry(self, name, files):
        size = 0
        for root, dirs, fs in os.walk(self.state.root):
            if os.path.abspath(root) == self.state.root:
                dirs[:] = [d for d in dirs if d != DIR]
            size += sum(os.path.getsize(os.path.join(root, f)) for f in fs if f != ".lock")
        keep, drop = plan_retention([*list(files), name], self.host, self.b["keep_daily"])
        self.say(f"would write: {name} (the state is {size / 1e6:.1f} MB before gzip)")
        self.say(f"would upload: rclone copyto --immutable <tmp>/{name} {self.crypt}:{name}, then lsjson + cryptcheck")
        self.say("retention would keep %d and delete %d%s" % (len(keep), len(drop),  # noqa: UP031
                                                             "".join("\n  delete " + n for n in drop)))
        self.say("dry run: nothing written, uploaded or deleted")
        return 0

    def verify(self, archive=None):
        st = load_stamp(self.state)
        tmp = None
        try:
            check_cfg(self.b)
            self.keys()
            files = self.listing()
            if archive:
                name = archive
                if name not in files:
                    raise BackupError(t("backup.v_missing", name=name), "verify")
            else:
                ours = [n for n in files if NAME_RE.match(n) and NAME_RE.match(n).group("host") == self.host]
                if not ours:
                    raise BackupError(t("backup.v_none", host=self.host), "verify")
                name = max(ours, key=lambda n: NAME_RE.match(n).group("ts"))
            tmp = tempfile.mkdtemp(prefix="team-verify-")
            local = os.path.join(tmp, name)
            self.say("download: %s (%d bytes) → %s" % (name, files[name], tmp))  # noqa: UP031
            p = self.rclone("copyto", "copyto", self.obj(name), local, *NET)
            if p.returncode != 0:
                raise self.rclone_error("copyto", p)
            size = os.path.getsize(local)
            if size != files[name]:
                raise BackupError(t("backup.v_size", got=size, want=files[name]), "verify")
            h = hashlib.sha256()
            with open(local, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()
            if st.get("archive") == name and st.get("sha256") and st["sha256"] != sha:
                raise BackupError(t("backup.v_sha"), "verify")
            p = subprocess.run(_nice(["tar", "-tzf", local]), stdin=subprocess.DEVNULL, capture_output=True,
                               text=True)
            if p.returncode != 0:
                raise BackupError(t("backup.v_tar_t", code=p.returncode), "verify", p.stderr.strip()[-500:])
            entries = [ln for ln in p.stdout.split("\n") if ln]
            tops = {e.split("/")[0] for e in entries}
            if len(tops) != 1 or any(e.startswith("/") or "/../" in "/" + e + "/" for e in entries):
                raise BackupError(t("backup.v_tops", tops=sorted(tops)[:3]), "verify")
            top = tops.pop()
            x = os.path.join(tmp, "x")
            os.makedirs(x)
            p = subprocess.run(_nice(["tar", "-xzf", local, "-C", x]), stdin=subprocess.DEVNULL,
                               capture_output=True, text=True)
            if p.returncode != 0:
                raise BackupError(t("backup.v_tar_x", code=p.returncode), "verify", p.stderr.strip()[-500:])
            repo = os.path.join(x, top)
            p = subprocess.run(_nice(["git", "-C", repo, "fsck", "--full", "--no-progress"]),
                               stdin=subprocess.DEVNULL, capture_output=True, text=True)
            if p.returncode != 0:
                raise BackupError(t("backup.v_fsck", code=p.returncode), "verify", (p.stdout + p.stderr).strip()[-800:])
            commits = subprocess.run(["git", "-C", repo, "rev-list", "--count", "HEAD"], capture_output=True,
                                     text=True).stdout.strip()
            head = subprocess.run(["git", "-C", repo, "log", "-1", "--format=%cI %s"], capture_output=True,
                                  text=True).stdout.strip()
            self.say("verified: {}, sha256 {}{}".format(name, sha, " (= the upload's)" if st.get("archive") == name else ""))
            self.say("  %d entries under %s/; git fsck --full ok; %s commits; HEAD %s" % (len(entries), top, commits,  # noqa: UP031
                                                                                         head))
            st = load_stamp(self.state)
            st["last_verify"] = {"ts": utc_iso(), "archive": name, "ok": True, "entries": len(entries),
                                 "commits": int(commits or 0)}
            save_stamp(self.state, st)
            return 0
        except BackupError as e:
            st = load_stamp(self.state)
            st["last_verify"] = {"ts": utc_iso(), "ok": False, "reason": e.reason}
            save_stamp(self.state, st)
            self.say("VERIFY FAILED ({}): {}{}".format(e.step, e.reason, ("\n" + e.detail) if e.detail else ""))
            return 1
        finally:
            if tmp:
                shutil.rmtree(tmp, ignore_errors=True)
                self.say(f"temp copy removed: {tmp}")


# --- the check after every command -----------------------------------------------------------

def _age_h(now, iso):
    return (now - parse_utc(iso)) / 3600.0


def warning(state, st, b, now):
    """The one ⚠ line, or None."""
    ls, err = st.get("last_success"), st.get("last_error")
    if st.get("source") and st["source"] != state.root:
        return t("backup.w_foreign", source=st["source"])
    if not ls:
        return t("backup.w_never_err", reason=err["reason"]) if err else t("backup.w_never")
    age = _age_h(now, ls)
    if age < float(b["every_h"]):
        return None
    if err and err.get("ts", "") >= ls:
        return t("backup.w_old_err", h=int(age), reason=err["reason"])
    att = st.get("last_attempt")
    if att and att > ls and _age_h(now, att) > 1:
        return t("backup.w_hung", h=int(age), log=log_path(state))
    return None


def due(state, st, b, now):
    """Start a background run? Never before the first success: that run is a person's (--now)."""
    ls = st.get("last_success")
    if not ls or _age_h(now, ls) < float(b["every_h"]):
        return False
    if st.get("source") and st["source"] != state.root:
        return False  # a copied state dir: its archives would carry this host's name
    sp = state.path(DIR, SPAWNED)
    if os.path.exists(sp) and now - os.path.getmtime(sp) < float(b["retry_min"]) * 60:  # noqa: SIM103
        return False  # one start per retry window; a run still going holds its lock anyway
    return True


def spawn(state, sets, args=("--drive", "--auto"), marker=SPAWNED, log=LOG):
    """`depot2 backup <args>`, detached: its own session, no tty, its output into the log. The
    Drive run by default; the push passes `--push --auto` and its own marker and log.
    """
    d = bdir(state)
    rotate_log(state, log)
    with open(os.path.join(d, marker), "w") as f:
        f.write(utc_iso() + "\n")
    team = os.path.join(os.path.dirname(os.path.abspath(__file__)), "__main__.py")
    argv = [sys.executable, team, "--state", state.root]
    for s in sets or []:
        argv += ["--set", s]
    with open(log_path(state, log), "a") as out:
        subprocess.Popen([*argv, "backup", *list(args)], stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                         cwd=d, start_new_session=True, close_fds=True)


def after_command(state, a):
    """depot2 calls this after every command but `backup` and `seed`. Never raises, never waits.
    The Drive part only with `backup.drive` true; the push part only when its remote exists.
    """
    if os.environ.get("DEPOT2_BACKUP", "").lower() in ("0", "off", "no", "false"):
        return
    if getattr(a, "cmd", None) in ("backup", "seed") or not os.path.isdir(state.root):
        return
    try:
        from . import config
        b = config.load(state.root, getattr(a, "set", None), warn=lambda m: None)["backup"]
    except Exception as e:  # the check must never break a command
        print(t("backup.check_crashed", kind=type(e).__name__, error=e), file=sys.stderr)
        return
    if b.get("drive") is True:
        drive_check(state, a, b)
    from . import push
    try:
        line = push.check(state, a, b)
        if line:
            print(line, file=sys.stderr)
    except Exception as e:
        print(t("backup.push_check_crashed", kind=type(e).__name__, error=push.redact(e)), file=sys.stderr)


def drive_check(state, a, b):
    """The daily Drive run's part of after_command (backup.drive true)."""
    try:
        st = load_stamp(state)
        now = time.time()
        line = warning(state, st, b, now)
        started = False
        if not getattr(a, "dry_run", False) and due(state, st, b, now):
            check_cfg(b)
            spawn(state, getattr(a, "set", None))
            started = True
        if line:
            print(line, file=sys.stderr)
        elif started:
            print(t("backup.started", h=int(_age_h(now, st["last_success"]))), file=sys.stderr)
    except BackupError as e:
        print(t("backup.not_started", reason=e.reason), file=sys.stderr)
    except Exception as e:  # the check must never break a command
        print(t("backup.check_crashed", kind=type(e).__name__, error=e), file=sys.stderr)


# --- `depot2 backup` ------------------------------------------------------------------------

def status(state, cfg):
    from . import push
    push.status(state, cfg)
    print()
    drive_status(state, cfg)
    return 0


def drive_status(state, cfg):
    b = cfg["backup"]
    st = load_stamp(state)
    now = time.time()
    on = b.get("drive") is True
    print("drive: %s" % ("on (backup.drive true): daily, started by any depot2 command" if on else
                         "off (backup.drive false): only by hand, depot2 backup --drive [--now|--dry-run|--verify]"))
    print("backup of {} → {}{}/ (crypt {}:, names and contents encrypted)".format(state.root, b["remote"], b["folder"],
                                                                            b["crypt_name"]))
    print("keys: pass {}, {} (never shown)".format(b["pass_password"], b["pass_salt"]))
    ls = st.get("last_success")
    if ls:
        print("last success: %s (%.1f h ago): %s, %d bytes, sha256 %s, %s s"  # noqa: UP031
              % (ls, _age_h(now, ls), st.get("archive"), st.get("size") or 0, st.get("sha256"), st.get("duration_s")))
        left = float(b["every_h"]) - _age_h(now, ls)
        if on:
            print("next: %s" % ("due: the next depot2 command starts it in the background" if left <= 0 else
                                f"in {left:.1f} h, started by any depot2 command"))
    elif on:
        print("last success: never. The first run is a person's: depot2 backup --drive --now "
              "(no background run starts before it)")
    else:
        print("last success: never")
    err = st.get("last_error")
    if err and (not ls or err.get("ts", "") >= ls):
        print("last error: {}, step {}: {}".format(err.get("ts"), err.get("step"), err.get("reason")))
    if st.get("last_attempt"):
        print("last attempt: {}".format(st["last_attempt"]))
    r = st.get("retention")
    if r:
        print("retention: %s" % (("kept %d, deleted %d" % (r.get("kept", 0), len(r.get("deleted") or [])))  # noqa: UP031
                                 if not r.get("error") else "FAILED: " + r["error"]))
    v = st.get("last_verify")
    if v:
        print("last verify: {}, {}".format(v.get("ts"), ("ok: {}, {} entries, {} commits".format(
            v.get("archive"), v.get("entries"), v.get("commits"))) if v.get("ok") else "FAILED: " + v.get("reason", "")))
    print("running now: %s" % ("yes" if is_running(state) else "no"))
    print(f"log: {log_path(state)}")
    line = warning(state, st, b, now) if on else None
    if line:
        print(line)


def command(state, cfg, a):
    drive_only = [f for f, on in (("--now", a.now), ("--dry-run", a.dry_run), ("--verify", a.verify),
                                  ("--archive", a.archive)) if on]
    if drive_only and not a.drive:
        print("error: {} is for Drive: depot2 backup --drive {} (the git push: depot2 backup --push)".format(drive_only[0], " ".join(drive_only)), file=sys.stderr)
        return 1
    if a.auto and not (a.push or a.drive):
        print("error: --auto goes with --push or --drive", file=sys.stderr)
        return 1
    if a.push:
        from . import push
        return push.command(state, cfg, a)
    if not a.drive:
        return status(state, cfg)
    r = Runner(state, cfg, auto=a.auto)
    try:
        with backup_lock(state):
            if a.verify:
                return r.verify(a.archive)
            return r.backup(dry_run=a.dry_run)
    except Busy:
        if a.auto:
            r.say(f"another backup run holds {state.path(DIR, LOCK)}: nothing to do")
            return 0
        print(f"error: a backup is running already ({state.path(DIR, LOCK)}); see depot2 backup --status",
              file=sys.stderr)
        return 1
