"""The merge queue's part: test_merge.py (CarTest)."""

import fcntl
import json
import os
import shutil

from helpers import BRIEF_TEXT
from test_tracks import FUTURE, PAST, TracksBase

from depot2 import config, merge, migrate
from depot2.stack import Stack
from depot2.store import read_json, read_jsonl, write_json
from depot2.tracks import Tracks
from depot2.trains import TrainError, Trains, check_name, parse_slots


class ModelTest(TracksBase):
    def setUp(self):
        super().setUp()
        tk = self.tracks()
        for title in ("Экспорт", "Печать"):
            t = tk.add(title)
            for x in ("a", "b", "c"):
                tk.car_add(t["id"], f"{title} {x}", size="S")

    def test_a_name_a_car_in_one_train_and_the_order(self):
        for bad in ("Export", "export_n", "-x", "x-", "tr-0001", "st-0040", "123", "", "a" * 41):
            with self.assertRaises(TrainError):
                check_name(bad)
        self.assertEqual(check_name("export-n-print"), "export-n-print")
        tr = self.trains()
        tn = tr.add("export-n-print", "Экспорт и печать", cars=["tr-0001:1", "tr-0002:0", "tr-0001:0"])
        self.assertEqual((tn["state"], tn["cars"]), ("design", ["tr-0001:1", "tr-0002:0", "tr-0001:0"]))
        self.assertEqual([r["state"] for r in tn["states"]], ["design"])
        with self.assertRaises(TrainError):  # the name is taken
            tr.add("export-n-print", "ещё")
        with self.assertRaises(TrainError):  # a car rides one live train
            tr.add("other", "Другой", cars=["tr-0002:0"])
        with self.assertRaises(ValueError):  # no such car
            tr.add("other", "Другой", cars=["tr-0002:9"])
        self.tracks().car_set("tr-0002:2", status="merged")
        with self.assertRaises(TrainError):  # a closed car joins no train
            self.trains().add("other", "Другой", cars=["tr-0002:2"])
        tr = self.trains()
        tr.car_add("export-n-print", "tr-0002:1", at=1)
        tr.car_move("export-n-print", "tr-0001:0", 2)
        self.assertEqual(self.trains().get("export-n-print")["cars"], ["tr-0002:1", "tr-0001:0", "tr-0001:1", "tr-0002:0"])
        tr = self.trains()
        tr.car_drop("export-n-print", "tr-0002:0", why="в другой поезд")
        tr.add("print", "Печать дальше", cars=["tr-0002:0"])  # free again
        self.assertEqual(self.trains().of_car("tr-0002:0")["name"], "print")
        # a scrapped train lets its cars go
        rc, _out, err = self.team("trains", "drop", "print", "--why", "передумали")
        self.assertEqual(rc, 0, err)
        self.assertIsNone(self.trains().of_car("tr-0002:0"))
        with self.assertRaises(TrainError):  # scrapped only with a reason
            self.trains().drop("export-n-print", "  ")
        # a rename takes the start rules that name it along; the history keeps every change
        tr = self.trains()
        tr.add("after-it", "После", after=["export-n-print"])
        tr.set("export-n-print", new_name="export")
        self.assertEqual(self.trains().get("after-it")["start"]["after"], ["export"])
        self.assertEqual(self.tracks().ref_state("export"), (False, "поезд export (дизайн)"))
        with self.assertRaises(TrainError):
            self.trains().set("export", after=["export"])  # never waits for itself
        events = [(h["event"], h["train"]) for h in self.trains().history()]
        self.assertIn(("car_drop", "export-n-print"), events)
        self.assertIn(("set", "export-n-print"), events)
        self.assertEqual(read_json(self.t.state.path("trains.json"))["schema"], migrate.SCHEMA)

    def test_the_states_and_their_gates(self):
        self.t.cfg["gates"] = {"dor": ["brief", "after"], "dod": []}
        brief = os.path.join(self.out, "brief.md")
        with open(brief, "w", encoding="utf-8") as f:
            f.write(BRIEF_TEXT)
        tr = self.trains()
        tr.add("export", "Экспорт", cars=["tr-0001:0", "tr-0001:1"], not_before="2015-11-08T15:00",
               after=["tr-0002:2"], owner=True)
        with self.assertRaises(TrainError) as e:
            self.trains().set_state("export", "merge", word="w")
        self.assertIn("no move", str(e.exception))
        with self.assertRaises(TrainError) as e:
            self.trains().set_state("export", "impl", now=PAST)
        msg = str(e.exception)
        self.assertIn("не раньше вс 2015-11-08 15:00", msg)
        self.assertIn("после tr-0002:2 (ждёт)", msg)
        self.assertIn("tr-0001:0: нет брифа", msg)
        tr = self.trains()
        self.assertIn("в «имплементация» ждёт: start: не раньше", tr.gate_line(tr.get("export"), now=PAST))
        for ref in ("tr-0001:0", "tr-0001:1"):
            self.tracks().car_set(ref, brief=brief)
        self.tracks().car_set("tr-0002:2", status="merged")
        tr = self.trains()
        self.assertEqual(tr.unmet(tr.get("export"), "impl", now=FUTURE), {"start": [], "DoR": []})
        nx = tr.next(FUTURE)  # open, and the owner's word: named, never taken
        self.assertEqual([(r["train"]["name"], r["owner"]) for r in nx["ready"]], [("export", True)])
        self.assertEqual(nx["picks"], [])
        self.assertIn("В «имплементацию» можно: export — `depot2 trains state export impl` (слово владельца).",
                      "\n".join(tr.next_lines(FUTURE)))
        lines = tr.set_state("export", "impl", now=FUTURE)
        self.assertIn("export: «дизайн» → «имплементация»", lines)
        tn = self.trains().get("export")
        self.assertEqual((tn["state"], bool(tn["started"])), ("impl", True))
        with self.assertRaises(TrainError) as e:
            self.trains().set_state("export", "merge", word="мердж")
        self.assertIn("PR — tr-0001:0: тележка, tr-0001:1: тележка", str(e.exception))
        # back with no gate, and forward past the gate with --force and a reason, recorded
        self.trains().set_state("export", "design")
        with self.assertRaises(TrainError):
            self.trains().set_state("export", "impl", force=True, now=PAST)  # --force needs --why
        self.tracks().car_set("tr-0001:0", brief=os.path.join(self.out, "nope.md"))
        lines = self.trains().set_state("export", "impl", force=True, why="бриф на словах", now=FUTURE)
        self.assertIn("ворота с --force", lines[-1])
        row = self.trains().get("export")["states"][-1]
        self.assertEqual((row["state"], row["forced"], row["why"]), ("impl", True, "бриф на словах"))
        # the last car closed: it arrives by itself; all of them dropped: it is scrapped
        for ref in ("tr-0001:0", "tr-0001:1"):
            self.tracks().car_set(ref, status="merged")
        self.assertEqual(self.trains().get("export")["state"], "arrived")
        self.trains().add("dropped", "Снятые", cars=["tr-0002:0"])
        tk = self.tracks()
        tk.car_set("tr-0002:0", status="dropped")
        self.assertIn("поезд dropped → «списан» (последний вагон tr-0002:0 закрыт)", tk.stage_lines)
        with self.assertRaises(TrainError):  # a train that arrived moves no more
            self.trains().set_state("export", "impl")
        # `trains done` by hand: refused while a car is open
        self.trains().add("open", "Открытый", cars=["tr-0002:1"])
        self.assertEqual(self.team("trains", "done", "open")[0], 1)
        self.assertEqual(self.team("trains", "done", "open", "--force", "--why", "довезли руками")[0], 0)
        self.assertEqual(self.trains().get("open")["state"], "arrived")

    def test_the_cli(self):
        rc, out, err = self.team("trains", "add", "--name", "export-n-print", "--title", "Экспорт и печать",
                                 "--car", "tr-0001:0", "--car", "tr-0002:0", "--slots", "max 2", "--after", "tr-0001:2")
        self.assertEqual(rc, 0, err)
        self.assertIn("export-n-print added (position 1, «дизайн», P5)", out)
        self.assertIn("## Поезд export-n-print «Экспорт и печать»", out)
        self.assertIn("| 1 | tr-0001:0 | P5 | tr-0001 Экспорт | Экспорт a |", out)
        self.assertIn("| 2 | tr-0002:0 | P5 | tr-0002 Печать | Печать a |", out)
        self.assertEqual(self.team("trains", "add", "--name", "Без_имени", "--title", "x")[0], 1)
        rc, out, err = self.team("trains")
        self.assertEqual(rc, 0, err)
        self.assertIn("| № | Поезд | Приоритет | Состояние | Старт | Вагонов (готово / всего) | Слоты | Effort (ожидаемо / потрачено) | "
                      "Дальше | Этапы вагонов |", out)
        self.assertIn("| 1 | export-n-print «Экспорт и печать» | P5 | дизайн | после tr-0001:2 (ждёт) | 0 / 2 | ≤ 2 |", out)
        self.assertIn("в «имплементация» ждёт: start: после tr-0001:2 (ждёт)", out)
        self.assertEqual(self.team("trains", "state", "export-n-print", "impl")[0], 1)
        rc, out, err = self.team("trains", "state", "export-n-print", "impl", "--force", "--why", "старт сейчас")
        self.assertEqual(rc, 0, err)
        rc, out, err = self.team("trains", "next")
        self.assertIn("Можно сейчас: tr-0001:0 «Экспорт a» (поезд export-n-print P5, вагон P5) — `depot2 tracks car stack "
                      "tr-0001:0 --start`; tr-0002:0 «Печать a» (поезд export-n-print P5, вагон P5) — `depot2 tracks car "
                      "stack tr-0002:0 --start`.", out)
        data = json.loads(self.team("trains", "--json")[1])
        self.assertEqual(data["next"]["picks"], [
            {"car": "tr-0001:0", "train": "export-n-print", "command": "depot2 tracks car stack tr-0001:0 --start"},
            {"car": "tr-0002:0", "train": "export-n-print", "command": "depot2 tracks car stack tr-0002:0 --start"}])
        j = json.loads(self.team("train", "export-n-print", "--json")[1])
        self.assertEqual((j["state"], j["state_ru"], j["next_state"]), ("impl", "имплементация", "merge"))
        self.assertEqual([(c["ref"], c["track"], c["train"]) for c in j["cars"]],
                         [("tr-0001:0", "tr-0001", "export-n-print"), ("tr-0002:0", "tr-0002", "export-n-print")])
        self.assertEqual(self.team("trains", "car", "add", "export-n-print", "tr-0001:1", "--at", "1")[0], 0)
        self.assertEqual(self.team("trains", "car", "move", "export-n-print", "tr-0001:1", "--at", "3")[0], 0)
        self.assertEqual(self.team("trains", "car", "drop", "export-n-print", "tr-0001:1")[0], 0)
        self.assertEqual(self.trains().get("export-n-print")["cars"], ["tr-0001:0", "tr-0002:0"])
        self.assertEqual(self.team("trains", "set", "export-n-print", "--name", "export", "--title", "Экспорт")[0], 0)
        self.assertEqual(self.team("train", "export")[0], 0)
        self.assertEqual(self.team("train", "export-n-print")[0], 1)
        rc, out, err = self.team("trains", "state", "export", "design")
        self.assertEqual(rc, 0, err)
        self.assertIn("export: «имплементация» → «дизайн»", out)

    def test_the_cli_traps_name_the_way_through(self):
        import contextlib
        import io

        from depot2 import cli as team

        def refused(*argv):  # argparse's refusal: exit 2 and its line on stderr
            err = io.StringIO()
            with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                with self.assertRaises(SystemExit) as cm:
                    team.main(["--state", self.t.dir] + list(argv))
            return cm.exception.code, err.getvalue()
        self.assertEqual(self.team("trains", "add", "--name", "export", "--title", "Экспорт", "--car", "tr-0001:0")[0], 0)
        for word, cmd in (("arrived", "depot2 trains done NAME"), ("прибыл", "depot2 trains done NAME"),
                          ("scrapped", "depot2 trains drop NAME --why"), ("списан", "depot2 trains drop NAME --why"),
                          ("nowhere", "design, impl or merge")):
            rc, err = refused("trains", "state", "export", word)
            self.assertEqual(rc, 2, word)
            self.assertIn(cmd, err)
        self.assertEqual(self.trains().get("export")["state"], "design")
        rc, out, err = self.team("trains", "state", "export", "имплементация", "--force", "--why", "старт")  # noqa: RUF059
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.trains().get("export")["state"], "impl")
        self.assertEqual(self.team("trains", "set", "export", "--owner-word")[0], 0)
        self.assertIs(self.trains().get("export")["start"]["owner"], True)
        self.assertEqual(self.team("trains", "set", "export", "--owner-word", "no")[0], 0)
        self.assertIs(self.trains().get("export")["start"]["owner"], False)
        self.assertEqual(self.team("trains", "set", "export", "--owner-word", "yes", "--note", "x")[0], 0)
        self.assertIs(self.trains().get("export")["start"]["owner"], True)
        for argv in (("--by", "владелец", "2015-11-21"), ("--by=владелец", "2015-11-21")):
            rc, err = refused("car", "ask", "tr-0001:0", "как?", *argv)
            self.assertEqual(rc, 2)
            self.assertIn('unrecognized arguments: 2015-11-21 — if «2015-11-21» belongs to --by, the value goes in quotes: '
                          '--by "владелец 2015-11-21"', err)
        rc, err = refused("trains", "set", "export", "--bogus")
        self.assertEqual((rc, err.strip().splitlines()[-1]), (2, "depot2: error: unrecognized arguments: --bogus"))
        # a scrapped (or arrived) train keeps its list of cars: no drop, no move
        self.assertEqual(self.team("trains", "drop", "export", "--why", "передумали")[0], 0)
        for argv in (("drop", "export", "tr-0001:0"), ("move", "export", "tr-0001:0", "--at", "1")):
            rc, _out, err = self.team("trains", "car", *argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn("error: export is списан: its cars stay as they were", err)
        self.assertEqual(self.trains().get("export")["cars"], ["tr-0001:0"])

    def test_a_held_state_lock_is_one_line_not_a_traceback(self):
        from depot2 import store
        before = read_json(self.t.state.path("trains.json"))
        fd = os.open(self.t.state.path(".lock"), os.O_RDWR | os.O_CREAT, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # another depot2
        wait, store.LOCK_WAIT_S = store.LOCK_WAIT_S, 0.3
        try:
            rc, _out, err = self.team("trains", "add", "--name", "export", "--title", "Экспорт")
        finally:
            store.LOCK_WAIT_S = wait
            os.close(fd)
        self.assertEqual(rc, 1)
        self.assertIn("error: another depot2 has held {} for over 0.3 s, nothing was written: run the command again".format(self.t.state.path(".lock")), err)
        self.assertNotIn("Traceback", err)
        self.assertEqual(read_json(self.t.state.path("trains.json")), before)

    def test_the_price_of_each_train_and_of_the_tab(self):
        tk = self.tracks()
        tk.car_add("tr-0002", "Печать d")
        tk.car_set("tr-0001:0", spent_mtok=6.0)
        tr = self.trains()
        tr.add("first", "Первый", cars=["tr-0001:0", "tr-0001:1", "tr-0002:3"])
        tr.add("own", "Своя оценка", cars=["tr-0002:0"], est_mtok=[12.0, 24.0])  # a train's own range
        tr.add("gone", "Довезён", cars=["tr-0001:2"])
        self.trains().done("gone", why="довезли руками", force=True)
        data = json.loads(self.team("trains", "--json")[1])
        by = {t["name"]: t["effort"] for t in data["trains"]}
        self.assertEqual(by["first"], {"expected": 7.2, "spent": 6.0, "unpriced": 1,
                                       "text": "≈ 0.6%UW (7.2 MTok) / 0.5%UW (6 MTok); вагонов без оценки: 1"})
        self.assertEqual((by["own"]["expected"], by["own"]["unpriced"]), ([12.0, 24.0], 0))
        self.assertEqual(data["effort"], {
            "note": "примерно, по размерам вагонов",
            "live": {"trains": 2, "expected": [19.2, 31.2], "spent": 6.0, "unpriced": 1,
                     "text": "≈ 1.6–2.6%UW (19.2–31.2 MTok) / 0.5%UW (6 MTok); вагонов без оценки: 1"},
            "done": {"trains": 1, "expected": 3.6, "spent": None, "unpriced": 0, "text": "≈ 0.3%UW (3.6 MTok) / —"}})
        line = "Цена %s поездов (%d) — примерно, по размерам вагонов; ожидаемо / потрачено: %s."
        rc, out, err = self.team("trains")
        self.assertEqual(rc, 0, err)
        self.assertIn(line % ("живых", 2, data["effort"]["live"]["text"]), out)
        self.assertNotIn("Цена прибывших", out)  # the done trains are not in the table
        for name in ("first", "own"):
            self.assertIn("| {} |".format(by[name]["text"]), out)
        out = self.team("trains", "--all")[1]
        self.assertIn(line % ("прибывших и снятых", 1, data["effort"]["done"]["text"]), out)
        self.assertIn("| {} |".format(by["gone"]["text"]), out)
        self.assertIn("Effort (ожидаемо / потрачено): {}.".format(by["first"]["text"]), self.team("train", "first")[1])
        self.assertEqual(json.loads(self.team("train", "first", "--json")[1])["effort"], by["first"])


class NextTest(TracksBase):
    def test_next_honours_max_2_and_all_free_and_the_trains_order(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("Чужая задача", autostart=True)
        stk.add("Занятый слот")
        stk.start("st-0002")          # 3 free of 4
        tk = self.tracks()
        a, b = tk.add("Поиск"), tk.add("Читалка")
        for title in ("a", "b", "c", "d"):
            tk.car_add(a["id"], title, size="M")
        tk.car_add(b["id"], "макет", n="R0", size="M")
        tr = self.trains()
        tr.add("poisk", "Поиск", cars=["tr-0002:R0", "tr-0001:0", "tr-0001:1", "tr-0001:2", "tr-0001:3"],
               slots="all-free")
        nx = tr.next(PAST)
        self.assertEqual((nx["picks"], [o["id"] for o in nx["stack"]]), ([], ["st-0001"]))
        self.assertEqual([t["name"] for t, why in nx["waiting"]], ["poisk"])
        tr.set_state("poisk", "impl")
        nx = self.trains().next(PAST)
        self.assertEqual(nx["free"], 3)
        self.assertEqual(["{}:{}".format(p["track"]["id"], p["car"]["n"]) for p in nx["picks"]],
                         ["tr-0002:R0", "tr-0001:0", "tr-0001:1"])  # every free slot, in the train's order
        self.assertEqual([p["train"]["name"] for p in nx["picks"]], ["poisk"] * 3)
        self.assertEqual(nx["stack"], [])                           # the train came first
        self.assertEqual([u["car"]["n"] for u in nx["upcoming"] if u.get("car")], ["2"])
        # The same cars under `max 2`: two, and the third free slot goes to the stack's queue.
        self.trains().set("poisk", slots="max 2")
        nx = self.trains().next(PAST)
        self.assertEqual([p["car"]["n"] for p in nx["picks"]], ["R0", "0"])
        self.assertEqual([o["id"] for o in nx["stack"]], ["st-0001"])
        # Two busy cars fill `max 2`: nothing more from this train.
        self.tracks().car_set("tr-0001:0", status="running")
        self.tracks().car_set("tr-0001:1", status="pr", pr=200)
        tr = self.trains()
        nx = tr.next(PAST)
        self.assertEqual(nx["picks"], [])
        self.assertEqual([o["id"] for o in nx["stack"]], ["st-0001"])
        self.assertIn("Можно сейчас: ничего", "\n".join(tr.next_lines(PAST, train=tr.get("poisk"))))
        self.assertEqual(parse_slots("≤ 2"), {"policy": "max", "n": 2})
        with self.assertRaises(TrainError):
            parse_slots("max 0")

    def test_after_a_track_a_stack_object_a_pr_and_a_train(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("Закладки", pr=190)
        tk = self.tracks()
        a, b = tk.add("Первый"), tk.add("Второй")
        tk.car_add(a["id"], "единственный", size="S", after=["st-0001"])
        tk.car_add(b["id"], "после #190", size="S", after=["#190"])
        tr = self.trains()
        tr.add("first", "Первый", cars=["tr-0001:0"])
        tr.add("second", "Второй", cars=["tr-0002:0"], after=["tr-0001", "first"])
        tr.set_state("first", "impl")
        self.assertEqual(self.trains().next(PAST)["picks"], [])
        stk.start("st-0001")
        stk.done("st-0001", "merged #190")
        self.assertEqual([p["car"]["n"] for p in self.trains().next(PAST)["picks"]], ["0"])
        with self.assertRaises(TrainError):  # the track and the train it waits for are not done
            self.trains().set_state("second", "impl")
        self.tracks().car_set("tr-0001:0", status="merged")  # the track is done; the train arrives with it
        self.assertEqual(self.trains().get("first")["state"], "arrived")
        tr = self.trains()
        self.assertEqual([r["train"]["name"] for r in tr.next(PAST)["ready"]], ["second"])
        tr.set_state("second", "impl")
        nx = self.trains().next(PAST)
        self.assertEqual(["{}:{}".format(p["track"]["id"], p["car"]["n"]) for p in nx["picks"]], ["tr-0002:0"])
        md = self.trains().md(now=PAST)
        self.assertIn("| 1 | second «Второй» | P5 | имплементация | с 20", md)
        self.assertNotIn("first «Первый»", md)  # an arrived train only with --all
        self.assertIn("| first «Первый» | P5 | прибыл |", self.trains().md(include_done=True, now=PAST))

    def test_only_autostart_objects_are_picked(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("без флага")
        stk.add("сам", autostart=True)
        stk.add("ждёт st-0001", autostart=True, depends=["st-0001"])
        tk = self.tracks()
        a = tk.add("Поиск")
        tk.car_add(a["id"], "индекс", size="M")
        tk.car_stack("tr-0001:0")
        self.ride("tr-0001", "poisk")
        nx = self.trains().next(PAST)
        self.assertEqual([p["command"] for p in nx["picks"]], ["depot2 stack start st-0004"])  # its train's gates
        self.assertEqual([o["id"] for o in nx["stack"]], ["st-0002"])
        self.assertEqual([(w["stack"]["id"], w["why"]) for w in nx["owner_word"]],
                         [("st-0001", "ждёт слова владельца (автозапуск — нет)")])
        lines = "\n".join(self.trains().next_lines(PAST))
        self.assertIn("Потом из очереди стека: st-0002 «сам» (P5) — `depot2 stack start st-0002`.", lines)
        self.assertIn("- st-0001 «без флага» — ждёт слова владельца (автозапуск — нет): `depot2 stack start st-0001`",
                      lines)
        self.assertNotIn("st-0004 «", lines.split("Очередь стека")[1])
        data = json.loads(self.team("trains", "next", "--json")[1])
        self.assertEqual((data["stack"], data["owner_word"][0]["id"]), (["st-0002"], "st-0001"))
        self.assertIn("| 4 | очередь | st-0004 tr-0001:0 Поиск — индекс | P5 | — | ждёт слота | вагон |",
                      self.team("stack")[1])
        self.t.cfg["stack"]["slots"] = 1
        nx = self.trains().next(PAST)
        self.assertEqual(([p["car"]["n"] for p in nx["picks"]], nx["stack"]), (["0"], []))
        self.assertEqual([u["stack"]["id"] for u in nx["upcoming"] if u.get("stack")], ["st-0002"])
        # `stack start` by hand works without the flag
        self.assertEqual(self.team("--set", "stack.slots=4", "stack", "start", "st-0001")[0], 0)


class SlotLinksTest(TracksBase):

    def test_the_slot_shows_its_car_its_train_and_its_track(self):
        tk = self.tracks()
        a = tk.add("Аккаунты")
        tk.car_add(a["id"], "почта", size="S")
        tk.car_add(a["id"], "сессии", size="S")
        self.ride("tr-0001", "accounts", slots="max 2")
        self.tracks().car_stack("tr-0001:0", start=True)
        self.trains().car_drop("accounts", "tr-0001:1")
        self.tracks().car_stack("tr-0001:1", start=True)
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("просто задача")
        stk.start("st-0003")
        md = self.team("stack", "--md")[1]
        self.assertIn("| Замок | Этап | Поезд | Трек |", md)
        self.assertRegex(md, r"\| С1 \| слот \| st-0001 tr-0001:0 Аккаунты — почта \|.*\| сборка \| accounts Аккаунты \| "
                             r"tr-0001 Аккаунты \|")
        self.assertRegex(md, r"\| С2 \| слот \| st-0002 .*\| сборка \| — \| tr-0001 Аккаунты \|")
        self.assertRegex(md, r"\| С3 \| слот \| st-0003 просто задача \|.*\| сборка \| — \| — \|")
        sj = json.loads(self.team("stack", "--json")[1])["in_slots"]
        self.assertEqual([(o["car"], o["train"], o["track"]) for o in sj],
                         [("tr-0001:0", "accounts", "tr-0001"), ("tr-0001:1", None, "tr-0001"), (None, None, None)])
        cells = json.loads(self.team("board", "--json")[1])["slots"]["cells"]
        self.assertEqual([(c.get("ref"), c.get("train"), c.get("track")) for c in cells],
                         [("tr-0001:0", "accounts", "tr-0001"), ("tr-0001:1", None, "tr-0001"), ("st-0003", None, None),
                          (None, None, None)])
        bmd = self.team("board", "--md")[1]
        self.assertIn("| Слот | Вагон | Что | Поезд | Трек | В слоте | Ворота дальше |", bmd)
        self.assertIn("| 1 | tr-0001:0 (st-0001) | почта | accounts «Аккаунты» | tr-0001 Аккаунты |", bmd)
        self.assertIn("| 2 | tr-0001:1 (st-0002) | сессии | — | tr-0001 Аккаунты |", bmd)
        car = json.loads(self.team("car", "show", "st-0001", "--json")[1])
        self.assertEqual((car["track"], car["train"]), ({"id": "tr-0001", "title": "Аккаунты"},
                                                        {"name": "accounts", "title": "Аккаунты", "state": "impl"}))
        self.assertIn("Трек: tr-0001 Аккаунты. Поезд: accounts «Аккаунты».", self.team("car", "show", "tr-0001:0")[1])


# --- the migration (schema 1 → 2) ------------------------------------------------------------------------------

def _car(n, title, status="waiting", **kw):
    return dict({"n": n, "title": title, "status": status, "after": [], "notes": [],
                 "added": "2015-11-05T10:00:00+02:00"}, **kw)


def old_depot(state):
    """A depot of before: one trains.json of planned trains, its history, a stack with a car's object."""
    start = {"after": [], "not_before": None, "owner": True, "note": None}
    data = {"next": 5, "trains": [
        {"id": "tr-0001", "title": "Поиск", "status": "done", "slots": {"policy": "all-free", "n": None},
         "start": dict(start, owner=False), "est_mtok": None, "added": "2015-11-05", "started": "2015-11-05",
         "finished": "2015-11-09", "notes": ["заметка"],
         "cars": [_car("0", "док", "merged", stage="arrived", pr=180), _car("1", "индекс", "dropped", stage="scrapped")]},
        {"id": "tr-0002", "title": "Аккаунты: почта, сессии, 2FA", "status": "running",
         "slots": {"policy": "max", "n": 4}, "start": dict(start, after=["tr-0001"], note="после поиска"),
         "est_mtok": 120.0, "added": "2015-11-05", "started": "2015-11-07", "finished": None, "notes": [],
         "cars": [_car("0", "пароли", "merged", stage="arrived", pr=118),
                  _car("M", "почта", "running", stage="build", st="st-0001", branch="acc-mail"),
                  _car("1", "сессии", stage="sketch", after=["M"]), _car("2", "2FA", stage="bogie")]},
        {"id": "tr-0003", "title": "Читательский", "status": "waiting", "slots": {"policy": "max", "n": 2},
         "start": dict(start, after=["tr-0002"], not_before="2015-11-26T00:00+02:00"), "est_mtok": None,
         "added": "2015-11-06", "started": None, "finished": None, "notes": [],
         "cars": [_car("0", "полка", stage="sketch"), _car("1", "отзывы", "ready", stage="sketch")]},
        {"id": "tr-0004", "title": "Импорт каталога", "status": "paused", "slots": {"policy": "max", "n": 1},
         "start": start, "est_mtok": None, "added": "2015-11-06", "started": None, "finished": None, "notes": [],
         "cars": [_car("0", "форматы")]}]}
    write_json(state.path("trains.json"), data)
    rows = [{"ts": "2015-11-05T10:00:00+02:00", "event": "add", "train": "tr-0001", "title": "Поиск"},
            {"ts": "2015-11-07T10:00:00+02:00", "event": "car_stack", "train": "tr-0002", "car": "M", "st": "st-0001"},
            {"ts": "2015-11-05T09:00:00+02:00", "event": "import", "train": None, "trains": ["tr-0001"]}]
    with open(state.path("trains-history.jsonl"), "w", encoding="utf-8") as f:
        f.write("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows))
    stk = Stack(state, config.load(state.root))
    o = stk.add("tr-0002:M Аккаунты — почта", branch="acc-mail")
    stk.start(o["id"])
    return data


def files_of(d):
    return {f: open(os.path.join(d, f), "rb").read() for f in sorted(os.listdir(d))
            if f.endswith(".json") or f.endswith(".jsonl")}


class MigrateTest(TracksBase):
    def test_old_trains_become_tracks_and_trains_once(self):
        old = old_depot(self.t.state)
        self.assertTrue(migrate.pending(self.t.state))
        before = files_of(self.t.dir)
        lines, n = migrate.run(self.t.state, self.t.cfg, dry_run=True)
        self.assertEqual(n, 1)
        self.assertIn("DRY RUN: ничего не записано", lines)
        self.assertEqual(files_of(self.t.dir), before)  # a dry run writes nothing
        lines, n = migrate.run(self.t.state, self.t.cfg)
        text = "\n".join(lines)
        self.assertIn("треки: 4 (из 4 поездов), вагонов 9", text)
        self.assertIn("akkaunty ← tr-0002 «Аккаунты: почта, сессии, 2FA»: имплементация, вагонов 3", text)
        self.assertIn("chitatelskiy ← tr-0003 «Читательский»: дизайн, вагонов 2", text)
        self.assertIn("tr-0001 «Поиск» — прибыл: поезда нет", text)
        self.assertIn("tr-0004 «Импорт каталога» — на паузе: поезда нет", text)
        self.assertFalse(migrate.pending(self.t.state))
        # the tracks: the same cars, their statuses, stages and PRs; the same ids
        tk = Tracks(self.t.state, self.t.cfg)
        self.assertEqual(migrate.cars_of(tk.tracks()), migrate.cars_of(old["trains"]))
        self.assertEqual(sum(len(t["cars"]) for t in tk.tracks()), 9)
        self.assertEqual([t["id"] for t in tk.tracks()], ["tr-0001", "tr-0002", "tr-0003", "tr-0004"])
        self.assertEqual((tk.get("tr-0002")["est_mtok"], tk.get("tr-0001")["notes"]), (120.0, ["заметка"]))
        self.assertNotIn("status", tk.get("tr-0002"))
        self.assertEqual(tk.data["next"], 5)
        # the trains: the open cars in order, the start rules and slots; the state by a build or a PR
        tr = Trains(self.t.state, self.t.cfg)
        self.assertEqual([(t["name"], t["state"], t["cars"]) for t in tr.trains()],
                         [("akkaunty", "impl", ["tr-0002:M", "tr-0002:1", "tr-0002:2"]),
                          ("chitatelskiy", "design", ["tr-0003:0", "tr-0003:1"])])
        self.assertEqual(tr.get("chitatelskiy")["start"],
                         {"after": ["tr-0002"], "not_before": "2015-11-26T00:00+02:00", "owner": True, "note": None})
        self.assertEqual(tr.get("akkaunty")["slots"], {"policy": "max", "n": 4})
        # the stack's object is still its car's, now with its train and its track
        self.assertEqual(Stack(self.t.state, self.t.cfg).car_places()["st-0001"]["train"], "akkaunty")
        self.assertEqual(tk.car_of_stack("st-0001")[1]["n"], "M")
        # the history: the old rows read `track`; the files as they were, kept byte for byte
        hist = read_jsonl(self.t.state.path("tracks-history.jsonl"))
        self.assertEqual([h.get("track") for h in hist[:3]], ["tr-0001", "tr-0002", None])
        self.assertEqual(hist[2]["tracks"], ["tr-0001"])
        self.assertFalse(any("train" in h for h in hist))
        self.assertEqual([(h["event"], h["schema"]) for h in hist[-4:]], [("migrate", 2), ("migrate", 3),
                                                                          ("migrate", 4), ("migrate", 5)])
        self.assertEqual(hist[-4]["planning"]["tr-0004"]["status"], "paused")
        self.assertEqual(open(self.t.state.path(migrate.ARCHIVE_TRAINS), "rb").read(), before["trains.json"])
        self.assertEqual(open(self.t.state.path(migrate.ARCHIVE_HISTORY), "rb").read(), before["trains-history.jsonl"])
        self.assertEqual([h["train"] for h in Trains(self.t.state, self.t.cfg).history()], ["akkaunty", "chitatelskiy"])
        # a second run changes nothing
        snap = files_of(self.t.dir)
        lines, n = migrate.run(self.t.state, self.t.cfg)
        self.assertEqual((n, lines), (0, ["миграция данных депо: нечего переносить (схема %d)" % migrate.SCHEMA]))  # noqa: UP031
        self.assertEqual(files_of(self.t.dir), snap)

    def test_a_run_cut_short_runs_again_and_the_tool_migrates_first(self):
        old_depot(self.t.state)
        migrate.run(self.t.state, self.t.cfg)
        done = {f: read_json(self.t.state.path(f)) for f in ("tracks.json", "trains.json")}
        # cut short before the gate flipped: trains.json still the old one, everything else written
        shutil.copy(self.t.state.path(migrate.ARCHIVE_TRAINS), self.t.state.path("trains.json"))
        self.assertTrue(migrate.pending(self.t.state))
        rc, out, err = self.team("stack", "--md")  # any command migrates first, once
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 1 → 2", err)
        self.assertFalse(migrate.pending(self.t.state))
        tk = read_json(self.t.state.path("tracks.json"))
        self.assertEqual(migrate.cars_of(tk["tracks"]), migrate.cars_of(done["tracks.json"]["tracks"]))
        self.assertEqual([t["name"] for t in read_json(self.t.state.path("trains.json"))["trains"]],
                         [t["name"] for t in done["trains.json"]["trains"]])
        self.assertEqual([h["schema"] for h in read_jsonl(self.t.state.path("tracks-history.jsonl"))
                          if h["event"] == "migrate"], [2, 3, 4, 5])  # one row per step, the run cut short not counted twice
        rc, out, err = self.team("migrate")
        self.assertIn("нечего переносить", out)

    def test_it_waits_for_the_merge_queue_and_the_names(self):
        old_depot(self.t.state)
        p = self.t.state.path(merge.RUN_DIR, merge.RUN_LOCK)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w") as f:
            f.write("4242")
            f.flush()
            fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            with self.assertRaises(migrate.MigrateError) as e:
                migrate.run(self.t.state, self.t.cfg)
            self.assertIn("the merge queue is riding (pid 4242)", str(e.exception))
        self.assertTrue(migrate.pending(self.t.state))
        self.assertEqual([migrate.slug(t) for t in ("Аккаунты: почта, сессии, 2FA", "search-rev2: поиск и фильтры",
                                                     "Рассылки (weekly_digest)", "Хранение отчётов: миграции",
                                                     "Книги и полки под замком: архив", "Docker: сборка")],
                         ["akkaunty", "search-rev2", "rassylki", "khranenie-otchyotov", "knigi-i-polki", "docker"])
        self.assertEqual(migrate.unique("docker", {"docker"}), "docker-2")
        self.assertEqual(check_name(migrate.slug("Читательский")), "chitatelskiy")
