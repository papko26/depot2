"""Local state: checked writes, append-only JSONL, a lock, and git commits of what changed.

Every write is read back and compared. Nothing here deletes a record.
"""

import contextlib
import fcntl
import hashlib
import json
import os
import subprocess
import time


class WriteCheckError(RuntimeError):
    """A write did not read back as written."""


class LockBusy(RuntimeError):
    pass


# fsync every write. The unit tests turn it off (DEPOT2_FSYNC=0) to stay fast; the read-back
# checks still run.
FSYNC = os.environ.get("DEPOT2_FSYNC", "1") != "0"
# How long a command waits for the state's lock before it fails.
LOCK_WAIT_S = 10.0


def _sync(f):
    f.flush()
    if FSYNC:
        os.fsync(f.fileno())


def now_iso():
    t = time.localtime()
    off = time.strftime("%z", t)  # +0200
    return time.strftime("%Y-%m-%dT%H:%M:%S", t) + off[:3] + ":" + off[3:]


def today():
    return time.strftime("%Y-%m-%d")


def sha256_text(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path, encoding="utf-8") as f:
        return f.read()


def atomic_write(path, text):
    """Write via a temp file and rename, then read back and compare."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = "%s.tmp-%d" % (path, os.getpid())  # noqa: UP031
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(text)
        _sync(f)
    os.replace(tmp, path)
    if read_text(path) != text:
        raise WriteCheckError(f"read-back mismatch: {path}")


def write_new(path, text):
    """Create a file that must not exist yet (history is never overwritten)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
        _sync(f)
    if read_text(path) != text:
        raise WriteCheckError(f"read-back mismatch: {path}")


def write_json(path, obj):
    atomic_write(path, json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def read_json(path, default=None):
    text = read_text(path)
    return default if text is None else json.loads(text)


def dumps_line(rec):
    line = json.dumps(rec, ensure_ascii=False, sort_keys=True)
    if "\n" in line:
        raise ValueError("a JSONL record may not contain a newline")
    return line


def append_jsonl(path, rec):
    """Append one record, fsync, then check the file's last line is exactly that record."""
    line = dumps_line(rec)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        _sync(f)
    with open(path, "rb") as f:
        f.seek(0, os.SEEK_END)
        size = f.tell()
        f.seek(max(0, size - len(line.encode("utf-8")) - 1))
        tail = f.read().decode("utf-8")
    if tail.rstrip("\n").split("\n")[-1] != line:
        raise WriteCheckError(f"append did not read back: {path}")
    return rec


def read_jsonl(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                raise ValueError("%s:%d is not valid JSON" % (path, n))  # noqa: UP031
    return out


class State:
    """The state dir (a local git repo). Collects the paths a command touched, then commits them."""

    def __init__(self, root):
        self.root = os.path.abspath(os.path.expanduser(root))
        self.touched = set()

    def path(self, *parts):
        return os.path.join(self.root, *parts)

    @property
    def ledger_path(self):
        return self.path("ledger.jsonl")

    @property
    def pending_path(self):
        return self.path("pending.jsonl")

    @property
    def decisions_path(self):
        return self.path("decisions.jsonl")

    def char_dir(self, cid):
        return self.path("characters", cid)

    def rounds_dir(self):
        return self.path("rounds")

    def touch(self, path):
        self.touched.add(os.path.abspath(path))

    def exists(self):
        return os.path.isdir(self.root)

    @contextlib.contextmanager
    def lock(self, wait_s=None):
        """The state's lock. A command waits up to LOCK_WAIT_S for it (the backup's snapshot holds
        it for about a second), then fails."""
        os.makedirs(self.root, exist_ok=True)
        fd = os.open(self.path(".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        try:
            deadline = time.time() + (LOCK_WAIT_S if wait_s is None else wait_s)
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except OSError:
                    if time.time() >= deadline:
                        raise LockBusy("another depot2 has held {} for over {:g} s, nothing was written: run the "
                                       "command again in a moment (a write of the merge runner or the backup's snapshot "
                                       "holds it for about a second, a team round for its whole run)".format(
                                           self.path(".lock"), LOCK_WAIT_S if wait_s is None else wait_s))
                    time.sleep(0.2)
            yield
        finally:
            os.close(fd)

    def is_git(self):
        return os.path.isdir(self.path(".git"))

    def git(self, *args, check=True):
        return subprocess.run(["git", "-C", self.root] + list(args), check=check,
                              stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                              text=True)

    def init_git(self):
        os.makedirs(self.root, exist_ok=True)
        if not self.is_git():
            self.git("init", "-q")
        gi = self.path(".gitignore")
        if not os.path.exists(gi):
            atomic_write(gi, ".lock\n*.tmp-*\n")
        self.touch(gi)

    def commit(self, message):
        """git add exactly the touched paths (never -A) and commit. Returns the commit id or None."""
        if not self.is_git() or not self.touched:
            return None
        rel = sorted(os.path.relpath(p, self.root) for p in self.touched if os.path.exists(p))
        if not rel:
            return None
        self.git("add", "--", *rel)
        staged = self.git("diff", "--cached", "--name-only").stdout.strip()
        if not staged:
            self.touched.clear()
            return None
        self.git("-c", "user.name=depot2", "-c", "user.email=depot2@localhost",
                 "commit", "-q", "-m", message)
        left = self.git("status", "--porcelain", "--", *rel).stdout.strip()
        if left:
            raise WriteCheckError(f"state commit left changes: {left}")
        self.touched.clear()
        return self.git("rev-parse", "HEAD").stdout.strip()
