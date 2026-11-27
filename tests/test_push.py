"""The remote is a local bare repository in a temp dir, so nothing reaches the network, and the private scan is a fake
(tests/fake_scan.py) that the tool is pointed at with --set. One string, HIT, plays the private word: the fake scan
flags it, and the tool must never show it.
"""

import contextlib
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

from helpers import BALLOT, HERE, TempState, slurp

from depot2 import backup, push

TEAM = os.path.join(os.path.dirname(HERE), "depot2", "__main__.py")
FAKE_SCAN = os.path.join(HERE, "fake_scan.py")
HIT = "zz-private-word-zz"
FORCE = ("--force", "-f", "--force-with-lease", "--mirror", "--delete", "-d", "--tags", "--all", "--prune")


def git(cwd, *args, check=True):
    return subprocess.run(["git", "-C", cwd, "-c", "user.name=t", "-c", "user.email=t@localhost"] + list(args),
                          check=check, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


class PushBase(unittest.TestCase):
    def setUp(self):
        self.t = TempState(git=True)
        self.t.state.commit("test: seed")
        self.root = self.t.state.root
        self.tmp = tempfile.mkdtemp(prefix="team-push-test-")
        self.bare = os.path.join(self.tmp, "remote.git")
        git(self.tmp, "init", "-q", "--bare", self.bare)
        self.slog = os.path.join(self.tmp, "scan.jsonl")
        self.env = dict(os.environ)
        self.env.pop("DEPOT2_BACKUP", None)  # helpers turns the check off for every other test
        self.env.update(FAKE_SCAN_LOG=self.slog, FAKE_SCAN_HIT=HIT)
        self.sets = ["backup.git.scan=" + FAKE_SCAN]
        self.outputs = []
        self.spawns = False
        self.n = 0
        self.branch = git(self.root, "symbolic-ref", "--short", "HEAD").stdout.strip()

    def tearDown(self):
        if self.spawns:  # let a detached push finish before its state goes away
            deadline = time.time() + 20
            time.sleep(0.3)
            while time.time() < deadline and backup.is_running(self.t.state, push.LOCK):
                time.sleep(0.1)
        self.t.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def team(self, *args, ok=True, **env):
        e = dict(self.env)
        e.update(env)
        argv = [sys.executable, TEAM, "--state", self.root]
        for s in self.sets:
            argv += ["--set", s]
        p = subprocess.run(argv + list(args), stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           text=True, env=e, timeout=120)
        self.outputs.append(p.stdout + p.stderr)
        if ok and p.returncode != 0:
            raise AssertionError("depot2 %s failed (%d): %s %s" % (args, p.returncode, p.stdout, p.stderr))
        return p

    def add_remote(self, url=None):
        git(self.root, "remote", "add", "backup", url or self.bare)

    def commits(self, k, text=None):
        for _ in range(k):
            self.n += 1
            name = "note-%03d.txt" % self.n
            with open(os.path.join(self.root, name), "w") as f:
                f.write((text or "note %d" % self.n) + "\n")
            git(self.root, "add", "--", name)
            git(self.root, "commit", "-q", "-m", "test: note %d" % self.n)

    def head(self):
        return git(self.root, "rev-parse", "HEAD").stdout.strip()

    def remote_head(self, bare=None):
        p = git(bare or self.bare, "rev-parse", "-q", "--verify", "refs/heads/" + self.branch, check=False)
        return p.stdout.strip() or None

    def scans(self):
        if not os.path.exists(self.slog):
            return []
        with open(self.slog) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def stamp(self):
        return push.load_stamp(self.t.state)

    def spawned(self):
        p = self.t.state.path(backup.DIR, push.SPAWNED)
        return os.path.getmtime(p) if os.path.exists(p) else None

    def age_marker(self, minutes):
        p = self.t.state.path(backup.DIR, push.SPAWNED)
        t = time.time() - minutes * 60
        os.utime(p, (t, t))

    def wait_for(self, cond, timeout=30):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        self.fail("timed out waiting")

    def wait_done(self):
        time.sleep(0.2)
        self.wait_for(lambda: not backup.is_running(self.t.state, push.LOCK))

    def log(self):
        p = push.log_path(self.t.state)
        if not os.path.exists(p):
            return ""
        with open(p, encoding="utf-8") as f:
            return f.read()

    def assert_never_shown(self, *secrets):
        texts = list(self.outputs) + [self.log()]
        sp = self.t.state.path(backup.DIR, push.STAMP)
        if os.path.exists(sp):
            with open(sp, encoding="utf-8") as f:
                texts.append(f.read())
        for s in secrets:
            for t in texts:
                self.assertNotIn(s, t)

    def first_push(self):
        self.add_remote()
        self.team("backup", "--push")
        self.assertEqual(self.remote_head(), self.head())


class FirstPushTest(PushBase):
    def test_no_remote_is_silent(self):
        self.commits(15)
        self.assertEqual(self.team("ledger").stderr, "")
        self.assertIsNone(self.spawned())
        out = self.team("backup", "--status").stdout
        self.assertIn("git push: remote backup не задан", out)
        self.assertNotIn("⚠", out)  # Drive is off by default too
        p = self.team("backup", "--push", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("remote backup не задан", p.stdout)
        self.assertEqual(self.scans(), [])

    def test_the_first_push_is_a_persons(self):
        self.add_remote()
        self.commits(12)
        p = self.team("ledger")
        self.assertIn("⚠ депо ещё не пушилось в backup: первый пуш — depot2 backup --push", p.stderr)
        self.assertIsNone(self.spawned())
        self.assertEqual(self.scans(), [])
        out = self.team("backup", "--push").stdout
        self.assertIn("ok: backup/%s at %s" % (self.branch, self.head()), out)
        self.assertEqual(self.remote_head(), self.head())
        # Two scans: what the push carries (all of the history: every blob, the commit messages),
        # then the files on disk, with the owner's arguments.
        s = self.scans()
        self.assertEqual(len(s), 2)
        self.assertEqual(s[0]["argv"][0], "--root")
        self.assertEqual(s[0]["argv"][2:], [])
        self.assertIn("commits.txt", s[0]["files"])
        blobs = [f for f in s[0]["files"] if f.startswith("blobs/")]
        self.assertTrue([f for f in blobs if f.endswith("/ledger.jsonl")])
        self.assertTrue([f for f in blobs if f.endswith("/note-012.txt")])
        self.assertFalse(os.path.exists(s[0]["argv"][1]))  # the export is gone
        self.assertEqual(s[1]["argv"], ["--repo", self.root])
        st = self.stamp()
        self.assertEqual(st["commit"], self.head())
        self.assertIsNone(st["last_error"])
        self.assertEqual(st["source"], self.root)
        self.assertEqual(git(self.root, "rev-parse", "refs/remotes/backup/" + self.branch).stdout.strip(), self.head())
        self.assertEqual(self.team("ledger").stderr, "")
        out = self.team("backup", "--status").stdout
        self.assertIn("last push: " + st["last_success"], out)
        self.assertIn("waiting: 0 commits", out)
        self.assertIn("running now: no", out)
        # Nothing new: no scan, no push, still a success.
        self.assertIn("nothing to push", self.team("backup", "--push").stdout)
        self.assertEqual(len(self.scans()), 2)


class EveryTenTest(PushBase):
    def test_every_10_commits_starts_one_push(self):
        self.spawns = True
        self.first_push()
        pushed = self.head()
        self.commits(9)
        self.assertEqual(self.team("ledger").stderr, "")
        self.assertIsNone(self.spawned())
        self.assertEqual(self.remote_head(), pushed)
        self.assertIn("waiting: 9 commits", self.team("backup", "--status").stdout)
        self.commits(1)
        p = self.team("ledger")
        self.assertIn("· пуш депо в backup: коммитов на очереди 10, запущен в фоне", p.stderr)
        self.assertIsNotNone(self.spawned())
        self.wait_for(lambda: self.remote_head() == self.head())
        self.wait_done()
        # The push scanned only what it carried: the 10 new notes' blobs, not the old history.
        s = self.scans()[2]
        blobs = sorted(os.path.basename(f) for f in s["files"] if f.startswith("blobs/"))
        self.assertEqual(blobs, ["note-%03d.txt" % i for i in range(1, 11)])
        started = self.spawned()
        self.assertEqual(self.team("stack").stderr, "")
        self.assertEqual(self.spawned(), started)
        self.assertIn("ok: backup/", self.log())
        self.assertEqual(self.stamp()["pushed"], 10)
        # A dry run never starts one.
        self.commits(10)
        os.remove(self.t.state.path(backup.DIR, push.SPAWNED))
        self.team("round", "--kind", "ballot", "--input", "/nonexistent", "--dry-run", ok=False)
        self.assertIsNone(self.spawned())

    def test_a_running_push_is_not_doubled(self):
        self.first_push()
        self.commits(10)
        n = len(self.scans())
        with backup.backup_lock(self.t.state, push.LOCK):
            self.assertEqual(self.team("backup", "--push", "--auto").returncode, 0)
            self.assertIn("another push holds", self.team("backup", "--push", "--auto").stdout)
            p = self.team("backup", "--push", ok=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("a push is running already", p.stderr)
            self.assertIn("running now: yes", self.team("backup", "--status").stdout)
        self.assertEqual(len(self.scans()), n)

    def test_a_commit_made_during_the_scan_waits_for_the_next_push(self):
        self.add_remote()
        scanned = self.head()
        self.team("backup", "--push", FAKE_SCAN_COMMIT=self.root)
        self.assertEqual(len(self.scans()), 2)
        self.assertNotEqual(self.head(), scanned)  # the fake scan committed while it ran
        self.assertEqual(self.remote_head(), scanned)  # exactly the scanned commit went out
        self.assertEqual(self.stamp()["commit"], scanned)
        self.assertIn("waiting: 1 commits", self.team("backup", "--status").stdout)
        self.team("backup", "--push")
        self.assertEqual(self.remote_head(), self.head())
        s = self.scans()[2]
        self.assertEqual([os.path.basename(f) for f in s["files"] if f.startswith("blobs/")], ["scan-moved-0.txt"])


class FailureTest(PushBase):
    def test_a_failed_push_warns_until_one_succeeds(self):
        self.spawns = True
        self.first_push()
        git(self.root, "remote", "set-url", "backup", os.path.join(self.tmp, "missing.git"))
        self.commits(10)
        self.team("ledger")
        self.wait_for(lambda: (self.stamp().get("last_error") or {}).get("reason"))
        self.wait_done()
        st = self.stamp()
        self.assertEqual(st["last_error"]["reason"], "нет репозитория на remote (адрес или права)")
        self.assertFalse(st["last_error"]["person"])
        self.assertEqual(st["fails"], 1)
        for cmd in (("ledger",), ("stack",), ("config",)):
            self.assertIn("⚠ пуш депо в backup не прошёл (не отправлено коммитов: 10): нет репозитория на remote",
                          self.team(*cmd).stderr)
        self.assertIn("⚠ пуш депо в backup не прошёл", self.team("backup", "--status").stdout)
        # The next background try waits retry_min (30) and doubles after each failure.
        started = self.spawned()
        self.team("ledger")
        self.assertEqual(self.spawned(), started)
        self.age_marker(31)
        self.team("ledger")
        self.assertNotEqual(self.spawned(), started)
        self.wait_for(lambda: self.stamp().get("fails") == 2)
        self.wait_done()
        self.age_marker(31)
        started = self.spawned()
        self.team("ledger")
        self.assertEqual(self.spawned(), started)  # 2 failures: 60 min
        # The owner fixes the address: the next push goes, the warning is gone.
        git(self.root, "remote", "set-url", "backup", self.bare)
        self.team("backup", "--push")
        self.assertEqual(self.remote_head(), self.head())
        self.assertEqual(self.stamp()["fails"], 0)
        self.assertEqual(self.team("ledger").stderr, "")

    def test_a_diverged_remote_is_never_forced(self):
        self.spawns = True
        self.first_push()
        git(self.bare, "tag", "keep-me", self.head())
        other = os.path.join(self.tmp, "other")
        git(self.tmp, "clone", "-q", self.bare, other)
        with open(os.path.join(other, "theirs.txt"), "w") as f:
            f.write("a commit made elsewhere\n")
        git(other, "add", "theirs.txt")
        git(other, "commit", "-q", "-m", "elsewhere")
        git(other, "push", "-q", "origin", "HEAD:" + self.branch)
        theirs = self.remote_head()
        self.commits(10)
        self.team("ledger")
        self.wait_for(lambda: self.stamp().get("last_error"))
        self.wait_done()
        st = self.stamp()
        self.assertEqual(st["last_error"]["reason"], "remote разошёлся, нужен владелец")
        self.assertTrue(st["last_error"]["person"])
        self.assertEqual(self.remote_head(), theirs)  # nothing forced
        self.assertIn("keep-me", git(self.bare, "tag").stdout)
        self.assertIn("⚠ пуш депо в backup не прошёл (не отправлено коммитов: 10): remote разошёлся, нужен владелец",
                      self.team("stack").stderr)
        # It needs a person: no background retry, whatever the window.
        self.age_marker(10000)
        started = self.spawned()
        self.team("ledger")
        self.assertEqual(self.spawned(), started)
        p = self.team("backup", "--push", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("remote разошёлся, нужен владелец", p.stdout)
        self.assertEqual(self.remote_head(), theirs)
        self.assertIn("needs a person", self.team("backup", "--status").stdout)

    def test_the_network_and_a_token_in_the_url(self):
        token = "tok3n-" + "q" * 20
        self.add_remote("https://someone:%s@127.0.0.1:9/state.git" % token)
        out = self.team("backup", "--status").stdout
        self.assertIn("https://***@127.0.0.1:9/state.git", out)
        p = self.team("backup", "--push", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("нет сети", p.stdout)
        self.assertIn("⚠ депо ещё не пушилось в backup: нет сети", self.team("ledger").stderr)
        self.assertIsNone(self.spawned())  # still never before the first success
        self.assert_never_shown(token)

    def test_refused_before_any_scan(self):
        self.add_remote()
        self.commits(3)
        for bad in ("backup.git.branch=+master", "backup.git.branch=a:b", "backup.git.remote=../x",
                    "backup.git.remote=-f", "backup.git.every_commits=0", 'backup.git.scan_pushed_args=["--repo"]'):
            p = self.team("--set", bad, "backup", "--push", ok=False)
            self.assertEqual(p.returncode, 1, bad)
            self.assertIn("⚠ пуш депо не запущен", self.team("--set", bad, "ledger").stderr, bad)
        git(self.root, "config", "remote.backup.mirror", "true")
        p = self.team("backup", "--push", ok=False)
        self.assertIn("mirror = true: отказ", p.stdout)
        git(self.root, "config", "--unset", "remote.backup.mirror")
        git(self.root, "checkout", "-q", "--detach")
        p = self.team("backup", "--push", ok=False)
        self.assertIn("HEAD не на ветке", p.stdout)
        self.assertIn("HEAD не на ветке", self.team("ledger").stderr)
        self.assertEqual(self.scans(), [])
        self.assertIsNone(self.remote_head())

    def test_push_argv_is_the_only_push(self):
        sha = "0123456789abcdef0123456789abcdef01234567"
        argv = push.push_argv("backup", "master", sha)
        self.assertEqual(argv, ["git", "-c", "push.followTags=false", "push", "--porcelain", "backup",
                                sha + ":refs/heads/master"])
        self.assertFalse(set(argv) & set(FORCE))
        for remote, branch, c in (("backup", "+master", sha), ("backup", "a:b", sha), ("-f", "master", sha),
                                  ("backup", ":master", sha), ("ba..ck", "master", sha), ("backup", "", sha),
                                  ("backup", "master", "HEAD"), ("backup", "master", "+" + sha[1:]),
                                  ("backup", "master", sha[:12])):
            with self.assertRaises(push.PushError):
                push.push_argv(remote, branch, c)
        env = push.push_env(self.t.state)
        self.assertEqual(env["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(env["GIT_ASKPASS"], "")
        self.assertIn("BatchMode=yes", env["GIT_SSH_COMMAND"])


class ScanTest(PushBase):
    def test_a_scan_that_is_not_clean_blocks_the_push(self):
        self.add_remote()
        for mode in ("dirty", "dirty0", "late", "spaced"):
            p = self.team("backup", "--push", ok=False, FAKE_SCAN_RESULT=mode)
            self.assertEqual(p.returncode, 1, mode)
            self.assertIn("скан не CLEAN", p.stdout, mode)
            self.assertIsNone(self.remote_head(), mode)
        self.assertTrue(self.stamp()["last_error"]["person"])
        self.assertIn("⚠ депо ещё не пушилось в backup: скан не CLEAN", self.team("ledger").stderr)
        # The line counts, not the exit code: CLEAN with exit 1 pushes.
        self.team("backup", "--push", FAKE_SCAN_RESULT="clean1")
        self.assertEqual(self.remote_head(), self.head())
        self.assert_never_shown(HIT)

    def test_a_word_redacted_within_the_ten_still_blocks(self):
        """The files on disk are clean again, but the push would carry the blob that held the word."""
        self.spawns = True
        self.first_push()
        pushed = self.head()
        self.commits(1, text="a line with %s in it" % HIT)
        with open(os.path.join(self.root, "note-001.txt"), "w") as f:
            f.write("redacted\n")
        git(self.root, "commit", "-q", "-am", "test: redact")
        self.commits(8)
        self.team("ledger")
        self.wait_for(lambda: self.stamp().get("last_error"))
        self.wait_done()
        self.assertEqual(self.stamp()["last_error"]["reason"], "скан не CLEAN (то, что уйдёт пушем), пуша нет: "
                         "запусти скан руками, затем depot2 backup --push")
        self.assertEqual(self.remote_head(), pushed)
        self.assertEqual(len(self.scans()), 3)  # the push's scan stopped it; the files' never ran
        self.age_marker(10000)
        started = self.spawned()
        self.team("ledger")
        self.assertEqual(self.spawned(), started)  # a person's: no background retry
        self.assert_never_shown(HIT)

    def test_a_missing_scan_blocks_the_push(self):
        self.add_remote()
        p = self.team("--set", "backup.git.scan=" + os.path.join(self.tmp, "no-scan.sh"), "backup", "--push", ok=False)
        self.assertIn("нет скана", p.stdout)
        self.assertIsNone(self.remote_head())


class CheckUnitTest(unittest.TestCase):
    def test_verdict_and_porcelain(self):
        self.assertTrue(push.is_clean("== x\nCLEAN\n"))
        self.assertTrue(push.is_clean("CLEAN\n\n"))
        for out in ("", "NOT CLEAN\n", "CLEAN\nmore\n", "CLEAN \n", " CLEAN\n", "clean\n"):
            self.assertFalse(push.is_clean(out), out)
        rows = push.parse_porcelain("To /x\n!\tHEAD:refs/heads/master\t[rejected] (fetch first)\nDone\n")
        self.assertEqual(rows, [("!", "HEAD:refs/heads/master", "[rejected] (fetch first)")])
        self.assertEqual(push.redact("fatal: https://u:p@h/x and http://t0k@h"),
                         "fatal: https://***@h/x and http://***@h")

    def test_backoff(self):
        g = {"retry_min": 30}
        self.assertEqual([push.wait_min(g, {"fails": f}) for f in (0, 1, 2, 3, 4, 5, 9)],
                         [30, 30, 60, 120, 240, 360, 360])


if __name__ == "__main__":
    unittest.main()


class RawGateTest(unittest.TestCase):

    def setUp(self):
        self.t = TempState(git=True)
        self.t.state.commit("test: seed")
        self.tmp = tempfile.mkdtemp(prefix="team-raw-test-")
        self.q = os.path.join(self.tmp, "quarantine")
        self.t.cfg["backup"]["git"].update(scan=FAKE_SCAN, quarantine=self.q)
        self.slog = os.path.join(self.tmp, "scan.jsonl")
        self.env = {k: os.environ.get(k) for k in ("DEPOT2_RAW_SCAN", "FAKE_SCAN_HIT", "FAKE_SCAN_LOG")}
        os.environ.pop("DEPOT2_RAW_SCAN", None)  # helpers turns the gate off for every other test
        os.environ.update(FAKE_SCAN_HIT=HIT, FAKE_SCAN_LOG=self.slog)

    def tearDown(self):
        for k, v in self.env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        self.t.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def raw(self, name, text):
        p = os.path.join(self.t.state.rounds_dir(), "r", "raw", name)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write(text)
        return p

    def scans(self):
        with open(self.slog) as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_a_clean_stream_stays(self):
        p = self.raw("builder-r1.jsonl", '{"type": "assistant"}\n')
        lines = []
        self.assertIsNone(push.gate_raw(self.t.state, self.t.cfg, p, lines.append))
        self.assertEqual((slurp(p), lines), ('{"type": "assistant"}\n', []))
        s = self.scans()
        self.assertEqual((len(s), s[0]["argv"][0], s[0]["files"]), (1, "--root", ["builder-r1.jsonl"]))
        self.assertFalse(os.path.exists(self.q))

    def test_a_dirty_stream_goes_to_the_quarantine_and_a_marker_stays(self):
        p = self.raw("chronicler-r1.jsonl", '{"text": "a %s here"}\n' % HIT)
        lines = []
        q = push.gate_raw(self.t.state, self.t.cfg, p, lines.append)
        self.assertEqual(q["file"], os.path.join("rounds", "r", "raw", "chronicler-r1.jsonl"))
        self.assertEqual(q["to"], os.path.join(self.q, "rounds", "r", "raw", "chronicler-r1.jsonl"))
        self.assertIn(HIT, slurp(q["to"]))
        marker = json.loads(slurp(p))
        self.assertEqual((marker["type"], marker["to"], marker["why"]),
                         ("quarantined", q["to"], "приватный скан не CLEAN"))
        self.assertEqual(len(lines), 1)
        self.assertIn("в карантине %s (вне депо)" % q["to"], lines[0])
        for text in lines + [slurp(p)]:
            self.assertNotIn(HIT, text)  # nothing the scan found is shown or kept in the state
        p = self.raw("chronicler-r1.jsonl", '{"text": "%s again"}\n' % HIT)  # a later one keeps the first
        self.assertEqual(push.gate_raw(self.t.state, self.t.cfg, p, lines.append)["to"], q["to"] + ".2")
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            push.status(self.t.state, self.t.cfg)
        self.assertIn("quarantine: 2 raw streams in %s (outside the depot" % self.q, buf.getvalue())
        shutil.rmtree(self.q)
        self.assertTrue(push.quarantine_line(self.t.cfg["backup"]["git"]).startswith("quarantine: empty in %s " % self.q))

    def test_no_scan_means_not_clean(self):
        self.t.cfg["backup"]["git"]["scan"] = os.path.join(self.tmp, "no-such-scan.sh")
        q = push.gate_raw(self.t.state, self.t.cfg, self.raw("painter-r1.jsonl", "{}\n"), lambda *a: None)
        self.assertIn("нет скана", q["why"])
        self.assertTrue(os.path.isfile(q["to"]))

    def test_off_scans_nothing(self):
        os.environ["DEPOT2_RAW_SCAN"] = "off"
        p = self.raw("x-r1.jsonl", HIT + "\n")
        self.assertIsNone(push.gate_raw(self.t.state, self.t.cfg, p, lambda *a: None))
        self.assertFalse(os.path.exists(self.slog))

    def test_a_round_commits_the_marker_never_the_stream(self):
        from test_round import FakeRunner

        from depot2.executors import FatalError
        from depot2.round import RoundAborted, run_round
        inp = os.path.join(self.tmp, "q.md")
        with open(inp, "w") as f:
            f.write(BALLOT)
        inner = FakeRunner(self.t.state, {("chronicler", "r1"): FatalError("API error 400: the message was refused")})

        def runner(spec):
            with open(spec.raw_path, "w") as f:
                f.write('{"text": "%s"}\n' % (HIT if spec.call_id.endswith("/chronicler-r1") else "plain"))
            return inner(spec)
        lines = []
        with self.assertRaises(RoundAborted):
            run_round(self.t.state, self.t.cfg, "ballot", inp, runner=runner, out=lines.append)
        rid = os.listdir(self.t.state.rounds_dir())[0]
        rel = os.path.join("rounds", rid, "raw", "chronicler-r1.jsonl")
        rj = json.loads(slurp(os.path.join(self.t.state.rounds_dir(), rid, "round.json")))
        self.assertEqual([q["file"] for q in rj["quarantined"]], [rel])
        self.assertEqual(len(self.scans()), 4)  # every call's stream, scanned alone
        self.assertTrue(any("в карантине" in x for x in lines))
        history = git(self.t.state.root, "log", "-p", "--all").stdout
        self.assertIn("quarantined", history)
        self.assertNotIn(HIT, history)
        self.assertEqual(git(self.t.state.root, "status", "--porcelain").stdout.strip(), "")
        self.assertIn(HIT, slurp(os.path.join(self.q, rel)))
        # the round goes on (`round --resume`); its report names the stream in the quarantine
        r = run_round(self.t.state, self.t.cfg, None, resume=rid, runner=FakeRunner(self.t.state, {}),
                      out=lambda *a: None)
        self.assertIn("%s: сырой поток в карантине (приватный скан не CLEAN)" % rel, r["report"])
