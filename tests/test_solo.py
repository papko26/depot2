import json
import os
import unittest

from helpers import slurp
from test_cli import CliTest
from test_extras import Base, order, proposal
from test_round import ans
from test_stack import lock

from depot2 import characters
from depot2.ledger import Ledger
from depot2.persona import Persona
from depot2.reflect import changes_since
from depot2.round import solo_caps
from depot2.stack import Ballots, Stack


class SoloRoundTest(Base):
    def stack_one(self):
        Stack(self.t.state, self.t.cfg).add("Поиск 3")

    def test_one_character_round_1_and_the_lock_pass_no_debate(self):
        self.stack_one()
        r, runner = self.play(
            {
                ("keeper", "r1"): dict(
                    ans([("B49", "agree", 3, "дорого, но да"), ("X1", "none", 2, "цена хостинга")]), order=order()
                ),
                ("keeper", "r3"): lock("st-0001"),
            },
            only="keeper",
        )
        self.assertEqual([(c[0], c[1]) for c in runner.calls], [("keeper", "r1"), ("keeper", "r3")])
        spec1 = runner.calls[0][3]
        self.assertNotIn("brainstorm_proposal", spec1.schema["properties"])  # a brainstorm needs the team
        self.assertIn("order", spec1.schema["properties"])
        self.assertIn("Это сольный раунд: владелец спрашивает только тебя.", spec1.user)
        self.assertNotIn("brainstorm_proposal", spec1.user)
        self.assertTrue(r["id"].endswith("-ballot-solo-keeper"))
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["only"], "keeper")
        # the caps scaled to one: max(1.8 / 5, 0.16 + 0.6) MTok, two calls
        self.assertEqual((rj["settings"]["round"]["budget_mtok"], rj["settings"]["round"]["max_calls"]), (0.76, 2))
        # the stakes debited and returned, hers only; the order and the lock debited
        led = Ledger(self.t.state)
        mine = [e for e in led.entries() if e.get("round") == r["id"]]
        self.assertEqual(
            [(e["type"], e["character"], e["amount"]) for e in mine],
            [
                ("stake", "keeper", -3),
                ("stake", "keeper", -2),
                ("auto_refund", "keeper", 3),
                ("auto_refund", "keeper", 2),
                ("order", "keeper", -5),
                ("lock", "keeper", -10),
            ],
        )
        self.assertEqual(led.balance("keeper"), 5)
        self.assertEqual(
            {cid: led.balance(cid) for cid in characters.IDS if cid != "keeper"},
            {cid: 20 for cid in characters.IDS if cid != "keeper"},
        )
        # thoughts: hers only
        self.assertEqual([cid for cid in characters.IDS if Persona(self.t.state, cid).thoughts(10)], ["keeper"])
        rep = r["report"]
        self.assertIn("# Команда: бюллетень, сольный раунд (Копилка), {}".format(r["id"]), rep)
        self.assertIn(
            "Сольный раунд: отвечает только Копилка; дебатов нет, мозговой штурм не созывается; ставки, "
            "журнал, выплаты, решения и рефлексия — как в полном раунде, только для неё. Капы — на одного: "
            "0.076%UW (0.76 MTok), 2 вызова.",
            rep,
        )
        self.assertEqual(list(self.rows(rep, "## Команда").keys()), ["Копилка"])
        self.assertEqual(
            self.rows(rep), {"Копилка": ["Копилка", "20", "B49: 3, X1: 2", "5", "5", "0", "-5", "-10", "5"]}
        )
        self.assertIn("- Копилка: согласна (ставка токенов: 3): дорого, но да", rep)
        self.assertNotIn("Строитель", rep)
        self.assertNotIn("## Дебаты", rep)
        self.assertNotIn("НЕ СХОДИТСЯ", rep)
        self.assertIn("Проход замков (раунд 3) прошёл: 1 вызовов.", rep)
        # the ballot queue knows it is a solo round
        self.assertEqual(Ballots(self.t.state).get(r["id"])["title"], "Бюллетень (соло: Копилка)")
        # the reflection gate: the owner's payout on the solo round opens hers, as in a full round
        self.assertEqual(changes_since(self.t.state, "keeper"), ([], []))
        rc, out, err = self.team(
            "payout", "keeper", "2", "--reason", "цена хостинга в точку", "--round", r["id"], "--point", "X1"
        )
        self.assertEqual(rc, 0, err)
        owner, _ = changes_since(self.t.state, "keeper")
        self.assertEqual([e["type"] for e in owner], ["payout"])
        rc, out, err = self.team("report", "--round", r["id"])
        self.assertIn("## После раунда", out)
        after = self.rows(out, "## После раунда")
        self.assertEqual(list(after.keys()), ["Копилка"])
        self.assertIn("сходится с журналом", out.split("## После раунда")[1])

    def test_a_resumed_solo_round_keeps_its_lock_pass(self):
        from depot2.executors import FatalError
        from depot2.round import RoundAborted

        self.stack_one()
        with self.assertRaises(RoundAborted):
            self.play({("keeper", "r1"): FatalError("API error 500")}, only="keeper")
        rid = os.listdir(self.t.state.rounds_dir())[0]
        r, runner = self.play({("keeper", "r3"): lock()}, kind=None, input_path=None, resume=rid)
        self.assertEqual([(c[0], c[1]) for c in runner.calls], [("keeper", "r1"), ("keeper", "r3")])
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual((rj["only"], rj["status"], rj["calls"], rj["locks"]["calls"]), ("keeper", "done", 3, 1))

    def test_no_stack_no_lock_pass_and_the_budget(self):
        r, runner = self.play({("counter", "r1"): ans()}, only="counter")
        self.assertEqual([(c[0], c[1]) for c in runner.calls], [("counter", "r1")])
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        # max(1.8 / 5, 0.16): one call, round 1 only
        self.assertEqual((rj["settings"]["round"]["budget_mtok"], rj["settings"]["round"]["max_calls"]), (0.36, 1))
        self.assertIsNone(rj["brainstorm"])
        self.assertEqual(solo_caps(dict(self.t.cfg, solo={"budget_mtok": 0.5}), 1), (0.5, 2))
        self.t.cfg["round"]["budget_mtok"] = 4.0  # the owner's state raises the caps: a fifth of them
        self.assertEqual(solo_caps(self.t.cfg, 1), (0.8, 2))

    def test_refused_and_the_dry_run(self):
        with self.assertRaises(ValueError):
            self.play({}, kind="brainstorm", brainstorm="bs-0001", only="keeper")
        with self.assertRaises(ValueError):
            self.play({}, only="nobody")
        self.stack_one()
        lines = []
        from depot2.round import run_round

        run_round(
            self.t.state,
            self.t.cfg,
            "discuss",
            self.input,
            dry_run=True,
            out=lines.append,
            only="painter",
            snapshot=self.snapshot,
        )
        text = "\n".join(lines)
        self.assertIn(
            "SOLO round: only Палитра, Краски answers (round 1 and the lock pass; no debate, no brainstorm)", text
        )
        self.assertIn("  round 1: 1 call, up to 4 tool turns each", text)
        self.assertIn("  round 2: none (a solo round: nobody to debate with)", text)
        self.assertIn("round caps: 0.076%UW (0.76 MTok), 2 calls (scaled to one character)", text)
        self.assertIn(
            "the fuse: stop at 90% of a usage window, and refuse a call when the window's last reading "
            "+ the spend since + its cap would reach 90%UW (900 MTok)",
            text,
        )
        self.assertIn("  round 1: 1 call, up to 4 tool turns each, cap 0.016%UW (0.16 MTok), 900s", text)
        self.assertNotIn("$", text.replace("--max-budget-usd", ""))
        self.assertIn("the lock pass (round 3): up to 1 call,", text)
        self.assertIn("not in a solo round", text)
        self.assertEqual(len([x for x in lines if "balance" in x and "system" in x]), 1)
        self.assertFalse(os.path.isdir(self.t.state.rounds_dir()))  # nothing written

    def test_a_full_round_is_unchanged(self):
        r, runner = self.play({("builder", "r1"): dict(ans(), brainstorm_proposal=proposal())})
        self.assertEqual([c[1] for c in runner.calls], ["r1"] * 5 + ["r2"] * 5)
        self.assertIn("brainstorm_proposal", runner.calls[0][3].schema["properties"])
        self.assertNotIn("сольный", r["report"])
        self.assertNotIn("only", json.loads(slurp(os.path.join(r["dir"], "round.json"))))


class SoloCliTest(unittest.TestCase):
    setUp = CliTest.setUp
    tearDown = CliTest.tearDown
    team = CliTest.team

    def test_a_team_stack_item_runs_a_solo_round(self):
        self.team(
            "seed", "--specs", os.path.join(self.dir, "brief.md"), "--ledger", os.path.join(self.dir, "seed.json")
        )
        q = os.path.join(self.dir, "q.md")
        out = self.team("tstack", "add", "--kind", "ballot", "--input", q, "--who", "Копилка").stdout
        self.assertIn("tm-0001 added to the team queue (1/4)", out)
        out = self.team("tstack", "start", "tm-0001").stdout
        self.assertIn(f"run: depot2 round --kind ballot --input {q} --only keeper --item tm-0001 --dry-run", out)
        p = self.team("round", "--kind", "ballot", "--input", q, "--item", "tm-0001", "--only", "builder", ok=False)
        self.assertEqual(p.returncode, 1)
        self.assertIn("tm-0001 is for Копилка alone, not Строитель", p.stderr)
        out = self.team("round", "--kind", "ballot", "--input", q, "--item", "tm-0001", "--dry-run").stdout
        self.assertIn("solo round: Копилка (from tm-0001)", out)
        self.assertIn("SOLO round: only Копилка", out)
        out = self.team("round", "--kind", "ballot", "--input", q, "--item", "tm-0001").stdout
        self.assertIn("# Команда: бюллетень, сольный раунд (Копилка)", out)
        rid = next(d for d in os.listdir(os.path.join(self.state, "rounds")) if d.endswith("-solo-keeper"))
        item = json.loads(self.team("tstack", "--json").stdout)["in_slots"][0]
        self.assertEqual((item["id"], item["round"]), ("tm-0001", rid))
        self.assertGreater(item["cost_usd"], 0)  # the CLI's ≈$, kept as data
        # from the round's own tokens (costs.json): 3 + 100 x 1.25 + 900 x 0.1 + 50 x 5 = 468 tokens
        self.assertEqual(item["spent_mtok"], 0.000468)
        self.assertIn(f"tm-0001: round {rid}, 0.000047%UW (0.00047 MTok)", out)
        self.assertNotIn("≈$", out)
        self.assertIn(f"tm-0001: round {rid}", out)
        costs = json.loads(slurp(os.path.join(self.state, "rounds", rid, "costs.json")))
        self.assertEqual(costs["calls"], 1)  # round 1 only: the stack of agents is empty, no lock pass
