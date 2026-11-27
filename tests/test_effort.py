
import json
import os
import subprocess
import sys
import unittest

from helpers import FAKE, TempState, slurp
from test_cli import TEAM, CliTest
from test_round import FakeRunner

from depot2 import characters, config, effort
from depot2.executors import CallSpec, describe
from depot2.executors.cli_agent import build_argv, run_call
from depot2.report import render
from depot2.round import Guard, RoundStop, run_round
from depot2.store import write_json


def walk(d, path=""):
    for k, v in d.items():
        if isinstance(v, dict):
            yield from walk(v, path + k + ".")
        else:
            yield path + k, v


class CalibrationTest(unittest.TestCase):
    def test_the_defaults(self):
        self.assertEqual(config.DEFAULTS["usage"]["mtok_per_pct"], 10.0)
        self.assertEqual(config.DEFAULTS["usage"]["usd_per_mtok"], 2.5)   # the CLI's ≈$ per MTok
        # every cap on the spend is MTok; no $ key is left in the config
        self.assertEqual([k for k, _ in walk(config.DEFAULTS) if "usd" in k and k != "usage.usd_per_mtok"], [])
        caps = {"{}.{}".format(*k): config.DEFAULTS[k[0]][k[1]] for k in config.MTOK_KEYS}
        self.assertEqual(caps, {"free.budget_mtok_r1": 0.16, "free.budget_mtok_r2": 0.2, "reflect.budget_mtok": 0.28,
                                "round.budget_mtok": 1.8, "solo.budget_mtok": None, "lock.budget_mtok": 0.6})

    def test_the_format(self):
        cfg = config.load("/nonexistent")
        self.assertEqual(effort.fmt(2.4, cfg), "0.24%UW (2.4 MTok)")  # a round cap of $6 in the old keys
        self.assertEqual(effort.fmt(10, cfg), "1%UW (10 MTok)")
        self.assertEqual(effort.fmt(0.16, cfg), "0.016%UW (0.16 MTok)")  # a call's cap never prints as 0
        self.assertEqual(effort.fmt(0.000468, cfg), "0.000047%UW (0.00047 MTok)")
        self.assertEqual(effort.fmt(0, cfg), "0%UW (0 MTok)")
        self.assertEqual(effort.fmt([100, 125], cfg), "10–12.5%UW (100–125 MTok)")
        self.assertEqual(effort.share(0.9, cfg), "90%UW (900 MTok)")
        self.assertEqual(effort.to_usd(0.16, cfg), 0.4)
        self.assertEqual(effort.from_usd(6, cfg), 2.4)

    def test_a_bad_calibration_is_refused(self):
        for bad in ("usage.usd_per_mtok=0", "usage.usd_per_mtok=\"x\"", "usage.mtok_per_pct=-1",
                    "round.budget_mtok=0", "free.budget_mtok_r1=\"a lot\"", "round.stop_at_utilization=0",
                    "round.stop_at_utilization=930"):
            with self.assertRaises(ValueError, msg=bad):
                config.load("/nonexistent", [bad], warn=None)
        self.assertIsNone(config.load("/nonexistent", ["solo.budget_mtok=null"])["solo"]["budget_mtok"])
        # a fuse given in percent can only mean that: read as a share, with a warning
        warned = []
        cfg = config.load("/nonexistent", ["round.stop_at_utilization=93"], warn=warned.append)
        self.assertEqual(cfg["round"]["stop_at_utilization"], 0.93)
        self.assertEqual(warned, ["round.stop_at_utilization 93 is a share of a window (0.9 = 90 %); read as 0.93"])


class OldDollarKeysTest(unittest.TestCase):
    """An older state config may still have the $ caps: read once, as MTok, with one warning each."""

    def setUp(self):
        self.t = TempState()

    def tearDown(self):
        self.t.close()

    def load(self, conf, sets=()):
        with open(os.path.join(self.t.dir, "config.json"), "w") as f:
            json.dump(conf, f)
        warned = []
        return config.load(self.t.dir, list(sets), warn=warned.append), warned

    def test_each_old_key_converted_once_with_a_warning(self):
        cfg, warned = self.load({"free": {"budget_usd_r1": 1.0, "budget_usd_r2": 1.5, "max_tool_turns_r1": 10},
                                 "round": {"budget_usd": 6}, "reflect": {"budget_usd": 0.7},
                                 "lock": {"budget_usd": 0.5}, "solo": {"budget_usd": None}})
        self.assertEqual((cfg["free"]["budget_mtok_r1"], cfg["free"]["budget_mtok_r2"], cfg["round"]["budget_mtok"],
                          cfg["reflect"]["budget_mtok"], cfg["lock"]["budget_mtok"], cfg["solo"]["budget_mtok"]),
                         (0.4, 0.6, 2.4, 0.28, 0.2, None))
        self.assertEqual(cfg["free"]["max_tool_turns_r1"], 10)
        self.assertEqual([k for k, _ in walk(cfg) if k.endswith("_usd") or "budget_usd" in k], [])
        self.assertEqual(len(warned), 6)
        self.assertIn("config.json: round.budget_usd is deprecated (the limits are in MTok now); read as "
                      "round.budget_mtok = 2.4: 0.24%UW (2.4 MTok) (6 / usage.usd_per_mtok 2.5); rename it", warned[3])
        self.assertIn("solo.budget_usd is deprecated; read as solo.budget_mtok = null; rename it", warned[4])

    def test_the_mtok_key_wins_and_the_rate_is_the_effective_one(self):
        cfg, warned = self.load({"round": {"budget_usd": 6, "budget_mtok": 3}})
        self.assertEqual(cfg["round"]["budget_mtok"], 3)
        self.assertIn("round.budget_usd is deprecated and round.budget_mtok is set too; round.budget_usd ignored",
                      warned[0])
        # a later layer wins either way: --set's new key over the file's old one, and its old one over the file's new
        cfg, _ = self.load({"round": {"budget_usd": 6}}, ["round.budget_mtok=5"])
        self.assertEqual(cfg["round"]["budget_mtok"], 5)
        cfg, warned = self.load({"round": {"budget_mtok": 5}}, ["round.budget_usd=10"])
        self.assertEqual(cfg["round"]["budget_mtok"], 4.0)
        self.assertTrue(warned[0].startswith("--set: round.budget_usd is deprecated"))
        # the rate is the effective usage.usd_per_mtok, whichever layer sets it
        cfg, _ = self.load({"round": {"budget_usd": 6}}, ["usage.usd_per_mtok=3"])
        self.assertEqual(cfg["round"]["budget_mtok"], 2.0)

    def test_the_cli_reads_them_and_says_so(self):
        with open(os.path.join(self.t.dir, "config.json"), "w") as f:
            json.dump({"round": {"budget_usd": 6}, "free": {"budget_usd_r1": 1.0}}, f)
        p = subprocess.run([sys.executable, TEAM, "--state", self.t.dir, "config"], stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, text=True)
        self.assertEqual(p.returncode, 0, p.stderr)
        cfg = json.loads(p.stdout)
        self.assertEqual((cfg["round"]["budget_mtok"], cfg["free"]["budget_mtok_r1"]), (2.4, 0.4))
        self.assertNotIn("budget_usd", cfg["round"])
        self.assertEqual(p.stderr.count("warning:"), 2)
        self.assertIn("round.budget_usd is deprecated", p.stderr)


class SpendTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load("/nonexistent")

    def test_a_calls_spend_is_its_tokens_weighted(self):
        res = {"usage": {"input": 3, "cache_write": 100, "cache_read": 900, "output": 50}, "cost_usd": 0.0123}
        self.assertAlmostEqual(effort.call_mtok(res, self.cfg), 468 / 1e6)  # 3 + 125 + 90 + 250
        self.assertFalse(effort.by_cost_only(res))
        # no tokens: the CLI's ≈$ / usage.usd_per_mtok; neither: 0
        self.assertAlmostEqual(effort.call_mtok({"usage": {}, "cost_usd": 0.5}, self.cfg), 0.2)
        self.assertTrue(effort.by_cost_only({"usage": {}, "cost_usd": 0.5}))
        self.assertEqual(effort.call_mtok({"usage": {}, "cost_usd": None}, self.cfg), 0.0)

    def test_a_costs_file_from_before_the_mtok(self):
        self.assertEqual(effort.costs_mtok({"mtok": 1.5, "total_usd": 99}, self.cfg), 1.5)
        old = {"total_usd": 0.5, "tokens": {"input": 0, "cache_write": 0, "cache_read": 1000000, "output": 0},
               "items": [{"usage": {"cache_read": 1000000}, "cost_usd": 0.3}, {"usage": {}, "cost_usd": 0.2}]}
        self.assertAlmostEqual(effort.costs_mtok(old, self.cfg), 0.1 + 0.08)  # its calls: tokens, else ≈$
        del old["items"]
        self.assertAlmostEqual(effort.costs_mtok(old, self.cfg), 0.1)  # its tokens
        self.assertAlmostEqual(effort.costs_mtok({"total_usd": 0.5, "tokens": 1234}, self.cfg), 0.2)  # a reflection's

    def test_the_cli_gets_the_cap_in_its_own_dollars(self):
        spec = CallSpec("r/builder-r1", "m", None, "/s", "u", {}, budget_mtok=0.16,
                        budget_usd=effort.to_usd(0.16, self.cfg))
        argv = build_argv(spec, "claude")
        self.assertEqual(argv[argv.index("--max-budget-usd") + 1], "0.40")

    def test_the_fake_cli_week_window_and_the_budget_message(self):
        d = TempState()
        try:
            sys_path = os.path.join(d.dir, "system.md")
            with open(sys_path, "w") as f:
                f.write("rules")
            sch = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"],
                   "additionalProperties": False}
            spec = CallSpec("t/builder-r1", "fake-model-1", None, sys_path, "hi", sch, budget_mtok=0.16,
                            budget_usd=0.4, timeout_s=20, cwd=d.dir)
            os.environ["FAKE_STRUCTURED"] = json.dumps({"x": 1})
            res = run_call(spec, executor_bin=FAKE)
            self.assertEqual((res["utilization"], res["week_utilization"]), (0.25, 0.25))
            line = describe(res, self.cfg)
            self.assertIn("0.000047%UW (0.00047 MTok)  1053 tok", line)
            self.assertNotIn("$", line)
            os.environ["FAKE_MODE"] = "budget"
            with self.assertRaises(Exception) as cm:
                run_call(spec, executor_bin=FAKE)
            self.assertIn("budget cap 0.16 MTok reached (--max-budget-usd 0.40)", str(cm.exception))
        finally:
            for k in ("FAKE_MODE", "FAKE_STRUCTURED"):
                os.environ.pop(k, None)
            d.close()


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.cfg = config.load("/nonexistent", ["round.budget_mtok=1.0", "round.max_calls=50"])

    def test_the_round_cap_in_mtok(self):
        g = Guard(self.cfg)
        g.check(0.6)
        g.record({"usage": {"cache_read": 5000000}})  # 0.5 MTok
        g.check(0.5)  # 0.5 + 0.5 = the cap: allowed
        with self.assertRaises(RoundStop) as cm:
            g.check(0.6)
        self.assertEqual(str(cm.exception), "cap: 0.05%UW (0.5 MTok) spent + 0.06%UW (0.6 MTok) for this call "
                                            "> 0.1%UW (1 MTok) per round")

    def test_the_fuse_is_a_share_of_the_window_at_the_same_calibration(self):
        g = Guard(config.load("/nonexistent", ["round.budget_mtok=10"]))
        g.record({"usage": {"cache_read": 6800000}, "utilization": 0.899, "week_utilization": 0.899})
        self.assertIsNone(g.stopped)
        g.check(0.2)  # 89.9 + 0.068 + 0.02 < 90
        with self.assertRaises(RoundStop) as cm:
            g.check(0.95)  # 89.9 + 0.068 + 0.095 ≥ 90: the call is refused before it runs
        self.assertIn("fuse: the window 89.9%UW + 0.068%UW (0.68 MTok) since + 0.095%UW (0.95 MTok) for this "
                      "call ≈ 90.06%UW ≥ the stop at 90%UW", str(cm.exception))
        # a new calibration moves the same MTok: at 27.2 MTok per 1 % the call fits
        g.cfg = config.load("/nonexistent", ["round.budget_mtok=10", "usage.mtok_per_pct=27.2"])
        g.check(0.95)
        # and any window over the stop still stops the round after the call that saw it
        g.record({"usage": {}, "utilization": 0.91})
        with self.assertRaises(RoundStop) as cm:
            g.check(0.0)
        self.assertIn("usage window 91% full (stop at 90%)", str(cm.exception))


class RoundInMtokTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.input = os.path.join(self.t.dir, "q.md")
        from helpers import BALLOT
        with open(self.input, "w") as f:
            f.write(BALLOT)

    def tearDown(self):
        self.t.close()

    def test_the_specs_the_costs_and_the_report(self):
        runner = FakeRunner(self.t.state, {})
        lines = []
        r = run_round(self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lines.append)
        r1 = [c[3] for c in runner.calls if c[1] == "r1"][0]
        r2 = [c[3] for c in runner.calls if c[1] == "r2"][0]
        self.assertEqual((r1.budget_mtok, r1.budget_usd), (0.16, 0.4))
        self.assertEqual((r2.budget_mtok, r2.budget_usd), (0.2, 0.5))
        costs = json.loads(slurp(os.path.join(r["dir"], "costs.json")))
        self.assertAlmostEqual(costs["mtok"], 0.04)  # 10 calls × ≈$0.01 / 2.5, no tokens
        self.assertEqual(costs["mtok_from_cost_calls"], 10)
        self.assertAlmostEqual(costs["total_usd"], 0.1)
        self.assertAlmostEqual(costs["by_model"]["model-a"]["mtok"], 0.04)
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["settings"]["usage"], {"mtok_per_pct": 10.0, "usd_per_mtok": 2.5})
        self.assertAlmostEqual(rj["mtok"], 0.04)
        self.assertEqual([l for l in lines if "$" in l], [])
        self.assertIn("0.004%UW (0.04 MTok); window 10% → 10%", lines[-1])
        self.assertIn("0.0004%UW (0.004 MTok)", lines[0])
        self.assertNotIn("$", r["report"])

    def test_an_old_round_reports_in_mtok(self):
        rid = "2015-11-03-discuss-solo-keeper"
        rdir = os.path.join(self.t.state.rounds_dir(), rid)
        os.makedirs(os.path.join(rdir, "answers"))
        write_json(os.path.join(rdir, "round.json"), {
            "id": rid, "kind": "discuss", "status": "done", "created": "2015-11-03T10:00:00Z", "only": "keeper",
            "points": [], "finals": {}, "balances_before": dict.fromkeys(characters.IDS, 5),
            "balances_after": dict.fromkeys(characters.IDS, 5),
            "settings": {"effort": "xhigh", "round": {"budget_usd": 0.9, "max_calls": 2}}})
        write_json(os.path.join(rdir, "costs.json"), {
            "calls": 1, "total_usd": 1.36, "unknown_cost_calls": 0, "duration_s": 30.0, "turns": 3, "tool_turns": 1,
            "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "total": 0},
            "utilization": {"before": 0.5, "after": 0.51}, "by_model": {"model-a": {"calls": 1, "usd": 1.36}},
            "items": [{"call": rid + "/keeper-r1", "cost_usd": 1.36, "usage": {}}]})
        rep = render(self.t.state, rid, self.t.cfg)
        self.assertIn("Капы — на одного: 0.036%UW (0.36 MTok), 2 вызова.", rep)  # its ≈$0.90 / 2.5
        self.assertIn("Расход 0.054%UW (0.54 MTok)", rep)  # its ≈$1.36 / 2.5, no tokens
        self.assertIn("у 1 вызовов без токенов — по оценке CLI / usage.usd_per_mtok", rep)
        self.assertNotIn("$", rep)


class ReflectionInMtokTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()

    def tearDown(self):
        self.t.close()

    def test_the_cap_the_costs_and_the_lines(self):
        from depot2.ledger import Ledger
        from depot2.reflect import reflect
        Ledger(self.t.state).payout("keeper", 1, "x")
        specs, lines = [], []

        def runner(spec):
            specs.append(spec)
            return {"call": spec.call_id, "model": spec.model, "effort": spec.effort, "duration_s": 0.1,
                    "cost_usd": 0.3, "usage": {"input": 0, "cache_write": 10000, "cache_read": 200000, "output": 3000},
                    "status": "ok", "reason": None, "utilization": None,
                    "structured": {"portrait": "Я Копилка.", "what_changed": "учла", "inner_thought": "хм"}}

        reflect(self.t.state, self.t.cfg, dry_run=True, out=lines.append)
        self.assertIn("no tools, cap 0.028%UW (0.28 MTok) each", "\n".join(lines))
        reflect(self.t.state, self.t.cfg, runner=runner, out=lines.append)
        self.assertEqual((specs[0].budget_mtok, specs[0].budget_usd), (0.28, 0.7))
        d = os.path.join(self.t.dir, "reflections")
        costs = json.loads(slurp(os.path.join(d, os.listdir(d)[0], "costs.json")))
        self.assertAlmostEqual(costs["mtok"], 0.0125 + 0.02 + 0.015)  # its tokens, not its ≈$0.30
        self.assertEqual(costs["total_usd"], 0.3)
        self.assertEqual([l for l in lines if "$" in l], [])
        self.assertIn("reflect: 1 calls, 213000 tokens, 0.0047%UW (0.048 MTok)", lines)


class TeamStackCliTest(unittest.TestCase):
    setUp = CliTest.setUp
    tearDown = CliTest.tearDown
    team = CliTest.team

    def test_the_round_and_the_dry_run(self):
        self.team("seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json"))
        q = os.path.join(self.dir, "q.md")
        out = self.team("round", "--kind", "ballot", "--input", q, "--dry-run").stdout
        self.assertIn("  round 1: 5 calls, up to 4 tool turns each, cap 0.016%UW (0.16 MTok), 900s", out)
        self.assertIn("round caps: 0.18%UW (1.8 MTok), 16 calls", out)
        self.assertNotIn("$", out.replace("--max-budget-usd", ""))
        out = self.team("--set", "round.budget_mtok=2.4", "round", "--kind", "ballot", "--input", q).stdout
        self.assertNotIn("$", out)
        self.assertRegex(out, r"round [0-9-]+ballot: 10 calls, 0 tool turns, 10530 tokens, \d+s, "
                              r"0.00047%UW \(0.0047 MTok\)")


if __name__ == "__main__":
    unittest.main()
