"""`backup --drive` runs it by hand either way (BackupNowTest, with the flag off). The checks of the daily run
(BackupCheckTest) turn the flag on.

Nothing here reaches the network, a real pass entry or rclone.conf: the fakes stand in for both, and the tool is
pointed at them with --set. The one real-rclone test runs only with DEPOT2_TEST_REAL_RCLONE=1, against a local
folder and an empty rclone.conf.
"""

import base64
import json
import os
import secrets
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import unittest

from helpers import HERE, TempState

from depot2 import backup
from depot2.store import write_json

TEAM = os.path.join(os.path.dirname(HERE), "depot2", "__main__.py")
FAKE_RCLONE = os.path.join(HERE, "fake_rclone.py")
FAKE_PASS = os.path.join(HERE, "fake_pass.py")
PW = "example/depot2-backup/crypt-password"
SALT = "example/depot2-backup/crypt-salt"
ALLOWED = {"obscure", "lsjson", "copyto", "cryptcheck", "deletefile"}


def fake_obscure(s):
    return "OBS" + base64.b64encode(s[::-1].encode()).decode().rstrip("=")


def iso_ago(hours):
    return backup.utc_iso(time.time() - hours * 3600)


class BackupBase(unittest.TestCase):
    real_rclone = False
    drive = False  # backup.drive: the daily run from after_command

    def setUp(self):
        self.t = TempState(git=True)
        self.t.state.commit("test: seed")
        self.root = self.t.dir
        self.tmp = tempfile.mkdtemp(prefix="team-backup-test-")
        self.cloud = os.path.join(self.tmp, "cloud")
        self.tmpdir = os.path.join(self.tmp, "tmp")
        os.makedirs(self.tmpdir)
        self.rlog = os.path.join(self.tmp, "rclone.jsonl")
        self.plog = os.path.join(self.tmp, "pass.jsonl")
        self.keys = {PW: "pw-" + secrets.token_hex(16), SALT: "salt-" + secrets.token_hex(16)}
        self.env = dict(os.environ)
        self.env.pop("DEPOT2_BACKUP", None)  # helpers turns the check off for every other test
        self.env.update(FAKE_PASS_SECRETS=json.dumps(self.keys), FAKE_RCLONE_DIR=self.cloud,
                        FAKE_RCLONE_LOG=self.rlog, FAKE_PASS_LOG=self.plog, TMPDIR=self.tmpdir)
        self.sets = ["backup.rclone_bin=" + FAKE_RCLONE, "backup.pass_bin=" + FAKE_PASS,
                     "backup.remote=fakedrive:", "backup.host=testhost"]
        if self.drive:
            self.sets.append("backup.drive=true")
        self.outputs = []
        self.spawns = False

    def tearDown(self):
        if self.spawns:  # let a detached run finish before its state goes away
            deadline = time.time() + 15
            time.sleep(0.3)
            while time.time() < deadline and backup.is_running(self.t.state):
                time.sleep(0.1)
        self.t.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def team(self, *args, ok=True, **env):
        e = dict(self.env)
        e.update(env)
        argv = [sys.executable, TEAM, "--state", self.root]
        for s in self.sets:
            argv += ["--set", s]
        p = subprocess.run(argv + list(args), capture_output=True, text=True, env=e, timeout=60)
        self.outputs.append(p.stdout + p.stderr)
        if ok and p.returncode != 0:
            raise AssertionError("depot2 %s failed (%d): %s %s" % (args, p.returncode, p.stdout, p.stderr))  # noqa: UP031
        return p

    def calls(self):
        if not os.path.exists(self.rlog):
            return []
        with open(self.rlog) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def cmds(self, name=None):
        return [c["argv"] for c in self.calls() if name is None or c["argv"][0] == name]

    def pass_calls(self):
        if not os.path.exists(self.plog):
            return []
        with open(self.plog) as f:
            return [json.loads(ln) for ln in f if ln.strip()]

    def stamp(self):
        return backup.load_stamp(self.t.state)

    def set_stamp(self, **kw):
        os.makedirs(self.t.state.path(backup.DIR), exist_ok=True)
        st = self.stamp()
        st.update(kw)
        write_json(self.t.state.path(backup.DIR, backup.STAMP), st)

    def wait_for(self, cond, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cond():
                return True
            time.sleep(0.1)
        self.fail("timed out waiting")
        return None

    def spawned(self):
        """The mtime of the marker a start writes before it detaches, or None: no start."""
        p = self.t.state.path(backup.DIR, backup.SPAWNED)
        return os.path.getmtime(p) if os.path.exists(p) else None

    def folder(self):
        d = os.path.join(self.cloud, "depot2-backup")
        return sorted(os.listdir(d)) if os.path.isdir(d) else []

    def assert_no_secret(self):
        texts = list(self.outputs)
        for p in (self.rlog, self.plog, backup.log_path(self.t.state), self.t.state.path(backup.DIR, backup.STAMP)):
            if os.path.exists(p):
                with open(p, encoding="utf-8") as f:
                    texts.append(f.read())
        for k in self.keys.values():
            for s in (k, fake_obscure(k)):
                for t in texts:
                    self.assertNotIn(s, t)
        for c in self.calls():
            self.assertNotIn(True, [k in a for a in c["argv"] for k in self.keys.values()])


class BackupNowTest(BackupBase):
    def test_now_uploads_verifies_and_stamps(self):
        out = self.team("backup", "--drive", "--now").stdout
        self.assertIn("uploaded and verified", out)
        st = self.stamp()
        name = st["archive"]
        self.assertRegex(name, r"^team-testhost-\d{8}T\d{6}Z\.tar\.gz$")
        self.assertEqual(self.folder(), [name])
        path = os.path.join(self.cloud, "depot2-backup", name)
        self.assertEqual(os.path.getsize(path), st["size"])
        self.assertIsNone(st["last_error"])
        self.assertEqual(st["source"], self.t.state.root)
        base = os.path.basename(self.root)
        with tarfile.open(path) as tf:
            names = tf.getnames()
        self.assertIn(base + "/.git/HEAD", names)
        self.assertIn(base + "/ledger.jsonl", names)
        self.assertFalse([n for n in names if n.startswith(base + "/.backup") or n == base + "/.lock"])
        # Only copyto writes: once, with --immutable, into the env-defined crypt remote.
        self.assertTrue(set(c[0] for c in self.cmds()) <= ALLOWED, self.cmds())
        ups = self.cmds("copyto")
        self.assertEqual(len(ups), 1)
        self.assertIn("--immutable", ups[0])
        self.assertEqual([a for a in ups[0] if a.startswith("depot2backupcrypt:")], ["depot2backupcrypt:" + name])
        for c in self.calls():
            if c["argv"][0] != "obscure":
                self.assertTrue(c["crypt_env_ok"], c)
                self.assertIn("RCLONE_CONFIG_DEPOT2BACKUPCRYPT_REMOTE", c["env"])
        # The state's git does not see the backup's own files.
        self.assertNotIn(".backup", self.t.state.git("status", "--porcelain", "--ignored=no").stdout)
        out = self.team("backup", "--status").stdout
        self.assertIn("last success: " + st["last_success"], out)
        self.assertIn("running now: no", out)
        # --verify: download, unpack, git fsck, and the temp copy is gone.
        out = self.team("backup", "--drive", "--verify").stdout
        self.assertIn("git fsck --full ok", out)
        self.assertIn("(= the upload's)", out)
        self.assertEqual(os.listdir(self.tmpdir), [])
        self.assertTrue(self.stamp()["last_verify"]["ok"])
        self.assertEqual(len(self.cmds("copyto")), 2)  # the upload, then the download
        self.assertTrue(self.cmds("copyto")[1][1].startswith("depot2backupcrypt:"))
        self.assert_no_secret()

    def test_dry_run_writes_nothing(self):
        out = self.team("backup", "--drive", "--dry-run").stdout
        self.assertIn("would write: team-testhost-", out)
        self.assertEqual(self.cmds("copyto") + self.cmds("deletefile"), [])
        self.assertEqual(self.stamp(), {})
        self.assertEqual(self.folder(), [])
        self.assert_no_secret()

    def test_network_error_is_recorded(self):
        p = self.team("backup", "--drive", "--now", ok=False, FAKE_RCLONE_FAIL="network")
        self.assertEqual(p.returncode, 1)
        self.assertIn("нет сети", p.stdout)
        self.assertEqual(self.stamp()["last_error"]["reason"], "нет сети")
        self.assertEqual(self.cmds("copyto"), [])
        self.assertEqual(self.team("ledger").stderr, "")  # the daily run is off: no ⚠
        self.assertIn("бэкап команды ещё не делался: нет сети",
                      self.team("--set", "backup.drive=true", "ledger", FAKE_RCLONE_FAIL="network").stderr)
        self.assert_no_secret()

    def test_snapshot_waits_for_the_state_lock(self):
        self.sets.append("backup.lock_wait_s=1")
        with self.t.state.lock():
            p = self.team("backup", "--drive", "--now", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("состояние занято дольше 1 с", p.stdout)
        self.assertEqual(self.cmds("copyto"), [])

    def test_refuses_foreign_names(self):
        for bad in ("backup.folder=backups", "backup.folder=other-backups", "backup.remote=other-backup:",
                    "backup.crypt_name=other-backup", "backup.folder=a/b"):
            p = self.team("--set", bad, "backup", "--drive", "--now", ok=False)
            self.assertEqual(p.returncode, 1, bad)
        self.assertEqual(self.calls(), [])
        self.assertEqual(self.pass_calls(), [])

    def test_missing_pass_entry(self):
        p = self.team("backup", "--drive", "--now", ok=False, FAKE_PASS_SECRETS=json.dumps({PW: self.keys[PW]}))
        self.assertIn(f"нет записи pass {SALT}", p.stdout)
        self.assertEqual(self.calls(), [])


class DriveFlagTest(BackupBase):
    def test_off_by_default_no_warning_no_start(self):
        self.assertEqual(self.team("ledger").stderr, "")  # never run: no ⚠ either
        self.set_stamp(last_success=iso_ago(78), last_attempt=iso_ago(1),
                       last_error={"ts": iso_ago(1), "reason": "pass заперт", "step": "pass"})
        for cmd in (("ledger",), ("stack",), ("config",)):
            self.assertEqual(self.team(*cmd).stderr, "", cmd)
        self.assertIsNone(self.spawned())
        self.assertEqual(self.calls() + self.pass_calls(), [])
        out = self.team("backup", "--status").stdout
        self.assertIn("drive: off (backup.drive false)", out)
        self.assertIn("last error: ", out)
        self.assertNotIn("⚠", out)
        self.assertFalse(json.loads(self.team("config").stdout)["backup"]["drive"])
        # The flag on: the same stamp warns and starts a run.
        self.spawns = True
        p = self.team("--set", "backup.drive=true", "ledger", FAKE_PASS_LOCKED="1")
        self.assertIn("⚠ бэкап команды не делался 78 ч: pass заперт", p.stderr)
        self.assertIsNotNone(self.spawned())
        self.wait_for(lambda: self.pass_calls())
        self.wait_for(lambda: not backup.is_running(self.t.state))

    def test_drive_options_need_the_flag_on_the_command(self):
        for args in (("--now",), ("--verify",), ("--dry-run",), ("--auto",)):
            p = self.team("backup", *args, ok=False)
            self.assertEqual(p.returncode, 1, args)
        self.assertIn("is for Drive: depot2 backup --drive --now", self.team("backup", "--now", ok=False).stderr)
        self.assertEqual(self.calls() + self.pass_calls(), [])
        self.assertEqual(self.stamp(), {})
        p = self.team("--set", "backup.drive=yes", "ledger", ok=False)
        self.assertIn("backup.drive must be true or false", p.stderr)

    def test_drive_alone_runs_now(self):
        out = self.team("backup", "--drive").stdout
        self.assertIn("uploaded and verified", out)
        self.assertEqual(len(self.cmds("copyto")), 1)
        self.assert_no_secret()


class BackupCheckTest(BackupBase):
    drive = True

    def test_never_run_warns_and_never_starts(self):
        p = self.team("ledger")
        self.assertIn("⚠ бэкап команды ещё не делался: первый запуск — depot2 backup --drive --now", p.stderr)
        self.assertIsNone(self.spawned())
        self.assertEqual(self.calls() + self.pass_calls(), [])

    def test_the_24h_check_starts_the_backup_once(self):
        self.spawns = True
        self.set_stamp(last_success=iso_ago(23), archive="team-testhost-20000101T000000Z.tar.gz")
        self.assertEqual(self.team("ledger").stderr, "")
        self.assertIsNone(self.spawned())
        old = iso_ago(25)
        self.set_stamp(last_success=old)
        p = self.team("ledger")
        self.assertIn("бэкап команды: последний 25 ч назад, запущен в фоне", p.stderr)
        self.wait_for(lambda: self.stamp().get("last_success") != old)
        self.wait_for(lambda: not backup.is_running(self.t.state))
        self.assertEqual(len(self.cmds("copyto")), 1)
        started = self.spawned()
        self.assertEqual(self.team("stack").stderr, "")
        self.team("ledger")
        self.assertEqual(self.spawned(), started)
        self.assertEqual(len(self.cmds("copyto")), 1)
        with open(backup.log_path(self.t.state), encoding="utf-8") as f:
            self.assertIn("ok in", f.read())
        # A dry run never starts one.
        self.set_stamp(last_success=iso_ago(30))
        os.remove(self.t.state.path(backup.DIR, backup.SPAWNED))
        self.team("round", "--kind", "ballot", "--input", "/nonexistent", "--dry-run", ok=False)
        self.assertIsNone(self.spawned())
        self.assert_no_secret()

    def test_a_copied_state_never_starts_one(self):
        self.set_stamp(last_success=iso_ago(30), source="/somewhere/else/team")
        p = self.team("ledger")
        self.assertIn("⚠ бэкап команды: отметка от /somewhere/else/team, а не от этой папки", p.stderr)
        self.assertIsNone(self.spawned())

    def test_a_running_backup_is_not_doubled(self):
        self.spawns = True
        old = iso_ago(25)
        self.set_stamp(last_success=old)
        for _ in range(3):
            self.team("ledger", FAKE_RCLONE_SLEEP="1")
        self.wait_for(lambda: self.stamp().get("last_success") != old)
        self.wait_for(lambda: not backup.is_running(self.t.state))
        self.assertEqual(len(self.cmds("copyto")), 1)
        # A run holds the lock: another start (the retry window over) exits at once, and --now refuses.
        self.set_stamp(last_success=iso_ago(25))
        os.remove(self.t.state.path(backup.DIR, backup.SPAWNED))
        n = len(self.calls())
        with backup.backup_lock(self.t.state):
            self.team("ledger")
            self.assertIsNotNone(self.spawned())

            def told():
                with open(backup.log_path(self.t.state), encoding="utf-8") as f:
                    return "another backup run holds" in f.read()
            self.wait_for(told)
            self.assertEqual(self.team("backup", "--drive", "--auto").returncode, 0)
            p = self.team("backup", "--drive", "--now", ok=False)
            self.assertEqual(p.returncode, 1)
            self.assertIn("a backup is running already", p.stderr)
            self.assertIn("running now: yes", self.team("backup", "--status").stdout)
        self.assertEqual(len(self.calls()), n)

    def test_a_locked_pass_warns_on_every_command(self):
        self.spawns = True
        self.set_stamp(last_success=iso_ago(31.2))
        self.team("ledger", FAKE_PASS_LOCKED="1")
        self.wait_for(lambda: self.stamp().get("last_error"))
        self.wait_for(lambda: not backup.is_running(self.t.state))
        self.assertEqual(self.stamp()["last_error"]["reason"], "pass заперт")
        # The background run never prompts: gpg is told to fail instead, stdin is no tty.
        bg = self.pass_calls()
        self.assertTrue(bg)
        for c in bg:
            self.assertIn("--pinentry-mode=error", c["gpg_opts"])
            self.assertIn("--batch", c["gpg_opts"])
            self.assertFalse(c["stdin_tty"])
        for cmd in (("ledger",), ("stack",), ("ballots",), ("config",)):
            self.assertIn("⚠ бэкап команды не делался 31 ч: pass заперт", self.team(*cmd).stderr)
        self.assertIn("⚠ бэкап команды не делался 31 ч: pass заперт", self.team("backup", "--status").stdout)
        self.assertEqual(self.cmds("copyto"), [])
        # The owner unlocks pass: the next run succeeds and the warning is gone.
        self.team("backup", "--drive", "--now")
        self.assertEqual(self.team("ledger").stderr, "")
        self.assert_no_secret()


class RetentionTest(BackupBase):
    def test_plan(self):
        names = ["team-h-201508%02dT030000Z.tar.gz" % d for d in range(1, 32)]  # noqa: UP031
        names += ["team-h-201509%02dT030000Z.tar.gz" % d for d in range(1, 31)]  # noqa: UP031
        names += ["team-h-20150930T120000Z.tar.gz", "team-other-20150801T000000Z.tar.gz", "notes.txt",
                  "team-h-latest.tar.gz", "team-h-20150815T030000Z.tar.gz.partial"]
        keep, drop = backup.plan_retention(names, "h", 14)
        days = ["team-h-201509%02dT030000Z.tar.gz" % d for d in range(17, 30)] + ["team-h-20150930T120000Z.tar.gz"]  # noqa: UP031
        self.assertEqual(keep, ["team-h-20150801T030000Z.tar.gz", "team-h-20150901T030000Z.tar.gz"] + days)
        self.assertNotIn("team-h-20150930T030000Z.tar.gz", keep)  # the same day's older one
        self.assertEqual(len(drop), 31 + 30 + 1 - len(keep))
        self.assertFalse([n for n in drop if not n.startswith("team-h-2015")])
        self.assertEqual(backup.plan_retention(names[:3], "h", 14), (names[:3], []))
        self.assertEqual(backup.plan_retention(["team-other-20150801T000000Z.tar.gz"], "h", 14), ([], []))

    def test_deletes_only_our_old_names_inside_the_folder(self):
        d = os.path.join(self.cloud, "depot2-backup")
        os.makedirs(d)
        ours = ["team-testhost-201508%02dT030000Z.tar.gz" % x for x in (1, 20)]  # noqa: UP031
        ours += ["team-testhost-201509%02dT030000Z.tar.gz" % x for x in (1, 2, 30)]  # noqa: UP031
        ours += ["team-testhost-201510%02dT030000Z.tar.gz" % x for x in range(12, 32)]  # noqa: UP031
        ours.append("team-testhost-20151030T150000Z.tar.gz")
        foreign = ["team-otherhost-20150801T030000Z.tar.gz", "notes.txt", "team-testhost-latest.tar.gz"]
        for n in ours + foreign:
            with open(os.path.join(d, n), "w") as f:
                f.write(n)
        outside = os.path.join(self.cloud, "spare-1")
        os.makedirs(outside)
        with open(os.path.join(outside, "team-testhost-20150801T030000Z.tar.gz"), "w") as f:
            f.write("not ours")
        self.team("backup", "--drive", "--now")
        new = self.stamp()["archive"]
        keep, drop = backup.plan_retention(ours + [new] + foreign, "testhost", 14)
        self.assertIn(new, keep)
        self.assertEqual(len(keep), 14 + 3)  # 14 days + the first of Aug, Sep, Oct (Nov's is the new one)
        self.assertEqual(sorted(a[1] for a in self.cmds("deletefile")), sorted("depot2backupcrypt:" + n for n in drop))
        self.assertEqual(self.folder(), sorted(keep + foreign))
        self.assertEqual(os.listdir(outside), ["team-testhost-20150801T030000Z.tar.gz"])
        self.assertTrue(set(c[0] for c in self.cmds()) <= ALLOWED, self.cmds())
        self.assertEqual(len(self.cmds("copyto")), 1)
        st = self.stamp()
        self.assertEqual(st["retention"]["kept"], len(keep))
        self.assertEqual(st["retention"]["deleted"], drop)
        self.assert_no_secret()


@unittest.skipUnless(os.environ.get("DEPOT2_TEST_REAL_RCLONE") == "1" and shutil.which("rclone"),
                     "set DEPOT2_TEST_REAL_RCLONE=1 to run the real rclone crypt against a local folder")
class RealRcloneTest(BackupBase):
    def test_env_crypt_over_a_local_folder(self):
        drive = os.path.join(self.tmp, "drive") + "/"
        conf = os.path.join(self.tmp, "empty-rclone.conf")
        open(conf, "w").close()
        self.env["RCLONE_CONFIG"] = conf
        self.sets = ["backup.rclone_bin=rclone", "backup.pass_bin=" + FAKE_PASS, "backup.remote=" + drive,
                     "backup.host=testhost"]
        self.team("backup", "--drive", "--now")
        name = self.stamp()["archive"]
        raw = os.listdir(os.path.join(drive, "depot2-backup"))
        self.assertEqual(len(raw), 1)
        self.assertNotEqual(raw[0], name)  # the name is encrypted too
        with open(os.path.join(drive, "depot2-backup", raw[0]), "rb") as f:
            self.assertEqual(f.read(8), b"RCLONE\x00\x00")  # rclone crypt's file header
        out = self.team("backup", "--drive", "--verify").stdout
        self.assertIn("git fsck --full ok", out)
        self.assertEqual(os.path.getsize(conf), 0)  # rclone.conf untouched
        self.assert_no_secret()


if __name__ == "__main__":
    unittest.main()
