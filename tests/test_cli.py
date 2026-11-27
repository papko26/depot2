import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers
from helpers import FAKE, HERE, slurp

TEAM = os.path.join(os.path.dirname(HERE), "depot2", "__main__.py")
BRIEF = """# Brief

## The five characters

1. **«Строгий строитель», the grumpy builder.**
   - wants speed
2. **«Любитель красок», the lover of colours.**
   - wants clean screens
3. **«Счетовод», the numbers lover.**
   - wants honesty
4. **«Летописец-натуралист», the storyteller.**
   - wants stories
5. **«Хозяйка копилки», the greedy one.**
   - wants money

## Inputs
"""
SEED = os.path.join(HERE, "seed-example.json")


class CliTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="team-cli-e2e-")
        self.state = os.path.join(self.dir, "state")
        for name, body in (("brief.md", BRIEF), ("seed.json", slurp(SEED)),
                           ("q.md", "### B49. Фильтры поиска\n**Рекомендую:** позже.\n")):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8") as f:
                f.write(body)
        os.environ.pop("FAKE_STRUCTURED", None)
        os.environ["FAKE_MODE"] = "ok"

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)
        os.environ.pop("FAKE_MODE", None)

    def team(self, *args, ok=True):
        p = subprocess.run([sys.executable, TEAM, "--state", self.state, "--set", "executor_bin=" + FAKE,
                            "--set", "bundle.decisions_glob=" + os.path.join(self.dir, "none-*.md"),
                            "--set", "deep_repo.clone=", "--set", "read_dirs=[]",
                            "--set", "brainstorm.topics_dir=" + os.path.join(self.dir, "topics"),
                            "--set", "orders.results_dir=" + os.path.join(self.dir, "orders")] + list(args),
                           capture_output=True, text=True)
        if ok and p.returncode != 0:
            raise AssertionError("depot2 %s failed (%d): %s %s" % (args, p.returncode, p.stdout, p.stderr))  # noqa: UP031
        return p

    def test_end_to_end(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        out = self.team("ledger").stdout
        self.assertIn("pend-0001", out)
        self.assertIn("amount=null", out)
        # Seeding runs once.
        self.assertEqual(self.team("seed", "--ledger", os.path.join(self.dir, "seed.json"), ok=False).returncode, 1)
        self.team("seed", "--portraits")
        self.assertIn("portrait v0001", self.team("show", "Краски").stdout)
        # The owner settles the old review: refund p1 for all, pay out, decide.
        out = self.team("refund", "all", "--round", "old-review", "--point", "p1", "--reason", "decided").stdout
        self.assertIn("Палитра, Краски +2", out)
        self.team("payout", "painter", "3", "--reason", "the dark theme won", "--settles", "pend-0001")
        self.assertNotIn("pend-0001", self.team("ledger").stdout)
        # A dry run calls nothing and writes nothing.
        out = self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"), "--dry-run").stdout
        self.assertIn("DRY RUN", out)
        self.assertEqual(sorted(os.listdir(os.path.join(self.state, "rounds"))), ["old-review"])
        out = self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md")).stdout
        self.assertIn("## Пункты по ставкам", out)
        self.assertIn("B49. Фильтры поиска", out)
        # Reflection only for those the owner touched (painter: refund + payout; keeper: refund).
        out = self.team("reflect").stdout
        self.assertIn("Строитель: nothing changed", out)
        self.assertIn("Палитра, Краски → portrait v0002", out)
        self.assertIn("Копилка → portrait v0002", out)
        out = self.team("gender", "Строитель", "f").stdout
        self.assertIn("Строитель: gender m → f", out)  # seeding wrote the default m
        self.assertIn("gender f", self.team("show", "builder").stdout)
        self.assertIn("Род: женский", self.team("show", "builder", "--spec").stdout)
        self.assertIn("gender f", self.team("show", "keeper").stdout)  # the seeding default
        cfg = json.loads(self.team("config").stdout)
        self.assertEqual((cfg["model"], cfg["effort"]), ("model-a", "xhigh"))
        self.assertIn("Счетовод: nothing changed", self.team("reflect").stdout)
        # The state is a git repo with one commit per mutating command, nothing left over.
        log = subprocess.run(["git", "-C", self.state, "log", "--oneline"], stdout=subprocess.PIPE,
                             text=True).stdout
        # seed, seed portraits, refund, payout, round, reflect, gender; the second
        # reflect had nothing to do
        self.assertEqual(len(log.splitlines()), 7)
        st = subprocess.run(["git", "-C", self.state, "status", "--porcelain"], stdout=subprocess.PIPE,
                            text=True).stdout
        self.assertEqual(st.strip(), "")

    def test_report_round_rebuilds_without_a_model(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"))
        rid = [d for d in os.listdir(os.path.join(self.state, "rounds")) if d != "old-review"][0]
        path = os.path.join(self.state, "rounds", rid, "report.md")
        saved = slurp(path)
        self.assertIn("Ставок нет.", saved)
        self.assertNotIn("размышлени", saved)
        os.remove(path)
        os.environ["FAKE_MODE"] = "crash"  # any model call would fail the command
        out = self.team("--set", "executor_bin=/nonexistent/claude", "report", "--round", rid).stdout
        self.assertEqual(slurp(path), saved)
        self.assertEqual(out, saved)
        st = subprocess.run(["git", "-C", self.state, "status", "--porcelain"], stdout=subprocess.PIPE,
                            text=True).stdout
        self.assertEqual(st.strip(), "")  # the same bytes as committed by the round
        p = self.team("report", "--round", "old-review", ok=False)  # seeded, never played here
        self.assertEqual(p.returncode, 1)

    def test_fine(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        out = self.team("fine", "all", "1", "--round", "old-review", "--point", "p1",
                        "--reason", "требовал(а) обратного").stdout
        self.assertIn("Строитель -1 → 4", out)
        self.assertIn("Палитра, Краски -1 → 2", out)
        out = self.team("fine", "Краски", "10", "--reason", "ещё").stdout
        self.assertIn("Палитра, Краски -2 (fine 10; the balance had only 2) → 0", out)
        self.assertEqual(self.team("fine", "builder", "0", "--reason", "x", ok=False).returncode, 1)
        out = self.team("ledger").stdout
        self.assertRegex(out, r"Палитра, Краски\s+0\s")
        self.assertRegex(out, r"Копилка\s+2\s")
        self.assertIn(" fine ", self.team("ledger", "--all").stdout)
        log = subprocess.run(["git", "-C", self.state, "log", "--oneline", "-2"], stdout=subprocess.PIPE,
                             text=True).stdout
        self.assertIn("team: fine painter 10 (ещё)", log)
        self.assertIn("team: fine all 1 old-review p1", log)

    def test_fatal_exit_code(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        os.environ["FAKE_MODE"] = "quota"
        p = self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"), ok=False)
        self.assertEqual(p.returncode, 2)
        self.assertIn("FATAL", p.stderr)
        self.assertIn("no stakes debited", p.stderr)
        p = self.team("seed", "--portraits", ok=False)
        self.assertEqual(p.returncode, 2)

    def test_resume_an_aborted_round(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        os.environ["FAKE_MODE"] = "quota"
        p = self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"), ok=False)
        self.assertEqual(p.returncode, 2)
        rid = [d for d in os.listdir(os.path.join(self.state, "rounds")) if d != "old-review"][0]
        self.assertIn(f"depot2 round --resume {rid}", p.stderr)
        os.environ["FAKE_MODE"] = "ok"
        p = self.team("round", "--resume", rid, "--kind", "ballot", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("goes on with the round's own kind, input and cast: no --kind", p.stderr)
        p = self.team("round", "--input", os.path.join(self.dir, "q.md"), ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("round needs --kind (or --resume ROUND_ID", p.stderr)
        self.assertIn(f"RESUME round {rid}", self.team("round", "--resume", rid, "--dry-run").stdout)
        out = self.team("round", "--resume", rid).stdout
        self.assertIn("## Пункты по ставкам", out)
        self.assertIn("раунд продолжен после фатальной ошибки", out)
        rj = json.loads(slurp(os.path.join(self.state, "rounds", rid, "round.json")))
        self.assertEqual(rj["status"], "done")
        st = subprocess.run(["git", "-C", self.state, "status", "--porcelain"], stdout=subprocess.PIPE,
                            text=True).stdout
        self.assertEqual(st.strip(), "")
        p = self.team("round", "--resume", rid, ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("is done: --resume goes on only with an aborted round", p.stderr)

    def test_refund_auto_on_an_old_round(self):
        """`refund all --round R --auto`: an older round's open stakes come back as a round's do
        now. auto_refund, not an owner action: nobody reflects; refund and forfeit then find nothing."""
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        self.team("seed", "--portraits")
        self.assertEqual(self.team("refund", "painter", "--round", "old-review", "--auto", ok=False).returncode, 1)
        self.assertEqual(self.team("refund", "painter", "--round", "old-review", ok=False).returncode, 1)  # no reason
        out = self.team("refund", "all", "--round", "old-review", "--auto").stdout
        self.assertIn("Палитра, Краски +2 auto (old-review p1", out)
        self.assertIn("3 stakes, 4 tokens back in old-review", out)
        data = json.loads(self.team("ledger", "--json", "--all").stdout)
        self.assertEqual(data["open_stakes"], [])
        self.assertEqual(data["balances"], {"builder": 5, "painter": 5, "counter": 5, "chronicler": 5, "keeper": 5})
        backs = [t for t in data["transactions"] if t["type"] == "auto_refund"]
        self.assertEqual([(t["character"], t["point"], t["amount"]) for t in backs],
                         [("painter", "p1", 2), ("keeper", "p1", 1), ("keeper", "p2", 1)])
        self.assertTrue(all(t["ref"].startswith("tx-") and t["reason"].startswith("auto:") for t in backs))
        self.assertNotIn("transactions", json.loads(self.team("ledger", "--json").stdout))
        out = self.team("reflect").stdout
        for name in ("Строитель", "Палитра, Краски", "Счетовод", "Перо, Летописец", "Копилка"):
            self.assertIn(f"{name}: nothing changed", out)  # the gate stays closed
        p = self.team("refund", "all", "--round", "old-review", "--reason", "x", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("came back by themselves when the round ended", p.stderr)
        p = self.team("forfeit", "keeper", "--round", "old-review", "--reason", "x", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("fine instead: depot2 fine", p.stderr)
        p = self.team("refund", "all", "--round", "old-review", "--auto", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("its 3 stakes came back already", p.stderr)
        log = subprocess.run(["git", "-C", self.state, "log", "--oneline", "-1"], stdout=subprocess.PIPE,
                             text=True).stdout
        self.assertIn("team: auto refund old-review (3 stakes, 4 tokens)", log)

    def test_no_paid_passes_left(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        p = self.team("round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"),
                      "--force-deep", "builder:1", "--dry-run", ok=False)
        self.assertEqual(p.returncode, 2)  # argparse: no such option
        self.assertIn("unrecognized arguments: --force-deep", p.stderr)
        cfg = json.loads(self.team("config").stdout)
        self.assertNotIn("deep_levels", cfg)
        self.assertNotIn("deep_dirs", cfg)

    def test_old_config_keys_are_ignored_with_a_warning(self):
        """An older state config may still have deep_levels and deep_dirs: no crash, one warning each;
        deep_dirs is the old name of read_dirs and keeps working until it is renamed."""
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        with open(os.path.join(self.state, "config.json"), "w") as f:
            json.dump({"deep_levels": {"1": {"price": 1, "max_tool_turns": 11}},
                       "deep_dirs": [self.dir], "free": {"max_tool_turns_r1": 10}}, f)
        p = subprocess.run([sys.executable, TEAM, "--state", self.state, "config"], capture_output=True,
                           text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = json.loads(p.stdout)
        self.assertNotIn("deep_levels", cfg)
        self.assertNotIn("deep_dirs", cfg)
        self.assertEqual(cfg["read_dirs"], [self.dir])
        self.assertEqual(cfg["free"]["max_tool_turns_r1"], 10)
        self.assertEqual(p.stderr.count("warning:"), 2)
        self.assertIn("deep_levels is obsolete (the paid passes are gone) and ignored; remove it", p.stderr)
        self.assertIn("deep_dirs is the old name of read_dirs; used as read_dirs; rename it", p.stderr)
        p = subprocess.run([sys.executable, TEAM, "--state", self.state, "--set", "deep_repo.clone=",
                            "--set", "bundle.decisions_glob=" + os.path.join(self.dir, "none-*.md"),
                            "round", "--kind", "ballot", "--input", os.path.join(self.dir, "q.md"), "--dry-run"],
                           capture_output=True, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        self.assertIn(f"  --add-dir <a snapshot of origin/main>\n  --add-dir {self.dir}\n", p.stdout)
        from depot2 import config
        warned = []
        cfg = config.load(self.state, ["deep_dirs=[\"/x\"]", "read_dirs=[\"/y\"]"], warn=warned.append)
        self.assertEqual(cfg["read_dirs"], ["/y"])
        self.assertEqual(len(warned), 3)
        self.assertIn("--set: deep_dirs is the old name of read_dirs, which is set too; deep_dirs ignored", warned[2])

    def test_overdraft_payout_refused_cleanly(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        p = self.team("refund", "builder", "--round", "old-review", "--reason", "x", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("no open stakes", p.stderr)
        p = self.team("payout", "nobody", "1", "--reason", "x", ok=False)
        self.assertEqual(p.returncode, 1)


if __name__ == "__main__":
    unittest.main()


class CharacterNames(unittest.TestCase):

    def test_display_and_lookup(self):
        from depot2 import characters
        self.assertEqual(characters.name("chronicler"), "Перо, Летописец")
        self.assertEqual(characters.display("builder"), "Строитель")
        for key in ("chronicler", "Летописец", "летописец", "Перо", "Перо, Летописец", "Летописец-натуралист"):
            self.assertEqual(characters.resolve(key), "chronicler")
        self.assertEqual(characters.label("chronicler"), "Перо, Летописец («Летописец-натуралист», летописец)")
        self.assertEqual(characters.name("painter"), "Палитра, Краски")
        for key in ("painter", "Краски", "Палитра", "Palette", "Палитра, Краски", "Любитель красок"):
            self.assertEqual(characters.resolve(key), "painter")
        self.assertEqual(characters.label("painter"), "Палитра, Краски («Любитель красок»)")
        self.assertEqual(characters.label("counter"), "Счетовод")

    def test_a_team_of_its_own_from_the_config(self):
        from depot2 import characters
        team = [{"id": "ana", "name": "Ана", "full": "Ана-тестер", "default_gender": "f", "spec_marker": "Ана-тестер",
                 "role": "tests first"},
                {"id": "bob", "name": "Боб", "full": "Боб", "default_gender": "m", "spec_marker": "Боб"}]
        try:
            characters.configure(team)
            self.assertEqual(characters.IDS, ["ana", "bob"])
            self.assertEqual(characters.resolve("Ана-тестер"), "ana")
            self.assertEqual(characters.role("ana"), "tests first")
            from depot2 import extras
            self.assertEqual(extras.majority(), 2)
            for bad in ([team[0]], [team[0], dict(team[0])], [dict(team[0], default_gender="x"), team[1]]):
                with self.assertRaises(ValueError):
                    characters.check_config(bad)
            characters.configure(None)
            self.assertEqual(characters.IDS, [])  # null: no team
            with self.assertRaises(characters.NoTeam):
                characters.require()
        finally:
            characters.configure(helpers.TEAM)
        self.assertEqual(len(characters.IDS), 5)
