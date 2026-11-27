"""The board and the gates: the DoR into the build and the DoD into the acceptance, soft (refused with what is
missing; `--force --why` passes, recorded), the columns' limits, `trains next` holding back what its DoR holds, the
board's views (`--md`, `--json`, `--view stack`), the stable `--json` formats, the merge queue's `--at`, `move` and
`handoff`.
"""

import json
import os
import unittest

from helpers import BRIEF_TEXT, GATES_ALL
from test_extras import Base
from test_merge import MergeBase

from depot2 import gates, marks, stages
from depot2.stack import Stack, StackError
from depot2.tracks import Tracks

ALL = ["--set", "gates.dor=" + json.dumps(GATES_ALL["dor"]), "--set", "gates.dod=" + json.dumps(GATES_ALL["dod"])]


class GateBase(Base):
    def setUp(self):
        super().setUp()
        self.t.cfg["gates"] = json.loads(json.dumps(GATES_ALL))
        self.brief = os.path.join(self.t.dir, "brief.md")
        with open(self.brief, "w") as f:
            f.write(BRIEF_TEXT)

    def team(self, *argv):
        return super().team(*(ALL + list(argv)))

    def stk(self):
        return Stack(self.t.state, self.t.cfg)


class DorTest(GateBase):
    def test_the_dor_is_soft_force_needs_a_reason_and_is_recorded(self):
        self.team("stack", "add", "--title", "Без брифа", "--stage", "эскиз")
        rc, out, err = self.team("stack", "start", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: st-0001 → «сборка» не пройдены: DoR (вход в сборку) — нет брифа. Пройти всё равно — "
                      "только с причиной: depot2 stack start st-0001 --force --why «…»", err)
        self.assertEqual(self.stk().get("st-0001")["where"], "queue")  # nothing moved
        rc, _, err = self.team("stack", "start", "st-0001", "--force")
        self.assertEqual(rc, 1)
        self.assertIn("--force — только с причиной: --why «…» (не пройдено: DoR (вход в сборку) — нет брифа)", err)
        rc, out, err = self.team("stack", "start", "st-0001", "--force", "--why", "владелец: срочно, бриф в PR")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001: ворота пройдены с --force (владелец: срочно, бриф в PR): DoR (вход в сборку) — нет брифа",
                      out)
        row = self.stk().get("st-0001")["stages"][-1]
        self.assertEqual((row["from"], row["stage"], row["forced"], row["why"], row["gates"]),
                         ("sketch", "build", True, "владелец: срочно, бриф в PR", {"DoR": ["нет брифа"]}))
        h = self.stk().history()[-1]
        self.assertEqual((h["event"], h["forced"], h["gates"]), ("start", True, {"DoR": ["нет брифа"]}))
        show = self.team("car", "show", "st-0001")[1]
        self.assertIn("эскиз → сборка (владелец: срочно, бриф в PR; stack start) [--force, ворота: DoR (вход в сборку) — "
                      "нет брифа]", show)
        board = self.team("board")[1]
        self.assertRegex(board, r"\| st-0001 \| P5 \| Без брифа \| — \| — \| — \| слот \| [^|]+ \| DoD: [^|]+ · ⚑ --force \|")

    def test_the_items_brief_questions_project_after(self):
        missing = os.path.join(self.t.dir, "нет.md")
        self.team("stack", "add", "--title", "Бриф пропал", "--brief", missing)
        self.team("stack", "add", "--title", "Парсер", "--brief", self.brief, "--size", "L")
        self.team("stack", "add", "--title", "После", "--brief", self.brief, "--depends", "st-0002")
        def un(oid):
            s = self.stk()
            return gates.dor_unmet(s, None, obj=s.get(oid))
        self.assertEqual(un("st-0001"), [f"бриф не найден: {missing}"])
        self.assertEqual(un("st-0002"), ["проект обязателен (размер L), этапа «проект» не было (depot2 car stage … "
                                         "project)"])
        self.assertEqual(un("st-0003"), ["st-0003 depends on st-0002, not done yet"])
        # an open question holds it; its answer lets it go
        rc, out, err = self.team("car", "ask", "st-0003", "Импорт — до или после обложек?")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0003: вопрос 1 открыт: Импорт — до или после обложек?", out)
        self.assertIn("открытые вопросы: 1 (1. Импорт — до или после обложек?)", un("st-0003"))
        self.assertIn("- 1. Импорт — до или после обложек? — открыт", self.team("car", "show", "st-0003")[1])
        rc, _, err = self.team("car", "answer", "st-0003", "1", "--answer", "после", "--by", "владелец")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.team("car", "answer", "st-0003", "1", "--answer", "ещё раз")[0], 1)  # closed already
        q = self.stk().get("st-0003")["asks"][0]
        self.assertEqual((q["answer"], q["answered_by"]), ("после", "владелец"))
        self.team("car", "needs-project", "st-0001", "--why", "app/parser")
        self.assertIn("проект обязателен (app/parser)", " ".join(gates.dor_unmet(self.stk(), None, obj=self.stk().get("st-0001"))))
        self.team("car", "needs-project", "st-0001", "--off")
        self.assertFalse(self.stk().get("st-0001").get("needs_project"))
        self.assertEqual(self.team("car", "stage", "st-0002", "проект")[0], 0)
        self.assertEqual(gates.dor_unmet(self.stk(), None, obj=self.stk().get("st-0002")), [])
        self.assertEqual(self.team("stack", "start", "st-0002")[0], 0)  # passes: no force, nothing recorded
        self.assertNotIn("forced", self.stk().get("st-0002")["stages"][-1])
        self.assertIn("Проект обязателен (размер L): этап пройден.", self.team("car", "show", "st-0002")[1])

    def test_a_car_back_for_work_and_a_parked_object_skip_the_dor(self):
        s = self.stk()
        o = s.add("Уже собирался", pr=301)  # no brief: a PR means it was in the build already
        self.assertEqual(s.stage_of(o), "bogie")
        s.start(o["id"], force=True, why="старый объект")
        s.park(o["id"], "ждёт поезда")
        self.assertEqual(self.team("stack", "start", o["id"])[0], 0)  # build → build: no DoR
        self.assertEqual(self.team("stack", "post", o["id"], "--ci", "green", "--heavy", "green", "--scan", "clean",
                                   "--shots", "n/a")[0], 0)
        rc, _, err = self.team("stack", "start", o["id"], "--reason", "доработать")
        self.assertEqual(rc, 0, err)


class DodTest(GateBase):
    def test_the_dod_by_what_the_agent_handed_in(self):
        self.team("stack", "add", "--title", "Импорт", "--brief", self.brief)
        self.team("stack", "start", "st-0001")
        self.team("stack", "set", "st-0001", "--pr", "301")
        rc, _, err = self.team("stack", "post", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: st-0001 → «приёмка» не пройдены: DoD (вход в приёмку) — CI: не сдано, Heavy: не сдано, "
                      "скан: не сдано, шоты: не сдано. Пройти", err)  # no principles in the depot: nothing to check
        self.team("stack", "set", "st-0001", "--ci", "red", "--heavy", "local")
        rc, _, err = self.team("stack", "post", "st-0001", "--scan", "CLEAN", "--shots", "n/a")
        self.assertEqual(rc, 1)
        self.assertIn("DoD (вход в приёмку) — CI: красный, Heavy: local.", err)  # a red CI stays: the post does not hide it
        rc, _out, err = self.team("stack", "post", "st-0001", "--ci", "green", "--heavy", "green (412/412)", "--scan",
                                 "CLEAN", "--shots", "n/a")  # a refused post wrote nothing
        self.assertEqual(rc, 0, err)
        o = self.stk().get("st-0001")
        self.assertEqual(o["where"], "post")
        self.assertEqual({k: v["state"] for k, v in o["dod"].items()},
                         {"heavy": "green (412/412)", "scan": "CLEAN", "shots": "n/a"})
        self.assertEqual(o["stages"][-1]["stage"], "acceptance")
        self.assertNotIn("forced", o["stages"][-1])
        self.assertEqual(gates.dod_cells(self.t.cfg, o, principles=marks.evaluate({}, [])),
                         "CI зелёный · Heavy green (412/412) · скан CLEAN · шоты n/a · принципы: не нужны (третья "
                         "проверка пуста)")

    def test_dod_forced_and_the_words(self):
        self.assertEqual([gates.verdict(w) for w in ("green", "CLEAN", "n/a", "не нужны", "red", "local", "", "да")],
                         ["ok", "ok", "na", "na", "bad", "other", None, "ok"])
        s = self.stk()
        o = s.add("x", brief=self.brief, pr=302)
        s.start(o["id"])
        with self.assertRaises(StackError):
            s.post(o["id"])
        s.post(o["id"], force=True, why="полный прогон недоступен, владелец: ставь", dod={"scan": "clean"})
        row = s.get(o["id"])["stages"][-1]
        self.assertEqual(row["gates"], {"DoD": ["CI: не сдано", "Heavy: не сдано", "шоты: не сдано",
                                                "принципы: не проверены"]})  # a post with no verdict: not checked
        self.assertIsNone(s.get(o["id"])["ci"])
        self.assertTrue(row["forced"])


class LimitTest(GateBase):
    def test_the_columns_limits_are_soft(self):
        self.t.cfg["stack"]["slots"] = 1
        sets = ["--set", "stack.slots=1", "--set", "stack.post_max=1"]
        self.team(*([*sets, "stack", "add", "--title", "a", "--brief", self.brief]))
        self.team(*([*sets, "stack", "add", "--title", "b", "--brief", self.brief]))
        self.assertEqual(self.team(*([*sets, "stack", "start", "st-0001"]))[0], 0)
        rc, _, err = self.team(*([*sets, "stack", "start", "st-0002"]))
        self.assertEqual(rc, 1)
        self.assertIn("лимит колонки — no free slot (1/1), stack.slots", err)
        rc, out, err = self.team(*([*sets, "stack", "start", "st-0002", "--force", "--why", "владелец: пятый агент"]))
        self.assertEqual(rc, 0, err)
        self.assertIn("| **Сборка — в слотах (2/1) ⚠ сверх лимита** |", out)
        self.assertIn("| С+ | слот | st-0002 b |", out)
        board = self.team(*([*sets, "board"]))[1]
        self.assertIn("| сборка | 2 | слоты 2/1 ⚠ сверх лимита |", board)
        self.assertIn("### Сборка — 2 · слоты 2/1 ⚠ сверх лимита", board)
        lim = json.loads(self.team(*([*sets, "board", "--json"]))[1])["columns"][3]["limit"]
        self.assertEqual(lim, {"what": "слоты", "key": "stack.slots", "used": 2, "max": 1, "over": True})
        # the post-stack's places
        for oid, pr in (("st-0001", "301"), ("st-0002", "302")):
            self.team(*([*sets, "stack", "set", oid, "--pr", pr, "--ci", "green", "--heavy", "green", "--scan", "clean", "--shots", "n/a"]))
        self.assertEqual(self.team(*([*sets, "stack", "post", "st-0001"]))[0], 0)
        rc, _, err = self.team(*([*sets, "stack", "post", "st-0002"]))
        self.assertIn("пост-стек полон (1/1): st-0002 stays in its slot", err)
        self.assertEqual(self.team(*([*sets, "stack", "post", "st-0002", "--force", "--why", "оба ждут слова"]))[0], 0)
        self.assertEqual(self.stk().get("st-0002")["stages"][-1]["gates"],
                         {"limit": ["пост-стек полон (1/1): st-0002 stays in its slot; the owner merges one first "
                                    "(stack done), or one goes back for work (stack start)"]})


class TrainGateTest(GateBase):
    def setUp(self):
        super().setUp()
        self.team("tracks", "add", "--title", "Импорт")
        self.team("tracks", "car", "add", "tr-0001", "--title", "парсер", "--size", "M")
        self.team("tracks", "car", "add", "tr-0001", "--title", "экран", "--size", "M", "--brief", self.brief)
        self.team("trains", "add", "--name", "imports", "--title", "Импорт", "--car", "tr-0001:0", "--car", "tr-0001:1",
                  "--slots", "max 2")
        self.team("trains", "state", "imports", "impl", "--force", "--why", "парсер — по DoR в слоте")

    def test_car_stack_start_is_gated_before_anything_is_written_and_next_holds_it_back(self):
        nx = json.loads(self.team("trains", "next", "--json")[1])
        self.assertEqual([p["car"] for p in nx["picks"]], ["tr-0001:1"])  # car 0 has no brief: its DoR holds it
        self.assertEqual(nx["gated"], [{"car": "tr-0001:0", "train": "imports", "id": None, "dor": ["нет брифа"],
                                        "command": "depot2 tracks car stack tr-0001:0 --start"}])
        self.assertIn("Ждут ворот DoR (слот их сам не возьмёт): tr-0001:0 «парсер» — нет брифа "
                      "(`depot2 tracks car stack tr-0001:0 --start`, или с --force --why «…»).",
                      self.team("trains", "next")[1])
        before = (open(self.t.state.path("stack.json")).read() if os.path.exists(self.t.state.path("stack.json"))
                  else None)
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:0", "--start")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: tr-0001:0 → «сборка» не пройдены: DoR (вход в сборку) — нет брифа", err)
        self.assertEqual(open(self.t.state.path("stack.json")).read() if os.path.exists(self.t.state.path("stack.json"))
                         else None, before)
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:0", "--start", "--force", "--why", "бриф в голове")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0: ворота пройдены с --force (бриф в голове): DoR (вход в сборку) — нет брифа", out)
        _t, c = Tracks(self.t.state, self.t.cfg).car("tr-0001:0")
        o = self.stk().get(c["st"])
        self.assertEqual((o["stages"][-1]["forced"], o["stages"][-1]["gates"]), (True, {"DoR": ["нет брифа"]}))
        j = json.loads(self.team("car", "show", "tr-0001:0", "--json")[1])
        self.assertEqual(j["stages"][-1]["forced"], True)
        self.assertEqual((j["v"], j["kind"], j["car"], j["object"], j["stage"], j["where"]),
                         (1, "car", "tr-0001:0", o["id"], "build", "slot"))
        self.assertEqual(j["gate"]["name"], "DoD")


class BoardTest(GateBase):
    def setUp(self):
        super().setUp()
        self.team("stack", "add", "--title", "Импорт", "--brief", self.brief, "--size", "M")
        self.team("stack", "add", "--title", "Мысль")
        self.team("tracks", "add", "--title", "Поезд")
        self.team("tracks", "car", "add", "tr-0001", "--title", "вагон 0", "--brief", self.brief)
        self.team("tracks", "car", "add", "tr-0001", "--title", "вагон 1", "--after", "0")
        self.team("wishes", "add", "--title", "Хочу импорт", "--words", "импорт бы")
        self.team("stack", "start", "st-0001")
        self.team("trains", "add", "--name", "poezd", "--title", "Поезд", "--car", "tr-0001:0", "--car", "tr-0001:1")
        self.team("trains", "state", "poezd", "impl", "--force", "--why", "вагон 1 — без брифа, его DoR держит его сам")

    def test_the_columns_md_and_json(self):
        rc, md, err = self.team("board")
        self.assertEqual(rc, 0, err)
        self.assertIn("| Этап | Вагонов | Лимит | Дольше всех на этапе |", md)
        self.assertIn("| тележка | 3 | — |", md)
        self.assertIn("| эскиз | 1 | — |", md)
        self.assertIn("| сборка | 1 | слоты 1/4 |", md)
        self.assertIn("| приёмка | 0 | пост-стек 0/4 | — |", md)
        self.assertIn("### Сборка — 1 · слоты 1/4", md)
        self.assertRegex(md, r"\| st-0001 \| P5 \| Импорт \| — \| — \| M \| слот \| [^|]+ \| DoD: CI: не сдано; Heavy: не сдано; "
                             r"скан: не сдано; шоты: не сдано \|")
        self.assertRegex(md, r"\| tr-0001:0 \| P5 \| вагон 0 \| poezd «Поезд» \| tr-0001 Поезд \| — \| на треке \| [^|]+ \| "
                             r"DoR пройдены \|")
        self.assertRegex(md, r"\| wl-0001 \| P5 \| Хочу импорт \| — \| — \| — \| — \| [^|]+ \| — \|")
        self.assertNotIn("### Прибыл", md)
        data = json.loads(self.team("board", "--json")[1])
        self.assertEqual((data["v"], data["view"]), (1, "all"))
        self.assertEqual([c["stage"] for c in data["columns"]], list(stages.STAGES[:8]))
        self.assertEqual(data["summary"], {"bogie": 3, "sketch": 1, "project": 0, "build": 1, "docs": 0, "acceptance": 0,
                                           "queued": 0, "rolling": 0})
        it = data["columns"][3]["items"][0]
        self.assertEqual(sorted(it), sorted(["ref", "kind", "object", "car", "track", "track_title", "train", "train_title",
                                             "train_state", "title", "size", "stage", "stage_ru", "since", "forced",
                                             "where", "pr", "branch", "brief", "moves", "merge", "gate", "returns",
                                             "claims", "repair", "principles", "docs", "priority"]))
        self.assertIsNone(it["principles"])
        self.assertEqual((it["ref"], it["kind"], it["where"], it["moves"]),
                         ("st-0001", "stack", "slot", ["acceptance", "docs", "queued", "arrived", "project", "sketch",
                                                       "scrapped"]))
        # a car that arrived shows with --all only
        self.team("stack", "done", "st-0001", "--reason", "сдан, без PR")
        self.assertNotIn("st-0001", [i["ref"] for c in json.loads(self.team("board", "--json")[1])["columns"]
                                     for i in c["items"]])
        full = json.loads(self.team("board", "--json", "--all")[1])
        arrived = next(c for c in full["columns"] if c["stage"] == "arrived")
        self.assertEqual([(i["ref"], i["kind"]) for i in arrived["items"]], [("st-0001", "left")])
        self.assertIn("### Прибыл — 1", self.team("board", "--all")[1])

    def test_the_stack_view_and_the_limit(self):
        self.assertEqual(self.team("board", "--view", "stack")[1], self.team("stack", "--md")[1])
        j = json.loads(self.team("board", "--view", "stack", "--json")[1])
        self.assertEqual([c["stage"] for c in j["columns"]], ["build", "docs", "acceptance", "queued"])
        self.assertEqual([i["ref"] for i in j["columns"][0]["items"]], ["st-0001"])
        self.assertEqual((j["queue"]["count"], j["queue"]["max"], [i["ref"] for i in j["queue"]["items"]]),
                         (1, 10, ["st-0002"]))
        self.assertIn("Доска (`depot2 board`): тележка 3 · эскиз 1 · сборка 1; стек — её вид", self.team("stack")[1])
        for n in range(3):
            self.team("wishes", "add", "--title", "ещё %d" % n, "--words", "w")  # noqa: UP031
        md = self.team("board", "--limit", "2")[1]
        self.assertIn("… и ещё 4 (`depot2 board --limit 0`).", md)

    def test_the_json_formats_for_the_web_view(self):
        tr = json.loads(self.team("trains", "--json")[1])
        self.assertEqual(sorted(tr), ["effort", "next", "states", "trains", "v"])
        car = tr["trains"][0]["cars"][0]
        for k in ("n", "ref", "track", "train", "title", "status", "stage", "stages", "after", "brief", "size"):
            self.assertIn(k, car)
        self.assertIn("gated", tr["next"])
        self.assertEqual(json.loads(self.team("train", "poezd", "--json")[1])["name"], "poezd")
        tk = json.loads(self.team("tracks", "--json")[1])
        self.assertEqual(sorted(tk), ["tracks", "v"])
        self.assertEqual(json.loads(self.team("track", "tr-0001", "--json")[1])["id"], "tr-0001")
        for ref in ("st-0001", "st-0002", "tr-0001:1", "wl-0001"):
            j = json.loads(self.team("car", "show", ref, "--json")[1])
            for k in ("v", "ref", "kind", "car", "object", "track", "train", "title", "stage", "stage_ru", "moves", "stages",
                      "brief", "size", "branch", "pr", "where", "status", "asks", "project", "project_passed", "gate",
                      "ci", "dod", "left", "taken_to"):
                self.assertIn(k, j, (ref, k))
        j = json.loads(self.team("car", "show", "tr-0001:1", "--json")[1])
        self.assertEqual((j["kind"], j["track"], j["train"], j["gate"]),
                         ("car", {"id": "tr-0001", "title": "Поезд"}, {"name": "poezd", "title": "Поезд", "state": "impl"},
                          None))
        self.assertEqual(json.loads(self.team("car", "show", "wl-0001", "--json")[1])["kind"], "wish")

    def test_the_slots_who_is_in_each_and_who_comes_next(self):
        md = self.team("board")[1]
        self.assertLess(md.index("### Слоты — 1/4 (свободно 3)"), md.index("| Этап | Вагонов |"))
        self.assertRegex(md, r"\| 1 \| st-0001 \| Импорт \| — \| — \| [^|]+ \| DoD: CI: не сдано;")
        self.assertIn("| 4 | свободен | — | — | — | — | — |", md)
        self.assertIn("Дальше (`depot2 trains next`): сейчас (свободный слот берёт) — tr-0001:0 (поезд poezd). ждут слова "
                      "владельца — st-0002 (ждёт слова владельца (автозапуск — нет)).", md)
        sl = json.loads(self.team("board", "--json")[1])["slots"]
        self.assertEqual((sl["used"], sl["max"], sl["free"]), (1, 4, 3))
        self.assertEqual([c["ref"] for c in sl["cells"]], ["st-0001", None, None, None])
        c = sl["cells"][0]
        self.assertEqual((c["n"], c["kind"], c["stage"], c["gate"]["name"]), (1, "stack", "build", "DoD"))
        self.assertTrue(c["in_slot"])
        self.assertEqual([(e["state"], e["ref"], e["command"]) for e in sl["next"]],
                         [("now", "tr-0001:0", "depot2 tracks car stack tr-0001:0 --start"),
                          ("owner", "st-0002", "depot2 stack start st-0002")])
        self.assertEqual(sorted(sl["next"][0]), ["command", "object", "priority", "ref", "state", "state_ru", "title", "track",
                                                 "train", "why"])
        self.assertEqual((sl["next"][0]["track"], sl["next"][0]["train"]), ("tr-0001", "poezd"))
        for _ in range(3):  # every slot busy: the train's car waits for a freed one
            self.team("stack", "add", "--title", "x", "--brief", self.brief)
        for oid in ("st-0003", "st-0004", "st-0005"):
            self.assertEqual(self.team("stack", "start", oid)[0], 0)
        sl = json.loads(self.team("board", "--json", "--view", "stack")[1])["slots"]
        self.assertEqual((sl["used"], sl["free"], [e["state"] for e in sl["next"]]), (4, 0, ["next", "owner"]))
        self.assertIn("### Слоты — 4/4 (все заняты)", self.team("board")[1])
        self.assertIn("когда освободится слот — tr-0001:0", self.team("board")[1])


class RouteTest(GateBase):
    def test_moves_allowed_and_not(self):
        self.team("stack", "add", "--title", "x")
        for stage, rc in (("build", 1), ("сборка", 1), ("эскиз", 0), ("проект", 0), ("тележка", 1)):
            self.assertEqual(self.team("car", "stage", "st-0001", stage)[0], rc, stage)
        self.assertEqual(self.team("car", "stage", "st-0001", "эскиз", "--why", "бриф переписать")[0], 0)
        o = self.stk().get("st-0001")
        self.assertEqual([(r["from"], r["stage"]) for r in o["stages"]],
                         [(None, "bogie"), ("bogie", "sketch"), ("sketch", "project"), ("project", "sketch")])
        self.assertEqual(o["stages"][-1]["why"], "бриф переписать")


class DocsTest(GateBase):

    HANDED = ["--ci", "green", "--heavy", "n/a", "--scan", "CLEAN", "--shots", "n/a"]

    def setUp(self):
        super().setUp()
        self.docs_brief = os.path.join(self.t.dir, "docs-brief.md")
        with open(self.docs_brief, "w") as f:
            f.write(BRIEF_TEXT + "\n## Тесты\n- **Документация:** нужна\n")
        self.team("tracks", "add", "--title", "Каталог")
        self.team("tracks", "car", "add", "tr-0001", "--title", "доки", "--brief", self.docs_brief)
        self.team("tracks", "car", "add", "tr-0001", "--title", "без доков", "--brief", self.brief)
        for ref, _pr in (("tr-0001:0", "501"), ("tr-0001:1", "502")):
            rc, _, err = self.team("tracks", "car", "stack", ref, "--start")
            self.assertEqual(rc, 0, err)
        self.team("stack", "set", "st-0001", "--pr", "501")
        self.team("stack", "set", "st-0002", "--pr", "502")

    def car(self, ref="tr-0001:0"):
        return Tracks(self.t.state, self.t.cfg).car(ref)[1]

    def test_the_route(self):
        stages.check_move("build", "docs", None)
        stages.check_move("docs", "acceptance", None)
        stages.check_move("build", "acceptance", None)  # the direct way stays
        stages.check_move("docs", "build", "доки нашли дыру")
        with self.assertRaises(stages.StageError) as e:
            stages.check_move("docs", "build", None)
        self.assertIn("«документация» → «сборка» — шаг назад: нужна причина", str(e.exception))
        self.assertEqual(stages.parse("Документация"), "docs")
        self.assertEqual(stages.STAGES.index("docs"), stages.STAGES.index("build") + 1)
        self.assertTrue(stages.is_return("queued", "docs"))
        self.assertFalse(stages.is_return("docs", "build"))
        self.assertEqual([gates.brief_docs(p) for p in (self.docs_brief, self.brief, None)], [True, None, None])

    def test_the_sketch_line_gates_the_way_into_the_acceptance(self):
        self.assertRegex(self.team("stack", "--md")[1],
                         r"\| С1 \| слот \| st-0001 [^\n]* \| сборка \(нужна документация\) \|")
        self.assertRegex(self.team("track", "tr-0001")[1],
                         r"\| 0 \| P5 \| доки \|[^\n]* \| сборка \(нужна документация\) \|")
        rc, out, _ = self.team("car", "show", "tr-0001:0")
        self.assertIn("Документация нужна (эскиз: «Документация: нужна»): не сделана.", out)
        self.assertIn("Отсюда по маршруту: приёмка, документация, на путях, прибыл, проект, эскиз, списан.", out)
        rc, _, err = self.team("stack", "post", "st-0001", *self.HANDED)
        self.assertEqual(rc, 1)
        self.assertIn("ворота: st-0001 → «приёмка» не пройдены: DoD (вход в приёмку) — документация: не сделана (нужна — "
                      "эскиз: «Документация: нужна»; depot2 car stage tr-0001:0 docs). Пройти всё равно", err)
        self.assertEqual(self.stk().get("st-0001")["where"], "slot")  # a refused post wrote nothing
        # the car with no line goes straight from the build, as before
        rc, _, err = self.team("stack", "post", "st-0002", *self.HANDED)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.car("tr-0001:1")["stages"][-1]["from"], "build")
        # build → docs: car stage's own, the slot is held; the car and its object move together
        rc, out, err = self.team("car", "stage", "tr-0001:0", "документация")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0 (объект st-0001): сборка → документация", out)
        self.assertEqual((self.car()["stage"], self.stk().get("st-0001")["stage"], self.stk().get("st-0001")["where"]),
                         ("docs", "docs", "slot"))
        self.assertRegex(self.team("stack", "--md")[1], r"\| С1 \| слот \| st-0001 [^\n]* \| документация \|")
        # back to the build needs a reason, and it is car stage's own too (no stack start: the object is in its slot)
        rc, _, err = self.team("car", "stage", "tr-0001:0", "build")
        self.assertIn("шаг назад: нужна причина", err)
        rc, _, err = self.team("car", "stage", "tr-0001:0", "build", "--why", "доки нашли дыру в коде")
        self.assertEqual(rc, 0, err)
        self.assertEqual(rows_of(self.car())[-2:], [("build", "docs"), ("docs", "build")])
        self.assertEqual(int(self.car().get("returns") or 0), 0)
        self.team("car", "stage", "st-0001", "docs")
        rc, out, err = self.team("stack", "post", "st-0001", *self.HANDED)
        self.assertEqual(rc, 0, err)
        self.assertEqual((rows_of(self.car())[-1], rows_of(self.stk().get("st-0001"))[-1]),
                         (("docs", "acceptance"), ("docs", "acceptance")))
        self.assertNotIn("forced", self.car()["stages"][-1])
        self.assertIn("Документация нужна (эскиз: «Документация: нужна»): этап пройден.",
                      self.team("car", "show", "tr-0001:0")[1])
        data = json.loads(self.team("board", "--json")[1])
        it = next(i for c in data["columns"] for i in c["items"] if i["ref"] == "tr-0001:0")
        self.assertEqual((it["stage"], it["docs"]["state"], it["gate"]), ("acceptance", "passed", None))

    def test_the_flag_wins_and_force_passes(self):
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--docs", "no")  # noqa: RUF059
        self.assertEqual(rc, 0, err)
        self.assertIs(self.car()["docs"], False)
        self.assertIsNone(json.loads(self.team("car", "show", "tr-0001:0", "--json")[1])["docs"]["need"])
        self.assertEqual(self.team("stack", "post", "st-0001", *self.HANDED)[0], 0)  # the flag beats the sketch's line
        self.team("tracks", "car", "set", "tr-0001:1", "--docs", "yes")
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:1", "--docs", "maybe")[2].strip(),
                         "error: --docs is yes or no, got 'maybe'")
        rc, _, err = self.team("stack", "post", "st-0002", *self.HANDED)
        self.assertEqual(rc, 1)
        self.assertIn("документация: не сделана (нужна — флаг вагона (tracks car set --docs yes); depot2 car stage "
                      "tr-0001:1 docs)", err)
        rc, _out, err = self.team("stack", "post", "st-0002", *[*self.HANDED, "--force", "--why", "доки едут отдельно"])
        self.assertEqual(rc, 0, err)
        row = self.stk().get("st-0002")["stages"][-1]
        self.assertEqual((row["from"], row["stage"], row["forced"], row["gates"]),
                         ("build", "acceptance", True, {"DoD": ["документация: не сделана (нужна — флаг вагона (tracks "
                                                                "car set --docs yes); depot2 car stage tr-0001:1 docs)"]}))
        self.assertIn("Документация нужна (флаг вагона (tracks car set --docs yes)): этапа не было.",
                      self.team("car", "show", "tr-0001:1")[1])
        js = json.loads(self.team("track", "tr-0001", "--json")[1])
        self.assertEqual([(c["docs"], c["docs_need"]) for c in js["cars"]],
                         [(False, None), (True, "флаг вагона (tracks car set --docs yes)")])

    def test_docs_hold_the_slot_and_a_car_out_of_a_slot_does_not_go(self):
        self.team("tracks", "car", "add", "tr-0001", "--title", "в очереди", "--brief", self.docs_brief)
        self.team("tracks", "car", "stack", "tr-0001:2")
        self.team("car", "stage", "tr-0001:2", "build", "--force", "--why", "в работе, но не в слоте")
        rc, _, err = self.team("car", "stage", "tr-0001:2", "docs")
        self.assertEqual(rc, 1)
        self.assertIn("tr-0001:2: «документация» держит слот сборки, а st-0003 не в слоте; сначала в слот: depot2 "
                      "tracks car stack tr-0001:2 --start", err)
        self.team("car", "stage", "tr-0001:0", "docs")
        self.assertEqual([o["id"] for o in self.stk().in_slots()], ["st-0001", "st-0002"])
        # a status set by hand keeps the docs (pr, running)
        self.team("tracks", "car", "set", "tr-0001:0", "--status", "pr")
        self.assertEqual(self.car()["stage"], "docs")

    def test_the_board_column_and_the_json(self):
        self.team("car", "stage", "tr-0001:0", "docs")
        rc, md, err = self.team("board")
        self.assertEqual(rc, 0, err)
        self.assertIn("| документация | 1 | — |", md)
        self.assertIn("### Документация — 1", md)
        self.assertIn("Документация — необязательный этап после сборки: держит слот сборки", md)
        data = json.loads(self.team("board", "--json")[1])
        self.assertEqual([c["stage"] for c in data["columns"]][3:6], ["build", "docs", "acceptance"])
        col = next(c for c in data["columns"] if c["stage"] == "docs")
        self.assertEqual((col["ru"], col["means"], col["count"], col["limit"]),
                         ("документация", "доки к изменению: README, справка, карты модулей", 1, None))
        it = col["items"][0]
        self.assertEqual((it["ref"], it["where"], it["docs"]["done"], it["gate"]["name"], it["moves"]),
                         ("tr-0001:0", "slot", True, "DoD", ["acceptance", "queued", "build", "scrapped"]))
        self.assertNotIn("документация", "; ".join(it["gate"]["unmet"]))
        build = next(c for c in data["columns"] if c["stage"] == "build")
        self.assertEqual([(i["ref"], i["docs"]) for i in build["items"]], [("tr-0001:1", None)])
        self.assertEqual(data["summary"]["docs"], 1)
        self.assertEqual([c["stage"] for c in json.loads(self.team("board", "--view", "stack", "--json")[1])["columns"]],
                         ["build", "docs", "acceptance", "queued"])
        self.assertEqual(data["slots"]["cells"][0]["stage"], "docs")
        j = json.loads(self.team("car", "show", "tr-0001:0", "--json")[1])
        self.assertEqual((j["stage"], j["docs"]), ("docs", {"need": "эскиз: «Документация: нужна»", "state": "now",
                                                            "word": "этап идёт", "done": True,
                                                            "cmd": "depot2 car stage tr-0001:0 docs"}))
        self.assertIn("документация: этап идёт", j["gate"]["handed"])
        self.assertIn("Документация нужна (эскиз: «Документация: нужна»): этап идёт.",
                      self.team("car", "show", "tr-0001:0")[1])


def rows_of(rec):
    return [(r["from"], r["stage"]) for r in rec.get("stages") or []]


class MergePlaceTest(MergeBase):
    def test_add_at_and_move_never_ahead_of_a_ride(self):
        for n, br in ((301, "feat-a"), (302, "feat-b"), (303, "feat-c")):
            self.pr(n, br, {f"{br}.txt": br})
        self.team("merge", "add", "301", "--word", "301 мердж")
        self.team("merge", "add", "302", "--word", "302 мердж")
        rc, out, err = self.team("merge", "add", "303", "--word", "303 — сначала фикс", "--at", "1")
        self.assertEqual(rc, 0, err)
        self.assertIn("PR #303 (feat-c) is in the merge queue, place 1", out)
        order = lambda: [e["pr"] for e in self.q().entries(open_only=True)]
        self.assertEqual(order(), [303, 301, 302])
        rc, out, _ = self.team("merge", "move", "302", "--at", "2")
        self.assertEqual(rc, 0)
        self.assertIn("mq-0002 #302: place 3 → 2", out)
        self.assertEqual(order(), [303, 302, 301])
        q = self.q()
        q.get(303)["status"] = "riding"
        q.save("test", "mq-0003")
        rc, out, _ = self.team("merge", "move", "301", "--at", "1")
        self.assertIn("place 3 → 2", out)
        self.assertIn("the runner holds what rides", out)
        self.assertEqual(order(), [303, 301, 302])
        self.assertEqual(self.team("merge", "move", "303", "--at", "3")[0], 1)  # a ride does not move

    def test_handoff_a_merged_pr_whose_main_ci_went_red_then_it_rides_out_with_the_next(self):
        self.pr(301, "feat-a", {"a.txt": "A\n"})
        self.pr(302, "feat-b", {"b.txt": "B\n"})
        self.ci(main=["failure", "success"])
        self.team("merge", "add", "301", "--word", "301 мердж")
        self.team("merge", "add", "302", "--word", "302 мердж")
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 1, out + err)
        e = self.entry(301)
        self.assertEqual((e["status"], bool(e["merged"])), ("stopped", True))
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, "st-0001").stage(), "rolling")
        self.assertEqual(self.team("merge", "handoff", "302", "--reason", "x")[0], 1)  # not merged
        rc, out, err = self.team("merge", "handoff", "301", "--reason", "main красный: нестабильный тест")
        self.assertEqual(rc, 0, err)
        self.assertIn("mq-0001 #301 (merged as ", out)
        self.assertIn("its deploy rides with the next merge", out)
        e = self.entry(301)
        self.assertEqual((e["status"], e["handoff"]["reason"]), ("merged", "main красный: нестабильный тест"))
        self.assertEqual(stages.entry_stage(e), "rolling")
        self.assertIn("выкатка — со следующим мерджем (main красный: нестабильный тест)",
                      self.team("merge", "--all")[1])
        board = json.loads(self.team("board", "--json")[1])
        rolling = next(c for c in board["columns"] if c["stage"] == "rolling")
        self.assertEqual([i["ref"] for i in rolling["items"]], ["st-0001"])
        rc, out, err = self.team("merge", "run")
        self.assertEqual(rc, 0, out + err)
        self.assertEqual(self.entry(302)["status"], "merged")
        self.assertEqual(stages.Car(self.t.state, self.t.cfg, "st-0001").stage(), "arrived")
        self.assertEqual(self.entry(301)["handoff"]["arrived"]["pr"], 302)
        self.assertEqual(stages.entry_stage(self.entry(301)), "arrived")
        rows = stages.Car(self.t.state, self.t.cfg, "st-0001").rec()["stages"]
        self.assertEqual((rows[-1]["stage"], rows[-1]["why"]), ("arrived", "выкачен со следующим: #302 (mq-0002)"))


if __name__ == "__main__":
    unittest.main()
