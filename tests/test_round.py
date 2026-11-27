import json
import os
import unittest

from fake_executor import minimal
from helpers import BALLOT, TempState, slurp

from depot2 import characters, schema
from depot2.bundle import parse_points
from depot2.executors import CallFailed, FatalError
from depot2.ledger import Ledger, LedgerError
from depot2.persona import Persona
from depot2.reflect import changes_since, reflect
from depot2.report import order_points, render
from depot2.round import RoundAborted, run_round, trim_to
from depot2.store import append_jsonl, write_json


def ans(points=(), main="главное", thought="мысль"):
    return {
        "points": [{"id": p[0], "position": p[1], "stake": p[2], "comment": p[3]} for p in points],
        "main_point": main,
        "inner_thought": thought,
    }


class FakeRunner:
    """Answers per (character, stage); records what the ledger said at each call. turns: the tool
    turns each (character, stage) reports having used (default 0).
    """

    def __init__(self, state, script, turns=None) -> None:
        self.state = state
        self.script = script
        self.turns = turns or {}
        self.calls = []

    def __call__(self, spec) -> dict[str, object]:
        cid, stage = spec.call_id.split("/")[1].rsplit("-", 1)
        self.calls.append((cid, stage, Ledger(self.state).balance(cid), spec))
        item = self.script.get((cid, stage))
        res = {
            "call": spec.call_id,
            "model": spec.model,
            "effort": spec.effort,
            "duration_s": 0.1,
            "cost_usd": 0.01,
            "usage": {},
            "num_turns": 2,
            "tool_turns": self.turns.get((cid, stage), 0),
            "tool_calls": 0,
            "utilization": 0.1,
            "status": "ok",
            "reason": None,
        }
        if isinstance(item, Exception):
            res.update(status="failed", reason=str(item))
            item.result = res
            raise item
        if item is None:
            item = minimal(spec.schema)  # the smallest answer that fits: no positions, no stakes
        errs = schema.validate(item, spec.schema)
        if errs:
            raise AssertionError(f"the test's answer does not fit the schema: {errs}")
        res["structured"] = item
        return res


class RoundTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 4, "painter": 5, "counter": 5, "chronicler": 10, "keeper": 1})
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)

    def tearDown(self):
        self.t.close()

    def play(self, script):
        runner = FakeRunner(self.t.state, script)
        r = run_round(self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None)
        return r, runner

    def test_points_parsed(self):
        pts = parse_points(BALLOT)
        self.assertEqual([p["id"] for p in pts], ["B49", "B50"])
        self.assertIn("вернуться после PR 2-3", pts[0]["recommendation"])
        self.assertEqual(pts[0]["question"], "Что значит фильтр поиска и когда к нему вернуться?")

    def test_report_orders_points_by_total_stake(self):
        r, _ = self.play(
            {
                ("builder", "r1"): ans([("B49", "agree", 1, "мало"), ("B50", "disagree", 3, "против")]),
                ("painter", "r1"): ans([("B50", "agree", 2, "за")]),
                ("counter", "r1"): ans([("B49", "agree", 0, "")]),
            }
        )
        rep = r["report"]
        self.assertLess(rep.index("B50. Обложки"), rep.index("B49. Пункт 40"))
        self.assertIn("**1. B50. Обложки: замена устаревшей (ставка токенов: 5)**", rep)
        self.assertIn("- Строитель: не согласен (ставка токенов: 3): против", rep)
        self.assertIn("- Палитра, Краски: согласна (ставка токенов: 2): за", rep)
        self.assertIn("- Рекомендую: одна общая кнопка «обновить».", rep)
        led = Ledger(self.t.state)
        self.assertEqual(len([e for e in led.entries() if e["type"] == "stake"]), 3)
        self.assertEqual(led.open_stakes(), [])  # they came back when the round ended
        self.assertEqual((led.balance("builder"), led.balance("painter")), (4, 5))
        self.assertIn("## Ставки", rep)

    def test_stakes_come_back_when_the_round_ends(self):
        before = Ledger(self.t.state).balances()
        r, runner = self.play(
            {
                ("builder", "r1"): ans([("B49", "agree", 1, "мало"), ("B50", "disagree", 3, "против")]),
                ("painter", "r1"): ans([("B50", "agree", 2, "за")]),
            }
        )
        # Round 2 saw the balance before any stake: nothing is debited during the round.
        self.assertEqual({c[0]: c[2] for c in runner.calls if c[1] == "r2"}, before)
        led = Ledger(self.t.state)
        mine = [e for e in led.entries() if e.get("round") == r["id"]]
        self.assertEqual(
            [(e["type"], e["character"], e["amount"]) for e in mine],
            [
                ("stake", "builder", -1),
                ("stake", "builder", -3),
                ("stake", "painter", -2),
                ("auto_refund", "builder", 1),
                ("auto_refund", "builder", 3),
                ("auto_refund", "painter", 2),
            ],
        )
        stakes = [e for e in mine if e["type"] == "stake"]
        backs = [e for e in mine if e["type"] == "auto_refund"]
        self.assertEqual([e["ref"] for e in backs], [e["id"] for e in stakes])
        self.assertEqual([e["point"] for e in backs], ["B49", "B50", "B50"])
        self.assertTrue(all(e["reason"].startswith("auto: stakes come back when the round ends") for e in backs))
        self.assertEqual(led.balances(), before)
        self.assertEqual(led.open_stakes(), [])
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["auto_refunds"], [e["id"] for e in backs])
        self.assertEqual(rj["balances_after"], before)
        self.assertEqual(rj["ledger_seq"], backs[-1]["seq"])
        self.assertNotIn("deep_levels", rj["settings"])
        rep = r["report"]
        said = "Ставки: 6 ток.; все вернулись, когда раунд закончился"
        self.assertIn(said, rep.split("## Команда")[0])  # the header
        self.assertIn(said, rep.split("## Ставки")[1])  # the table
        self.assertIn("| Строитель | 4 | B49: 1, B50: 3 | 4 | 4 | 0 | 0 | 0 | 4 |", rep)
        self.assertIn("**1. B50. Обложки: замена устаревшей (ставка токенов: 5)**", rep)
        self.assertNotIn("## После раунда", rep)  # an auto refund is not the owner's
        # Nothing is left for the owner to refund or forfeit.
        with self.assertRaises(LedgerError):
            led.refund_stakes(None, r["id"], reason="x")
        with self.assertRaises(LedgerError):
            led.forfeit_stakes("builder", r["id"], reason="x")

    def test_order_points_is_stable_on_ties(self):
        pts = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        fin = {"builder": {"b": {"stake": 2}, "c": {"stake": 2}}}
        self.assertEqual([p["id"] for p, _ in order_points(pts, fin)], ["b", "c", "a"])

    def test_stakes_trimmed_to_the_balance(self):
        """The balance still caps a round's total stakes, and the code cuts the smallest first."""
        r, _ = self.play({("builder", "r1"): ans([("B49", "agree", 1, "длинно " * 40), ("B50", "agree", 4, "важно")])})
        led = Ledger(self.t.state)
        staked = [(e["point"], -e["amount"]) for e in led.entries() if e["type"] == "stake"]
        self.assertEqual(staked, [("B50", 4)])
        self.assertEqual(led.balance("builder"), 4)  # and the 4 came back
        self.assertIn("- Строитель: ставки урезаны до баланса (B49: 1 → 0).", r["report"])

    def test_trim_cuts_smallest_first(self):
        pos = {"a": {"stake": 1, "comment": "x"}, "b": {"stake": 4, "comment": "y"}, "c": {"stake": 2, "comment": "z"}}
        notes = trim_to(pos, 4, ["a", "b", "c"], 140)
        self.assertEqual((pos["a"]["stake"], pos["b"]["stake"], pos["c"]["stake"]), (0, 4, 0))
        self.assertEqual(notes, ["a: 1 → 0", "c: 2 → 0"])

    def test_round2_revises_and_rebuts(self):
        r, runner = self.play(
            {
                ("builder", "r1"): ans([("B49", "agree", 1, "")]),
                ("counter", "r2"): {
                    "points": [{"id": "B49", "position": "disagree", "stake": 2, "comment": "задел — это скрипт"}],
                    "rebuttals": [{"to": "builder", "point": "B49", "text": "не согласен"}],
                    "inner_thought": "посмотрим",
                },
            }
        )
        self.assertIn("- Счетовод → Строитель (B49): не согласен", r["report"])
        self.assertIn("Счетовод: не согласен (ставка токенов: 2)", r["report"])
        r2 = [c for c in runner.calls if c[1] == "r2"]
        self.assertEqual(len(r2), 5)
        # Round 2 shares one system prompt with the round-1 table in it.
        systems = {c[3].system_path for c in r2}
        self.assertEqual(len(systems), 1)
        self.assertIn("Итоги раунда 1", slurp(systems.pop()))

    def test_round1_system_prompt_is_shared(self):
        _, runner = self.play({})
        r1 = [c for c in runner.calls if c[1] == "r1"]
        self.assertEqual(len({c[3].system_path for c in r1}), 1)
        self.assertEqual(len({c[3].user for c in r1}), 5)
        self.assertTrue(all(c[3].tools == ["Read", "Grep", "Glob"] for c in r1))
        self.assertTrue(all(c[3].max_tool_turns == 4 for c in r1))
        self.assertTrue(all((c[3].model, c[3].effort) == ("model-a", "xhigh") for c in r1))
        r2 = [c for c in runner.calls if c[1] == "r2"]
        self.assertTrue(all(c[3].max_tool_turns == 2 for c in r2))

    def test_fatal_error_aborts_without_stakes(self):
        with self.assertRaises(RoundAborted):
            self.play(
                {
                    ("builder", "r1"): ans([("B49", "agree", 2, "x")]),
                    ("painter", "r1"): FatalError("API error 429: usage limit"),
                }
            )
        led = Ledger(self.t.state)
        self.assertEqual([e for e in led.entries() if e["type"] == "stake"], [])
        rounds = os.listdir(self.t.state.rounds_dir())
        rj = json.loads(slurp(os.path.join(self.t.state.rounds_dir(), rounds[0], "round.json")))
        self.assertEqual(rj["status"], "aborted")

    def test_round_budget_cap_skips_calls(self):
        self.t.cfg["free"]["budget_mtok_r1"] = self.t.cfg["free"]["budget_mtok_r2"] = 0.4
        # a call spends 0.004 MTok (its ≈$0.01 / 2.5: no tokens) + the next cap 0.4: two fit
        self.t.cfg["round"]["budget_mtok"] = 0.406
        r, runner = self.play({})
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("пропущен", r["report"])

    def test_dry_run_makes_no_calls(self):
        lines = []
        runner = FakeRunner(self.t.state, {})
        out = run_round(self.t.state, self.t.cfg, "ballot", self.input, dry_run=True, runner=runner, out=lines.append)
        self.assertIsNone(out)
        self.assertEqual(runner.calls, [])
        self.assertFalse(os.path.exists(self.t.state.rounds_dir()))
        self.assertTrue(any("identical for every character" in l for l in lines))  # noqa: E741
        self.assertIn("every pass reads the same --add-dir list (round 1 and round 2):", lines)
        self.assertIn("  --add-dir <a snapshot of origin/main>", lines)
        self.assertTrue(any("then at once returned (auto_refund)" in l for l in lines))  # noqa: E741
        self.assertFalse(any("paid" in l or "level" in l for l in lines))  # noqa: E741

    def test_thoughts_are_kept(self):
        self.play({("chronicler", "r1"): ans(thought="каталог сверяют раз в год")})
        from depot2.persona import Persona

        t = Persona(self.t.state, "chronicler").thoughts()
        self.assertEqual(t[0]["thought"], "каталог сверяют раз в год")


class ResumeTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 4, "painter": 5, "counter": 5, "chronicler": 10, "keeper": 1})
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)

    def tearDown(self):
        self.t.close()

    def abort(self, script):
        with self.assertRaises(RoundAborted) as cm:
            run_round(
                self.t.state,
                self.t.cfg,
                "ballot",
                self.input,
                runner=FakeRunner(self.t.state, script),
                out=lambda *a: None,
            )
        rid = os.listdir(self.t.state.rounds_dir())[0]
        return rid, str(cm.exception)

    def resume(self, rid, script=None, **kw):
        runner = FakeRunner(self.t.state, script or {})
        lines = []
        r = run_round(self.t.state, self.t.cfg, None, resume=rid, runner=runner, out=lines.append, **kw)
        return r, runner, lines

    def test_a_fatal_error_in_round_1_goes_on_with_the_missing_calls_only(self):
        rid, msg = self.abort(
            {
                ("builder", "r1"): ans([("B49", "agree", 2, "x")], thought="одна мысль"),
                ("painter", "r1"): FatalError("API error 400: the message was refused"),
            }
        )
        self.assertIn(f"depot2 round --resume {rid} calls only for the missing ones", msg)
        # the input file may be gone by now: the round goes on from its saved bundle
        os.remove(self.input)
        r, runner, _ = self.resume(rid, {("painter", "r1"): ans([("B50", "agree", 1, "y")])})
        self.assertEqual(r["id"], rid)
        calls = [(c[0], c[1]) for c in runner.calls]
        self.assertEqual([c for c in calls if c[1] == "r1"], [(c, "r1") for c in characters.IDS if c != "builder"])
        self.assertEqual(len([c for c in calls if c[1] == "r2"]), 5)
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["status"], "done")
        self.assertNotIn("reason", rj)
        self.assertEqual(len(rj["resumed"]), 1)
        self.assertEqual(rj["resumed"][0]["kept"], ["builder-r1"])
        self.assertIn("the message was refused", rj["resumed"][0]["reason"])
        self.assertEqual((rj["finals"]["builder"]["B49"]["stake"], rj["finals"]["painter"]["B50"]["stake"]), (2, 1))
        self.assertEqual(rj["calls"], 2 + 4 + 5)  # the aborted pass's two (one ok, the fatal one) count too
        self.assertIn("раунд продолжен после фатальной ошибки", r["report"])
        # stakes debited once, when the round ended; the kept answer's thought kept once
        led = Ledger(self.t.state)
        self.assertEqual(
            [(e["character"], e["amount"]) for e in led.entries() if e["type"] == "stake"],
            [("builder", -2), ("painter", -1)],
        )
        from depot2.persona import Persona

        self.assertEqual([t["thought"] for t in Persona(self.t.state, "builder").thoughts()].count("одна мысль"), 1)
        with self.assertRaises(ValueError) as cm:  # done: nothing to go on with
            self.resume(rid)
        self.assertIn("is done", str(cm.exception))

    def test_a_fatal_error_in_round_2_keeps_round_1_and_asks_the_same(self):
        rid, _ = self.abort(
            {("builder", "r1"): ans([("B49", "agree", 2, "x")]), ("painter", "r2"): FatalError("API error 500")}
        )
        rdir = os.path.join(self.t.state.rounds_dir(), rid)
        asked = slurp(os.path.join(rdir, "prompts", "painter-r2.user.md"))
        r, runner, _ = self.resume(rid)
        self.assertEqual([(c[0], c[1]) for c in runner.calls], [(c, "r2") for c in characters.IDS if c != "builder"])
        self.assertEqual(runner.calls[0][3].user, asked)  # the same bundle, the same round-1 table
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["status"], "done")
        self.assertEqual(sorted(rj["resumed"][0]["kept"]), sorted([f"{c}-r1" for c in characters.IDS] + ["builder-r2"]))

    def test_aborted_again_then_resumed_again(self):
        rid, _ = self.abort({("painter", "r1"): FatalError("API error 529: overloaded")})
        with self.assertRaises(RoundAborted):
            self.resume(rid, {("counter", "r1"): FatalError("API error 529: overloaded")})
        r, runner, _ = self.resume(rid)
        self.assertEqual(
            [(c[0], c[1]) for c in runner.calls if c[1] == "r1"],
            [("counter", "r1"), ("chronicler", "r1"), ("keeper", "r1")],
        )
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual([len(x["kept"]) for x in rj["resumed"]], [1, 2])

    def test_the_dry_run_says_what_stays_and_what_is_called(self):
        rid, _ = self.abort({("builder", "r1"): ans(), ("painter", "r1"): FatalError("API error 500")})
        path = os.path.join(self.t.state.rounds_dir(), rid, "round.json")
        before = slurp(path)
        r, runner, lines = self.resume(rid, dry_run=True)
        self.assertIsNone(r)
        self.assertEqual(runner.calls, [])
        self.assertEqual(slurp(path), before)
        text = "\n".join(lines)
        self.assertIn(f"RESUME round {rid} (ballot)", text)
        self.assertIn("kept from the aborted pass: builder-r1", text)
        self.assertIn("round 1: calls for Палитра, Краски, ", text)
        self.assertIn("spent so far: 2 calls", text)

    def test_only_an_aborted_round_and_no_brainstorm(self):
        with self.assertRaises(ValueError):
            self.resume("2015-01-01-ballot")
        rid, _ = self.abort({("builder", "r1"): FatalError("API error 500")})
        path = os.path.join(self.t.state.rounds_dir(), rid, "round.json")
        rj = json.loads(slurp(path))
        rj["brainstorm_of"] = {"id": "bs-0001", "initiator": "counter", "round": "x"}
        write_json(path, rj)
        with self.assertRaises(ValueError) as cm:
            self.resume(rid)
        self.assertIn("brainstorm", str(cm.exception))


class StakeReportTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 4, "painter": 5, "counter": 5, "chronicler": 10, "keeper": 1})
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)

    def tearDown(self):
        self.t.close()

    def play(self, script, turns=None):
        runner = FakeRunner(self.t.state, script, turns)
        return run_round(self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None)

    def mixed(self):
        return self.play(
            {
                ("painter", "r1"): ans([("B49", "agree", 2, "прочитала: задел есть"), ("B50", "agree", 1, "да")]),
                ("painter", "r2"): {
                    "points": [{"id": "B50", "position": "disagree", "stake": 1, "comment": "передумала"}],
                    "rebuttals": [],
                    "inner_thought": "ок",
                },
                ("builder", "r1"): ans([("B50", "disagree", 1, "дорого")]),
                ("keeper", "r1"): ans([("B49", "agree", 1, "дёшево")]),
                ("chronicler", "r1"): ans([("B49", "agree", 2, "описание есть")]),
            },
            turns={("painter", "r1"): 4, ("builder", "r1"): 3, ("counter", "r1"): 1, ("chronicler", "r1"): 1},
        )

    def rows(self, rep, heading):
        part = next(x for x in rep[rep.index(heading) :].split("\n\n") if x.startswith("| Персонаж"))
        return {
            r.split(" | ")[0].strip("| "): [c.strip() for c in r.strip("|").split("|")] for r in part.splitlines()[2:]
        }

    def test_no_paid_thinking_in_the_report(self):
        rep = self.mixed()["report"]
        for gone in ("За размышления", "Размышления", "размышлени", "ур. ", "Платные", "бесплатно"):
            self.assertNotIn(gone, rep)
        self.assertIn("| Персонаж | Ходы | Главное |", rep)
        self.assertIn("| Палитра, Краски | 4/4 | главное |", rep)
        self.assertIn("| Строитель | 3/4 | главное |", rep)
        self.assertIn("| Копилка | 0/4 | главное |", rep)
        self.assertIn("- Палитра, Краски: согласна (ставка токенов: 2): прочитала: задел есть", rep)
        self.assertIn("- Палитра, Краски: не согласна (ставка токенов: 1): передумала", rep)
        # The owner's cost stays in the header.
        self.assertIn("Вызовов 10, ходов 20 (с инструментами 9)", rep)
        # no tokens from the fake runner: its ≈$0.01 per call / usage.usd_per_mtok 2.5 = 0.004 MTok
        self.assertIn(
            "Расход 0.004%UW (0.04 MTok): токены вызовов по весам (вход x1, запись кэша x1.25, чтение "
            "кэша x0.1, выход x5; 1%UW ≈ 10 MTok); у 10 вызовов без токенов — по оценке CLI / "
            "usage.usd_per_mtok. Окно расхода: 10% → 10%.",
            rep,
        )
        self.assertNotIn("$", rep.split("## Команда")[0])

    def test_turns_of_a_failed_or_skipped_pass(self):
        rep = self.play({("builder", "r1"): CallFailed("tool-turn cap 4 reached")}, turns={("builder", "r1"): 5})[
            "report"
        ]
        self.assertIn("| Строитель | 5/4, вызов упал | — |", rep)
        self.t.cfg["round"]["max_calls"] = 0
        rep = self.play({})["report"]
        self.assertIn("| Строитель | пропущено | — |", rep)

    def test_stake_table_matches_the_ledger(self):
        r = self.mixed()
        rep = r["report"]
        rows = self.rows(rep, "## Ставки")
        self.assertIn("| Персонаж | Было | Ставки | Поставлено | Вернулось | Штурм | Заказ | Замок | Осталось |", rep)
        led = Ledger(self.t.state)
        entries = [e for e in led.entries() if e.get("round") == r["id"]]
        for cid in characters.IDS:
            _name, before, stakes, staked, back, fee, order, lock, left = rows[characters.name(cid)]
            self.assertEqual((fee, order, lock), ("0", "0", "0"), cid)
            mine = [e for e in entries if e["character"] == cid]
            self.assertEqual(int(staked), -sum(e["amount"] for e in mine if e["type"] == "stake"), cid)
            self.assertEqual(
                sum(int(x.split(": ")[1]) for x in stakes.split(", ")) if stakes != "—" else 0, int(staked), cid
            )
            self.assertEqual(int(back), sum(e["amount"] for e in mine if e["type"] == "auto_refund"), cid)
            self.assertEqual(int(back), int(staked), cid)
            self.assertEqual(int(left), led.balance(cid), cid)  # what `depot2 ledger` prints
            self.assertEqual(int(left), int(before), cid)
        self.assertEqual(
            rows["Палитра, Краски"], ["Палитра, Краски", "5", "B49: 2, B50: 1", "3", "3", "0", "0", "0", "5"]
        )
        self.assertIn("Всего: поставлено 7 ток., вернулось 7 ток.; все в пределах балансов; сходится с журналом (", rep)

    def test_a_tampered_ledger_does_not_match(self):
        r = self.mixed()
        led = Ledger(self.t.state)
        txs = [json.loads(l) for l in slurp(led.path).splitlines()]  # noqa: E741
        back = next(t for t in txs if t["type"] == "auto_refund" and t["character"] == "chronicler")
        back["amount"] = 1
        with open(led.path, "w") as f:
            f.write("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in txs))
        self.assertIn("НЕ СХОДИТСЯ с журналом: Перо, Летописец", render(self.t.state, r["id"]))

    def test_after_the_round_shows_payouts_and_fines(self):
        r = self.mixed()
        led = Ledger(self.t.state)
        self.assertNotIn("## После раунда", r["report"])  # the owner has not touched it yet
        self.play({("chronicler", "r1"): ans([("B50", "agree", 3, "ещё раунд")])})  # the next round: nets 0
        led.payout("chronicler", 1, "толково", round_id=r["id"], point="B49")
        led.payout("painter", 2, "без раунда")
        for cid in characters.IDS:
            led.fine(cid, 1, "требовал(а) обратного", round_id=r["id"], point="B50")
        led.fine("keeper", 1, "ещё", round_id=r["id"])
        rep = render(self.t.state, r["id"])
        self.assertEqual(rep[: rep.index("## После раунда")].rstrip("\n"), r["report"].rstrip("\n"))
        rows = self.rows(rep, "## После раунда")
        self.assertEqual(rows["Палитра, Краски"], ["Палитра, Краски", "5", "0", "0", "-1", "+2", "6"])
        self.assertEqual(rows["Перо, Летописец"], ["Перо, Летописец", "10", "0", "+1", "-1", "0", "10"])
        self.assertEqual(rows["Копилка"], ["Копилка", "1", "0", "0", "-1", "0", "0"])
        for cid in characters.IDS:
            self.assertEqual(int(rows[characters.name(cid)][-1]), led.balance(cid), cid)  # `depot2 ledger`
        self.assertIn("Перо, Летописец: выплата +1 (B49): толково", rep)
        self.assertIn("Строитель: штраф -1 (B50): требовал(а) обратного", rep)
        self.assertIn("Копилка: штраф -0 из 1 (больше не было): ещё", rep)
        self.assertNotIn("возврат ставки", rep)  # the auto refunds are not the owner's
        self.assertIn("«Сейчас» сходится с журналом (баланс на tx-", rep)

    def test_report_round_rebuilds_the_report_without_a_model(self):
        r = self.mixed()
        path = os.path.join(r["dir"], "report.md")
        self.assertEqual(slurp(path), r["report"])
        os.remove(path)
        self.assertEqual(render(self.t.state, r["id"]), r["report"])  # reads files and the ledger only
        with self.assertRaises(ValueError):
            render(self.t.state, "no-such-round")


class OldRoundTest(unittest.TestCase):
    RID = "2015-11-03-ballot"

    def setUp(self):
        self.t = TempState()  # 5 tokens each: tx-000001 … tx-000005
        self.led = Ledger(self.t.state)
        bal = self.led.balances()
        txs = [
            ("think", "builder", -1, {"call": self.RID + "/builder-deep", "level": 1}),
            ("think", "painter", -2, {"call": self.RID + "/painter-deep", "level": 2}),
            ("think_refund", "painter", 2, {"ref": "tx-000007"}),
            ("stake", "builder", -2, {"point": "B49"}),
            ("stake", "painter", -1, {"point": "B50"}),
        ]
        for n, (typ, cid, amount, extra) in enumerate(txs, 6):
            bal[cid] += amount
            rec = {
                "seq": n,
                "id": "tx-%06d" % n,  # noqa: UP031
                "ts": "2015-11-03T12:00:00+02:00",
                "type": typ,
                "character": cid,
                "amount": amount,
                "balance_after": bal[cid],
                "round": self.RID,
            }
            rec.update(extra)
            append_jsonl(self.led.path, rec)
        rdir = os.path.join(self.t.state.rounds_dir(), self.RID)
        os.makedirs(os.path.join(rdir, "answers"))
        write_json(
            os.path.join(rdir, "round.json"),
            {
                "id": self.RID,
                "kind": "ballot",
                "created": "2015-11-03T11:00:00+02:00",
                "status": "done",
                "finished": "2015-11-03T12:00:00+02:00",
                "input": None,
                "sources": [],
                "cuts": [],
                "points": [
                    {"id": "B49", "title": "Поиск", "question": "Когда?", "recommendation": "позже"},
                    {"id": "B50", "title": "Обложки", "question": "Что?", "recommendation": "обновить"},
                ],
                "finals": {
                    "builder": {"B49": {"position": "agree", "stake": 2, "comment": "прочитал код"}},
                    "painter": {"B50": {"position": "agree", "stake": 1, "comment": "да"}},
                },
                "deep": [
                    {
                        "character": "builder",
                        "level": 1,
                        "reason": "читать код",
                        "price": 1,
                        "turns": 8,
                        "status": "готово",
                        "tx": "tx-000006",
                    },
                    {
                        "character": "painter",
                        "level": 2,
                        "reason": "и я",
                        "price": 2,
                        "turns": 12,
                        "status": "не удалось, возврат (tx-000008): cap",
                        "tx": "tx-000007",
                    },
                ],
                "settings": {
                    "model": "model-a",
                    "effort": "xhigh",
                    "free": {"max_tool_turns_r1": 4},
                    "deep_levels": {"1": {"price": 1, "max_tool_turns": 8}},
                },
                "balances_before": dict.fromkeys(characters.IDS, 5),
                "balances_after": bal,
                "stakes": ["tx-000009", "tx-000010"],
                "ledger_seq": 10,
                "repo_commit": "c" * 40,
            },
        )
        write_json(
            os.path.join(rdir, "costs.json"),
            {
                "calls": 2,
                "total_usd": 0.5,
                "unknown_cost_calls": 0,
                "duration_s": 60.0,
                "turns": 9,
                "tool_turns": 7,
                "tokens": {"input": 1, "cache_read": 2, "cache_write": 3, "output": 4, "total": 10},
                "utilization": {"before": 0.2, "after": 0.21},
                "by_model": {"model-a": {"calls": 2, "usd": 0.5}},
                "items": [
                    {"call": self.RID + "/builder-r1", "status": "ok", "tool_turns": 4},
                    {"call": self.RID + "/builder-deep", "status": "ok", "tool_turns": 3, "level": 1},
                ],
            },
        )
        write_json(os.path.join(rdir, "answers", "builder-r1.json"), {"main_point": "до чтения"})
        write_json(os.path.join(rdir, "answers", "builder-deep.json"), {"main_point": "после чтения"})

    def tearDown(self):
        self.t.close()

    def test_an_old_round_still_rebuilds(self):
        rep = render(self.t.state, self.RID)
        for gone in ("За размышления", "размышлени", "ур. 1", "Платные"):
            self.assertNotIn(gone, rep)
        self.assertIn("| Строитель | 4/4 | после чтения |", rep)  # an old paid answer still counts
        self.assertIn("Ставки: 3 ток.; списаны до решения владельца", rep)
        self.assertIn("| Строитель | 5 | B49: 2 | 2 | 0 | 0 | 0 | 0 | 2 |", rep)
        self.assertIn("| Палитра, Краски | 5 | B50: 1 | 1 | 0 | 0 | 0 | 0 | 4 |", rep)
        self.assertIn("Старые платные проходы (их отменили), учтены в «Осталось»: Строитель -1.", rep)
        self.assertIn("сходится с журналом (tx-000006 … tx-000010)", rep)
        self.assertIn("Проходы читали origin/main " + "c" * 40, rep)

    def test_the_owner_can_still_refund_and_forfeit_it(self):
        self.led.refund_stakes("builder", self.RID, reason="прав")
        self.led.forfeit_stakes("painter", self.RID, reason="нет")
        rep = render(self.t.state, self.RID)
        rows = StakeReportTest.rows(None, rep, "## После раунда")
        self.assertEqual(rows["Строитель"], ["Строитель", "2", "+2", "0", "0", "0", "4"])
        self.assertIn("Палитра, Краски: ставка 1 не возвращена (B50): нет", rep)
        self.assertIn("«Сейчас» сходится с журналом (баланс на tx-000012)", rep)

    def test_refund_auto_returns_the_open_stakes(self):
        calls = []

        def gate():
            return [cid for cid in characters.IDS if changes_since(self.t.state, cid) != ([], [])]

        txs = self.led.auto_refund_round(self.RID)
        self.assertEqual(
            [(t["type"], t["ref"], t["amount"]) for t in txs],
            [("auto_refund", "tx-000009", 2), ("auto_refund", "tx-000010", 1)],
        )
        self.assertEqual(gate(), [])  # not the owner's: nobody reflects
        reflect(self.t.state, self.t.cfg, runner=lambda spec: calls.append(spec), out=lambda *a: None)
        self.assertEqual(calls, [])
        rep = render(self.t.state, self.RID)
        self.assertIn("Ставки: 3 ток.; все вернулись, когда раунд закончился", rep)
        self.assertIn("| Строитель | 5 | B49: 2 | 2 | 2 | 0 | 0 | 0 | 4 |", rep)  # the old paid pass stays paid
        self.assertIn("| Палитра, Краски | 5 | B50: 1 | 1 | 1 | 0 | 0 | 0 | 5 |", rep)
        self.assertIn("сходится с журналом (tx-000006 … tx-000012)", rep)
        self.assertNotIn("## После раунда", rep)
        with self.assertRaises(LedgerError) as e:
            self.led.refund_stakes(None, self.RID, reason="x")
        self.assertIn("came back by themselves", str(e.exception))
        self.led.payout("builder", 1, "толково", round_id=self.RID, point="B49")
        rows = StakeReportTest.rows(None, render(self.t.state, self.RID), "## После раунда")
        self.assertEqual(rows["Строитель"], ["Строитель", "4", "0", "+1", "0", "0", "5"])
        self.assertEqual(gate(), ["builder"])  # the payout opens the gate


class ReflectionGateTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()

    def tearDown(self):
        self.t.close()

    def fake(self, spec):
        return {
            "call": spec.call_id,
            "model": spec.model,
            "effort": spec.effort,
            "duration_s": 0.1,
            "cost_usd": 0.02,
            "usage": {},
            "status": "ok",
            "reason": None,
            "utilization": None,
            "structured": {
                "portrait": "Я {}, новая версия.".format(spec.call_id.split("/")[1]),
                "what_changed": "учёл выплату",
                "inner_thought": "хм",
            },
        }

    def test_only_affected_characters_reflect(self):
        calls = []

        def runner(spec):
            calls.append(spec.call_id.split("/")[1])
            return self.fake(spec)

        led = Ledger(self.t.state)
        _status, skipped = reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(calls, [])  # nothing changed: no calls
        self.assertEqual(sorted(skipped), sorted(characters.IDS))
        led.payout("counter", 2, "exact numbers")
        led.stake("builder", 1, "r", "p")  # a stake is not an owner action
        reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(calls, ["counter"])
        from depot2.persona import Persona

        self.assertEqual(Persona(self.t.state, "counter").version(), 1)
        reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(calls, ["counter"])  # gated again until the owner acts
        from depot2.ledger import Decisions

        Decisions(self.t.state).add("all", "export after the reader")
        reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(sorted(calls[1:]), sorted(characters.IDS))
        self.assertEqual(Persona(self.t.state, "counter").version(), 2)
        self.assertEqual(changes_since(self.t.state, "counter"), ([], []))

    def test_a_rounds_auto_refunds_do_not_open_the_gate(self):
        calls = []

        def runner(spec):
            calls.append(spec.call_id.split("/")[1])
            return self.fake(spec)

        path = os.path.join(self.t.dir, "q.md")
        with open(path, "w") as f:
            f.write(BALLOT)
        script = {(cid, "r1"): ans([("B49", "agree", 2, "важно")]) for cid in characters.IDS}
        r = run_round(
            self.t.state, self.t.cfg, "ballot", path, runner=FakeRunner(self.t.state, script), out=lambda *a: None
        )
        self.assertEqual(len(Ledger(self.t.state).auto_refunds(r["id"])), 5)
        reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(calls, [])  # every stake came back, and nobody reflects for it
        Ledger(self.t.state).payout("painter", 1, "толково", round_id=r["id"], point="B49")
        reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertEqual(calls, ["painter"])  # a payout still opens it

    def test_failed_reflection_keeps_the_gate_open(self):
        Ledger(self.t.state).payout("keeper", 1, "x")

        def runner(spec):
            e = CallFailed("budget cap")
            e.result = {"call": spec.call_id, "status": "failed", "reason": "budget cap", "cost_usd": 0.5}
            raise e

        status, _ = reflect(self.t.state, self.t.cfg, runner=runner, out=lambda *a: None)
        self.assertTrue(status["keeper"].startswith("failed"))
        owner, _ = changes_since(self.t.state, "keeper")
        self.assertEqual(len(owner), 1)

    def test_reflection_dry_run(self):
        Ledger(self.t.state).payout("keeper", 1, "x")
        lines = []
        reflect(self.t.state, self.t.cfg, dry_run=True, runner=None, out=lines.append)
        self.assertTrue(any("DRY RUN" in l for l in lines))  # noqa: E741
        self.assertTrue(any("Копилка" in l for l in lines))  # noqa: E741


if __name__ == "__main__":
    unittest.main()


class ReviewRoundTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 6, "painter": 6, "counter": 6, "chronicler": 6, "keeper": 6})
        self.gh_calls = []

    def tearDown(self):
        self.t.close()

    def gh(self, args):
        self.gh_calls.append(args)
        self.assertIn(args[1], ("list", "view"))
        if "merged" in args:
            return [
                {"number": 123, "title": "Login fix", "mergedAt": "2015-11-02T17:59:24Z", "body": "B" * 5000},
                {"number": 100, "title": "old", "mergedAt": "2015-10-01T00:00:00Z", "body": "old"},
            ]
        return [{"number": 108, "title": "Reader 2", "isDraft": False, "body": "pages"}]

    def test_review_with_proposals(self):
        def r1(verdict, props):
            return {
                "points": [],
                "main_point": "главное",
                "inner_thought": "мысль",
                "verdict": verdict,
                "proposals": [{"title": t, "question": q, "stake": s} for t, q, s in props],
            }

        script = {
            ("builder", "r1"): r1("on_track", [("Время поиска #108", "замерить", 4)]),
            ("painter", "r1"): r1("drifting", [("Фильтры в меню", "убрать", 2)]),
            ("keeper", "r2"): {
                "points": [{"id": "N1", "position": "agree", "stake": 3, "comment": "да, срочно"}],
                "rebuttals": [],
                "inner_thought": "ок",
            },
        }
        runner = FakeRunner(self.t.state, script)
        r = run_round(
            self.t.state,
            self.t.cfg,
            "review",
            since="2015-11-01T00:00:00+02:00",
            runner=runner,
            out=lambda *a: None,
            gh=self.gh,
        )
        rep = r["report"]
        self.assertIn("| Персонаж | Вердикт | Ходы | Главное |", rep)
        self.assertIn("| Строитель | на курсе | 0/4 | главное |", rep)
        self.assertIn("| Палитра, Краски | дрейф | 0/4 | главное |", rep)
        self.assertEqual(Ledger(self.t.state).open_stakes(), [])  # a review returns its stakes too
        self.assertIn("**1. N1. Время поиска #108 (ставка токенов: 7)**", rep)
        self.assertIn("- Рекомендую: — (предложение: Строитель)", rep)
        self.assertLess(rep.index("N1. Время"), rep.index("N2. Фильтры"))
        bundle = slurp(os.path.join(r["dir"], "bundle.md"))
        self.assertIn("#123 Login fix", bundle)
        self.assertNotIn("#100 old", bundle)  # merged before the last review
        self.assertIn("тело PR обрезано", bundle)
        self.assertIn("#108 Reader 2", bundle)
        self.assertTrue(all(a[0] == "pr" for a in self.gh_calls))


TOPIC = """# Тема: всё хранение — в каталоге

## Что решено

- Старый импорт убираем.

## Вопросы для раунда

1. **Поля карточки по форматам.** Книга — ISBN. Что для журналов?
2. **Конвертеры экспорта.** Что во что превращает:
   - CSV (для таблиц!);
   - PDF.
3. **Когда делать** относительно фазы тестов?

Конец темы.
"""


class DiscussRoundTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState({"builder": 6, "painter": 6, "counter": 6, "chronicler": 6, "keeper": 6})
        self.input = os.path.join(self.t.dir, "topic.md")
        with open(self.input, "w") as f:
            f.write(TOPIC)

    def tearDown(self):
        self.t.close()

    def test_topic_questions_become_points(self):
        pts = parse_points(TOPIC)
        self.assertEqual([p["id"] for p in pts], ["Q1", "Q2", "Q3"])
        self.assertEqual(pts[1]["title"], "Конвертеры экспорта")
        self.assertIn("- CSV (для таблиц!);", pts[1]["question"])
        self.assertEqual(pts[2]["title"], "Когда делать")
        self.assertEqual(pts[0]["recommendation"], "")

    def test_discuss_round_answers_open_questions(self):
        def r1(points, props=()):
            return {
                "points": [{"id": i, "position": p, "stake": s, "comment": c} for i, p, s, c in points],
                "main_point": "главное",
                "inner_thought": "мысль",
                "proposals": [{"title": t, "question": q, "stake": s} for t, q, s in props],
            }

        script = {
            ("painter", "r1"): r1([("Q2", "none", 3, "CSV превращает карточки в строки таблицы")]),
            ("counter", "r1"): r1(
                [("Q2", "none", 1, "только по точным цифрам")], [("Миграция старого импорта", "база FORMAT 2", 1)]
            ),
            ("builder", "r1"): r1([("Q3", "none", 0, "после тестов")]),
        }
        runner = FakeRunner(self.t.state, script)
        r = run_round(self.t.state, self.t.cfg, "discuss", self.input, runner=runner, out=lambda *a: None)
        rep = r["report"]
        self.assertIn("**1. Q2. Конвертеры экспорта (ставка токенов: 4)**", rep)
        self.assertIn("- Палитра, Краски (ставка токенов: 3): CSV превращает карточки в строки таблицы", rep)
        self.assertIn("- Строитель: после тестов", rep)
        self.assertIn("- Рекомендую: — (открытый вопрос)", rep)
        self.assertIn("N1. Миграция старого импорта", rep)
        self.assertIn("Тема обсуждения", slurp(os.path.join(r["dir"], "bundle.md")))
        led = Ledger(self.t.state)
        self.assertEqual(led.open_stakes(), [])  # a discussion returns its stakes too
        self.assertEqual(len(led.auto_refunds(r["id"])), 3)
        self.assertEqual(led.balances(), dict.fromkeys(characters.IDS, 6))


class SnapshotAndCostTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)
        self.snap_dirs = []

    def tearDown(self):
        self.t.close()

    def snapshot(self, cfg, out, commit_wanted=None):
        self.wanted = commit_wanted
        import tempfile

        d = tempfile.mkdtemp(prefix="team-repo-test-")
        self.snap_dirs.append(d)
        return d, "a" * 40

    def test_every_pass_reads_the_one_snapshot(self):
        runner = FakeRunner(self.t.state, {})
        run_round(
            self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None, snapshot=self.snapshot
        )
        self.assertEqual(len(self.snap_dirs), 1)
        self.assertTrue(all(c[3].add_dirs == [self.snap_dirs[0]] for c in runner.calls))
        self.assertFalse(os.path.exists(self.snap_dirs[0]))

    def test_every_pass_has_the_same_add_dirs(self):
        import tempfile

        extra = [tempfile.mkdtemp(prefix="team-read-test-") for _ in range(2)]
        self.addCleanup(lambda: [os.rmdir(d) for d in extra])
        self.t.cfg["read_dirs"] = [*extra, os.path.join(self.t.dir, "no-such-dir")]
        runner = FakeRunner(self.t.state, {})
        r = run_round(
            self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None, snapshot=self.snapshot
        )
        want = [self.snap_dirs[0], *extra]
        self.assertEqual(sorted({c[1] for c in runner.calls}), ["r1", "r2"])
        for cid, stage, _, spec in runner.calls:
            self.assertEqual(spec.add_dirs, want, (cid, stage))
            self.assertEqual(spec.deny, self.t.cfg["deny_read"], (cid, stage))
            self.assertIn("Доступно: {}.".format(", ".join(want)), spec.user, (cid, stage))  # the task says so
        self.assertEqual(json.loads(slurp(os.path.join(r["dir"], "round.json")))["readable"], want)
        self.assertIn("Проходы читали origin/main " + "a" * 40, r["report"])

    def test_a_killed_call_marks_the_cost_approximate(self):
        def runner(spec):
            res = FakeRunner(self.t.state, {})(spec)
            if spec.call_id.endswith("painter-r1"):
                res.update(status="failed", reason="tool-turn cap 0 reached", cost_usd=None)
                raise CallFailed("tool-turn cap 0 reached", res)
            return res

        r = run_round(self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None)
        self.assertIn("(≈: у убитых вызовов — по потоку)", r["report"])
        self.assertIn("Расход 0.0036%UW (0.036 MTok)", r["report"])  # 9 x 0.004: the killed one had no tokens


class OnePrReviewTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.gh_calls = []

    def tearDown(self):
        self.t.close()

    def gh(self, args):
        self.gh_calls.append(args)
        self.assertEqual(args[:3], ["pr", "view", "125"])
        return {
            "number": 125,
            "title": "Catalogue import",
            "body": "the importer",
            "state": "OPEN",
            "isDraft": False,
            "headRefName": "catalogue-import",
            "baseRefName": "main",
            "headRefOid": "b" * 40,
            "additions": 10,
            "deletions": 1,
            "changedFiles": 2,
            "url": "https://example/125",
            "files": [
                {"path": "app/importer/run.py", "additions": 9, "deletions": 0},
                {"path": ".github/workflows/ci.yml", "additions": 1, "deletions": 1},
            ],
        }

    def test_review_of_one_pr(self):
        wanted = []

        def snapshot(cfg, out, commit_wanted=None):
            import tempfile

            wanted.append(commit_wanted)
            return tempfile.mkdtemp(prefix="team-repo-test-"), commit_wanted

        runner = FakeRunner(self.t.state, {})
        r = run_round(
            self.t.state,
            self.t.cfg,
            "review",
            pr=125,
            runner=runner,
            out=lambda *a: None,
            gh=self.gh,
            snapshot=snapshot,
        )
        self.assertEqual(len(self.gh_calls), 1)  # no merged/open lists for a one-PR review
        self.assertTrue(r["id"].endswith("-review-pr125"))
        bundle = slurp(os.path.join(r["dir"], "bundle.md"))
        self.assertIn("app/importer/run.py (+9 -0)", bundle)
        self.assertIn("the importer", bundle)
        self.assertEqual(wanted, ["b" * 40])  # every pass reads the PR's head, not origin/main
        self.assertIn("PR #125 «Catalogue import»", r["report"])
        self.assertIn("Проходы читали голову PR " + "b" * 40, r["report"])
        from depot2.round import last_review_since

        self.assertIsNone(last_review_since(self.t.state))  # one PR does not move the merge window

    def test_pr_needs_review_kind(self):
        with self.assertRaises(ValueError):
            run_round(
                self.t.state,
                self.t.cfg,
                "discuss",
                pr=1,
                runner=FakeRunner(self.t.state, {}),
                out=lambda *a: None,
                gh=self.gh,
            )


class GenderTest(unittest.TestCase):
    """The grammatical gender comes from the state (meta.json + spec.md), not from the code."""

    def setUp(self):
        self.t = TempState()
        for cid in characters.IDS:
            Persona(self.t.state, cid).set_gender(characters.BY_ID[cid]["default_gender"])
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)

    def tearDown(self):
        self.t.close()

    def everyone_agrees(self):
        script = {(cid, "r1"): ans([("B49", "agree", 0, ""), ("B50", "disagree", 0, "")]) for cid in characters.IDS}
        runner = FakeRunner(self.t.state, script)
        r = run_round(self.t.state, self.t.cfg, "ballot", self.input, runner=runner, out=lambda *a: None)
        return r["report"], runner

    def test_report_agrees_in_each_characters_gender(self):
        rep, _ = self.everyone_agrees()
        for line in (
            "- Строитель: согласен",
            "- Палитра, Краски: согласна",
            "- Счетовод: согласен",
            "- Перо, Летописец: согласен",
            "- Копилка: согласна",
            "- Копилка: не согласна",
            "- Перо, Летописец: не согласен",
        ):
            self.assertIn(line + "\n", rep)

    def test_gender_is_read_from_the_state(self):
        Persona(self.t.state, "builder").set_gender("f")
        Persona(self.t.state, "keeper").set_gender("m")
        rep, runner = self.everyone_agrees()
        self.assertIn("- Строитель: согласна\n", rep)
        self.assertIn("- Копилка: согласен\n", rep)
        spec = next(c[3] for c in runner.calls if c[0] == "builder" and c[1] == "r1")
        self.assertIn("Ты говоришь о себе в женском роде", spec.user)
        system = slurp(spec.system_path)
        self.assertIn("Строитель («Строгий строитель»): simple code, small budgets", system)
        self.assertIn(
            "She speaks of herself in the feminine", system.split("Строитель («Строгий строитель»)")[1].split("\n")[0]
        )

    def test_set_gender_keeps_one_line_in_the_spec(self):
        p = Persona(self.t.state, "painter")
        p.write_spec("# Краски\n\nспека\n\nКраски пишет о себе в женском роде.\n")
        p.set_gender("f")
        p.set_gender("f")
        spec = p.spec()
        self.assertEqual(spec.count("Род:"), 1)
        self.assertNotIn("Краски пишет о себе", spec)
        self.assertIn("Род: женский — пишет о себе в женском роде", spec)
        self.assertEqual(p.meta()["gender"], "f")
        with self.assertRaises(ValueError):
            p.set_gender("x")
