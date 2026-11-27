
import json
import os
import sys
import unittest

from helpers import HERE
from test_merge import MergeBase

from depot2 import merge, mergeauto

FAKE_GEN = os.path.join(HERE, "fake_gen.py")
MAP0 = "files: 3\n\na.txt\nb.txt\nc.txt\n"


class AutoBase(MergeBase):
    def setUp(self):
        super().setUp()
        self.genlog = os.path.join(self.tmp, "gen.jsonl")
        os.environ["FAKE_GEN_LOG"] = self.genlog
        self.sets += ["merge.auto.generated.cmd=" + json.dumps([sys.executable, FAKE_GEN, "generated"]),
                      "merge.auto.snapshots.cmd=" + json.dumps([sys.executable, FAKE_GEN, "snapshots", "{test}", "-u"]),
                      "merge.auto.golden.cmd=" + json.dumps([sys.executable, FAKE_GEN, "golden", "{remote}/{branch}",
                                                             "{out}"]),
                      "merge.net_retry_s=0"]

    def write(self, d, files):
        for name, text in files.items():
            os.makedirs(os.path.dirname(os.path.join(d, name)), exist_ok=True)
            with open(os.path.join(d, name), "w") as f:
                f.write(text)

    def log(self, ref):
        return "\n".join(self.entry(ref)["log"])

    def show(self, ref, path):
        return self.g(self.tmp, "--git-dir", self.bare, "show", f"{ref}:{path}")

    def trial_refs(self):
        return self.g(self.tmp, "--git-dir", self.bare, "for-each-ref", "--format=%(refname)", "refs/heads/mq/").split()

    def pushed_to(self, name):
        return [p for p in self.pushes() if any(a.endswith("refs/heads/" + name) or a == name for a in p["argv"])]

    def gens(self, what):
        if not os.path.exists(self.genlog):
            return []
        with open(self.genlog) as f:
            return [json.loads(ln) for ln in f if json.loads(ln)["argv"][0] == what]


class AdditionsTest(AutoBase):
    BASE = "# T\n\nintro\n\n| k | v |\n| a | 1 |\n\nend\n"

    def test_both_sides_only_added_one_merge_commit_and_a_journal_line(self):
        self.main_commit({"README.md": self.BASE})
        self.pr(301, "feat-a", {"README.md": self.BASE.replace("| a | 1 |\n", "| a | 1 |\n| b | 2 |\n") +
                                "\nbranch note\n"})
        before = self.bare_head("refs/heads/feat-a")
        self.main_commit({"README.md": self.BASE.replace("| a | 1 |\n", "| a | 1 |\n| c | 3 |\n") + "\nmain note\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        # the rows of one table join with no blank line; two paragraphs get one; the branch's first, then main's
        self.assertEqual(self.show("main", "README.md"),
                         "# T\n\nintro\n\n| k | v |\n| a | 1 |\n| b | 2 |\n| c | 3 |\n\nend\n\nbranch note\n\nmain note")
        self.assertIn("the queue resolved in the merge of origin/main (", out)
        self.assertIn("README.md: both sides only added (2 places): the branch's lines, then main's", self.log(301))
        head = self.g(self.clone, "rev-parse", "refs/heads/feat-a")
        self.assertEqual(self.g(self.tmp, "--git-dir", self.bare, "rev-parse", head + "^1"), before)  # one commit
        msg = self.g(self.tmp, "--git-dir", self.bare, "log", "-1", "--format=%B", head)
        self.assertTrue(msg.startswith("Merge origin/main ("), msg)
        self.assertIn("Resolved by the merge queue:\n- README.md: both sides only added", msg)
        self.assertEqual(len(self.pushed_to("feat-a")), 1)

    def test_a_change_of_common_text_stops_as_before(self):
        self.main_commit({"README.md": self.BASE})
        self.pr(301, "feat-a", {"README.md": self.BASE.replace("intro", "outro, said the branch")})
        self.main_commit({"README.md": self.BASE.replace("intro", "intro, said main")})
        self.team("merge", "add", "301", "--word", "мердж")
        wt = os.path.join(self.wt, "feat-a")
        head = self.g(wt, "rev-parse", "HEAD")
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("STOP: no prepared resolution for README.md (", out)
        self.assertIn("--from <the resolved file> (both sides changed the common text at line 3)", out)
        self.assertEqual(self.g(wt, "rev-parse", "HEAD"), head)  # the branch's worktree as it was
        self.assertEqual(self.g(wt, "status", "--porcelain"), "")
        self.assertEqual(self.pushes(), [])


class GeneratedTest(AutoBase):
    def test_the_code_map_takes_mains_side_and_is_rebuilt(self):
        self.main_commit({"docs/GENERATED.md": MAP0})
        self.pr(301, "feat-a", {"d.txt": "d\n", "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + "d.txt\n"})
        self.main_commit({"c2.txt": "c2\n", "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + "c2.txt\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.show("main", "docs/GENERATED.md"), "files: 5\n\na.txt\nb.txt\nc.txt\nc2.txt\nd.txt")
        self.assertIn("docs/GENERATED.md: main's side, rebuilt (`", self.log(301))
        self.assertEqual(len(self.gens("generated")), 1)
        self.assertFalse(os.path.exists(self.marker))

    def test_a_map_git_merged_stale_is_rebuilt_in_the_same_commit(self):
        self.main_commit({"docs/GENERATED.md": MAP0})
        self.pr(301, "feat-a", {"d.txt": "d\n", "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + "d.txt\n"})
        self.main_commit({"0.txt": "0\n", "docs/GENERATED.md": "files: 4\n\n0.txt\n" + MAP0.split("\n\n", 1)[1]})
        self.team("merge", "add", "301", "--word", "мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.show("main", "docs/GENERATED.md"), "files: 5\n\n0.txt\na.txt\nb.txt\nc.txt\nd.txt")
        self.assertIn("docs/GENERATED.md: git's merge of it was stale, rebuilt", self.log(301))

    def snapshot_repo(self, broken=False):
        self.main_commit({"strings.txt": "hello\n", "test/parser.test.ts": "// the strings\n",
                          "test/__snapshots__/parser.test.ts.snap": "snap: hello\n"})
        self.pr(301, "feat-a", {"strings.txt": "hello\nzeta\n" + ("BROKEN\n" if broken else ""),
                                "test/__snapshots__/parser.test.ts.snap": "snap: hello zeta\n"})
        self.main_commit({"strings.txt": "alpha\nhello\n", "test/__snapshots__/parser.test.ts.snap": "snap: alpha hello\n"})
        self.team("merge", "add", "301", "--word", "мердж")

    def test_a_snapshot_takes_mains_side_and_its_test_updates_it(self):
        self.snapshot_repo()
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.show("main", "test/__snapshots__/parser.test.ts.snap"), "snap: alpha hello zeta")
        self.assertIn("test/__snapshots__/parser.test.ts.snap: main's side, then `", self.log(301))
        self.assertIn("test/parser.test.ts -u`: green", self.log(301))

    def test_a_snapshot_whose_test_is_red_after_the_update_stops(self):
        self.snapshot_repo(broken=True)
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("STOP: test/__snapshots__/parser.test.ts.snap: main's side, then `", out)
        self.assertIn("is red (exit 1)", out)
        self.assertEqual(self.pushes(), [])

    def golden_repo(self):
        g = "test/golden/ranking.golden.json"
        self.main_commit({"ranking.txt": "s1\n", g: "golden of s1\n"})
        self.pr(301, "feat-a", {"ranking.txt": "s1\ns-branch\n", g: "golden of s-branch s1\n"})
        self.main_commit({"ranking.txt": "s-main\ns1\n", g: "golden of s-main s1\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        return g

    def test_the_golden_takes_mains_side_and_is_re_recorded_after_the_push(self):
        g = self.golden_repo()
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.show("main", g), "golden of s-branch s-main s1")
        self.assertEqual(len(self.pushed_to("feat-a")), 2)
        head = self.g(self.clone, "rev-parse", "refs/heads/feat-a")
        self.assertEqual(self.g(self.tmp, "--git-dir", self.bare, "log", "-1", "--format=%s", head),
                         f"Re-record {g} on the merge")
        self.assertIn(f"main's {g} is in ", self.log(301))
        self.assertEqual(self.gens("golden")[0]["argv"][1], "origin/feat-a")
        self.assertIsNone(self.entry(301).get("golden_pending"))

    def test_a_failed_re_record_stops_and_the_resume_tries_it_again(self):
        g = self.golden_repo()
        os.environ["FAKE_GEN_GOLDEN_FAIL"] = "1"
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn(f"STOP: the re-record of {g} on feat-a @ ", out)
        self.assertTrue(self.entry(301).get("golden_pending"))
        os.environ.pop("FAKE_GEN_GOLDEN_FAIL")
        self.team("merge", "resume", "--reason", "primary вернулся")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.show("main", g), "golden of s-branch s-main s1")
        self.assertIsNone(self.entry(301).get("golden_pending"))


class TrialTest(AutoBase):
    state_git = True

    def three(self):
        self.main_commit({"docs/GENERATED.md": MAP0})
        for n, br, f in ((301, "feat-a", "d.txt"), (302, "feat-b", "e.txt"), (303, "feat-c", "f.txt")):
            self.pr(n, br, {f: f + "\n", "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + f + "\n"})
            self.team("merge", "add", str(n), "--word", "мердж %d" % n)  # noqa: UP031

    def test_trials_on_top_of_the_one_riding_and_its_tree_tested_already(self):
        self.three()
        self.ci(feat_a=["in_progress", "in_progress", "in_progress", "success"])
        feat_b0 = self.bare_head("refs/heads/feat-b")
        scans = os.path.join(self.tmp, "scan.jsonl")
        os.environ["FAKE_SCAN_LOG"] = scans
        rc, out, err = self.team("merge", "run", "--once")
        self.assertEqual(rc, 0, out + err)
        with open(scans) as f:
            roots = [json.loads(ln) for ln in f if "--root" in json.loads(ln)["argv"]]
        # a trial's scan: what its push carries (its merge commit, the map the merge made), not the whole tree
        self.assertEqual(len(roots), 2)
        for s in roots:
            self.assertEqual(sorted(x.split("/")[-1] for x in s["files"]), ["GENERATED.md", "commits.txt"])
        m301 = self.load_gh()["prs"]["301"]["mergeCommit"]["oid"]
        t2, t3 = self.entry(302)["trial"], self.entry(303)["trial"]
        self.assertEqual((t2["status"], t2["branch"], t2["on"]), ("pushed", "mq/302", "origin/main + #301"))
        self.assertEqual((t3["status"], t3["on"]), ("pushed", "origin/main + #301 + #302"))
        self.assertEqual(t3["base"], t2["sha"])
        self.assertEqual(sorted(self.trial_refs()), ["refs/heads/mq/302", "refs/heads/mq/303"])
        self.assertEqual(self.bare_head("refs/heads/feat-b"), feat_b0)  # the PR's own branch is not touched
        hist = [(h["event"], h["entry"]) for h in self.q().history()]
        self.assertLess(hist.index(("trial", "mq-0002")), hist.index(("merged_pr", "mq-0001")))
        md = self.team("merge")[1]
        self.assertIn("Пробы (ветки mq/<PR>, глубина 2): #302 — mq/302 {} поверх origin/main + #301 (CI на mq/302)".format(t2["sha"][:7]), md)
        self.assertIn("ждёт · проба mq/303 {}".format(t3["sha"][:7]), md)
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        # each PR's own sync after the one before merged: the same tree its trial had
        for n, br, t in ((302, "feat-b", t2), (303, "feat-c", t3)):
            self.assertEqual(self.bare_head(f"refs/heads/{br}^{{tree}}"), t["tree"])
            self.assertIn("is the trial mq/%d's" % n, self.log(n))  # noqa: UP031
        self.assertEqual(self.g(self.tmp, "--git-dir", self.bare, "rev-parse", "refs/heads/feat-b^2"), m301)
        self.assertEqual(self.trial_refs(), [])  # the trials' branches go with the merges
        self.assertEqual(self.show("main", "docs/GENERATED.md"), "files: 6\n\na.txt\nb.txt\nc.txt\nd.txt\ne.txt\nf.txt")

    def test_a_stop_of_the_one_riding_throws_the_trials_away(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        for n in (301, 302):
            self.team("merge", "add", str(n), "--word", "мердж")
        self.ci(feat_a=["in_progress", "failure"])
        feat_b0 = self.bare_head("refs/heads/feat-b")
        rc, _out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertEqual(len(self.pushed_to("mq/302")), 2)  # pushed, then deleted
        self.assertTrue(any("--delete" in p["argv"] for p in self.pushed_to("mq/302")))
        self.assertEqual(self.trial_refs(), [])
        self.assertIsNone(self.entry(302)["trial"])
        self.assertIn("trial mq/302 of #302 thrown away: #301 stopped", self.log(302))
        self.assertEqual(self.bare_head("refs/heads/feat-b"), feat_b0)
        self.assertIn("| М2 | мердж | st-0002 PR 302 | P5 | feat-b / PR #302 | ждёт |", self.team("stack", "--md")[1])

    def test_a_conflict_shows_on_the_trial_before_its_turn(self):
        self.pr(301, "feat-a", {"a.txt": "from a\n"})
        self.pr(302, "feat-b", {"a.txt": "from b\n", "c.txt": "from b\n"})
        self.main_commit({"c.txt": "from main\n"})
        for n in (301, 302):
            self.team("merge", "add", str(n), "--word", "мердж")
        self.ci(feat_a=["in_progress", "success"])
        rc, out, err = self.team("merge", "run", "--once")
        self.assertEqual(rc, 0, out + err)
        t = self.entry(302)["trial"]
        self.assertEqual(t["status"], "stop")
        self.assertEqual(sorted((c["file"], c["with"]) for c in t["conflicts"]), [("a.txt", "#301"), ("c.txt", "main")])
        self.assertIn("#302: конфликт с #301 в a.txt, с main в c.txt", out)
        self.assertIn("#302: конфликт с #301 в a.txt, с main в c.txt", self.team("merge", "--md")[1])
        self.assertIn("ждёт · проба: конфликт с #301 в a.txt", self.team("stack", "--md")[1])
        self.assertEqual(self.pushed_to("mq/302"), [])
        rc, out, _ = self.team("merge", "run")  # its own turn: the stop it showed
        self.assertEqual(rc, 1)
        self.assertIn("STOP: no prepared resolution for a.txt", out)

    def test_a_trial_needing_the_golden_is_not_pushed_and_its_turn_re_records_it(self):
        g = "test/golden/ranking.golden.json"
        self.main_commit({"ranking.txt": "s1\n", g: "golden of s1\n"})
        self.pr(301, "feat-a", {"ranking.txt": "s1\ns-a\n", g: "golden of s-a s1\n"})
        self.pr(302, "feat-b", {"ranking.txt": "s0\ns1\n", g: "golden of s0 s1\n"})
        for n in (301, 302):
            self.team("merge", "add", str(n), "--word", "мердж")
        self.ci(feat_a=["in_progress", "success"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        t = self.entry(302)["trial"]
        self.assertEqual(t["status"], "golden")
        self.assertEqual(self.pushed_to("mq/302"), [])
        self.assertIn(f"not pushed: {g} takes main's side and is re-recorded at its turn", self.log(302))
        self.assertEqual(self.show("main", g), "golden of s-a s0 s1")
        self.assertEqual(len(self.gens("golden")), 1)

    def test_the_dry_run_shows_the_trials_and_pushes_nothing(self):
        self.main_commit({"docs/GENERATED.md": MAP0})
        self.pr(301, "feat-a", {"a.txt": "from a\n", "d.txt": "d\n",
                                "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + "d.txt\n"})
        self.pr(302, "feat-b", {"e.txt": "e\n", "docs/GENERATED.md": MAP0.replace("files: 3", "files: 4") + "e.txt\n"})
        self.pr(303, "feat-c", {"a.txt": "from c\n"})
        for n in (301, 302, 303):
            self.team("merge", "add", str(n), "--word", "мердж %d" % n)  # noqa: UP031
        files = (merge.FILE, merge.HISTORY, "stack.json")
        before = {p: self.bytes(p) for p in files}
        rc, out, err = self.team("merge", "run", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("trials: the 2 PR(s) after the one riding are tried on top of it on mq/<PR>", out)
        part = out[out.index("2. mq-0002 PR #302"):out.index("3. mq-0003 PR #303")]
        self.assertIn("the queue resolves by itself: docs/GENERATED.md: main's side, rebuilt (`", part)
        self.assertIn("trial: mq/302 = #302 + (origin/main + #301), built while #301 rides", part)
        part = out[out.index("3. mq-0003 PR #303"):]
        self.assertIn("STOP: no prepared resolution for a.txt", part)
        self.assertIn("#303: конфликт с #301 в a.txt", part)
        self.assertIn("trial: mq/303 = #303 + (origin/main + #301 + #302), built while #302 rides", part)
        self.assertEqual(self.pushes(), [])
        self.assertEqual(self.gens("generated"), [])  # the dry run runs no generator
        self.assertEqual({p: self.bytes(p) for p in files}, before)
        self.assertEqual(len(self.g(self.clone, "worktree", "list").splitlines()), 4)


class NetworkTest(AutoBase):
    def test_a_push_the_network_failed_is_tried_again(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.main_commit({"c.txt": "C\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        fail = os.path.join(self.tmp, "git-fail")
        with open(fail, "w") as f:
            f.write("push 2 unable to access 'https://github.com/x/y/': Connection timed out after 300001 milliseconds\n")
        os.environ["FAKE_GIT_FAIL"] = fail
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("network: git push failed ("), 2)
        self.assertIn("again in 0 s (2 of 5)", out)
        self.assertEqual(self.entry(301)["status"], "merged")

    def test_a_gh_call_the_network_failed_is_tried_again(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        self.gh = self.load_gh()
        self.gh["net_fail"] = {"pr view": 2, "run list": 1}
        self.save_gh()
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(out.count("network: gh pr view failed (error connecting to api.github.com"), 2)
        self.assertEqual(out.count("network: gh run list failed"), 1)

    def test_a_push_refused_for_another_reason_still_stops(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.main_commit({"c.txt": "C\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        fail = os.path.join(self.tmp, "git-fail")
        with open(fail, "w") as f:
            f.write("push 1 Authentication failed for 'https://github.com/x/y/'\n")
        os.environ["FAKE_GIT_FAIL"] = fail
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("STOP: git push of feat-a failed: fatal: Authentication failed", out)
        self.assertNotIn("network:", out)

    def test_ci_red_only_on_the_network_is_rerun(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        self.ci(feat_a=["failure!", "success"])
        self.gh = self.load_gh()
        self.gh["failed_log"] = {"feat-a": "Test and build\tSet up job\t##[error]Failed to download action "
                                           "'https://api.github.com/repos/actions/checkout/tarball/x'. Error: "
                                           "The request was canceled due to the configured HttpClient.Timeout of 100 seconds"}
        self.save_gh()
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn("failed only on the network («HttpClient.Timeout» in its failed log): gh run rerun ", out)
        self.assertEqual(len(self.gh_calls("run", "rerun")), 1)
        self.assertEqual(self.entry(301)["status"], "merged")

    def test_ci_red_for_a_test_still_stops(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "мердж")
        self.ci(feat_a=["failure"])
        self.gh = self.load_gh()
        self.gh["failed_log"] = {"feat-a": "Test and build\tRun tests\t FAIL  test/search.test.ts > ranks\n"}
        self.save_gh()
        rc, out, _ = self.team("merge", "run")
        self.assertEqual(rc, 1)
        self.assertIn("STOP: CI of feat-a @ ", out)
        self.assertEqual(self.gh_calls("run", "rerun"), [])


class RulesTest(unittest.TestCase):
    AUTO = {"generated": {"file": "docs/GENERATED.md", "cmd": ["x"]},
            "snapshots": {"glob": "test/__snapshots__/*.snap", "cmd": ["x"]},
            "golden": {"file": "test/golden/ranking.golden.json", "cmd": ["x"]},
            "union": ["README.md", "NOTES.md", "docs/*/GUIDE.md"]}

    def test_rule_of(self):
        r = lambda p: mergeauto.rule_of(p, self.AUTO)
        self.assertEqual(r("docs/GENERATED.md"), "generated")
        self.assertEqual(r("test/__snapshots__/parser.test.ts.snap"), "snapshot")
        self.assertEqual(r("test/golden/ranking.golden.json"), "golden")
        self.assertEqual([r(p) for p in ("README.md", "NOTES.md", "docs/api/GUIDE.md", "docs/web/ui/GUIDE.md")],
                         ["union"] * 4)
        self.assertEqual([r(p) for p in ("src/app/search.py", "tools/x/README.md", "GUIDE.md", "docs/README.md")],
                         [None] * 4)
        self.assertIsNone(mergeauto.rule_of("README.md", None))
        self.assertEqual(mergeauto.snapshot_test("test/__snapshots__/parser.test.ts.snap"), "test/parser.test.ts")
        self.assertIsNone(mergeauto.snapshot_test("test/strings.snap"))

    def hunk(self, ours, base, theirs):
        o, b, s, t = mergeauto.markers()
        return "\n".join(["top", o, *ours, b, *base, s, *theirs, t, "bottom", ""])

    def test_union(self):
        text, n = mergeauto.union(self.hunk(["", "branch para"], [], ["", "main para"]))
        self.assertEqual((text, n), ("top\n\nbranch para\n\nmain para\nbottom\n", 1))
        text, _ = mergeauto.union(self.hunk(["- x"], [], ["- y"]))  # two list items: no blank line between
        self.assertEqual(text, "top\n- x\n- y\nbottom\n")
        text, _ = mergeauto.union(self.hunk(["same", "A"], [], ["same", "B"]))  # a line both added: once
        self.assertEqual(text, "top\nsame\nA\n\nB\nbottom\n")
        text, _ = mergeauto.union(self.hunk(["Files: a, b, c"], ["Files: a, b"], ["Files: a, b, d"]))
        self.assertEqual(text, "top\nFiles: a, b, c, d\nbottom\n")  # one line both appended to
        self.assertEqual(mergeauto.union(self.hunk(["Files: a, c"], ["Files: a, b"], ["Files: a, b, d"]))[0], None)
        self.assertEqual(mergeauto.union(self.hunk(["the branch's"], ["common"], ["main's"])),
                         (None, "both sides changed the common text at line 2"))
        self.assertEqual(mergeauto.union("plain\ntext\n"), ("plain\ntext\n", 0))
        crlf = self.hunk(["A"], [], ["B"]).replace("\n", "\r\n")  # markers it cannot read: nothing joined
        self.assertEqual(mergeauto.union(crlf), (crlf, 0))

    def test_is_net(self):
        net = mergeauto.is_net
        self.assertTrue(net(128, "fatal: unable to access 'https://github.com/x/': Connection timed out after 300000 ms"))
        self.assertTrue(net(128, "fatal: the remote end hung up unexpectedly"))
        self.assertTrue(net(1, "error connecting to api.github.com"))
        self.assertTrue(net(1, 'Post "https://api.github.com/graphql": net/http: TLS handshake timeout'))
        self.assertTrue(net(None, ""))
        self.assertFalse(net(0, "timed out"))
        self.assertFalse(net(128, "remote: Permission denied\nfatal: Could not read from remote repository."))
        self.assertFalse(net(1, " ! [rejected]        x -> x (non-fast-forward)"))
        self.assertFalse(net(1, "HTTP 404: Not Found"))
        self.assertFalse(net(1, "merge conflict in a.txt"))


if __name__ == "__main__":
    unittest.main()
