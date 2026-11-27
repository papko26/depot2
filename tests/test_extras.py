
import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest

from helpers import BALLOT, TempState, slurp
from test_round import FakeRunner, ans

from depot2 import characters, extras, schema
from depot2 import cli as team
from depot2.executors import CallFailed, FatalError
from depot2.ledger import Ledger, LedgerError
from depot2.reflect import changes_since
from depot2.round import RoundAborted, run_round

TOPIC = """# Тема

## Вопросы для раунда

1. **Первый.** Что делать?
2. **Второй.** Когда?
"""


def proposal(topic="Поиск и фильтры", n=3):
    return {"topic": topic, "why": "к релизу всё ломается", "questions": ["Вопрос %d?" % i for i in range(1, n + 1)]}  # noqa: UP031


def order(command="node tools/bench/run.mjs --seeds 1-20", hardware="cpu", **kw):
    o = {"command": command, "seeds": "1-20", "hardware": hardware, "threshold": "p90 < 8 мс",
         "forecast": "6 мс", "why": "B49"}
    o.update(kw)
    return o


def r2(votes=(), order_=None, points=()):
    a = {"points": [{"id": p[0], "position": p[1], "stake": p[2], "comment": p[3]} for p in points],
         "rebuttals": [], "inner_thought": "ок"}
    if votes is not None:
        a["brainstorm_votes"] = [{"proposal": pid, "vote": v, "line": f"потому что {v}"} for pid, v in votes]
    if order_:
        a["order"] = order_
    return a


class Base(unittest.TestCase):
    balances = dict.fromkeys(characters.IDS, 20)

    def setUp(self):
        self.t = TempState(self.balances)
        self.out = tempfile.mkdtemp(prefix="team-extras-")
        self.t.cfg["brainstorm"]["topics_dir"] = os.path.join(self.out, "topics")
        self.t.cfg["orders"]["results_dir"] = os.path.join(self.out, "orders")
        self.input = os.path.join(self.t.dir, "q.md")
        with open(self.input, "w") as f:
            f.write(BALLOT)
        self.snaps = []

    def tearDown(self):
        self.t.close()
        shutil.rmtree(self.out, ignore_errors=True)

    def snapshot(self, cfg, out, commit_wanted=None):
        d = tempfile.mkdtemp(prefix="team-repo-test-")
        os.makedirs(os.path.join(d, "tools", "bench"))
        with open(os.path.join(d, "tools", "bench", "run.mjs"), "w") as f:
            f.write("// a bench run\n")
        self.snaps.append(d)
        return d, "a" * 40

    def play(self, script, kind="ballot", **kw):
        runner = FakeRunner(self.t.state, script)
        if kind == "brainstorm":
            kw.setdefault("input_path", None)
        else:
            kw.setdefault("input_path", self.input)
        r = run_round(self.t.state, self.t.cfg, kind, runner=runner, out=lambda *a: None,
                      snapshot=self.snapshot, **kw)
        return r, runner

    def rows(self, rep, heading="## Ставки"):
        part = next(x for x in rep[rep.index(heading):].split("\n\n") if x.startswith("| Персонаж"))
        return {r.split(" | ")[0].strip("| "): [c.strip() for c in r.strip("|").split("|")]
                for r in part.splitlines()[2:]}

    def team(self, *argv):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf), contextlib.redirect_stderr(io.StringIO()) as err:
            rc = team.main(["--state", self.t.dir, "--set", "orders.results_dir=" + self.t.cfg["orders"]["results_dir"],
                            "--set", "brainstorm.topics_dir=" + self.t.cfg["brainstorm"]["topics_dir"]] + list(argv))
        return rc, buf.getvalue(), err.getvalue()


class StandingQuestionsTest(Base):
    def gh_one(self, args):
        return {"number": 7, "title": "x", "body": "", "headRefOid": "b" * 40, "files": []}

    def gh_list(self, args):
        return []

    def test_x1_x2_in_every_kind(self):
        topic = os.path.join(self.t.dir, "topic.md")
        with open(topic, "w") as f:
            f.write(TOPIC)
        runs = [("ballot", {}), ("discuss", {"input_path": topic}),
                ("review", {"input_path": None, "since": "2015-11-01", "gh": self.gh_list}),
                ("review", {"input_path": None, "pr": 7, "gh": self.gh_one})]
        for kind, kw in runs:
            r, runner = self.play({}, kind, **kw)
            rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
            self.assertEqual([p["id"] for p in rj["points"]][-2:], ["X1", "X2"], kind)
            self.assertEqual(rj["points"][-1]["source"], "standing")
            spec = runner.calls[0][3]
            self.assertEqual(spec.schema["properties"]["points"]["items"]["properties"]["id"]["enum"][-2:], ["X1", "X2"])
            self.assertIn("X1 и X2 — постоянные вопросы каждого раунда", spec.user)
            rep = r["report"]
            self.assertIn("X1. Что упущено? (без ставок)**", rep, kind)
            self.assertIn("- Вопрос (развёрнуто): Где у нас меньше всего уверенности по теме раунда?", rep)
            self.assertEqual(rep.count("- Рекомендую: — (постоянный вопрос)"), 2)

    def test_standing_points_are_sorted_with_the_rest(self):
        r, _ = self.play({("builder", "r1"): ans([("X2", "none", 4, "в скорости поиска"), ("B49", "agree", 1, "")])})
        rep = r["report"]
        self.assertIn("**1. X2. Где меньше всего уверенности? (ставка токенов: 4)**", rep)
        self.assertIn("- Строитель (ставка токенов: 4): в скорости поиска", rep)

    def test_a_clash_with_the_input_is_an_error(self):
        with open(self.input, "a") as f:
            f.write("\n### X1. Чужой пункт\n**Рекомендую:** нет.\n")
        with self.assertRaises(ValueError):
            self.play({})


class BrainstormTest(Base):
    def unanimous(self, initiator="counter", script=None, n=3, vote=True):
        s = {(initiator, "r1"): dict(ans(), brainstorm_proposal=proposal(n=n))}
        for cid in characters.IDS:
            s[(cid, "r2")] = r2([("BR1", "yes")] if vote else None)
        s.update(script or {})
        return self.play(s)

    def test_a_unanimous_pass_debits_10_plus_4x5(self):
        r, runner = self.unanimous()
        led = Ledger(self.t.state)
        fees = [e for e in led.entries() if e["type"] == "brainstorm_fee"]
        self.assertEqual([(e["character"], e["amount"], e["role"]) for e in fees],
                         [("builder", -5, "member"), ("painter", -5, "member"), ("counter", -10, "initiator"),
                          ("chronicler", -5, "member"), ("keeper", -5, "member")])
        self.assertEqual(led.balances(), {"builder": 15, "painter": 15, "counter": 10, "chronicler": 15, "keeper": 15})
        b = extras.Brainstorms(self.t.state).get("bs-0001")
        self.assertEqual((b["status"], b["initiator"], b["round"]), ("approved", "counter", r["id"]))
        self.assertEqual(b["fees"], [e["id"] for e in fees])
        self.assertEqual(b["votes"]["keeper"], ["yes", "потому что yes"])
        # The topic file, in the discuss format, signed: its questions become the next round's points.
        topic = slurp(b["topic_file"])
        self.assertTrue(os.path.basename(b["topic_file"]).endswith("-brainstorm-poisk-i-filtry.md"))
        self.assertIn("Созвал: Счетовод (bs-0001", topic)
        self.assertIn("## Вопросы для раунда\n\n1. Вопрос 1?\n2. Вопрос 2?\n3. Вопрос 3?", topic)
        self.assertIn("— Счетовод", topic)
        from depot2.bundle import parse_points
        self.assertEqual([p["id"] for p in parse_points(topic)], ["Q1", "Q2", "Q3"])
        # Round 2 saw the proposal and the vote; the schema is the same for every character.
        r2_calls = [c[3] for c in runner.calls if c[1] == "r2"]
        self.assertEqual(len({json.dumps(c.schema, sort_keys=True) for c in r2_calls}), 1)
        self.assertIn("BR1. Поиск и фильтры (предлагает Счетовод)", slurp(r2_calls[0].system_path))
        self.assertIn("В `brainstorm_votes` проголосуй", r2_calls[0].user)
        # The report.
        rep = r["report"]
        self.assertIn("## Команда созывает мозговой штурм", rep)
        self.assertIn("**BR1. Поиск и фильтры** (предлагает Счетовод) — **одобрено: bs-0001**", rep)
        self.assertIn("- Копилка: да: потому что yes", rep)
        self.assertIn("- Взнос: Строитель 5, Палитра, Краски 5, Счетовод 10, Перо, Летописец 5, Копилка 5 — списан", rep)
        self.assertIn("`depot2 round --kind brainstorm --brainstorm bs-0001`", rep)
        self.assertEqual(self.rows(rep)["Счетовод"], ["Счетовод", "20", "—", "0", "0", "−10", "0", "0", "10"])
        self.assertIn("Кроме ставок: взнос за мозговой штурм 30 ток. (не возвращается).", rep)
        self.assertIn("сходится с журналом (", rep)
        self.assertNotIn("НЕ СХОДИТСЯ", rep)
        # The team's own act: nobody reflects for it.
        for cid in characters.IDS:
            self.assertEqual(changes_since(self.t.state, cid), ([], []), cid)

    def test_one_no_fails_it(self):
        r, _ = self.unanimous(script={("keeper", "r2"): r2([("BR1", "no")])})
        self.assertFalse([e for e in Ledger(self.t.state).entries() if e["type"] == "brainstorm_fee"])
        self.assertEqual(extras.Brainstorms(self.t.state).items(), [])
        self.assertIn("— не прошло", r["report"])
        self.assertIn("- Почему нет: Копилка: «нет»", r["report"])

    def test_one_who_cannot_pay_fails_it(self):
        Ledger(self.t.state).fine("keeper", 17, "тест")
        r, _ = self.unanimous()
        self.assertIn("- Почему нет: Копилка не может заплатить (3 < 5)", r["report"])
        self.assertFalse([e for e in Ledger(self.t.state).entries() if e["type"] == "brainstorm_fee"])

    def test_a_missing_debate_call_or_vote_fails_it(self):
        r, _ = self.unanimous(script={("painter", "r2"): CallFailed("tool-turn cap"),
                                      ("builder", "r2"): r2([])})
        self.assertIn("- Почему нет: Палитра, Краски не участвовала в раунде 2", r["report"])
        self.assertIn("- Почему нет: Строитель не голосовал", r["report"])

    def test_two_passing_proposals_run_only_one(self):
        s = {("keeper", "r1"): dict(ans(), brainstorm_proposal=proposal("Цена")),
             ("painter", "r1"): dict(ans(), brainstorm_proposal=proposal("Шрифты"))}
        for cid in characters.IDS:
            s[(cid, "r2")] = r2([("BR1", "yes"), ("BR2", "yes")])
        r, _ = self.play(s)
        self.assertEqual([b["initiator"] for b in extras.Brainstorms(self.t.state).items()], ["painter"])
        self.assertEqual(len([e for e in Ledger(self.t.state).entries() if e["type"] == "brainstorm_fee"]), 5)
        self.assertIn("**BR2. Цена** (предлагает Копилка) — прошло, но не выбрано", r["report"])
        self.assertIn("- Почему нет: прошло, но штурм за раунд один: выбрано BR1", r["report"])
        # The next time, the one who has called fewer goes first.
        r, _ = self.play(s)
        self.assertEqual([b["initiator"] for b in extras.Brainstorms(self.t.state).items()], ["painter", "keeper"])

    def test_an_incomplete_proposal_is_refused(self):
        r, runner = self.unanimous(n=2, vote=False)  # refused by code: nothing to vote on
        self.assertIn("**Поиск и фильтры** (предлагает Счетовод) — не прошло", r["report"])
        self.assertIn("- Почему нет: предложение отклонено: вопросов 2, нужно 3–5", r["report"])
        self.assertNotIn("brainstorm_votes", next(c[3] for c in runner.calls if c[1] == "r2").schema["properties"])

    def test_the_brainstorm_round_runs_on_its_topic(self):
        self.unanimous()
        before = Ledger(self.t.state).balances()
        r, _runner = self.play({("builder", "r1"): dict(ans([("Q2", "none", 3, "мой ответ")]), proposals=[])}, "brainstorm",
                              brainstorm="bs-0001")
        self.assertTrue(r["id"].endswith("-brainstorm"))
        rep = r["report"]
        self.assertTrue(rep.startswith("# Команда: мозговой штурм команды (созвал Счетовод), {}".format(r["id"])))
        self.assertIn("штурм bs-0001, одобрен в раунде", rep)
        self.assertIn("Тема мозгового штурма команды", slurp(os.path.join(r["dir"], "bundle.md")))
        self.assertEqual([p["id"] for p in json.loads(slurp(os.path.join(r["dir"], "round.json")))["points"]],
                         ["Q1", "Q2", "Q3", "X1", "X2"])
        self.assertIn("- Рекомендую: — (открытый вопрос)", rep)
        self.assertEqual(Ledger(self.t.state).balances(), before)  # its stakes came back; the fee stays spent
        self.assertEqual(extras.Brainstorms(self.t.state).get("bs-0001")["status"], "done")
        self.assertEqual(extras.Brainstorms(self.t.state).get("bs-0001")["ran_in"], r["id"])
        with self.assertRaises(ValueError):  # done: it does not run twice
            self.play({}, "brainstorm", brainstorm="bs-0001")
        with self.assertRaises(ValueError):
            self.play({}, "discuss", brainstorm="bs-0001")

    def test_the_fee_comes_back_only_on_a_fatal_error(self):
        self.unanimous()
        spent = Ledger(self.t.state).balances()
        with self.assertRaises(RoundAborted):
            self.play({("painter", "r1"): FatalError("API error 429: usage limit")}, "brainstorm", brainstorm="bs-0001")
        led = Ledger(self.t.state)
        backs = [e for e in led.entries() if e["type"] == "brainstorm_refund"]
        self.assertEqual(len(backs), 5)
        self.assertEqual(led.balances(), dict.fromkeys(characters.IDS, 20))
        b = extras.Brainstorms(self.t.state).get("bs-0001")
        self.assertEqual(b["status"], "failed")
        self.assertIn("fatal error", b["reason"])
        for cid in characters.IDS:
            self.assertEqual(changes_since(self.t.state, cid), ([], []), cid)
        self.assertNotEqual(spent, led.balances())

    def test_cancel_a_brainstorm_that_never_ran(self):
        self.unanimous()
        rc, out, _ = self.team("brainstorm", "list")
        self.assertIn("run: depot2 round --kind brainstorm --brainstorm bs-0001", out)
        rc, out, err = self.team("brainstorm", "cancel", "bs-0001", "--reason", "тема устарела")
        self.assertEqual(rc, 0, err)
        self.assertIn("bs-0001 cancelled; the fee came back", out)
        self.assertEqual(Ledger(self.t.state).balances(), dict.fromkeys(characters.IDS, 20))
        self.assertEqual(extras.Brainstorms(self.t.state).get("bs-0001")["status"], "cancelled")
        rc, _, err = self.team("brainstorm", "cancel", "bs-0001", "--reason", "ещё раз")
        self.assertEqual(rc, 1)
        self.assertIn("only an approved brainstorm", err)
        with self.assertRaises(LedgerError):  # a fee is refunded once
            led = Ledger(self.t.state)
            led.brainstorm_refund(led.get(extras.Brainstorms(self.t.state).get("bs-0001")["fees"][0]), "x")

    def test_majority_is_ready_behind_the_key(self):
        self.t.cfg["brainstorm"]["quorum"] = "majority"
        r, _ = self.unanimous(script={("keeper", "r2"): r2([("BR1", "no")]),
                                      ("builder", "r2"): CallFailed("cap")})
        self.assertIn("— **одобрено: bs-0001**", r["report"])  # 3 of 5 said yes
        self.assertEqual(len([e for e in Ledger(self.t.state).entries() if e["type"] == "brainstorm_fee"]), 5)


class OrdersTest(Base):
    def test_a_valid_order_is_queued_and_debited_after_the_stakes_came_back(self):
        r, _ = self.play({("builder", "r1"): dict(ans([("B49", "agree", 20, "всё на это")]), order=order())})
        led = Ledger(self.t.state)
        mine = [e["type"] for e in led.entries() if e["character"] == "builder"]
        self.assertEqual(mine, ["seed", "stake", "auto_refund", "order"])
        self.assertEqual(led.balance("builder"), 15)
        o = extras.Orders(self.t.state).get("ord-0001")
        self.assertEqual((o["status"], o["character"], o["script"], o["hardware"]),
                         ("queued", "builder", "tools/bench/run.mjs", "cpu"))
        rep = r["report"]
        self.assertIn("## Заказы", rep)
        self.assertIn("- ord-0001 Строитель (−5 ток.): `node tools/bench/run.mjs --seeds 1-20`; seeds 1-20; cpu; "
                      "порог: p90 < 8 мс; прогноз: 6 мс; зачем: B49.", rep)
        self.assertEqual(self.rows(rep)["Строитель"], ["Строитель", "20", "B49: 20", "20", "20", "0", "−5", "0", "15"])
        self.assertNotIn("НЕ СХОДИТСЯ", rep)
        self.assertEqual(changes_since(self.t.state, "builder"), ([], []))  # not an owner action

    def test_invalid_orders_are_refused_by_code(self):
        Ledger(self.t.state).fine("keeper", 17, "тест")  # 3 < 5
        r, _ = self.play({
            ("builder", "r1"): dict(ans(), order=order("node tools/bench/nope.mjs")),
            ("painter", "r1"): dict(ans(), order=order(hardware="gpu")),
            ("counter", "r1"): dict(ans(), order=order(forecast="")),
            ("chronicler", "r1"): dict(ans(), order=order()),
            ("chronicler", "r2"): r2(None, order()),
            ("keeper", "r1"): dict(ans(), order=order()),
        })
        rep = r["report"]
        self.assertIn("- отклонён, Строитель: `node tools/bench/nope.mjs` — нет такого скрипта в снимке: "
                      "tools/bench/nope.mjs.", rep)
        self.assertIn("- отклонён, Палитра, Краски: `node tools/bench/run.mjs --seeds 1-20` — hardware: только cpu "
                      "(другое — только с OK владельца).", rep)
        self.assertIn("- отклонён, Счетовод: `node tools/bench/run.mjs --seeds 1-20` — не заполнено: forecast.", rep)
        self.assertIn("- отклонён, Перо, Летописец: `node tools/bench/run.mjs --seeds 1-20` — второй заказ за раунд", rep)
        self.assertIn("- отклонён, Копилка: `node tools/bench/run.mjs --seeds 1-20` — не хватает токенов (3 < 5).", rep)
        self.assertEqual([o["character"] for o in extras.Orders(self.t.state).items()], ["chronicler"])
        for bad, why in (("node tools/x.mjs; rm -rf /", "без ;"), ("node lib/main.ts", "скрипт из tools/"),
                         ("node tools/../lib/x.mjs", "вне tools/")):
            self.assertIn(why, extras.check_order(order(bad), None, self.t.cfg)[1] or "")
        self.assertEqual(extras.check_order(order(hardware="tpu"), None, self.t.cfg)[1],
                         "нет снимка репозитория: скрипт не проверить")

    def test_done_settle_and_the_results_in_the_next_bundle(self):
        r, _ = self.play({("builder", "r1"): dict(ans(), order=order()),
                          ("painter", "r1"): dict(ans(), order=order()),
                          ("counter", "r1"): dict(ans(), order=order())})
        res = os.path.join(self.out, "run.json")
        with open(res, "w") as f:
            f.write('{"p90": 6.2}\n')
        rc, out, err = self.team("order", "settle", "ord-0001", "moved", "--reason", "рано")  # noqa: RUF059
        self.assertEqual(rc, 1)
        self.assertIn("only a done order is settled", err)
        for oid in ("ord-0001", "ord-0002", "ord-0003"):
            rc, _out, err = self.team("order", "done", oid, "--result", res, "--note", "nice 10, CPU")
            self.assertEqual(rc, 0, err)
        dest = os.path.join(self.t.cfg["orders"]["results_dir"], "ord-0001", "run.json")
        self.assertEqual(slurp(dest), '{"p90": 6.2}\n')
        self.assertEqual(self.team("order", "done", "ord-0001", "--result", res)[0], 1)  # done already
        # The owner settles: moved = the price back + 1, refund = the price back, burn = nothing.
        for oid, outcome in (("ord-0001", "moved"), ("ord-0002", "refund"), ("ord-0003", "burn")):
            self.assertEqual(self.team("order", "settle", oid, outcome, "--reason", "решено")[0], 0)
        led = Ledger(self.t.state)
        self.assertEqual([(e["character"], e["amount"], e["outcome"]) for e in led.entries() if e["type"] == "order_settle"],
                         [("builder", 6, "moved"), ("painter", 5, "refund"), ("counter", 0, "burn")])
        self.assertEqual((led.balance("builder"), led.balance("painter"), led.balance("counter")), (21, 20, 15))
        rc, _, err = self.team("order", "settle", "ord-0001", "burn", "--reason", "ещё")
        self.assertEqual(rc, 1)
        # A settlement is the owner's: it opens the gate.
        self.assertEqual([e["type"] for e in changes_since(self.t.state, "counter")[0]], ["order_settle"])
        self.assertIn("ord-0001", self.team("orders", "--all")[1])
        self.assertNotIn("ord-0001", self.team("orders")[1])
        from depot2.report import render
        rep = render(self.t.state, r["id"])
        self.assertEqual(self.rows(rep, "## После раунда")["Строитель"], ["Строитель", "15", "0", "+6", "0", "0", "21"])
        self.assertIn("заказ ord-0001: сдвинул решение, цена назад и +1, +6", rep)
        self.assertIn("«Сейчас» сходится с журналом", rep)
        # The next round's bundle lists the results once.
        r2_, _runner = self.play({})
        bundle = slurp(os.path.join(r2_["dir"], "bundle.md"))
        self.assertIn("## Результаты заказов (замеры и прогоны)", bundle)
        self.assertIn("### ord-0001: Строитель, раунд {}".format(r["id"]), bundle)
        self.assertIn("- прогноз автора до прогона: 6 мс", bundle)
        self.assertIn(f"- результат: {dest}", bundle)
        self.assertIn("- результаты в пакете этого раунда: ord-0001, ord-0002, ord-0003.", r2_["report"])
        r3, _ = self.play({})
        self.assertNotIn("Результаты заказов", slurp(os.path.join(r3["dir"], "bundle.md")))

    def test_the_prompts_tell_the_terms(self):
        _, runner = self.play({})
        spec = runner.calls[0][3]
        self.assertIn("`order` (по желанию, один за раунд", spec.user)
        self.assertIn("заказ замера или прогона, 5 ток.", spec.user)
        self.assertIn("Взнос: 10 ток. с инициатора и по 5 с каждого из остальных; взнос не возвращается", spec.user)
        self.assertIn("вся команда участвует в раунде 2 и все говорят «да»", spec.user)
        props = spec.schema["properties"]
        self.assertNotIn("order", spec.schema["required"])
        self.assertNotIn("brainstorm_proposal", spec.schema["required"])
        self.assertIn("order", props)
        self.assertEqual(schema.validate(dict(ans(), order=order()), spec.schema), [])


if __name__ == "__main__":
    unittest.main()


class QueueJsonTest(Base):

    def test_the_open_ones_and_all(self):
        o, b = extras.Orders(self.t.state), extras.Brainstorms(self.t.state)
        o.add("ord-0001", "queued", character="builder", round="r1", command="node tools/bench/run.mjs", why="замер")
        o.add("ord-0002", "queued", character="painter", round="r1", command="node tools/bench/run.mjs --seed 2")
        o.add("ord-0001", "done", results=["run.json"])
        o.add("ord-0001", "settled", outcome="burn")
        b.add("bs-0001", "approved", initiator="counter", round="r1", topic="Тема")
        b.add("bs-0002", "approved", initiator="painter", round="r2", topic="Другая")
        b.add("bs-0002", "cancelled", reason="не нужен")
        for argv, want in ((["orders", "--json"], ["ord-0002"]), (["orders", "--all", "--json"], ["ord-0001", "ord-0002"]),
                           (["brainstorm", "list", "--json"], ["bs-0001"]),
                           (["brainstorm", "list", "--all", "--json"], ["bs-0001", "bs-0002"])):
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 0, err)
            got = json.loads(out)
            self.assertEqual([x["id"] for x in got], want, argv)
        got = json.loads(self.team("orders", "--all", "--json")[1])
        self.assertEqual((got[0]["status"], got[0]["outcome"], got[0]["why"], [h["status"] for h in got[0]["history"]]),
                         ("settled", "burn", "замер", ["queued", "done", "settled"]))
        self.assertEqual(json.loads(self.team("orders", "--json")[1]), o.listing())
        self.assertEqual(set(extras.Orders.STATUS_RU), {"queued", "done", "settled"})
        self.assertEqual(set(extras.Brainstorms.STATUS_RU), {"approved", "done", "failed", "cancelled"})
