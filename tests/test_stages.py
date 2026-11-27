
import json
import os
import time
import unittest

from helpers import slurp
from test_extras import Base
from test_merge import MergeBase

from depot2 import merge, stages
from depot2.queues import Think, Wishes
from depot2.stack import Stack
from depot2.store import read_json, read_jsonl, write_json
from depot2.tracks import Tracks


def rows(rec):
    return [(r["from"], r["stage"]) for r in rec.get("stages") or []]


class RouteTest(unittest.TestCase):
    def test_the_route(self):
        stages.check_move("bogie", "sketch", None)
        stages.check_move("sketch", "project", None)
        stages.check_move("project", "build", None)
        stages.check_move("build", "acceptance", None)
        stages.check_move("acceptance", "queued", None)
        stages.check_move("queued", "rolling", None)
        stages.check_move("rolling", "arrived", None)
        stages.check_move("acceptance", "build", "доработать")
        stages.check_move("scrapped", "bogie", "вернули")
        for cur, to, text in (("bogie", "build", "«тележка» → «сборка» не по маршруту; из «тележка» можно: эскиз, списан"),
                              ("arrived", "bogie", "из «прибыл» можно: никуда (конечный этап)"),
                              ("rolling", "build", "из «в пути» можно: прибыл"),
                              ("acceptance", "build", "«приёмка» → «сборка» — шаг назад: нужна причина"),
                              ("project", "scrapped", "«проект» → «списан» — списание: нужна причина"),
                              ("sketch", "sketch", "уже на этапе «эскиз»")):
            with self.assertRaises(stages.StageError) as e:
                stages.check_move(cur, to, None)
            self.assertIn(text, str(e.exception))

    def test_names_and_records(self):
        self.assertEqual(stages.parse("На путях"), "queued")
        self.assertEqual(stages.parse("приемка"), "acceptance")
        self.assertEqual(stages.parse("build"), "build")
        with self.assertRaises(stages.StageError):
            stages.parse("депо")
        rec = {}
        stages.start(rec, "sketch", "stack add")
        self.assertIsNone(stages.record(rec, "sketch", "x"))  # there already: no row
        row = stages.record(rec, "acceptance", "stack post", "  PR готов ")
        self.assertEqual((row["from"], row["stage"], row["by"], row["why"], row["off_route"]),
                         ("sketch", "acceptance", "stack post", "PR готов", True))
        self.assertEqual(rows(rec), [(None, "sketch"), ("sketch", "acceptance")])
        self.assertEqual(stages.hist(row), "sketch→acceptance")
        self.assertIn("не по маршруту (stack post)", stages.say("st-0001", row))
        self.assertEqual(stages.prev_planning(rec), "sketch")


class StackStagesTest(Base):
    def stk(self):
        return Stack(self.t.state, self.t.cfg)

    def test_the_commands_move_an_objects_stage(self):
        brief = os.path.join(self.t.dir, "brief.md")
        with open(brief, "w") as f:
            f.write("# бриф\n")
        self.assertEqual(self.team("stack", "add", "--title", "Импорт", "--brief", brief)[0], 0)
        self.team("stack", "add", "--title", "Мысль")
        self.team("stack", "add", "--title", "Парсер", "--brief", brief, "--stage", "проект")
        rc, _, err = self.team("stack", "add", "--title", "x", "--stage", "build")
        self.assertEqual(rc, 1)
        self.assertIn("--stage при добавлении — тележка, эскиз или проект", err)
        s = self.stk()
        self.assertEqual([s.stage_of(o) for o in s.objects()], ["sketch", "bogie", "project"])
        self.team("stack", "start", "st-0001")
        self.team("stack", "set", "st-0001", "--pr", "301")
        self.team("stack", "post", "st-0001", "--reason", "CI зелёный")
        rc, out, err = self.team("stack", "start", "st-0001", "--reason", "доработать: подписи")
        self.assertEqual(rc, 0, err)
        self.team("stack", "post", "st-0001")
        o = self.stk().get("st-0001")
        self.assertEqual(rows(o), [(None, "sketch"), ("sketch", "build"), ("build", "acceptance"),
                                   ("acceptance", "build"), ("build", "acceptance")])
        back = o["stages"][3]
        self.assertEqual((back["by"], back["why"]), ("stack start", "доработать: подписи"))
        self.assertTrue(back["at"])
        h = [x for x in self.stk().history() if x["object"] == "st-0001" and x.get("stage")]
        self.assertEqual([x["stage"] for x in h], ["sketch", "sketch→build", "build→acceptance", "acceptance→build",
                                                   "build→acceptance"])
        rc, out, _ = self.team("stack", "done", "st-0001", "--reason", "merged #301")
        self.assertEqual(rc, 0)
        self.assertEqual(self.stk().left_stage("st-0001"), "rolling")  # merged by hand: rolls out; arrived by hand
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, "st-0001").stage(), "rolling")
        # off the route: a bogie straight into a slot is said, never refused
        rc, out, _ = self.team("stack", "start", "st-0002")
        self.assertEqual(rc, 0)
        self.assertIn("этап: st-0002 «тележка» → «сборка» не по маршруту (stack start); из «тележка» можно: эскиз, "
                      "списан", out)
        self.assertTrue(self.stk().get("st-0002")["stages"][-1]["off_route"])
        self.team("stack", "done", "st-0002", "--reason", "готово, без PR")
        self.assertEqual(self.stk().left_stage("st-0002"), "arrived")
        self.team("stack", "done", "st-0003", "--reason", "dropped: не нужен")
        self.assertEqual(self.stk().left_stage("st-0003"), "scrapped")
        # rolling → arrived by hand; the left object keeps it in a `stage` row
        rc, out, err = self.team("car", "stage", "st-0001", "прибыл", "--why", "выкачен руками")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001: в пути → прибыл (выкачен руками)", out)
        self.assertEqual(self.stk().left_obj("st-0001")["stage"], "arrived")
        self.assertEqual(self.stk().history()[-1]["event"], "stage")

    def test_park_keeps_build_and_a_team_item_has_no_stage(self):
        s = self.stk()
        s.add("Закладки")
        s.start("st-0001")
        s.set("st-0001", pr=205)
        s.park("st-0001", "ждёт замера")
        o = self.stk().get("st-0001")
        self.assertEqual((o["where"], o["stage"]), ("queue", "build"))
        self.assertEqual(self.stk().stage_default_why(dict(o, stage=None)), ("build", "запаркован, PR #205"))
        self.team("tstack", "add", "--kind", "discuss", "--title", "тема")
        js = json.loads(self.team("tstack", "--json")[1])
        self.assertNotIn("stage", js["queue"][0])
        self.assertNotIn("Этап", self.team("tstack")[1])

    def test_views(self):
        self.team("stack", "add", "--title", "Импорт")
        self.team("stack", "start", "st-0001")
        md = self.team("stack", "--md")[1]
        self.assertIn("| Зависит от | Замок | Этап |", md)
        self.assertIn("| С1 | слот | st-0001 Импорт | P5 | — | агент работает | нет | — | — | — | сборка |", md)
        self.assertIn("| **Сборка — в слотах (1/4)** | | | | | | | | | | | | |", md)
        self.assertIn("Этап: тележка → эскиз → (проект) → сборка", md)
        js = json.loads(self.team("stack", "--json")[1])
        self.assertEqual(js["in_slots"][0]["stage"], "build")
        self.assertEqual(rows(js["in_slots"][0]), [(None, "bogie"), ("bogie", "build")])
        out = self.team("queues", "--md")[1]
        self.assertIn("Этапы вагонов (стек, открытые вагоны поездов, хотелки, «подумать»; без прибывших и списанных): "
                      "1 — сборка 1", out)


class TrainStagesTest(Base):
    def setUp(self):
        super().setUp()
        self.brief = os.path.join(self.t.dir, "brief.md")
        with open(self.brief, "w") as f:
            f.write("# бриф\n")
        self.team("tracks", "add", "--title", "Поезд")
        self.team("tracks", "car", "add", "tr-0001", "--title", "парсер: поле", "--brief", self.brief)
        self.team("tracks", "car", "add", "tr-0001", "--title", "идея")

    def car(self, ref="tr-0001:0"):
        return Tracks(self.t.state, self.t.cfg).car(ref)[1]

    def test_a_car_and_its_object_are_one_car(self):
        self.assertEqual((self.car()["stage"], self.car("tr-0001:1")["stage"]), ("sketch", "bogie"))
        rc, out, err = self.team("car", "stage", "tr-0001:0", "project", "--why", "парсер: проект обязателен")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0: эскиз → проект (парсер: проект обязателен)", out)
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:0", "--start")
        self.assertEqual(rc, 0, err)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual(rows(o), [(None, "project"), ("project", "build")])  # the object starts at its car's stage
        self.assertEqual(rows(self.car()), [(None, "sketch"), ("sketch", "project"), ("project", "build")])
        self.team("stack", "set", "st-0001", "--pr", "401")
        self.team("stack", "post", "st-0001")
        self.assertEqual(self.car()["stage"], "acceptance")
        self.team("stack", "start", "st-0001", "--reason", "доработать")
        self.assertEqual((self.car()["stage"], self.car()["stages"][-1]["why"]), ("build", "доработать"))
        # the car stage refuses what the stack does, and names the command
        rc, _, err = self.team("car", "stage", "st-0001", "acceptance")
        self.assertEqual(rc, 1)
        self.assertIn("приёмка — depot2 stack post st-0001", err)
        rc, _, err = self.team("car", "stage", "tr-0001:0", "sketch", "--why", "x")
        self.assertIn("st-0001 в слоте: назад к брифу — depot2 stack done st-0001 --car back", err)
        # back to its track with no PR: the stage it had before the build
        t = Tracks(self.t.state, self.t.cfg)
        t.car("tr-0001:0")[1]["pr"] = None
        t._save("test", "tr-0001")
        s = Stack(self.t.state, self.t.cfg)
        s.get("st-0001")["pr"] = None
        s._save("test", "st-0001")
        rc, out, err = self.team("stack", "done", "st-0001", "--reason", "бриф не тот", "--car", "back")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.car()["stage"], "project")
        self.assertEqual(Stack(self.t.state, self.t.cfg).left_obj("st-0001")["stage"], "project")
        # a status set by hand moves the stage
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--status", "dropped", "--note", "не будем")
        self.assertEqual(rc, 0, err)
        self.assertEqual((self.car("tr-0001:1")["stage"], self.car("tr-0001:1")["stages"][-1]["why"]),
                         ("scrapped", "не будем"))

    def test_views_and_show(self):
        cm = self.team("track", "tr-0001")[1]
        self.assertIn("| Вагон | Приоритет | Что | После | Размер: effort (оценка / факт) | Ветка / PR | Стек | Поезд | Статус | Этап |",
                      cm)
        self.assertIn("| 0 | P5 | парсер: поле | — | — | — | — | — | готов | эскиз |", cm)
        self.assertIn("| 1 | P5 | идея | — | — | — | — | — | готов | тележка |", cm)
        md = self.team("tracks")[1]
        self.assertIn("| Состояние | Этапы вагонов |", md)
        self.assertIn("| тележка 1 · эскиз 1 |", md)
        js = json.loads(self.team("track", "tr-0001", "--json")[1])
        self.assertEqual([c["stage"] for c in js["cars"]], ["sketch", "bogie"])
        self.assertEqual(json.loads(self.team("tracks", "--json")[1])["tracks"][0]["cars"][1]["stage"], "bogie")
        rc, out, _ = self.team("car", "show", "tr-0001:0")
        self.assertEqual(rc, 0)
        self.assertIn("# tr-0001:0 — эскиз", out)
        self.assertIn("Отсюда по маршруту: проект, сборка, тележка, списан.", out)
        self.assertRegex(out, r"- \d{4}-\d\d-\d\d \d\d:\d\d → эскиз \(tracks car add\)")
        js = json.loads(self.team("car", "show", "tr-0001:1", "--json")[1])
        self.assertEqual((js["car"], js["stage"], js["object"]), ("tr-0001:1", "bogie", None))
        self.assertEqual(self.team("car", "show", "tr-0001:9")[0], 1)
        self.assertIn("вагон — это st-NNNN", self.team("car", "show", "xx-1")[2])

    def test_force_is_a_correction_with_a_reason(self):
        rc, _, err = self.team("car", "stage", "tr-0001:1", "build", "--force")
        self.assertEqual(rc, 1)
        self.assertIn("--force — только с причиной", err)
        rc, out, err = self.team("car", "stage", "tr-0001:1", "build", "--force", "--why", "перенос ошибся")
        self.assertEqual(rc, 0, err)
        self.assertIn("[--force]", out)
        r = self.car("tr-0001:1")["stages"][-1]
        self.assertEqual((r["stage"], r["forced"], r["off_route"]), ("build", True, True))


class QueueStagesTest(Base):
    def test_wishes_and_think(self):
        self.team("wishes", "add", "--title", "кнопка", "--words", "сделай кнопку")
        self.team("wishes", "add", "--title", "обложки")
        self.team("wishes", "add", "--title", "автоимпорт")
        self.team("think", "add", "--topic", "подсказки", "--question", "Нужна ли она?", "--question", "Когда?")
        self.team("think", "add", "--topic", "обложки")
        w, th = Wishes(self.t.state), Think(self.t.state)
        self.assertEqual([e["stage"] for e in w.entries()], ["bogie"] * 3)
        self.assertEqual([(e["stage"], e["question"]) for e in th.entries()],
                         [("bogie", "Нужна ли она?"), ("bogie", "обложки")])
        self.team("stack", "add", "--title", "Кнопка")
        self.team("stack", "start", "st-0001")
        self.team("wish", "take", "wl-0001", "--to", "st-0001")
        self.team("wishes", "drop", "wl-0002", "--reason", "не в v1")
        rc, out, _ = self.team("wishes", "done", "wl-0003", "--reason", "сделано в #300")
        self.assertIn("этап: wl-0003 «тележка» → «прибыл» не по маршруту (wishes done)", out)
        self.team("think", "drop", "th-0002", "--reason", "слито")
        w, th = Wishes(self.t.state), Think(self.t.state)
        self.assertEqual([e["stage"] for e in w.entries()], ["bogie", "scrapped", "arrived"])
        self.assertEqual(w.get("wl-0002")["stages"][-1]["why"], "не в v1")
        self.assertEqual(th.get("th-0002")["stage"], "scrapped")
        md = self.team("wishes", "--all")[1]
        self.assertIn("| Приоритет | Статус | Этап |", md)
        self.assertIn("| взята → st-0001 | тележка → сборка |", md)  # the wish lives on in what took it
        self.assertIn("| отклонена | списан |", md)
        self.assertIn("| Статус | Этап |", self.team("think")[1])
        self.assertEqual(json.loads(self.team("wishes", "--json", "--all")[1])[1]["stage"], "scrapped")
        rc, _, err = self.team("car", "stage", "wl-0001", "scrapped", "--why", "x")
        self.assertIn("списан — depot2 wishes drop wl-0001", err)
        rc, out, err = self.team("car", "stage", "th-0001", "sketch")
        self.assertEqual(rc, 0, err)
        self.assertIn("stages:", self.team("think", "show", "th-0001")[1])
        self.assertIn("Взята → st-0001: сборка.", self.team("car", "show", "wl-0001")[1])
        # a todo is no car
        self.team("todo", "add", "--text", "опечатка", "--version", "v")
        self.assertNotIn("stage", self.team("todo", "show", "td-0001")[1])
        self.assertNotIn("Этап", self.team("todo")[1])
        self.assertIn("вагон — это st-NNNN", self.team("car", "show", "td-0001")[2])


class MigrateStagesTest(Base):
    """`queues migrate` writes the stage the place says on every car from before the field, once."""

    def setUp(self):
        super().setUp()
        brief = os.path.join(self.t.dir, "brief.md")
        with open(brief, "w") as f:
            f.write("# бриф\n")
        s = Stack(self.t.state, self.t.cfg)
        s.add("в слоте")
        s.add("в пост-стеке", pr=302)
        s.add("в мердж-очереди", pr=303)
        s.add("с брифом", brief=brief)
        s.add("голый")
        s.start("st-0001")
        s.start("st-0002")
        s.post("st-0002")
        s.start("st-0003")
        s.post("st-0003")
        write_json(self.t.state.path("merge-queue.json"), {"next": 2, "entries": [
            {"id": "mq-0001", "pr": 303, "st": "st-0003", "car": None, "status": "waiting", "branch": "b3"}]})
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Поезд")
        for n, status in (("a", "merged"), ("b", "dropped"), ("c", "waiting"), ("d", "pr"), ("e", "waiting")):
            tr.car_add(t["id"], "вагон " + n, n=n, status=status, brief=brief if n == "e" else None,
                       pr=404 if n == "d" else None)
        tr.car_set("tr-0001:d", st="st-0005")
        Wishes(self.t.state).add("хотелка")
        Wishes(self.t.state).add("ещё одна")
        self.team("wishes", "drop", "wl-0002", "--reason", "нет")
        Think(self.t.state).add("вопрос", questions=["Какой?"])
        self.strip()

    def strip(self):
        """As before the field: no stage, no history, no question."""
        for name, key in (("stack.json", "objects"), ("wishes.json", "entries"), ("think.json", "entries")):
            d = read_json(self.t.state.path(name))
            for r in d[key]:
                for k in ("stage", "stages", "question"):
                    r.pop(k, None)
            write_json(self.t.state.path(name), d)
        d = read_json(self.t.state.path("tracks.json"))
        for t in d["tracks"]:
            for c in t["cars"]:
                c.pop("stage", None)
                c.pop("stages", None)
        write_json(self.t.state.path("tracks.json"), d)

    def files(self):
        return {n: slurp(self.t.state.path(n)) for n in ("stack.json", "tracks.json", "wishes.json", "think.json",
                                                         "stack-history.jsonl", "tracks-history.jsonl")}

    def test_dry_run_then_once_and_idempotent(self):
        # before the move the views show the place's stage already
        self.assertIn("| на путях |", self.team("stack", "--md")[1])
        before = self.files()
        rc, out, err = self.team("queues", "migrate", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("- этапы: стек — записан у 5 объектов (тележка 1 · эскиз 1 · сборка 1 · приёмка 1 · на путях 1)", out)
        self.assertEqual(self.files(), before)  # wrote nothing
        rc, out, err = self.team("queues", "migrate")
        self.assertEqual(rc, 0, err)
        s = Stack(self.t.state, self.t.cfg)
        self.assertEqual({o["id"]: o["stage"] for o in s.objects()}, {
            "st-0001": "build", "st-0002": "acceptance", "st-0003": "queued", "st-0004": "sketch", "st-0005": "bogie"})
        self.assertEqual(s.get("st-0003")["stages"], [dict(s.get("st-0003")["stages"][0], **{
            "stage": "queued", "from": None, "by": "queues migrate", "why": "перенос: в мердж-очереди mq-0001"})])
        cars = {c["n"]: c["stage"] for c in Tracks(self.t.state, self.t.cfg).get("tr-0001")["cars"]}
        self.assertEqual(cars, {"a": "arrived", "b": "scrapped", "c": "bogie", "d": "bogie", "e": "sketch"})
        self.assertEqual(Tracks(self.t.state, self.t.cfg).car("tr-0001:d")[1]["stages"][0]["why"],
                         "перенос: как его объект st-0005")
        self.assertEqual([e["stage"] for e in Wishes(self.t.state).entries()], ["bogie", "scrapped"])
        e = Think(self.t.state).get("th-0001")
        self.assertEqual((e["stage"], e["question"]), ("bogie", "Какой?"))
        self.assertIn("- этапы: вагоны треков — записан у 5 (тележка 2 · эскиз 1 · прибыл 1 · списан 1)", out)
        self.assertIn("- этапы: на подумать — записан у 1 (тележка 1); вопрос (question) у 1", out)
        self.assertIn("перенесено: 14 записей и объектов", out)  # 5 objects, 5 cars, 2 wishes, 1 entry + its question
        h = Stack(self.t.state, self.t.cfg).history()[-1]
        self.assertEqual((h["event"], h["objects"]["st-0003"]), ("migrate_stage", "queued"))
        after = self.files()
        rc, out, _ = self.team("queues", "migrate")
        self.assertIn("перенесено: 0 записей и объектов", out)
        self.assertIn("нечего переносить: миграция уже прошла", out)
        self.assertEqual(self.files(), after)


class MergeStagesTest(MergeBase):
    def test_on_the_tracks_then_rolling_then_arrived(self):
        oid = self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.assertEqual(Stack(self.t.state, self.t.cfg).get(oid)["stage"], "acceptance")
        rc, out, err = self.team("merge", "add", "301", "--word", "301 — мердж")
        self.assertEqual(rc, 0, err)
        o = Stack(self.t.state, self.t.cfg).get(oid)
        self.assertEqual((o["stage"], o["stages"][-1]["why"], o["stages"][-1]["by"]),
                         ("queued", "слово владельца: «301 — мердж» (mq-0001)", "merge add"))
        self.assertEqual(self.entry(301)["prev_stage"], "acceptance")
        self.assertIn("| М1 | мердж | st-0001 PR 301 | P5 | feat-a / PR #301 | ждёт | — | — | — | — | на путях |",
                      self.team("stack", "--md")[1])
        self.assertIn("| на путях |", self.team("merge")[1])
        self.assertEqual(json.loads(self.team("merge", "--json")[1])["entries"][0]["stage"], "queued")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        left = Stack(self.t.state, self.t.cfg).left_obj(oid)
        self.assertEqual(rows(left)[-2:], [("queued", "rolling"), ("rolling", "arrived")])
        self.assertEqual(left["stages"][-2]["by"], "merge run (mq-0001)")
        self.assertIn("выкачен: #301", left["stages"][-1]["why"])
        self.assertIn("st-0001: в пути → прибыл", out)

    def test_drop_goes_back_and_a_car_follows(self):
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Поезд")
        tr.car_add(t["id"], "вагон", n="1", branch="feat-a")
        self.pr(301, "feat-a", {"a.txt": "A\n"}, stack=False)
        self.assertEqual(self.team("tracks", "car", "stack", "tr-0001:1", "--start")[0], 0)
        self.team("stack", "set", "st-0001", "--pr", "301")
        self.team("stack", "post", "st-0001")
        self.team("merge", "add", "301", "--word", "мердж")
        def car():
            return Tracks(self.t.state, self.t.cfg).car("tr-0001:1")[1]
        self.assertEqual(car()["stage"], "queued")
        rc, out, err = self.team("merge", "drop", "301", "--reason", "чинить")
        self.assertEqual(rc, 0, err)
        self.assertEqual((car()["stage"], Stack(self.t.state, self.t.cfg).get("st-0001")["stage"]),
                         ("acceptance", "acceptance"))
        self.team("merge", "add", "301", "--word", "мердж, теперь да")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(rows(car())[-3:], [("acceptance", "queued"), ("queued", "rolling"), ("rolling", "arrived")])
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, "st-0001").stage(), "arrived")

    def test_a_car_merged_by_hand_rides_out_with_the_next_ride(self):
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Поезд")
        for n in ("1", "2"):
            tr.car_add(t["id"], "вагон " + n, n=n)
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--status", "merged")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:1 «в пути»: «прибыл» поставит следующая выкатка мердж-очереди: она берёт всё, что "
                      "слито раньше неё; вручную (выкатка уже прошла) — depot2 car stage tr-0001:1 arrived --why "
                      "«выкачен с #N …»", out)
        self.assertNotIn("warning", out)
        self.team("tracks", "car", "set", "tr-0001:2", "--status", "merged")
        left = self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.assertEqual(self.team("stack", "done", left, "--reason", "слит руками")[0], 0)
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, left).stage(), "rolling")
        self.assertIn("Дальше: «прибыл» поставит следующая выкатка мердж-очереди",
                      self.team("car", "show", "tr-0001:1")[1])
        self.clock.t = time.time() + 3600  # the queue's clock: its ride merges after the merges by hand
        self.clock.write()
        d = read_json(self.t.state.path("tracks.json"))
        d["tracks"][0]["cars"][1]["stages"][-1]["at"] = merge.stamp(self.clock.t + 86400)
        write_json(self.t.state.path("tracks.json"), d)
        oid = self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.team("merge", "add", "301", "--word", "301 — мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertIn(f"{oid}: в пути → прибыл", out)  # its own car first
        self.assertIn("tr-0001:1: в пути → прибыл (слит руками ", out)
        self.assertIn(f"{left}: в пути → прибыл (слит руками ", out)
        c1 = Tracks(self.t.state, self.t.cfg).car("tr-0001:1")[1]
        self.assertEqual((c1["stage"], c1["stages"][-1]["why"], c1["stages"][-1]["by"]),
                         ("arrived", "выкачен с #301 (mq-0001): слит руками раньше", "merge run (mq-0001, слит руками)"))
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, left).stage(), "arrived")
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, "tr-0001:2").stage(), "rolling")  # after the ride
        self.assertIn("Дальше: «прибыл» поставит следующая выкатка", self.team("car", "show", "tr-0001:2")[1])
        self.assertNotIn("Дальше:", self.team("car", "show", "tr-0001:1")[1])
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--note", "после выкатки")  # closed now: warned
        self.assertIn("warning: tr-0001:1 «вагон 1» — слит, этап «прибыл»: запись идёт в закрытый вагон", out)


class MigrateArrivalsTest(Base):

    FILES = ("tracks.json", "trains.json", "stack.json", "stack-history.jsonl", "tracks-history.jsonl",
             "merge-queue.json")

    def ride(self, n, pr, ts, status="merged", **kw):
        e = {"id": "mq-%04d" % n, "pr": pr, "branch": "b%d" % pr, "title": "PR %d" % pr, "status": status,  # noqa: UP031
             "added": ts, "merged": {"sha": "%07d" % pr, "ts": ts} if status == "merged" or kw.get("merged") else None}  # noqa: UP031
        e.update(kw)
        return e

    def depot4(self):
        """A depot at schema 4: four cars merged by hand at their times, the queue's rides around them."""
        tr = Tracks(self.t.state, self.t.cfg)
        t = tr.add("Поезд")
        at = {"1": "2015-11-16T10:00:00+02:00", "2": "2015-11-17T10:00:00+02:00", "3": "2015-11-19T10:00:00+02:00"}
        for n in ("1", "2", "3", "4"):
            tr.car_add(t["id"], "вагон " + n, n=n)
            if n in at:
                tr.car_set("tr-0001:" + n, status="merged")
        d = read_json(self.t.state.path("tracks.json"))
        for c in d["tracks"][0]["cars"]:
            if c["n"] in at:
                c["stages"][-1]["at"] = at[c["n"]]
        write_json(self.t.state.path("tracks.json"), d)
        write_json(self.t.state.path("merge-queue.json"), {"next": 5, "entries": [
            self.ride(1, 301, "2015-11-16T12:00:00+02:00"),
            self.ride(2, 302, "2015-11-18T12:00:00+02:00"),
            self.ride(3, 303, "2015-11-18T13:00:00+02:00", status="dropped"),  # dropped: no ride
            self.ride(4, 304, "2015-11-20T12:00:00+02:00", handoff={"ts": "2015-11-20T12:00:00+02:00", "reason": "x"},
                      car=None),  # a handoff: its deploy rides with another, no ride of its own
        ]})
        for f in ("tracks.json", "trains.json"):
            d = read_json(self.t.state.path(f)) or {"trains": []}
            d["schema"] = 4
            write_json(self.t.state.path(f), d)

    def test_the_cars_before_the_last_ride_arrive_once(self):
        from depot2 import migrate
        self.depot4()
        self.assertEqual(migrate.version(self.t.state), 4)
        before = {f: slurp(self.t.state.path(f)) for f in self.FILES if os.path.exists(self.t.state.path(f))}
        lines, n = migrate.run(self.t.state, self.t.cfg, dry_run=True)
        self.assertEqual((n, lines[0]), (1, "DRY RUN: ничего не записано"))
        self.assertIn("последняя выкатка мердж-очереди: #302 (mq-0002), слит 2015-11-18 12:00; вагонов «в пути» до "
                      "неё, слитых мимо очереди: 2", lines)
        self.assertIn("- tr-0001:1 «вагон 1» (в пути с 2015-11-16 10:00) → прибыл: выкачен с #301 (mq-0001, "
                      "2015-11-16 12:00): слит руками раньше (миграция)", lines)
        self.assertIn("- tr-0001:2 «вагон 2» (в пути с 2015-11-17 10:00) → прибыл: выкачен с #302 (mq-0002, "
                      "2015-11-18 12:00): слит руками раньше (миграция)", lines)
        self.assertTrue(any(x.startswith("остаются «в пути» ") and x.endswith(": 2 — tr-0001:3, #304") for x in lines),
                        lines)
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in before}, before)  # a dry run writes nothing
        rc, _out, err = self.team("board")  # any command migrates first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 4 → 5", err)
        self.assertEqual(migrate.version(self.t.state), migrate.SCHEMA)
        self.assertEqual(read_json(self.t.state.path("tracks.json"))["schema"], migrate.SCHEMA)
        tr = Tracks(self.t.state, self.t.cfg)
        got = {n: tr.stage_of(tr.car("tr-0001:" + n)[1]) for n in ("1", "2", "3", "4")}
        self.assertEqual(got, {"1": "arrived", "2": "arrived", "3": "rolling", "4": "bogie"})
        row = tr.car("tr-0001:2")[1]["stages"][-1]
        self.assertEqual((row["from"], row["why"], row["by"]),
                         ("rolling", "выкачен с #302 (mq-0002, 2015-11-18 12:00): слит руками раньше (миграция)",
                          "depot2 migrate (схема 5)"))
        hist = [h for h in read_jsonl(self.t.state.path("tracks-history.jsonl")) if h["event"] == "migrate"]
        self.assertEqual([(h["schema"], h["arrived"], h["last_ride"]) for h in hist],
                         [(5, {"tr-0001:1": "mq-0001", "tr-0001:2": "mq-0002"}, "mq-0002")])
        snap = {f: slurp(self.t.state.path(f)) for f in before}
        self.assertEqual(migrate.run(self.t.state, self.t.cfg),
                         (["миграция данных депо: нечего переносить (схема %d)" % migrate.SCHEMA], 0))  # noqa: UP031
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in before}, snap)
        lines = migrate.step_arrivals(self.t.state, self.t.cfg, dry_run=True)  # cut short and run again: nothing left
        self.assertIn("последняя выкатка мердж-очереди: #302 (mq-0002), слит 2015-11-18 12:00; вагонов «в пути» до "
                      "неё, слитых мимо очереди: 0", lines)


