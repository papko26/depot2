import contextlib
import fcntl
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from helpers import HERE, TempState

from depot2 import cli as team
from depot2 import merge
from depot2.stack import Stack
from depot2.tracks import Tracks
from depot2.trains import Trains

FAKE_GH = str(Path(HERE, "fake_gh.py"))
FAKE_GIT = str(Path(HERE, "fake_git.sh"))
FAKE_SCAN = str(Path(HERE, "fake_scan.py"))
SMOKE_OK = '{"check": "home page", "ok": true}'
EXAMPLE_PLUGIN = str(Path(os.path.dirname(HERE), "examples", "plugins", "example_checks.py"))


def sh(cwd, *argv, check=True):
    p = subprocess.run(list(argv), cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise AssertionError(f"{argv} failed: {p.stdout} {p.stderr}")
    return p.stdout.strip()


START = 1447329600.0  # 2015-11-12T12:00:00Z


class FakeClock:
    """The merge queue's clock in these tests (merge.CLOCK, merge.SLEEP): it stands still between the queue's
    sleeps and moves a second (or the sleep's length) on each, so one poll's readings agree, and the fake gh
    reads the same time ($FAKE_GH_NOW). No test hangs on the real time or on how fast the machine is.
    """

    def __init__(self, path, start=START) -> None:
        self.path, self.t = path, float(start)
        self.write()

    def time(self):
        return self.t

    def sleep(self, s):
        self.t += max(1.0, float(s))
        self.write()

    def write(self):
        with Path(self.path).open("w") as f:
            f.write(repr(self.t))


class MergeBase(unittest.TestCase):
    state_git = False  # the state's own git commits are slow through the logging git: one test class has them

    def setUp(self):
        self.t = TempState(git=self.state_git)
        self.t.state.commit("test: seed")
        self.tmp = tempfile.mkdtemp(prefix="team-merge-test-")
        self.git_bin = shutil.which("git")
        self.env0 = dict(os.environ)
        b = str(Path(self.tmp, "bin"))
        Path(b).mkdir(parents=True)
        with Path(b, "gh").open("w") as f:
            f.write(f'#!/bin/sh\nexec {sys.executable} -S {FAKE_GH} "$@"\n')
        Path(b, "gh").chmod(0o755)
        os.symlink(FAKE_GIT, Path(b, "git"))
        self.bare = str(Path(self.tmp, "origin.git"))
        self.seed = str(Path(self.tmp, "seed"))
        self.clone = str(Path(self.tmp, "clone"))
        self.wt = str(Path(self.tmp, "wt"))
        self.ghstate = str(Path(self.tmp, "gh.json"))
        self.ghlog = str(Path(self.tmp, "gh.jsonl"))
        self.gitlog = str(Path(self.tmp, "git.jsonl"))
        self.marker = str(Path(self.tmp, "after-resolve.txt"))
        os.environ.update(
            GIT_AUTHOR_NAME="t",
            GIT_AUTHOR_EMAIL="t@localhost",
            GIT_COMMITTER_NAME="t",
            GIT_COMMITTER_EMAIL="t@localhost",
        )
        self.g(self.tmp, "init", "-q", "--bare", self.bare)
        self.g(self.tmp, "--git-dir", self.bare, "symbolic-ref", "HEAD", "refs/heads/main")
        self.g(self.tmp, "init", "-q", self.seed)
        self.g(self.seed, "checkout", "-q", "-b", "main")
        self.write(self.seed, {"a.txt": "a\n", "b.txt": "b\n", "c.txt": "c\n"})
        self.g(self.seed, "add", "-A")
        self.g(self.seed, "commit", "-q", "-m", "base")
        self.g(self.seed, "remote", "add", "origin", self.bare)
        self.g(self.seed, "push", "-q", "origin", "main")
        self.gh = {"bare": self.bare, "git": self.git_bin, "prs": {}, "ci": {}, "merge_refuse": []}
        self.save_gh()
        os.environ.update(
            PATH=b + os.pathsep + self.env0.get("PATH", ""),
            FAKE_GH_STATE=self.ghstate,
            FAKE_GH_LOG=self.ghlog,
            FAKE_GIT_REAL=self.git_bin,
            FAKE_GIT_LOG=self.gitlog,
        )
        os.environ.pop("FAKE_SCAN_RESULT", None)
        self.clock = FakeClock(str(Path(self.tmp, "now")))
        os.environ["FAKE_GH_NOW"] = self.clock.path
        self.real_clock = (merge.CLOCK, merge.SLEEP)
        merge.CLOCK, merge.SLEEP = self.clock.time, self.clock.sleep
        self.sets = [
            "merge.clone=" + self.clone,
            "merge.worktrees=" + self.wt,
            "merge.poll_s=0",
            "merge.scan=" + FAKE_SCAN,
            "merge.githubstatus=null",
            "merge.github_rerun_after_min=0",
            "merge.github_wait_max_min=0",
            'merge.site_check=["echo", "200"]',
            "merge.smoke=" + json.dumps([sys.executable, "-c", f"print({SMOKE_OK!r})"]),
            "plugins=" + json.dumps([EXAMPLE_PLUGIN]),
            "merge.after_resolve="
            + json.dumps([[sys.executable, "-c", f"open({self.marker!r}, 'a').write('checked\\n')"]]),
        ]

    def tearDown(self):
        merge.CLOCK, merge.SLEEP = self.real_clock
        os.environ.clear()
        os.environ.update(self.env0)
        self.t.close()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # --- the repos ---
    def g(self, cwd, *args, check=True):
        return sh(cwd, self.git_bin, *args, check=check)

    def write(self, d, files):
        for name, text in files.items():
            with Path(d, name).open("w") as f:
                f.write(text)

    def main_commit(self, files, msg="main moves"):
        self.g(self.seed, "fetch", "-q", "origin")
        self.g(self.seed, "checkout", "-q", "main")
        self.g(self.seed, "reset", "-q", "--hard", "origin/main")
        self.write(self.seed, files)
        self.g(self.seed, "add", "-A")
        self.g(self.seed, "commit", "-q", "-m", msg)
        self.g(self.seed, "push", "-q", "origin", "main")

    def pr(self, n, branch, files, title=None, base="main", stack=True):
        """A branch with one commit from origin/main, pushed; its PR; its worktree; a stack object in the post-stack."""
        self.g(self.seed, "fetch", "-q", "origin")
        self.g(self.seed, "checkout", "-q", "-B", branch, "origin/" + base)
        self.write(self.seed, files)
        self.g(self.seed, "add", "-A")
        self.g(self.seed, "commit", "-q", "-m", "work on " + branch)
        self.g(self.seed, "push", "-q", "origin", branch)
        self.g(self.seed, "checkout", "-q", "main")
        self.gh = self.load_gh()
        self.gh["prs"][str(n)] = {
            "number": n,
            "title": title or "PR %d" % n,  # noqa: UP031
            "state": "OPEN",
            "headRefName": branch,
            "baseRefName": "main",
            "isDraft": False,
        }
        self.save_gh()
        if not os.path.isdir(self.clone):
            self.g(self.tmp, "clone", "-q", self.bare, self.clone)
        self.g(self.clone, "fetch", "-q", "origin")
        self.g(self.clone, "worktree", "add", "-q", "-b", branch, os.path.join(self.wt, branch), "origin/" + branch)
        if stack:
            s = Stack(self.t.state, self.t.cfg)
            o = s.add(title or "PR %d" % n, branch=branch, pr=n)  # noqa: UP031
            s.post(o["id"])
            s.set(o["id"], status="PR #%d: CI green" % n)  # noqa: UP031
            self.t.state.commit("test: {}".format(o["id"]))
            return o["id"]
        return None

    def load_gh(self):
        with Path(self.ghstate).open() as f:
            return json.load(f)

    def save_gh(self):
        with Path(self.ghstate).open("w") as f:
            json.dump(self.gh, f)

    def ci(self, **scripts):
        self.gh = self.load_gh()
        for branch, states in scripts.items():
            self.gh["ci"][branch.replace("_", "-")] = list(states)
        self.save_gh()

    def gh_calls(self, *prefix):
        if not os.path.exists(self.ghlog):
            return []
        with Path(self.ghlog).open() as f:
            calls = [json.loads(ln) for ln in f if ln.strip()]
        return [c for c in calls if c[: len(prefix)] == list(prefix)]

    def pushes(self):
        if not os.path.exists(self.gitlog):
            return []
        with Path(self.gitlog).open() as f:
            calls = [ln.rstrip("\n").split("\t", 1) for ln in f if ln.strip()]
        calls = [{"cwd": cwd, "argv": args.split("\037")[:-1]} for cwd, args in calls]
        return [c for c in calls if "push" in c["argv"]]

    def bare_head(self, ref):
        return self.g(self.tmp, "--git-dir", self.bare, "rev-parse", ref)

    # --- the tool ---
    def team(self, *argv):
        args = ["--state", self.t.dir]
        for s in self.sets:
            args += ["--set", s]
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = team.main(args + list(argv))
        return rc, buf.getvalue(), err.getvalue()

    def q(self):
        return merge.MergeQueue(self.t.state)

    def bytes(self, rel):
        with Path(self.t.state.path(rel)).open("rb") as f:
            return f.read()

    def entry(self, ref):
        return self.q().get(ref)


class AddTest(MergeBase):
    def test_add_without_the_owners_word_is_refused(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        for argv in (["merge", "add", "301"], ["merge", "add", "301", "--word", "   "]):
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 1, out)
            self.assertIn("мердж только по слову владельца", err)
        self.assertFalse(os.path.exists(self.t.state.path(merge.FILE)))
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["status"], "PR #301: CI green")
        rc, out, err = self.team("merge", "add", "301", "--word", "301 — мердж")
        self.assertEqual(rc, 0, err)
        e = self.entry(301)
        self.assertEqual(
            (e["id"], e["word"], e["branch"], e["st"], e["status"]),
            ("mq-0001", "301 — мердж", "feat-a", "st-0001", "waiting"),
        )
        self.assertEqual(e["worktree"], str(Path(self.wt, "feat-a")))
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual(o["status"], "в мердж-очереди (mq-0001)")
        self.assertIn("added on the owner's word: «301 — мердж»", e["log"][0])
        rc, _, err = self.team("merge", "add", "301", "--word", "ещё раз")
        self.assertEqual(rc, 1)
        self.assertIn("in the merge queue already", err)
        md = self.team("stack", "--md")[1]
        self.assertIn("| **На путях — мердж-очередь, едут по одному (1)** | | | | | | | | | |", md)
        self.assertRegex(md, r"\| М1 \| мердж \| st-0001 PR 301 \| P5 \| feat-a / PR #301 \| ждёт \|")
        self.assertLess(md.index("**Приёмка — пост-стек"), md.index("**На путях — мердж-очередь"))
        self.assertLess(md.index("**На путях — мердж-очередь"), md.index("**Очередь — ждёт слота"))
        self.assertIn("Мердж-очередь: PR по слову владельца", md)
        self.assertIn(
            "| Мердж-очередь | ждёт 1 | — | #301 ждёт | `depot2 merge --md` |", self.team("queues", "--md")[1]
        )
        self.assertEqual(json.loads(self.team("stack", "--json")[1])["merge"]["entries"][0]["pr"], 301)

    def test_add_refuses_a_locked_object_a_closed_pr_and_drop_restores_the_status(self):
        oid = self.pr(301, "feat-a", {"a.txt": "A\n"})
        s = Stack(self.t.state, self.t.cfg)
        s.lock(oid, "builder", "a named test", "why", "r-1", None, 10)
        rc, _, err = self.team("merge", "add", "301", "--word", "мердж")
        self.assertEqual(rc, 1)
        self.assertIn("is locked", err)
        s = Stack(self.t.state, self.t.cfg)
        s.unlock(oid, "met", "done")
        self.gh = self.load_gh()
        self.gh["prs"]["301"]["state"] = "CLOSED"
        self.save_gh()
        self.assertIn("is CLOSED", self.team("merge", "add", "301", "--word", "мердж")[2])
        self.gh["prs"]["301"]["state"] = "OPEN"
        self.save_gh()
        self.assertEqual(self.team("merge", "add", "301", "--word", "мердж")[0], 0)
        rc, _out, err = self.team("merge", "drop", "301", "--reason", "владелец передумал")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(301)["status"], "dropped")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get(oid)["status"], "PR #301: CI green")
        self.assertEqual(json.loads(self.team("merge", "--json")[1])["entries"], [])
        self.assertEqual(
            [
                (e["pr"], e["status"], e["status_ru"])
                for e in json.loads(self.team("merge", "--json", "--all")[1])["entries"]
            ],
            [(301, "dropped", merge.STATUS_RU["dropped"])],
        )  # the page's words for it

    def test_a_second_runner_is_refused(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        r = merge.Rider(self.t.state, self.t.cfg)
        with r.run_lock():
            rc, _, err = self.team("merge", "run")
            self.assertEqual(rc, 1)
            self.assertIn("another merge run holds the queue (pid %d)" % os.getpid(), err)  # noqa: UP031
            self.assertIn("работает (pid %d)" % os.getpid(), self.team("merge")[1])  # noqa: UP031
        self.assertEqual(self.entry(301)["status"], "waiting")

    def test_a_probe_of_the_lock_at_the_runners_start_refuses_nothing(self):
        import threading

        r = merge.Rider(self.t.state, self.t.cfg)
        with r.run_lock():
            pass  # the lock file is there
        fd = os.open(r.lock_path(), os.O_RDWR)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # the probe, in its moment
        t = threading.Timer(0.25, lambda: (fcntl.flock(fd, fcntl.LOCK_UN), os.close(fd)))
        t.start()
        try:
            with r.run_lock():
                self.assertEqual(merge.lock_pid(self.t.state), str(os.getpid()))
        finally:
            t.join()


STAND_IN = r"""
import fcntl, os, sys, time
if len(sys.argv) > 2:
    print("boom", flush=True)
    sys.exit(int(sys.argv[2]))
fd = os.open(sys.argv[1], os.O_RDWR | os.O_CREAT, 0o644)
fcntl.flock(fd, fcntl.LOCK_EX)
print("stand-in rides", flush=True)
os.ftruncate(fd, 0)
os.write(fd, ("%d\n" % os.getpid()).encode())
time.sleep(60)
"""


class EnsureTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.spawned, self.procs, self.exit_code = [], [], None
        self.real_spawn = merge.SPAWN
        merge.SPAWN = self.spawn

    def tearDown(self):
        merge.SPAWN = self.real_spawn
        for p in self.procs:
            p.kill()
            p.wait()
        self.t.close()

    def spawn(self, argv, log, cwd):
        self.spawned.append((argv, log, cwd))
        extra = [] if self.exit_code is None else ["x", str(self.exit_code)]
        with Path(log).open("a") as out:
            p = subprocess.Popen(
                [sys.executable, "-c", STAND_IN, self.t.state.path(merge.RUN_DIR, merge.RUN_LOCK), *extra],
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=out,
            )
        self.procs.append(p)
        return p

    def team(self, *argv):
        buf, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(err):
            rc = team.main(["--state", self.t.dir, "--set", "merge.poll_s=0", *list(argv)])
        return rc, buf.getvalue(), err.getvalue()

    def put(self, n, status="waiting", **kw):
        q = merge.MergeQueue(self.t.state)
        e = dict(
            {
                "id": q.new_id(),
                "pr": n,
                "title": "PR %d" % n,  # noqa: UP031
                "branch": "b-%d" % n,  # noqa: UP031
                "worktree": "/nowhere",
                "st": None,
                "car": None,
                "status": status,
                "word": "%d — мердж" % n,  # noqa: UP031
                "added": merge.stamp(),
                "step": None,
                "log": [],
            },
            **kw,
        )
        q.data["entries"].append(e)
        q.save("add", e["id"])
        return e

    def test_a_runner_that_rides_is_left_alone(self):
        self.put(301)
        with merge.Rider(self.t.state, self.t.cfg).run_lock():
            rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual(rc, 0, err)
        self.assertIn("уже едет (pid %d): второго не запускаю" % os.getpid(), out)  # noqa: UP031
        self.assertEqual(self.spawned, [])

    def test_an_empty_queue_has_nothing_to_carry(self):
        self.put(300, status="merged", merged={"sha": "a" * 40, "ts": merge.stamp()})
        rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual((rc, out.strip()), (0, "мердж-очередь пуста: нечего везти"), err)
        self.assertEqual(self.spawned, [])
        self.assertIn("Исполнитель: не запущен, очередь пуста.", self.team("merge")[1])
        self.assertIsNone(json.loads(self.team("merge", "--json")[1])["runner_idle"])

    def test_a_dead_runners_pid_starts_one_detached_and_a_second_ensure_starts_none(self):
        self.put(301)
        self.put(302)
        Path(self.t.state.path(merge.RUN_DIR)).mkdir(parents=True, exist_ok=True)
        with Path(self.t.state.path(merge.RUN_DIR, merge.RUN_LOCK)).open("w") as f:
            f.write("99999999\n")  # the runner that wrote it is gone: the lock is free
        rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.spawned), 1)
        argv, log, _ = self.spawned[0]
        pid = self.procs[0].pid
        self.assertEqual(log, self.t.state.path(merge.RUN_DIR, merge.RUN_LOG))
        self.assertEqual(
            argv[1:],
            [
                str(Path(os.path.dirname(HERE), "depot2", "__main__.py")),
                "--state",
                self.t.dir,
                "--set",
                "merge.poll_s=0",
                "merge",
                "run",
            ],
        )
        self.assertIn("исполнитель не едет (в замке был pid 99999999, замок свободен), в очереди 2: запускаю", out)
        self.assertIn("исполнитель едет (pid %d); лог: %s" % (pid, log), out)  # noqa: UP031
        with Path(log).open() as f:
            text = f.read()
        self.assertIn("merge run --ensure: start, 2 in the queue", text)
        self.assertIn("stand-in rides", text)
        rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual(rc, 0, err)
        self.assertIn("уже едет (pid %d)" % pid, out)  # noqa: UP031
        self.assertEqual(len(self.spawned), 1)
        self.assertIn("Исполнитель: работает (pid %d)." % pid, self.team("merge")[1])  # noqa: UP031
        j = json.loads(self.team("merge", "--json")[1])
        self.assertEqual((j["runner_pid"], j["runner_idle"]), (str(pid), None))

    def test_a_runner_that_exits_at_once_is_said_with_its_log(self):
        self.put(301)
        self.exit_code = 1
        rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual(rc, 1, err)
        self.assertIn("исполнитель (pid %d) вышел сразу, код 1; лог: " % self.procs[0].pid, out)  # noqa: UP031
        self.assertIn("\n  boom\n", out)

    def test_a_stopped_queue_waits_for_a_person(self):
        self.put(301, status="stopped", stop={"step": "ci", "reason": "CI red", "ts": merge.stamp()})
        self.put(302)
        rc, out, err = self.team("merge", "run", "--ensure")
        self.assertEqual(rc, 1, err)
        self.assertIn("мердж-очередь остановлена на mq-0001 #301: CI red — исполнителя не запускаю", out)
        self.assertIn("depot2 merge resume 301", out)
        self.assertEqual(self.spawned, [])
        md = self.team("merge")[1]
        self.assertIn(
            "**Очередь 2, исполнитель не едет**: остановлена на #301 — `depot2 merge resume 301` или "
            "`depot2 merge drop 301 --reason …`.",
            md,
        )
        self.assertIn(
            "исполнитель: не едет, очередь 2 остановлена на #301 — `depot2 merge resume 301`",
            merge.legend(self.t.state),
        )
        self.assertEqual(json.loads(self.team("merge", "--json")[1])["runner_idle"], {"open": 2, "stopped": 301})

    def test_the_queue_with_no_runner_says_so(self):
        self.put(301)
        md = self.team("merge")[1]
        self.assertIn("**Очередь 1, исполнитель не едет** — `depot2 merge run --ensure`.", md)
        self.assertIn("исполнитель: не едет, очередь 1 — `depot2 merge run --ensure`", merge.legend(self.t.state))
        self.assertIn("исполнитель: не едет, очередь 1", merge.report(self.t.state, self.t.cfg))
        j = json.loads(self.team("merge", "--json")[1])
        self.assertEqual((j["runner_pid"], j["runner_idle"]), (None, {"open": 1, "stopped": None}))
        self.assertEqual(self.team("merge", "run", "--ensure", "--once")[0], 1)
        self.assertEqual(self.spawned, [])


class OrderTest(MergeBase):
    state_git = True

    def test_order_each_on_top_of_the_one_before_stack_done_and_report(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"}, title="Первый")
        self.pr(302, "feat-b", {"b.txt": "B\n"}, title="Второй")
        self.pr(303, "feat-c", {"c.txt": "C\n"}, title="Третий")
        for n, word in ((302, "302 — мердж"), (301, "и 301"), (303, "303 тоже")):
            self.assertEqual(self.team("merge", "add", str(n), "--word", word)[0], 0)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        merged = [c[2] for c in self.gh_calls("pr", "merge")]
        self.assertEqual(merged, ["302", "301", "303"])  # the order they were added
        for c in self.gh_calls("pr", "merge"):
            self.assertIn("--match-head-commit", c)
            self.assertIn("--merge", c)
        gh = self.load_gh()
        m302 = gh["prs"]["302"]["mergeCommit"]["oid"]
        m301 = gh["prs"]["301"]["mergeCommit"]["oid"]
        # 302 rode first: main was in it already, so no push; 301 got main + 302 (pushed), 303 main + 302 + 301
        self.assertEqual(
            [p["cwd"] for p in self.pushes()], [str(Path(self.wt, "feat-a")), str(Path(self.wt, "feat-c"))]
        )
        self.g(self.tmp, "--git-dir", self.bare, "merge-base", "--is-ancestor", m302, "refs/heads/feat-a")
        self.g(self.tmp, "--git-dir", self.bare, "merge-base", "--is-ancestor", m301, "refs/heads/feat-c")
        self.assertIn(
            "Merge origin/main (",
            self.g(self.tmp, "--git-dir", self.bare, "log", "-1", "--format=%s", "refs/heads/feat-a"),
        )
        self.assertEqual(self.g(self.tmp, "--git-dir", self.bare, "show", "main:a.txt"), "A")
        self.assertEqual([e["status"] for e in self.q().entries()], ["merged"] * 3)
        # stack done by itself, --car merged, with the owner's word
        s = Stack(self.t.state, self.t.cfg)
        self.assertEqual(s.objects(), [])
        rows = [h for h in s.history() if h["event"] == "done"]
        self.assertEqual(
            [(h["object"], h["outcome"]) for h in rows],
            [("st-0002", "merged"), ("st-0001", "merged"), ("st-0003", "merged")],
        )
        self.assertIn(f"merged #302 as {m302[:7]} (mq-0001; слово владельца: «302 — мердж»)", rows[0]["reason"])
        self.assertTrue(Tracks(self.t.state, self.t.cfg).pr_merged(302))
        e = self.entry(301)
        self.assertEqual(e["merged"]["sha"], m301)
        self.assertEqual(
            e["deploy"], {"run": e["deploy"]["run"], "conclusion": "success", "deploy_job": "success", "site": "200"}
        )
        self.assertEqual(e["smoke"], {"ok": True, "summary": "checks 1/1 ok"})
        self.assertEqual(e["usage"]["lines"], [e["usage"]["plugins"][0]["line"]])  # the example plugin's after_merge
        self.assertTrue(e["usage"]["lines"][0].startswith("finished at "))
        self.assertIn("== done: #303", out)
        self.assertIn("origin/main is in feat-b already: its head stays", out)
        self.assertIn("the main clone → {}".format(self.bare_head("refs/heads/main")[:7]), out)
        # the report, for the owner
        rc, rep, _ = self.team("merge", "report", "--last", "2")
        self.assertEqual(rc, 0)
        self.assertIn(f"#301 Первый — смерджен {m301[:7]}", rep)
        self.assertIn("#303 Третий — смерджен", rep)
        self.assertNotIn("#302", rep)
        self.assertIn("выкатка: CI/CD success, деплой success, сайт 200", rep)
        self.assertIn("smoke: ок — checks 1/1 ok", rep)
        self.assertIn("после мерджа: finished at ", rep)
        self.assertIn("слово владельца: «303 тоже»", rep)
        self.assertIn("Очередь: пусто", rep)
        self.assertIn("Последний мердж: #303", self.team("merge")[1])
        # the state's git has every change: nothing left uncommitted, the merges in its log
        self.assertEqual(
            sh(self.t.dir, self.git_bin, "status", "--porcelain", "--", merge.FILE, merge.HISTORY, "stack.json"), ""
        )
        self.assertIn("team: merge mq-0003 #303 merged", sh(self.t.dir, self.git_bin, "log", "--format=%s"))


class RideTest(MergeBase):
    def test_main_adds_nothing_keeps_the_green_head_no_push(self):
        self.pr(301, "feat-a", {"a.txt": "A2\n", "d.txt": "d\n"})
        head = self.bare_head("refs/heads/feat-a")
        self.main_commit({"a.txt": "A2\n"})  # main gets the same change: merged in, nothing changes
        self.team("merge", "add", "301", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("main adds nothing to feat-a: its head stays", out)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.bare_head("refs/heads/feat-a"), head)
        self.assertEqual(
            self.g(self.clone, "rev-parse", "refs/heads/feat-a"), head
        )  # (its worktree went after the merge)
        ci = [c for c in self.gh_calls("run", "list") if "feat-a" in c]
        self.assertTrue(ci and all(c[c.index("--commit") + 1] == head for c in ci))
        self.assertEqual(self.entry(301)["status"], "merged")

    def test_failure_stops_the_queue_until_resume_or_drop(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.ci(feat_a=["in_progress", "failure", "success"])
        self.team("merge", "add", "301", "--word", "301 — мердж")
        self.team("merge", "add", "302", "--word", "302 — мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 1, out + err)
        self.assertIn("STOP: CI of feat-a @ ", out)
        e = self.entry(301)
        self.assertEqual((e["status"], e["stop"]["step"]), ("stopped", "ci"))
        self.assertEqual(self.entry(302)["status"], "waiting")
        self.assertEqual(self.gh_calls("pr", "merge"), [])  # 302 never rode
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("the merge queue is stopped at mq-0001 #301", out)
        md = self.team("stack", "--md")[1]
        self.assertRegex(
            md, r"\| М1 \| мердж \| st-0001 PR 301 \| P5 \| feat-a / PR #301 \| остановлен \(шаг «CI на голове», "
        )
        self.assertIn("| М2 | мердж | st-0002 PR 302 | P5 | feat-b / PR #302 | ждёт |", md)
        self.assertIn("- #301 остановлен (шаг «CI на голове»", self.team("merge", "report")[1])
        # resume: the same PR from the start; its CI is green the second time
        rc, out, err = self.team("merge", "resume", "--reason", "перезапустил тест")
        self.assertEqual(rc, 0, err)
        self.assertIn("mq-0001 #301 waits again, from the start", out)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([c[2] for c in self.gh_calls("pr", "merge")], ["301", "302"])

    def test_drop_of_the_stopped_one_lets_the_queue_go_on(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.ci(feat_a=["failure"])
        self.team("merge", "add", "301", "--word", "мердж 301")
        self.team("merge", "add", "302", "--word", "мердж 302")
        self.assertEqual(self.team("merge", "run")[0], 1)
        self.assertEqual(self.team("merge", "drop", "301", "--reason", "чинить в ветке")[0], 0)
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["status"], "PR #301: CI green")
        self.assertEqual(self.team("merge", "run")[0], 0)
        self.assertEqual([c[2] for c in self.gh_calls("pr", "merge")], ["302"])
        self.assertEqual(self.entry(301)["status"], "dropped")

    def test_a_pr_dropped_after_its_merge_is_in_no_view_of_the_merges(self):
        """Merged, its deploy failed, resumed, then dropped: `merge report`, its --json and the last merge show it alike
        while it is watched and leave it out alike once it is dropped.
        """
        self.pr(301, "feat-a", {"a.txt": "A\n"}, title="Первый")
        self.pr(302, "feat-b", {"b.txt": "B\n"}, title="Второй")
        self.ci(main=["failure", "success"])
        self.team("merge", "add", "301", "--word", "мердж 301")
        self.team("merge", "add", "302", "--word", "мердж 302")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 1, out + err)
        e = self.entry(301)
        self.assertEqual((e["status"], e["stop"]["step"], bool(e["merged"]["sha"])), ("stopped", "deploy", True))
        self.assertIn("#301 Первый — смерджен", self.team("merge", "report")[1])  # merged and stopped: still a merge
        self.assertEqual(self.team("merge", "resume", "--reason", "try the deploy again")[0], 0)
        self.assertEqual((self.entry(301)["status"], self.entry(301)["step"]), ("waiting", "deploy"))
        md = self.team("merge", "report")[1]  # waits again for its deploy: still a merge, in every view
        self.assertIn("#301 Первый — смерджен", md)
        self.assertIn("сейчас: ждёт (с шага", md)
        self.assertEqual([x["pr"] for x in json.loads(self.team("merge", "report", "--json")[1])], [301])
        rc, out, err = self.team("merge", "drop", "301", "--reason", "the deploy rides with the next merge")
        self.assertEqual(rc, 0, err)
        self.assertIn("note: #301 is merged already", out)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([self.entry(n)["status"] for n in (301, 302)], ["dropped", "merged"])
        self.assertTrue(self.entry(301)["merged"]["sha"])  # merged, then dropped
        md = self.team("merge", "report", "--last", "5")[1]
        self.assertIn("#302 Второй — смерджен", md)
        self.assertNotIn("#301", md)
        js = json.loads(self.team("merge", "report", "--last", "5", "--json")[1])
        self.assertEqual([x["pr"] for x in js], [302])
        self.assertEqual([x["pr"] for x in merge.report_json(self.t.state, last=5)], [302])  # what the web tab reads
        self.assertEqual(json.loads(self.team("merge", "--json")[1])["last_merged"]["pr"], 302)

    def test_github_not_starting_a_job_is_a_wait_with_a_rerun(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.ci(feat_a=["never_started!", "success"], main=["stuck", "success"])
        self.sets.append("merge.github_wait_max_min=60")  # main's stuck job gets a runner on the next poll
        self.team("merge", "add", "301", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(
            "waiting for GitHub: GitHub не запустил задание «Lint and typecheck» (queued), прогон failure "
            "(not a failure)",
            out,
        )
        self.assertEqual(len(self.gh_calls("run", "rerun")), 1)  # the timer (0 min here)
        self.assertIn(
            "waiting for GitHub: задание «Test and build» в очереди 60+ мин, раннера нет", out
        )  # main's: no runner
        self.assertIn("GitHub is back: задание «Test and build»", out)
        self.assertNotIn("STOP", out)
        events = [h["event"] for h in self.q().history()]
        self.assertEqual(events.count("github"), 2)
        self.assertIn("rerun", events)
        self.assertNotIn("stop", events)
        self.assertEqual(self.entry(301)["status"], "merged")

    def test_waiting_for_github_past_the_ceiling_leaves_it_waiting_then_merge_rerun(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.ci(main=["never_started!", "success"])
        self.sets.append("merge.github_rerun_after_min=null")  # rerun by hand only
        self.team("merge", "add", "301", "--word", "мердж")
        self.team("merge", "add", "302", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 3, out + err)
        e = self.entry(301)
        self.assertEqual((e["status"], e["step"]), ("github", "deploy"))
        self.assertTrue(e["merged"]["sha"])
        self.assertIn("«Deploy» (queued)", e["github"]["reason"])
        self.assertIsNone(self.q().stopped())  # not a stop
        self.assertIn("Последний мердж: #301 {} (".format(e["merged"]["sha"][:7]), self.team("merge")[1])
        self.assertIn("; сейчас: ждёт GitHub; копия удалена (", self.team("merge")[1])
        self.assertIn("| Мердж-очередь | ждёт GitHub 1, ждёт 1 |", self.team("queues", "--md")[1])
        self.assertEqual([c[2] for c in self.gh_calls("pr", "merge")], ["301"])
        self.assertRegex(
            self.team("stack", "--md")[1],
            r"\| М1 \| мердж \| PR #301 PR 301 \| — \| feat-a / PR #301 \| ждёт GitHub "
            r"\(шаг «CI/CD main, выкатка, сайт»\): GitHub не запустил",
        )
        rc, out, err = self.team("merge", "rerun", "301")
        self.assertEqual(rc, 0, err)
        self.assertIn("gh run rerun 9", out)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([self.entry(n)["status"] for n in (301, 302)], ["merged", "merged"])


PRIMARY = '["self-hosted","primary"]'
THE_SPARE = '["self-hosted","spare"]'


def runner(name, status, *labels):
    return {
        "name": name,
        "status": status,
        "busy": False,
        "labels": [{"name": x, "type": "custom"} for x in ("self-hosted", "Linux", "X64", *labels)],
    }


class CdRunnerTest(MergeBase):
    def setUp(self):
        super().setUp()
        self.sets += ["repo=test-owner/no-such-repo", "merge.github_wait_max_min=60"]
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")

    def runners(self, primary, spare_1="online", **extra):
        self.gh = self.load_gh()
        self.gh["runners"] = [runner("primary-ci-%d" % i, primary, "project-ci", "primary") for i in (1, 2, 3)] + [  # noqa: UP031
            runner("spare-1", spare_1, "spare", "project-ci")
        ]
        self.gh["variables"] = {"CD_RUNNER": PRIMARY}
        self.gh.update(extra)
        self.save_gh()

    def changes(self):
        """The calls that change GitHub's side, in order: the variable, the cancel, the rerun."""
        return [c for c in self.gh_calls() if c[:2] in (["variable", "set"], ["run", "cancel"], ["run", "rerun"])]

    def test_primary_online_nothing_changes(self):
        self.runners("online")
        self.ci(main=["deploy_stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("waiting for GitHub: задание «Deploy» в очереди 20+ мин, раннера нет", out)
        self.assertEqual(
            len(
                self.gh_calls(
                    "api", "--paginate", "--slurp", "repos/test-owner/no-such-repo/actions/runners?per_page=100"
                )
            ),
            1,
        )
        self.assertEqual(self.changes(), [])
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY})
        e = self.entry(301)
        self.assertEqual((e["status"], e.get("cd_runner")), ("merged", None))
        self.assertEqual(e["deploy"]["deploy_job"], "success")
        self.assertNotIn("CD_RUNNER", self.team("merge", "report")[1])

    def test_no_runner_online_at_all_nothing_changes(self):
        self.runners("offline", spare_1="offline")
        self.ci(main=["deploy_stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("no primary runner online, and no spare one either: the deploy waits", out)
        self.assertEqual(self.changes(), [])
        self.assertIsNone(self.entry(301).get("cd_runner"))

    def test_primary_offline_the_deploy_goes_to_spare_and_the_variable_comes_back(self):
        self.runners("offline", cancel_lag=1)  # the first run list after the cancel still shows it queued
        self.ci(main=["deploy_stuck!", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        e = self.entry(301)
        rid = str(e["deploy"]["run"])
        ch = self.changes()
        self.assertEqual(
            [c[:3] for c in ch],
            [
                ["variable", "set", "CD_RUNNER"],
                ["run", "cancel", rid],
                ["run", "rerun", rid],
                ["variable", "set", "CD_RUNNER"],
            ],
        )
        self.assertEqual([c[c.index("--body") + 1] for c in ch if c[0] == "variable"], [THE_SPARE, PRIMARY])
        self.assertIn("--failed", ch[2])
        self.assertTrue(all(c[c.index("-R") + 1] == "test-owner/no-such-repo" for c in ch))
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY})
        self.assertIn(
            f"no primary runner online, «Deploy» queued 20+ min: CD_RUNNER → {THE_SPARE} (spare-1), "
            f"gh run cancel {rid}, then rerun --failed",
            out,
        )
        self.assertIn(f"CD_RUNNER → {PRIMARY} again (run {rid}, attempt 2: success)", out)
        self.assertNotIn("STOP", out)
        fb = e["cd_runner"]
        self.assertEqual(
            (fb["run"], fb["attempt"], fb["to"], fb["back_to"], fb["spare"]),
            (int(rid), 1, ["self-hosted", "spare"], ["self-hosted", "primary"], ["spare-1"]),
        )
        self.assertTrue(fb["back"] and fb["rerun"])
        self.assertEqual((e["status"], e["deploy"]["deploy_job"], e["deploy"]["site"]), ("merged", "success", "200"))
        self.assertIn("cd_runner_back", [h["event"] for h in self.q().history()])
        line = (
            "  CD_RUNNER: primary не в сети, «Deploy» ждал 20+ мин → выкатка на spare "
            f"(прогон {rid}: отмена, перезапуск упавших); CD_RUNNER снова {PRIMARY} ("
        )
        rep = self.team("merge", "report")[1]
        self.assertEqual(next(ln for ln in rep.splitlines() if "CD_RUNNER" in ln)[: len(line)], line)
        self.assertLess(rep.index("CD_RUNNER:"), rep.index("после мерджа"))
        self.assertIn(line.strip(), out)  # the ride's own report too

    def test_a_flip_back_github_refuses_is_said_with_the_command(self):
        self.runners("offline", variable_refuse_after=1)
        self.ci(main=["deploy_stuck!", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": THE_SPARE})
        self.assertIn(
            "gh variable set CD_RUNNER refused (HTTP 403: Resource not accessible by integration): set it back "
            f"by hand: gh variable set CD_RUNNER --body '{PRIMARY}' -R test-owner/no-such-repo",
            out,
        )
        self.assertIn("CD_RUNNER НЕ вернулся (HTTP 403", self.team("merge", "report")[1])

    def test_a_refused_cancel_sets_the_variable_back_at_once(self):
        self.runners("offline", cancel_refuse=True)
        self.ci(main=["deploy_stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([c[c.index("--body") + 1] for c in self.changes() if c[0] == "variable"], [THE_SPARE, PRIMARY])
        self.assertEqual(self.gh_calls("run", "rerun"), [])
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY})
        self.assertIn(
            "не прошла (HTTP 500: cancel refused), выкатка ждала primary; CD_RUNNER снова",
            self.team("merge", "report")[1],
        )

    def test_a_refused_variable_is_one_try_and_the_deploy_waits(self):
        self.runners("offline", variable_refuse_after=0)
        self.ci(main=["deploy_stuck", "deploy_stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(len(self.gh_calls("variable", "set")), 1)  # not one per poll
        self.assertEqual((self.gh_calls("run", "cancel"), self.gh_calls("run", "rerun")), ([], []))
        self.assertEqual(out.count("gh variable set CD_RUNNER refused (HTTP 403"), 1)
        self.assertNotIn("then rerun --failed", out)  # the switch is said only once the variable changed
        self.assertIn(f"сменить CD_RUNNER на {THE_SPARE} не удалось (HTTP 403", self.team("merge", "report")[1])

    def test_the_runner_leaving_mid_fallback_the_next_run_sets_it_back(self):
        self.runners("offline", cancel_lag=1)
        self.ci(main=["deploy_stuck!", "success"])
        self.sets.append("merge.github_wait_max_min=0")  # the poll after the cancel (not landed yet): the runner leaves
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 3, out + err)
        e = self.entry(301)
        self.assertEqual((e["status"], e["step"]), ("github", "deploy"))
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": THE_SPARE})
        self.assertIn(
            f"CD_RUNNER пока {THE_SPARE}: вернётся на {PRIMARY}, когда прогон закончится",
            self.team("merge", "report")[1],
        )
        self.assertEqual(
            merge.fallback_notes(e),
            [
                "note: CD_RUNNER is still {} (the deploy's fallback, run {}): once that run is over, gh variable set "
                "CD_RUNNER --body '{}' -R test-owner/no-such-repo".format(THE_SPARE, e["cd_runner"]["run"], PRIMARY)
            ],
        )
        rc, out, err = self.team("merge", "run")  # goes on from the deploy: the cancel landed, the rerun, back
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY})
        self.assertEqual((len(self.gh_calls("run", "cancel")), len(self.gh_calls("run", "rerun"))), (1, 1))
        self.assertEqual(self.entry(301)["status"], "merged")


class CiRunnerTest(MergeBase):
    def setUp(self):
        super().setUp()
        self.sets += ["repo=test-owner/no-such-repo", "merge.github_wait_max_min=60"]
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")

    changes = CdRunnerTest.changes

    def runners(self, primary, spare_1="online", **extra):
        CdRunnerTest.runners(self, primary, spare_1, **extra)
        self.gh["variables"] = {"CD_RUNNER": PRIMARY, "CI_RUNNER": PRIMARY}
        self.save_gh()

    def test_primary_online_the_branch_waits_for_it(self):
        self.runners("online")
        self.ci(feat_a=["ci_stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("waiting for GitHub: задание «Test and build» в очереди 20+ мин, раннера нет", out)
        self.assertEqual(self.changes(), [])
        e = self.entry(301)
        self.assertEqual((e["status"], e.get("ci_runner")), ("merged", None))

    def test_primary_offline_the_branchs_ci_goes_to_spare_and_comes_back(self):
        self.runners("offline", cancel_lag=1)
        self.ci(feat_a=["ci_stuck!", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        e = self.entry(301)
        fb = e["ci_runner"]
        rid = str(fb["run"])
        ch = self.changes()
        self.assertEqual(
            [c[:3] for c in ch],
            [
                ["variable", "set", "CI_RUNNER"],
                ["run", "cancel", rid],
                ["run", "rerun", rid],
                ["variable", "set", "CI_RUNNER"],
            ],
        )
        self.assertEqual([c[c.index("--body") + 1] for c in ch if c[0] == "variable"], [THE_SPARE, PRIMARY])
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY, "CI_RUNNER": PRIMARY})
        self.assertIn(
            f"no primary runner online, «Test and build» queued 20+ min: CI_RUNNER → {THE_SPARE} (spare-1), "
            f"gh run cancel {rid}, then rerun --failed",
            out,
        )
        self.assertIn(f"gh run rerun {rid} --failed (CI to spare", out)
        self.assertIn(f"CI_RUNNER → {PRIMARY} again (run {rid}, attempt 2: success)", out)
        self.assertEqual(
            (fb["var"], fb["job"], fb["to"], fb["back_to"]),
            ("CI_RUNNER", "Test and build", ["self-hosted", "spare"], ["self-hosted", "primary"]),
        )
        self.assertTrue(fb["back"] and fb["rerun"])
        self.assertIsNone(e.get("cd_runner"))
        self.assertEqual(e["status"], "merged")
        self.assertIn("ci_runner_back", [h["event"] for h in self.q().history()])
        line = (
            "  CI_RUNNER: primary не в сети, «Test and build» ждал 20+ мин → CI на spare "
            f"(прогон {rid}: отмена, перезапуск упавших); CI_RUNNER снова {PRIMARY} ("
        )
        rep = self.team("merge", "report")[1]
        self.assertEqual(next(ln for ln in rep.splitlines() if "CI_RUNNER" in ln)[: len(line)], line)

    def test_mains_test_job_stuck_goes_to_spare_in_the_deploy_step(self):
        self.runners("offline")
        self.ci(main=["ci_stuck!", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        e = self.entry(301)
        self.assertEqual((e["status"], e["deploy"]["deploy_job"]), ("merged", "success"))
        self.assertEqual(e["ci_runner"]["run"], e["deploy"]["run"])
        self.assertIsNone(e.get("cd_runner"))
        self.assertEqual([c[c.index("--body") + 1] for c in self.changes() if c[0] == "variable"], [THE_SPARE, PRIMARY])
        self.assertEqual(self.load_gh()["variables"], {"CD_RUNNER": PRIMARY, "CI_RUNNER": PRIMARY})

    def test_one_fallback_per_run_and_a_new_one_for_the_next(self):
        self.runners("offline")
        self.ci(feat_a=["ci_stuck!", "success"], main=["ci_stuck!", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        e = self.entry(301)
        self.assertEqual(e["ci_runner"]["run"], e["deploy"]["run"])  # main's run: the branch's one was another
        self.assertEqual(len(self.gh_calls("run", "cancel")), 2)
        self.assertEqual(
            [c[c.index("--body") + 1] for c in self.changes() if c[0] == "variable"],
            [THE_SPARE, PRIMARY, THE_SPARE, PRIMARY],
        )

    def test_a_job_for_other_labels_is_not_moved(self):
        self.runners("offline")
        self.ci(feat_a=["stuck", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.changes(), [])
        self.assertIsNone(self.entry(301).get("ci_runner"))


class ConflictTest(MergeBase):
    def setUp(self):
        super().setUp()
        self.pr(301, "feat-a", {"a.txt": "branch\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.main_commit({"a.txt": "main\n"})
        self.ours = self.bare_head("refs/heads/feat-a:a.txt")
        self.theirs = self.bare_head("refs/heads/main:a.txt")
        self.team("merge", "add", "301", "--word", "мердж")
        self.team("merge", "add", "302", "--word", "мердж")
        self.wt_a = str(Path(self.wt, "feat-a"))

    def test_a_conflict_with_no_prepared_resolution_stops(self):
        head = self.g(self.wt_a, "rev-parse", "HEAD")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 1, out + err)
        self.assertIn(
            f"STOP: no prepared resolution for a.txt ({self.ours[:12]} / {self.theirs[:12]}): depot2 merge resolve add a.txt --ours {self.ours} --theirs {self.theirs}",
            out,
        )
        self.assertEqual(self.g(self.wt_a, "rev-parse", "HEAD"), head)  # the merge was aborted
        self.assertEqual(self.g(self.wt_a, "status", "--porcelain"), "")
        self.assertFalse(
            os.path.exists(Path(self.wt_a, ".git"))
            and os.path.exists(self.g(self.wt_a, "rev-parse", "--git-path", "MERGE_HEAD"))
        )
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.gh_calls("pr", "merge"), [])
        self.assertEqual(self.entry(302)["status"], "waiting")
        self.assertFalse(os.path.exists(self.marker))

    def test_a_prepared_resolution_by_the_blobs_is_applied(self):
        f = str(Path(self.tmp, "a.resolved"))
        with Path(f).open("w") as fh:
            fh.write("<<<<<<< ours\nbranch\n=======\nmain\n>>>>>>> theirs\n")
        rc, _, err = self.team(
            "merge", "resolve", "add", "a.txt", "--ours", self.ours, "--theirs", self.theirs, "--from", f
        )
        self.assertEqual(rc, 1)
        self.assertIn("conflict markers", err)
        rc, _, err = self.team(
            "merge", "resolve", "add", "a.txt", "--ours", self.ours[:12], "--theirs", self.theirs, "--from", f
        )
        self.assertIn("the full blob id", err)
        with Path(f).open("w") as fh:
            fh.write("branch and main\n")
        rc, out, err = self.team(
            "merge",
            "resolve",
            "add",
            "a.txt",
            "--ours",
            self.ours,
            "--theirs",
            self.theirs,
            "--from",
            f,
            "--note",
            "both lines kept",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("added a prepared resolution for a.txt", out)
        self.assertTrue(os.path.isfile(self.t.state.path(merge.RES_DIR, f"{self.ours}-{self.theirs}")))
        self.assertIn("a.txt", self.team("merge", "resolve", "list")[1])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("resolved a.txt from the prepared resolution", out)
        with Path(self.marker).open() as fh:
            self.assertEqual(fh.read(), "checked\n")  # the checks ran after the resolution
        self.assertEqual(self.g(self.tmp, "--git-dir", self.bare, "show", "main:a.txt"), "branch and main")
        self.assertEqual([c[2] for c in self.gh_calls("pr", "merge")], ["301", "302"])
        rc, out, err = self.team("merge", "resolve", "drop", self.ours[:16], "--reason", "landed")
        self.assertEqual(rc, 0, err)
        self.assertEqual(merge.Resolutions(self.t.state).items(), [])
        self.assertFalse(os.path.exists(self.t.state.path(merge.RES_DIR, f"{self.ours}-{self.theirs}")))

    def test_a_resolutions_path_is_the_repos(self):
        f = str(Path(self.tmp, "ci.resolved"))
        with Path(f).open("w") as fh:
            fh.write("on: push\n")
        res = merge.Resolutions(self.t.state)
        it, replaced = res.add("./.github/workflows/ci.yml", self.ours, self.theirs, f)
        self.assertEqual((it["file"], replaced), (".github/workflows/ci.yml", False))
        for bad in ("../x", "/etc/x", "a/../../b", ""):
            with self.assertRaises(merge.MergeError):
                res.add(bad, self.ours, self.theirs, f, replace=True)
        with self.assertRaises(merge.MergeError):
            res.add(".github/workflows/ci.yml", self.ours, self.theirs, f)  # there already: --replace
        self.assertTrue(res.add(".github/workflows/ci.yml", self.ours, self.theirs, f, replace=True)[1])

    def test_a_check_failing_after_the_resolution_stops(self):
        f = str(Path(self.tmp, "a.resolved"))
        with Path(f).open("w") as fh:
            fh.write("branch and main\n")
        self.team("merge", "resolve", "add", "a.txt", "--ours", self.ours, "--theirs", self.theirs, "--from", f)
        self.sets.append("merge.after_resolve=" + json.dumps([[sys.executable, "-c", "import sys; sys.exit(3)"]]))
        head = self.g(self.wt_a, "rev-parse", "HEAD")
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("after the prepared resolution on feat-a, ", out)
        self.assertEqual(self.g(self.wt_a, "rev-parse", "HEAD"), head)
        self.assertEqual(self.g(self.wt_a, "status", "--porcelain"), "")
        self.assertEqual(self.pushes(), [])


class AheadTest(MergeBase):
    def test_a_local_head_ahead_of_origin_is_pushed_not_waited_for(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.main_commit({"c.txt": "C\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        os.environ["FAKE_SCAN_RESULT"] = "dirty"
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("STOP: the private scan of feat-a is not CLEAN: no push", out)
        wt = str(Path(self.wt, "feat-a"))
        local = self.g(wt, "rev-parse", "HEAD")
        self.assertNotEqual(local, self.bare_head("refs/heads/feat-a"))  # merged here, never pushed
        self.g(wt, "merge-base", "--is-ancestor", "origin/main", "HEAD")
        self.assertEqual(self.pushes(), [])
        os.environ.pop("FAKE_SCAN_RESULT")
        self.team("merge", "resume", "--reason", "скан чист")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("is 1 commit(s) ahead of origin/feat-a", out)
        self.assertIn(f"pushed the local head of feat-a: {local[:7]}", out)
        self.assertEqual(len(self.pushes()), 1)
        self.assertEqual(self.bare_head("refs/heads/feat-a"), local)
        ci = [c for c in self.gh_calls("run", "list") if "feat-a" in c]
        self.assertTrue(ci and all(c[c.index("--commit") + 1] == local for c in ci))
        self.assertEqual(self.entry(301)["status"], "merged")

    def test_the_ci_step_never_waits_for_a_head_github_lacks(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.main_commit({"c.txt": "C\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        wt = str(Path(self.wt, "feat-a"))
        self.g(wt, "fetch", "-q", "origin")
        self.g(wt, "merge", "-q", "--no-ff", "origin/main", "-m", "merged by hand, not pushed")
        local = self.g(wt, "rev-parse", "HEAD")
        with self.t.state.lock():  # a runner that died between its merge and its push
            q = self.q()
            e = q.get(301)
            e.update(status="riding", step="ci", head=local)
            q.save("test", e["id"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"not the head {local[:7]}: back to the sync, which pushes it", out)
        self.assertEqual(self.bare_head("refs/heads/feat-a"), local)
        self.assertEqual(self.entry(301)["status"], "merged")


class CarTest(MergeBase):
    def test_a_cars_object_leaves_with_its_car_merged(self):
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Трек")
        tr.car_add(t["id"], "вагон", n="1", branch="feat-a")
        self.pr(301, "feat-a", {"a.txt": "A\n"}, stack=False)
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:1", "--start")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.team("stack", "set", "st-0001", "--pr", "301")[0], 0)
        self.assertEqual(self.team("stack", "post", "st-0001")[0], 0)
        rc, out, err = self.team("merge", "add", "301", "--word", "вагон — мердж")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.entry(301)["car"], "tr-0001:1")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        c = Tracks(self.t.state, self.t.cfg).car("tr-0001:1")[1]
        self.assertEqual(c["status"], "merged")
        self.assertIn("tr-0001:1 → слит", out)

    def test_a_train_into_the_merge_queue_its_prs_in_its_order_then_it_arrives(self):
        tr = Tracks(self.t.state, self.t.cfg)
        a, b = tr.add("Поиск"), tr.add("Экспорт")
        tr.car_add(a["id"], "фильтры", n="1", branch="feat-a")
        tr.car_add(b["id"], "выгрузка", n="1", branch="feat-b")
        self.pr(301, "feat-a", {"a.txt": "A\n"}, stack=False)
        self.pr(302, "feat-b", {"b.txt": "B\n"}, stack=False)
        self.assertEqual(
            self.team(
                "trains",
                "add",
                "--name",
                "search-n-export",
                "--title",
                "Поиск и экспорт",
                "--car",
                "tr-0002:1",
                "--car",
                "tr-0001:1",
                "--slots",
                "max 2",
            )[0],
            0,
        )
        rc, out, err = self.team("trains", "state", "search-n-export", "impl")
        self.assertEqual(rc, 0, err)
        for ref, _n in (("tr-0002:1", 302), ("tr-0001:1", 301)):
            self.assertEqual(self.team("tracks", "car", "stack", ref, "--start")[0], 0)
        self.assertEqual(self.team("stack", "set", "st-0001", "--pr", "302")[0], 0)
        self.assertEqual(self.team("stack", "set", "st-0002", "--pr", "301")[0], 0)
        self.assertEqual(self.team("stack", "post", "st-0001")[0], 0)
        # one PR is green, the other still in the build: the gate holds, nothing is queued
        rc, _, err = self.team("trains", "state", "search-n-export", "merge", "--word", "поезд — мердж")
        self.assertEqual(rc, 1)
        self.assertIn("PR — tr-0001:1: сборка", err)
        self.assertFalse(os.path.exists(self.t.state.path(merge.FILE)))
        self.assertEqual(self.team("stack", "post", "st-0002")[0], 0)
        rc, _, err = self.team("trains", "state", "search-n-export", "merge")  # the owner's word is required
        self.assertEqual(rc, 1)
        self.assertIn("только по слову владельца", err)
        rc, out, err = self.team("trains", "state", "search-n-export", "merge", "--word", "поезд — мердж")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            [(e["pr"], e["car"], e["word"]) for e in self.q().entries()],
            [(302, "tr-0002:1", "поезд — мердж"), (301, "tr-0001:1", "поезд — мердж")],
        )
        self.assertIn("search-n-export: «имплементация» → «мердж-очередь»", out)
        self.assertEqual(Trains(self.t.state, self.t.cfg).get("search-n-export")["state"], "merge")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        tn = Trains(self.t.state, self.t.cfg).get("search-n-export")
        self.assertEqual(tn["state"], "arrived")
        self.assertIn("поезд search-n-export → «прибыл» (последний вагон tr-0001:1 закрыт)", out)
        self.assertEqual([r["state"] for r in tn["states"]], ["design", "impl", "merge", "arrived"])
        self.assertIn("stack done st-0001 --car merged", out)

    def train(self, prs, name="search"):
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Поиск")
        refs = []
        for i, n in enumerate(prs, 1):
            tr.car_add(t["id"], "вагон %d" % i, n=str(i), branch="feat-%d" % (n or i))  # noqa: UP031
            refs.append("%s:%d" % (t["id"], i))  # noqa: UP031
            if n:
                self.pr(n, "feat-%d" % n, {"f%d.txt" % n: "%d\n" % n}, stack=False)  # noqa: UP031
        args = ["trains", "add", "--name", name, "--title", "Т", "--slots", "all-free"]
        for r in refs:
            args += ["--car", r]
        self.assertEqual(self.team(*args)[0], 0)
        self.assertEqual(self.team("trains", "state", name, "impl")[0], 0)
        k = 0
        for r, n in zip(refs, prs, strict=False):
            if not n:
                continue
            k += 1
            for argv in (
                ("tracks", "car", "stack", r, "--start"),
                ("stack", "set", "st-%04d" % k, "--pr", str(n)),  # noqa: UP031
                ("stack", "post", "st-%04d" % k),  # noqa: UP031
            ):
                rc, out, err = self.team(*argv)
                self.assertEqual(rc, 0, out + err)
        return refs

    def refused(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 1, out + err)
        self.assertNotIn("Traceback", err)
        return err

    def test_a_train_into_the_merge_queue_all_or_nothing(self):
        refs = self.train([301, None, None])
        self.assertIn("PR — tr-0001:2: тележка", self.refused("trains", "state", "search", "merge", "--word", "мердж"))
        self.assertEqual(self.team("tracks", "car", "set", refs[2], "--pr", "301")[0], 0)
        err = self.refused("trains", "state", "search", "merge", "--word", "мердж", "--force", "--why", "едут")
        self.assertIn("встаёт весь поезд или ничего — не готовы 2 из 3 вагонов", err)
        self.assertIn(
            "- tr-0001:2: нет PR — `depot2 tracks car set tr-0001:2 --pr N`, или снять с поезда: "
            "`depot2 trains car drop search tr-0001:2 --why «…»`",
            err,
        )
        self.assertIn("- tr-0001:3: PR #301 — тот же, что у tr-0001:1", err)
        self.assertIn("Ничего не записано", err)
        self.assertFalse(os.path.exists(self.t.state.path(merge.FILE)))
        self.assertEqual(Trains(self.t.state, self.t.cfg).get("search")["state"], "impl")

    def test_a_closed_and_a_draft_pr_refuse_the_train_before_the_first_write(self):
        self.train([301, 302, 303])
        self.gh = self.load_gh()
        self.gh["prs"]["302"]["state"] = "CLOSED"
        self.gh["prs"]["303"]["isDraft"] = True
        self.save_gh()
        err = self.refused("trains", "state", "search", "merge", "--word", "мердж")
        self.assertIn("не готовы 2 из 3 вагонов", err)
        self.assertIn(
            "- tr-0001:2: PR #302 is CLOSED — поправить PR или вагон (другой PR: `depot2 tracks car set "
            "tr-0001:2 --pr N`), или снять с поезда: `depot2 trains car drop search tr-0001:2 --why «…»`",
            err,
        )
        self.assertIn("- tr-0001:3: PR #303 is a draft — поправить PR или вагон", err)
        self.assertNotIn("tr-0001:1", err)
        self.assertFalse(os.path.exists(self.t.state.path(merge.FILE)))
        self.assertEqual(Trains(self.t.state, self.t.cfg).get("search")["state"], "impl")
        tk = Tracks(self.t.state, self.t.cfg)
        self.assertEqual(tk.stage_of(tk.car("tr-0001:1")[1]), "acceptance")

    def test_a_merge_add_refused_on_the_way_takes_back_what_this_call_queued(self):
        self.train([301, 302])
        was = Stack(self.t.state, self.t.cfg).get("st-0001")["status"]
        self.gh = self.load_gh()
        self.gh["view_states"] = {"302": ["OPEN", "CLOSED"]}
        self.save_gh()
        err = self.refused("trains", "state", "search", "merge", "--word", "мердж")
        self.assertIn("search: в «мердж-очередь» не встал — tr-0001:2: PR #302 is CLOSED", err)
        self.assertIn(
            "Откачено (откат trains state merge (поезд search: PR #302 не встал)):\n"
            "- tr-0001:1: mq-0001 PR #301 снят с мердж-очереди",
            err,
        )
        self.assertIn("Состояние поезда — «имплементация», как было.", err)
        e = self.entry("mq-0001")
        self.assertEqual((e["pr"], e["status"]), (301, "dropped"))
        self.assertTrue(e["dropped"]["reason"].startswith("откат trains state merge"))
        self.assertEqual([h["event"] for h in self.q().history()], ["add", "drop"])
        self.assertEqual(self.q().entries(open_only=True), [])
        self.assertEqual(Trains(self.t.state, self.t.cfg).get("search")["state"], "impl")
        tk = Tracks(self.t.state, self.t.cfg)
        self.assertEqual(tk.stage_of(tk.car("tr-0001:1")[1]), "acceptance")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["status"], was)
        self.assertEqual(len(self.gh_calls("pr", "view", "302")), 2)  # one read by the check, one by the add
        self.gh = self.load_gh()
        self.gh["view_states"] = {}
        self.gh["prs"]["302"]["state"] = "OPEN"
        self.save_gh()
        rc, out, err = self.team("trains", "state", "search", "merge", "--word", "мердж")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(
            [(e["id"], e["pr"], e["car"]) for e in self.q().entries(open_only=True)],
            [("mq-0002", 301, "tr-0001:1"), ("mq-0003", 302, "tr-0001:2")],
        )
        self.assertIn("search: «имплементация» → «мердж-очередь»", out)
        self.assertEqual(Trains(self.t.state, self.t.cfg).get("search")["state"], "merge")


class WorktreeTest(MergeBase):
    def wt_of(self, branch):
        return os.path.join(self.wt, branch)

    def test_after_the_merge_a_clean_worktree_goes_a_dirty_one_stays_with_the_reason(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"}, title="Чистая")
        self.pr(302, "feat-b", {"b.txt": "B\n"}, title="Грязная")
        nm = str(Path(self.tmp, "nm"))
        Path(nm).mkdir(parents=True)
        self.write(nm, {"pkg.js": "x\n"})
        os.symlink(nm, Path(self.wt_of("feat-a"), "node_modules"))
        with Path(self.clone, ".git", "info", "exclude").open("a") as f:
            f.write("*.log\n")
        self.write(self.wt_of("feat-a"), {"build.log": "ignored\n" * 2000})  # ignored: no hindrance
        self.write(self.wt_of("feat-b"), {"notes.txt": "the agent's notes\n"})  # untracked: it rides, it stays
        for n in (301, 302):
            self.assertEqual(self.team("merge", "add", str(n), "--word", "мердж")[0], 0)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual([e["status"] for e in self.q().entries()], ["merged", "merged"])
        # feat-a: gone; what its link points at and the branch stay
        self.assertFalse(os.path.lexists(self.wt_of("feat-a")))
        self.assertTrue(os.path.exists(Path(nm, "pkg.js")))
        self.assertEqual(self.g(self.clone, "rev-parse", "refs/heads/feat-a"), self.entry(301)["head"])
        self.assertNotIn(self.wt_of("feat-a"), self.g(self.clone, "worktree", "list"))
        w = self.entry(301)["wt"]
        self.assertTrue(w["removed"])
        self.assertGreater(w["freed"], 8192)
        self.assertIn("рабочая копия {} удалена: освобождено ".format(self.wt_of("feat-a")), out)
        self.assertIn("(ветка feat-a осталась)", out)
        # feat-b: as it was, the reason in the runner's output, the entry's log and its `wt`
        self.assertTrue(os.path.exists(Path(self.wt_of("feat-b"), "notes.txt")))
        w = self.entry(302)["wt"]
        self.assertFalse(w["removed"])
        self.assertEqual(w["reason"], "грязная: ?? notes.txt")
        self.assertIn("рабочая копия {} не тронута: грязная: ?? notes.txt".format(self.wt_of("feat-b")), out)
        self.assertTrue(any("не тронута: грязная" in ln for ln in self.entry(302)["log"]))
        # seen: `merge` (the last merge), `merge --all`, `merge report`, `merge --json` (the web view), the history
        self.assertIn("Последний мердж: #302", self.team("merge")[1])
        self.assertIn("копия не тронута: грязная: ?? notes.txt", self.team("merge")[1])
        self.assertIn("копия удалена (", self.team("merge", "--all")[1])
        rep = self.team("merge", "report", "--last", "2")[1]
        self.assertIn("рабочая копия: удалена, освобождено ", rep)
        self.assertIn("рабочая копия: не тронута — грязная: ?? notes.txt", rep)
        j = json.loads(self.team("merge", "--json")[1])
        self.assertEqual((j["last_merged"]["pr"], j["last_merged"]["wt"]["reason"]), (302, "грязная: ?? notes.txt"))
        ev = [h for h in self.q().history() if h["event"] == "worktree"]
        self.assertEqual([(h["entry"], h["wt"]["removed"]) for h in ev], [("mq-0001", True), ("mq-0002", False)])

    def merged_on_github(self, n):
        """Merged as by a person on GitHub (the fake's `pr merge`), not by the queue."""
        sh(self.tmp, "gh", "pr", "merge", str(n), "-R", "x/y", "--merge")

    def test_prune_worktrees_removes_only_clean_merged_ones_and_says_why_for_the_rest(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"}, stack=False)  # merged, clean: goes
        self.pr(302, "feat-b", {"b.txt": "B\n"}, stack=False)  # merged, a tracked file changed
        self.pr(303, "feat-c", {"c.txt": "C\n"}, stack=False)  # its PR is open
        self.pr(304, "feat-d", {"d.txt": "D\n"}, stack=False)  # merged, then a local commit
        self.pr(306, "feat-f", {"f.txt": "F\n"}, stack=False)  # merged, its branch in a slot again
        self.pr(307, "feat-g", {"g.txt": "G\n"}, stack=False)  # merged, locked
        for n in (301, 302, 304, 306, 307):
            self.merged_on_github(n)
        self.g(self.clone, "fetch", "-q", "origin")
        self.g(self.clone, "worktree", "add", "-q", "-b", "feat-e", self.wt_of("feat-e"), "origin/main")  # a new build
        self.g(self.clone, "worktree", "add", "-q", "--detach", self.wt_of("look"), "origin/main")
        Path(self.wt_of("other-repo")).mkdir(parents=True)
        self.write(self.wt_of("feat-b"), {"b.txt": "changed\n"})
        self.write(self.wt_of("feat-d"), {"d2.txt": "more\n"})
        self.g(self.wt_of("feat-d"), "add", "d2.txt")
        self.g(self.wt_of("feat-d"), "commit", "-q", "-m", "after the merge")
        self.g(self.clone, "worktree", "lock", "--reason", "on a stick", self.wt_of("feat-g"))
        s = Stack(self.t.state, self.t.cfg)
        o = s.add("follow-up", branch="feat-f")
        s.start(o["id"])
        before = sorted(os.listdir(self.wt))
        rc, out, err = self.team("merge", "prune-worktrees", "--dry-run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(os.listdir(self.wt)), before)  # nothing touched
        self.assertIn("--dry-run (ничего не трогаю)", out)
        self.assertIn("удалю (1, освободится ", out)
        self.assertIn("  - feat-a: PR #301 слит; ", out)
        self.assertEqual([h for h in self.q().history() if h["event"] == "prune_worktrees"], [])
        rc, out, err = self.team("merge", "prune-worktrees")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(sorted(os.listdir(self.wt)), [d for d in before if d != "feat-a"])
        self.assertIn(f"каталогов 9 (рабочих копий {self.clone} 8)", out)
        self.assertIn("удалены (1, освобождено ", out)
        self.assertIn("  - feat-a: PR #301 слит; ", out)
        self.assertIn("не тронуты (8):", out)
        head_d = self.g(self.wt_of("feat-d"), "rev-parse", "HEAD")
        for line in (
            "  - feat-b: грязная: M b.txt",
            "  - feat-c: открыт PR #303",
            f"  - feat-d: HEAD {head_d[:7]} не в origin/main (коммитов сверх: 1)",
            "  - feat-e: ветка не слита (PR нет)",
            "  - feat-f: в слоте: {}".format(o["id"]),
            "  - feat-g: заперта (git worktree lock: on a stick)",
            "  - look: отсоединённая HEAD, не ветка",
            f"  - other-repo: не рабочая копия {self.clone}",
        ):
            self.assertIn(line, out)
        self.assertIn("итого: удалены 1, не тронуто 8, освобождено ", out)
        self.assertEqual(self.g(self.clone, "rev-parse", "refs/heads/feat-a"), self.bare_head("refs/heads/feat-a"))
        ev = [h for h in self.q().history() if h["event"] == "prune_worktrees"]
        self.assertEqual(len(ev), 1)
        self.assertEqual(([r["name"] for r in ev[0]["removed"]], ev[0]["kept"]), (["feat-a"], 8))
        self.assertGreater(ev[0]["freed"], 0)
        # once more: nothing left to remove
        rc, out, _ = self.team("merge", "prune-worktrees")
        self.assertIn("удалены (0, освобождено 0.0 MB):", out)


class DryRunTest(MergeBase):
    def test_the_dry_run_plans_on_top_of_the_ones_ahead_and_writes_nothing(self):
        self.pr(302, "feat-b", {"a.txt": "from b\n"})
        self.pr(301, "feat-a", {"a.txt": "from a\n", "d.txt": "d\n"})
        self.pr(303, "feat-c", {"c.txt": "C\n"})
        for n in (302, 301, 303):
            self.team("merge", "add", str(n), "--word", "мердж %d" % n)  # noqa: UP031
        files = (merge.FILE, merge.HISTORY, "stack.json")
        before = {p: self.bytes(p) for p in files}
        heads = {b: self.g(os.path.join(self.wt, b), "rev-parse", "HEAD") for b in ("feat-a", "feat-b", "feat-c")}
        rc, out, err = self.team("merge", "run", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("DRY RUN: the merge queue's plan (no push, no merge, the depot unchanged)", out)
        self.assertIn("1. mq-0001 PR #302", out)
        self.assertIn("sync: origin/main is in feat-b already: its head stays — no push; CI on", out)
        part = out[out.index("2. mq-0002 PR #301") : out.index("3. mq-0003 PR #303")]
        self.assertIn("STOP: no prepared resolution for a.txt", part)
        part = out[out.index("3. mq-0003 PR #303") :]
        self.assertIn(
            "would merge origin/main + #302 into feat-c with a merge commit; private scan; git push origin feat-c", part
        )
        self.assertIn("stack done st-0003 --car merged", part)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.gh_calls("pr", "merge"), [])
        self.assertEqual({p: self.bytes(p) for p in files}, before)
        self.assertEqual({b: self.g(os.path.join(self.wt, b), "rev-parse", "HEAD") for b in heads}, heads)
        self.assertEqual(len(self.g(self.clone, "worktree", "list").splitlines()), 4)  # the throwaway one is gone


class PartsTest(unittest.TestCase):
    NOW = 1762905600.0

    def run_(self, state, concl=""):
        return {"databaseId": 1, "status": state, "conclusion": concl}

    def test_classify(self):
        def job(**kw):
            return dict(
                {
                    "name": "j",
                    "status": "completed",
                    "conclusion": "success",
                    "runner_name": "r",
                    "steps": [1],
                    "created_at": "2025-11-11T23:59:00Z",
                },
                **kw,
            )

        def c(runs, jobs):
            return merge.classify(runs, lambda rid: jobs, self.NOW, 15)[:2]

        self.assertEqual(c([], [])[0], "none")
        self.assertEqual(c([self.run_("completed", "success")], [job()])[0], "green")
        self.assertEqual(c([self.run_("in_progress")], [job(status="in_progress", conclusion=None)])[0], "pending")
        k, t = c([self.run_("completed", "failure")], [job(conclusion="failure", name="Test and build")])
        self.assertEqual((k, t), ("failed", "failure: Test and build failure"))
        k, t = c(
            [self.run_("completed", "failure")],
            [job(), job(name="Deploy", status="queued", conclusion=None, runner_name=None, steps=[])],
        )
        self.assertEqual((k, t), ("github", "GitHub не запустил задание «Deploy» (queued), прогон failure"))
        self.assertTrue(
            merge.classify(
                [self.run_("completed", "failure")],
                lambda r: [job(conclusion="cancelled", runner_name=None, steps=[])],
                self.NOW,
                15,
            )[3]
        )
        # queued with no runner: 10 min is a wait, 20 min is GitHub's
        q = {"status": "queued", "conclusion": None, "runner_name": None, "steps": []}
        self.assertEqual(c([self.run_("in_progress")], [job(created_at="2025-11-11T23:50:00Z", **q)])[0], "pending")
        k, t = c([self.run_("in_progress")], [job(created_at="2025-11-11T23:40:00Z", **q)])
        self.assertEqual((k, t), ("github", "задание «j» в очереди 20+ мин, раннера нет"))
        self.assertEqual(c([self.run_("completed", "startup_failure")], [])[0], "github")
        # a failed run among green ones decides
        self.assertEqual(
            merge.classify(
                [self.run_("completed", "success"), dict(self.run_("completed", "failure"), databaseId=2)],
                lambda rid: [job()] if rid == 1 else [job(conclusion="failure")],
                self.NOW,
                15,
            )[0],
            "failed",
        )

    def test_the_cd_fallback_setting(self):
        from depot2 import config

        m = dict(config.load("/nonexistent")["merge"])  # the test project's config (tests/project-config.json)
        config.check_merge(m)
        config.check_merge(dict(m, cd_fallback=None, ci_fallback=None))  # off
        for bad in (
            {"var": "", "main": ["a"], "fallback": ["b"]},
            {"var": "V", "main": [], "fallback": ["b"]},
            {"var": "V", "main": ["a"]},
            ["self-hosted"],
        ):
            for key in ("cd_fallback", "ci_fallback"):
                with self.assertRaises(ValueError):
                    config.check_merge(dict(m, **{key: bad}))
        with self.assertRaises(ValueError):  # CI's names the jobs it moves
            config.check_merge(dict(m, ci_fallback=dict(m["ci_fallback"], jobs=[])))
        self.assertEqual(merge.labels_json(m["cd_fallback"]["fallback"]), THE_SPARE)
        self.assertEqual(merge.label_names(m["cd_fallback"]["main"]), "primary")

    def test_the_last_merge_by_its_stamp_then_the_queues_order(self):
        t = TempState()
        try:
            q = merge.MergeQueue(t.state)
            same = "2015-11-12T08:00:00+00:00"
            for n, ts, status in (
                (302, same, "merged"),
                (301, same, "merged"),
                (303, same, "merged"),
                (304, "2015-11-12T07:59:59+00:00", "merged"),
                (305, "2015-11-12T08:00:05+00:00", "dropped"),
            ):
                q.data["entries"].append(
                    {"id": "mq-%d" % n, "pr": n, "status": status, "merged": {"sha": "a" * 40, "ts": ts}}  # noqa: UP031
                )
            self.assertEqual(merge.last_merged(q)["pr"], 303)  # a tie: the later entry; a dropped one never
            q.data["entries"][0]["merged"]["ts"] = "2015-11-12T08:00:01+00:00"
            self.assertEqual(merge.last_merged(q)["pr"], 302)  # a later stamp wins whatever the order
        finally:
            t.close()

    def test_smoke_verdict(self):
        cfg = {"plugins": []}  # no plugin: the exit code and the last line
        self.assertEqual(merge.smoke_verdict(cfg, 0, "warming up\nall good\n"), (True, "exit 0: all good"))
        self.assertEqual(merge.smoke_verdict(cfg, 1, "boom\n"), (False, "exit 1: boom"))
        self.assertFalse(merge.smoke_verdict(cfg, None, "")[0])
        cfg = {"plugins": [EXAMPLE_PLUGIN]}  # the example plugin reads its JSON lines
        ok = '{"check": "home", "ok": true}\n{"check": "login", "ok": true}\n'
        self.assertEqual(merge.smoke_verdict(cfg, 0, ok), (True, "checks 2/2 ok"))
        bad = '{"check": "login", "ok": false, "error": "TypeError: x"}\n'
        self.assertEqual(merge.smoke_verdict(cfg, 1, bad), (False, "checks 0/1 ok, login: TypeError: x, exit 1"))
        self.assertEqual(merge.smoke_verdict(cfg, 1, "SMOKE FAILED boom"), (False, "exit 1: SMOKE FAILED boom"))


if __name__ == "__main__":
    unittest.main()
