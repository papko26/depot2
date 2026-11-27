
import json
import os

from helpers import slurp
from test_extras import Base, order, proposal, r2
from test_round import ans

from depot2 import characters
from depot2.executors import FatalError
from depot2.ledger import Ledger
from depot2.reflect import changes_since
from depot2.round import _place_locks
from depot2.stack import Ballots, Stack, StackError
from depot2.store import read_jsonl
from depot2.tstack import TeamStack


def lock(obj="st-0001", condition="тест «isbn-dedup-cap» зелёный", why="иначе каталог двоится"):
    return {"lock": {"object": obj, "condition": condition, "why": why}, "inner_thought": "замок"}


class StackTest(Base):
    def stack(self):
        self.t.cfg["stack"].update(slots=2, queue_max=3)
        return Stack(self.t.state, self.t.cfg)

    def test_add_start_set_move_done(self):
        s = self.stack()
        a = s.add("Импорт 3", branch="import-3")
        b = s.add("Обложки", depends=[a["id"]])
        c = s.add("Закладки", at=1)
        self.assertEqual([o["id"] for o in s.queue()], ["st-0003", "st-0001", "st-0002"])
        s.move("st-0002", 1)
        self.assertEqual([o["id"] for o in s.queue()], ["st-0002", "st-0003", "st-0001"])
        with self.assertRaises(StackError) as e:
            s.add("лишняя")
        self.assertEqual(str(e.exception), "очередь полна (3): место освобождает depot2 stack done ID --reason «…» (что уже "
                                           "не нужно) или stack start ID (голова очереди — в слот)")
        with self.assertRaises(StackError) as e:
            s.start(b["id"])
        self.assertIn("depends on st-0001, not done yet", str(e.exception))
        s.start(a["id"])
        s.start(c["id"], branch="bookmarks-size")
        with self.assertRaises(StackError) as e:
            s.start(b["id"])  # the dependency is still in a slot
        self.assertIn("not done yet", str(e.exception))
        s.set(a["id"], status="PR открыт, CI", pr=180)
        s.done(a["id"], "merged #180")
        with self.assertRaises(StackError) as e:
            s.add("x", depends=["st-9999"])
        self.assertIn("unknown dependency", str(e.exception))
        s.add("после слияния", depends=[a["id"]])  # a done object is still a known id
        s2 = Stack(self.t.state, self.t.cfg)  # read back from stack.json
        self.assertEqual([o["id"] for o in s2.in_slots()], ["st-0003"])
        self.assertEqual(s2.get("st-0003")["branch"], "bookmarks-size")
        s2.start("st-0002")
        with self.assertRaises(StackError) as e:
            s2.start("st-0004")
        self.assertIn("no free slot (2/2)", str(e.exception))
        self.assertEqual(s2.add("ещё")["id"], "st-0005")  # ids are never reused
        events = [h["event"] for h in s2.history()]
        self.assertEqual(events.count("done"), 1)
        self.assertEqual([h for h in s2.history() if h["event"] == "done"][0]["obj"]["pr"], 180)

    def test_a_locked_object_does_not_start_or_leave(self):
        s = self.stack()
        s.add("Импорт 3")
        s.lock("st-0001", "counter", "тест X", "почему", "r", "tx-000009", 10)
        with self.assertRaises(StackError) as e:
            s.start("st-0001")
        self.assertIn("a locked object does not start", str(e.exception))
        with self.assertRaises(StackError):
            s.done("st-0001", "x")
        with self.assertRaises(StackError):
            s.lock("st-0001", "keeper", "y", "z", "r", "tx-000010", 10)
        s.unlock("st-0001", "met", "тест X зелёный: CI #1")
        s.start("st-0001")

    def test_the_table(self):
        s = self.stack()
        s.add("Импорт 3", branch="import-3", pr=181)
        s.add("Обложки", depends=["st-0001"])
        s.start("st-0001")
        s.lock("st-0002", "keeper", "ответ владельца про лимиты", "дорого", "r", "tx-000001", 10)
        md = s.md()
        self.assertIn("| № | Где | Задача | Приоритет | Ветка / PR | Статус | Автозапуск | Effort (ожидаемо / потрачено) "
                      "| Зависит от | Замок |", md)
        self.assertIn("| **Сборка — в слотах (1/2)** | | | | | | | | | |", md)
        self.assertIn("| С1 | слот | st-0001 Импорт 3 | P5 | import-3 / PR #181 | агент работает | нет | — | — | — |", md)
        self.assertIn("| С2 | слот | свободен | | | | | | | |", md)
        self.assertIn("| **Очередь — ждёт слота (1/3)** | | | | | | | | | |", md)
        self.assertIn("| 1 | очередь | st-0002 Обложки | P5 | — | ждёт слота | нет | — | st-0001 | lk-0001 Копилка: ответ "
                      "владельца про лимиты (−10) |", md)
        self.assertLess(md.index("в слотах"), md.index("ждёт слота"))

    def test_the_effort_column(self):
        s = self.stack()
        s.add("Импорт 3", size="M")
        s.add("Замер", est_mtok=3.0)
        s.set("st-0001", spent_mtok=14.4)
        with self.assertRaises(ValueError):
            s.add("x", size="XXL")
        md = s.md()
        self.assertIn("| 1 | очередь | st-0001 Импорт 3 | P5 | — | ждёт слота | нет | ≈ 1%UW (10 MTok) / 1.44%UW "
                      "(14.4 MTok) | — | — |", md)
        self.assertIn("| 2 | очередь | st-0002 Замер | P5 | — | ждёт слота | нет | ≈ 0.3%UW (3 MTok) / — | — | — |", md)
        self.assertIn("1%UW ≈ 10 MTok (usage.mtok_per_pct)", md)
        self.t.cfg["usage"]["mtok_per_pct"] = 24.0
        md = Stack(self.t.state, self.t.cfg).md()
        self.assertIn("≈ 1%UW (24 MTok) / 0.6%UW (14.4 MTok)", md)
        self.assertIn("≈ 0.12%UW (3 MTok) / —", md)
        rc, out, err = self.team("stack", "set", "st-0002", "--est-pct", "0.5", "--spent-mtok", "2.5")
        self.assertEqual(rc, 0, err)
        o = Stack(self.t.state, self.t.cfg).get("st-0002")
        self.assertEqual((o["est_mtok"], o["spent_mtok"]), (5.0, 2.5))

    def test_the_cli(self):
        # the CLI reads the state's config: the defaults, 4 slots and 10 waiting
        rc, out, err = self.team("stack", "add", "--title", "Импорт 3", "--branch", "import-3")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 added to the queue (1/10)", out)
        self.assertEqual(self.team("stack", "start", "st-0001")[0], 0)
        self.assertEqual(self.team("stack", "set", "st-0001", "--status", "PR открыт, CI", "--pr", "190")[0], 0)
        rc, out, _ = self.team("stack", "--md")
        self.assertIn("| **Сборка — в слотах (1/4)** | | | | | | | | | |", out)
        self.assertIn("PR открыт, CI", out)
        self.assertEqual(self.team("stack")[1], out)  # --md is the default
        data = json.loads(self.team("stack", "--json")[1])
        self.assertEqual((data["slots"], data["queue_max"], data["in_slots"][0]["pr"]), (4, 10, 190))
        rc, _, err = self.team("stack", "start", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("in a slot already", err)
        self.assertEqual(self.team("stack", "done", "st-0001", "--reason", "merged #190")[0], 0)
        self.assertNotIn("st-0001", self.team("stack")[1])


    def test_a_note_onto_the_object(self):
        from depot2.store import now_iso
        self.assertEqual(self.team("stack", "add", "--title", "Импорт 3")[0], 0)
        rc, out, _ = self.team("stack", "set", "st-0001", "--note", "ждёт CI")
        self.assertEqual(rc, 0)
        day = now_iso()[:10]
        self.assertIn(f"st-0001: заметка 1 — {day}: ждёт CI", out)
        rc, out, _ = self.team("stack", "set", "st-0001", "--note", "CI зелёный", "--status", "PR открыт")
        self.assertEqual(rc, 0)
        self.assertIn(f"st-0001: заметка 2 — {day}: CI зелёный", out)
        o = json.loads(self.team("stack", "--json")[1])["queue"][0]
        self.assertEqual((o["notes"], o["status"]), ([f"{day}: ждёт CI", f"{day}: CI зелёный"], "PR открыт"))
        rows = [r for r in Stack(self.t.state, self.t.cfg).history() if r["event"] == "set"]
        self.assertEqual([r.get("note_added") for r in rows], ["ждёт CI", "CI зелёный"])
        self.assertNotIn("notes", rows[0])  # the row keeps the line added, not the whole list again
        rc, _, err = self.team("stack", "set", "st-0001", "--note", "  ")
        self.assertEqual(rc, 1)
        self.assertIn("an empty --note adds nothing", err)


class AutostartAndParkTest(Base):

    def test_the_autostart_flag(self):
        rc, out, err = self.team("stack", "add", "--title", "Импорт 3")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 added to the queue (1/10), autostart no", out)
        rc, out, err = self.team("stack", "add", "--title", "Обложки", "--autostart", "yes")
        self.assertIn("st-0002 added to the queue (2/10), autostart yes", out)
        self.assertIn("| 1 | очередь | st-0001 Импорт 3 | P5 | — | ждёт слота | нет | — | — | — |", out)
        self.assertIn("| 2 | очередь | st-0002 Обложки | P5 | — | ждёт слота | да | — | — | — |", out)
        self.assertIn("Автозапуск: да — первый свободный слот берёт объект сам (`trains next`, после поездов); "
                      "нет (по умолчанию) — ждёт слова владельца, `stack start`;", out)
        s = Stack(self.t.state, self.t.cfg)
        self.assertEqual([o["autostart"] for o in s.queue()], [False, True])
        self.assertEqual(self.team("stack", "set", "st-0002", "--autostart", "no")[0], 0)
        self.assertIs(Stack(self.t.state, self.t.cfg).get("st-0002")["autostart"], False)
        self.assertEqual(self.team("stack", "set", "st-0001", "--autostart", "да")[0], 0)
        self.assertIs(Stack(self.t.state, self.t.cfg).get("st-0001")["autostart"], True)
        rc, _, err = self.team("stack", "set", "st-0001", "--autostart", "maybe")
        self.assertEqual(rc, 1)
        self.assertIn("--autostart is yes or no", err)
        # `stack start` by hand works whatever the flag says
        self.assertEqual(self.team("stack", "start", "st-0002")[0], 0)
        # an object from before the flag reads as no, in the table and in --json
        s = Stack(self.t.state, self.t.cfg)
        del s.get("st-0001")["autostart"]
        s._save("test", "st-0001")
        self.assertIn("| 1 | очередь | st-0001 Импорт 3 | P5 | — | ждёт слота | нет |", self.team("stack")[1])
        self.assertIs(json.loads(self.team("stack", "--json")[1])["queue"][0]["autostart"], False)

    def test_est_none_clears_the_estimate(self):
        self.team("stack", "add", "--title", "Импорт 3", "--est-mtok", "5", "--size", "S")
        rc, out, err = self.team("stack", "set", "st-0001", "--est-pct", "none")
        self.assertEqual(rc, 0, err)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertIsNone(o["est_mtok"])
        self.assertIn("| ≈ 0.3%UW (3 MTok) / — |", out)  # its size shows again
        self.assertTrue(Stack(self.t.state, self.t.cfg).history()[-1]["est_cleared"])
        self.team("stack", "set", "st-0001", "--est-mtok", "7")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["est_mtok"], 7.0)
        self.assertEqual(self.team("stack", "set", "st-0001", "--est-mtok", "none")[0], 0)
        self.assertIsNone(Stack(self.t.state, self.t.cfg).get("st-0001")["est_mtok"])
        rc, out, err = self.team("tstack", "add", "--kind", "discuss", "--est-mtok", "1")
        self.assertEqual(self.team("tstack", "set", "tm-0001", "--est-mtok", "none")[0], 0)  # the team stack too
        self.assertIsNone(TeamStack(self.t.state, self.t.cfg).get("tm-0001")["est_mtok"])

    def test_park_an_object_that_is_no_car(self):
        self.team("stack", "add", "--title", "Закладки", "--branch", "bookmarks-size", "--autostart", "yes")
        self.team("stack", "add", "--title", "Обложки", "--autostart", "yes")
        self.team("stack", "start", "st-0001")
        self.team("stack", "set", "st-0001", "--pr", "205", "--status", "PR #205 готов; ждёт замера")
        with self.assertRaises(SystemExit):  # --reason is required
            self.team("stack", "park", "st-0001")
        rc, out, err = self.team("stack", "park", "st-0001", "--reason", "ждёт замера выдачи")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 parked: slot → the head of the queue, its branch, PR and status kept (0/4)", out)
        s = Stack(self.t.state, self.t.cfg)
        self.assertEqual([o["id"] for o in s.queue()], ["st-0001", "st-0002"])
        o = s.get("st-0001")
        self.assertEqual((o["where"], o["branch"], o["pr"], o["status"], o["parked"]["reason"]),
                         ("queue", "bookmarks-size", 205, "PR #205 готов; ждёт замера", "ждёт замера выдачи"))
        self.assertIn("| 1 | очередь | st-0001 Закладки | P5 | bookmarks-size / PR #205 | PR #205 готов; ждёт замера (запаркован {}: "
                      "ждёт замера выдачи) | да |".format(o["parked"]["ts"][:10]), out)
        # a freed slot never takes it by itself, though its autostart is yes; the next object goes
        from depot2.trains import Trains
        nx = Trains(self.t.state, self.t.cfg).next()
        self.assertEqual([x["id"] for x in nx["stack"]], ["st-0002"])
        self.assertEqual([(w["stack"]["id"], w["why"], w["command"]) for w in nx["owner_word"]],
                         [("st-0001", "запаркован: ждёт замера выдачи", "depot2 stack start st-0001")])
        rc, out, _ = self.team("stack", "park", "st-0001", "--reason", "ещё раз")
        self.assertEqual(rc, 1)  # only an object in a slot
        # `stack start` brings it back
        self.assertEqual(self.team("stack", "start", "st-0001")[0], 0)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["where"], o.get("parked"), o["status"]), ("slot", None, "PR #205 готов; ждёт замера"))
        self.assertEqual([h["event"] for h in Stack(self.t.state, self.t.cfg).history()][-2:], ["park", "start"])


class LockPassTest(Base):
    def setUp(self):
        super().setUp()
        s = Stack(self.t.state, self.t.cfg)
        s.add("Импорт 3", branch="import-3")
        s.add("Обложки")
        s.start("st-0001")

    def test_the_lock_pass_runs_after_the_vote_and_sees_the_balance_after_the_debits(self):
        script = {("counter", "r1"): dict(ans([("B49", "agree", 4, "важно")]),
                                          brainstorm_proposal=proposal("Обложки и полки")),
                  ("builder", "r1"): dict(ans(), order=order(seeds="1-5", threshold="8 мс", forecast="6")),
                  ("keeper", "r3"): lock("st-0001")}
        for cid in characters.IDS:
            script[(cid, "r2")] = r2([("BR1", "yes")])
        r, runner = self.play(script)
        stages = [c[1] for c in runner.calls]
        self.assertEqual(stages, ["r1"] * 5 + ["r2"] * 5 + ["r3"] * 5)  # after the debate and its vote
        seen = {c[0]: c[2] for c in runner.calls if c[1] == "r3"}
        self.assertEqual(seen, {"builder": 10, "painter": 15, "counter": 10, "chronicler": 15, "keeper": 15})
        r3 = [c[3] for c in runner.calls if c[1] == "r3"]
        self.assertEqual(r3[0].max_tool_turns, 2)
        self.assertEqual(r3[0].budget_usd, 1.50)  # lock.budget_mtok 0.6 × usage.usd_per_mtok 2.5
        self.assertEqual(r3[0].add_dirs, [self.snaps[0]])
        self.assertIn("У тебя 15 ток.", [c.user for c in r3 if "keeper" in c.call_id][0])
        system = slurp(r3[0].system_path)
        self.assertIn("## Стек: слоты агентов и очередь задач", system)
        self.assertIn("st-0001 Импорт 3", system)
        self.assertIn("- Обложки и полки (предлагает Счетовод): одобрен: bs-0001, взнос списан", system)
        self.assertEqual(len({c.system_path for c in r3}), 1)  # shared by all five: it caches
        # The lock: debited 10, on the object, in the report.
        led = Ledger(self.t.state)
        locks = [e for e in led.entries() if e["type"] == "lock"]
        self.assertEqual([(e["character"], e["amount"], e["object"]) for e in locks], [("keeper", -10, "st-0001")])
        lk = Stack(self.t.state, self.t.cfg).get("st-0001")["lock"]
        self.assertEqual((lk["id"], lk["by"], lk["round"], lk["tx"]), ("lk-0001", "keeper", r["id"], locks[0]["id"]))
        rep = r["report"]
        self.assertIn("Проход замков (раунд 3) прошёл: 5 вызовов.", rep)
        self.assertIn("- lk-0001 Копилка на st-0001 «Импорт 3» (−10 ток.): условие: тест «isbn-dedup-cap» зелёный; "
                      "зачем: иначе каталог двоится.", rep)
        self.assertEqual(self.rows(rep)["Копилка"], ["Копилка", "20", "—", "0", "0", "−5", "0", "−10", "5"])
        self.assertIn("замки 10 ток. (не возвращаются)", rep)
        self.assertNotIn("НЕ СХОДИТСЯ", rep)
        self.assertEqual(changes_since(self.t.state, "keeper"), ([], []))  # the team's act

    def test_refused_locks(self):
        Ledger(self.t.state).fine("painter", 15, "тест")  # 5 < 10: no call at all
        r, runner = self.play({("builder", "r3"): lock("st-0002"), ("counter", "r3"): lock("st-0002"),
                               ("keeper", "r3"): lock("st-0001", condition="")})
        self.assertNotIn("painter", [c[0] for c in runner.calls if c[1] == "r3"])
        rep = r["report"]
        self.assertIn("- отклонён, Палитра, Краски — не может заплатить замок (5 < 10): без вызова.", rep)
        self.assertIn("- отклонён, Счетовод: st-0002 — st-0002 уже под замком (lk-0001).", rep)
        self.assertIn("- отклонён, Копилка: st-0001 — не заполнено: condition.", rep)
        self.assertEqual([e["character"] for e in Ledger(self.t.state).entries() if e["type"] == "lock"], ["builder"])
        # One per character per round, and only on an object in the stack (checked by code).
        stk = Stack(self.t.state, self.t.cfg)
        placed, refused = _place_locks(self.t.state, self.t.cfg, Ledger(self.t.state), stk, "r-x",
                                       [("chronicler", lock("st-0001")["lock"]), ("chronicler", lock("st-0002")["lock"]),
                                        ("keeper", lock("st-0404")["lock"])])
        self.assertEqual([p["object"] for p in placed], ["st-0001"])
        self.assertEqual([x["reason"] for x in refused], ["второй замок за раунд: один на персонажа",
                                                         "нет такого объекта в стеке: st-0404"])

    def test_skipped_on_an_empty_stack(self):
        s = Stack(self.t.state, self.t.cfg)
        s.done("st-0001", "merged")
        s.done("st-0002", "dropped")
        r, runner = self.play({})
        self.assertNotIn("r3", [c[1] for c in runner.calls])
        self.assertIn("Проход замков (раунд 3) пропущен: стек пуст.", r["report"])

    def test_the_rounds_call_cap_counts_it(self):
        self.t.cfg["round"]["max_calls"] = 12
        r, runner = self.play({})
        self.assertEqual([c[1] for c in runner.calls].count("r3"), 2)
        self.assertIn("Перо, Летописец: раунд 3 (замки) пропущен (cap: 12 calls per round)", r["report"])

    def test_a_fatal_error_in_the_lock_pass_keeps_the_round(self):
        r, runner = self.play({("painter", "r3"): FatalError("API error 429: usage limit"),
                               ("builder", "r3"): lock("st-0001")})
        self.assertIn("lock pass stopped", r["fatal"])
        self.assertEqual([c[0] for c in runner.calls if c[1] == "r3"], ["builder", "painter"])
        rj = json.loads(slurp(os.path.join(r["dir"], "round.json")))
        self.assertEqual(rj["status"], "done")
        self.assertEqual([x["character"] for x in rj["locks"]["placed"]], ["builder"])

    def test_unlock_met_and_owner_only_owner_opens_the_gate(self):
        self.play({("builder", "r3"): lock("st-0001"), ("keeper", "r3"): lock("st-0002")})
        rc, out, err = self.team("stack", "unlock", "st-0001", "--met", "--reason", "тест зелёный, CI #5")
        self.assertEqual(rc, 0, err)
        self.assertEqual(changes_since(self.t.state, "builder"), ([], []))
        rc, out, err = self.team("stack", "unlock", "st-0002", "--owner", "--reason", "беру на себя")
        self.assertEqual(rc, 0, err)
        owner, dec = changes_since(self.t.state, "keeper")
        self.assertEqual(len(dec), 1)
        self.assertIn("Владелец снял замок lk-0002 (st-0002", dec[0]["text"])
        self.assertEqual(Ledger(self.t.state).balance("keeper"), 10)  # the price stays spent
        self.assertEqual(self.team("stack", "unlock", "st-0002", "--met", "--reason", "x")[0], 1)  # no lock now
        # The next round's report lists them as lifted.
        r, _ = self.play({})
        self.assertIn("- снят с прошлого раунда: lk-0001 на st-0001 (Строитель), условие выполнено: тест зелёный, CI #5.",
                      r["report"])
        self.assertIn("- снят с прошлого раунда: lk-0002 на st-0002 (Копилка), снял владелец: беру на себя.", r["report"])


class BallotsTest(Base):
    def test_every_round_joins_the_queue_in_the_reports_order(self):
        r, _ = self.play({("builder", "r1"): ans([("B50", "agree", 3, "да"), ("B49", "agree", 1, "")])})
        bq = Ballots(self.t.state)
        e = bq.get(r["id"])
        self.assertEqual((e["status"], e["kind"], e["title"]), ("waiting", "ballot", "Бюллетень"))
        self.assertEqual(e["points_left"], ["B50", "B49", "X1", "X2"])
        self.assertEqual(e["report"], os.path.join(r["dir"], "report.md"))
        # The next round's bundle shows it to the characters.
        r2_, runner = self.play({})
        system = slurp(runner.calls[0][3].system_path)
        self.assertIn("## Очередь бюллетеней владельца (ещё не решены)", system)
        self.assertIn("| 1 | {} | Бюллетень | ждёт | — | 4: B50, B49, X1, X2 |".format(r["id"]), system)
        self.assertEqual([x["round"] for x in Ballots(self.t.state).entries()], [r["id"], r2_["id"]])

    def test_decide_marks_a_point_and_the_commands(self):
        r, _ = self.play({})
        rid = r["id"]
        rc, out, err = self.team("decide", "all", "--text", "да", "--round", rid, "--point", "B49")
        self.assertEqual(rc, 0, err)
        self.assertIn(f"ballot {rid}: B49 answered; 3 points left (presenting)", out)
        n = len(read_jsonl(self.t.state.decisions_path))
        rc, out, err = self.team("decide", "all", "--text", " ", "--round", rid, "--point", "B50")
        self.assertEqual((rc, len(read_jsonl(self.t.state.decisions_path))), (1, n))
        self.assertIn("a decision needs its words", err)
        rc, out, err = self.team("decide", "all", "--text", "нет", "--round", rid, "--point", "Z9")
        self.assertIn(f"ballot {rid}: no open point Z9 (open: B50, X1, X2): the ballot is as it was", out)
        rc, out, err = self.team("decide", "all", "--text", "да", "--round", rid, "--point", "b50")
        self.assertIn(f"ballot {rid}: B50 answered; 2 points left (presenting)", out)
        self.assertEqual(read_jsonl(self.t.state.decisions_path)[-1]["point"], "B50")
        self.assertEqual(self.team("ballots", "at", rid, "X1")[0], 0)
        rc, out, _ = self.team("ballots", "--md")
        self.assertIn(f"| 1 | {rid} | Бюллетень | показываю | X1 | 2: X1, X2 |", out)
        self.assertEqual(self.team("ballots", "point", rid, "B50")[0], 1)  # answered already
        r2_, _ = self.play({})
        self.assertEqual(self.team("ballots", "move", r2_["id"], "--at", "1")[0], 0)
        self.assertEqual([e["round"] for e in json.loads(self.team("ballots", "--json")[1])], [r2_["id"], rid])
        self.assertEqual(self.team("ballots", "drop", r2_["id"], "--reason", "устарел")[0], 0)
        self.assertEqual(self.team("ballots", "point", rid, "X1")[0], 0)
        self.assertEqual(self.team("ballots", "point", rid, "X2")[0], 0)  # the last one: answered
        self.assertEqual(Ballots(self.t.state).get(rid)["status"], "answered")
        self.assertIn("| — | | (пусто) | | | |", self.team("ballots")[1])
        rc, out, _ = self.team("ballots", "--all")
        self.assertIn("отвечен", out)
        self.assertIn("снят", out)
        self.assertEqual(Ballots(self.t.state).get(r2_["id"])["notes"][0][-7:], "устарел")
        self.assertEqual(self.team("ballots", "add", "--round", rid)[0], 1)  # in the queue already
        os.remove(Ballots(self.t.state).path)
        self.assertEqual(self.team("ballots", "add", "--round", rid)[0], 0)  # an existing round
        self.assertEqual(Ballots(self.t.state).get(rid)["status"], "waiting")
        self.assertEqual(self.team("ballots", "done", rid)[0], 0)
        self.assertEqual(Ballots(self.t.state).get(rid)["status"], "answered")
