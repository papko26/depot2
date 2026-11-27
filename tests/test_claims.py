"""Defects and claims (claims.py): `returns` on a return to the build, the returns from before the stages written
by `queues migrate`, `repair add` (a repair car out of turn: the head of the queue, autostart, ahead of the trains),
`repair link`, the culprit's `claims`, the bug table, the report `claims`, the views.
"""

import json
import os
import unittest

from helpers import BRIEF_TEXT, GATES_ALL
from test_extras import Base

from depot2 import claims, stages
from depot2.stack import Stack
from depot2.store import append_jsonl, write_json
from depot2.tracks import Tracks
from depot2.trains import Trains

BUGS = """# Баги

| id | кто | что (дословно, кратко) | куда | статус |
|---|---|---|---|---|
| t1_q1 | тестер | Книга выдаётся с первого клика, раньше было два | st-0005 | разобран |
| t1_q2 | тестер | Кнопка исчезает | — | новый |
"""


class ClaimBase(Base):
    def setUp(self):
        super().setUp()
        self.brief = os.path.join(self.t.dir, "brief.md")
        with open(self.brief, "w") as f:
            f.write(BRIEF_TEXT)
        self.bugs = os.path.join(self.t.dir, "BUGS.md")
        with open(self.bugs, "w") as f:
            f.write(BUGS)

    def team(self, *argv):
        return super().team("--set", "repair.bugs=" + self.bugs, *argv)

    def stk(self):
        return Stack(self.t.state, self.t.cfg)

    def car(self, ref):
        return stages.Car(self.t.state, self.t.cfg, ref)

    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, "{}: {}{}".format(" ".join(argv), out, err))
        return out

    def arrived(self, title="Импорт каталога", pr="142"):
        """A standalone object, merged and on production: st-NNNN."""
        out = self.ok("stack", "add", "--title", title, "--brief", self.brief)
        oid = out.split()[0]
        self.ok("stack", "start", oid)
        self.ok("stack", "set", oid, "--pr", pr)
        self.ok("stack", "done", oid, "--reason", f"merged #{pr}")
        self.ok("car", "stage", oid, "arrived", "--why", "выкачен")
        return oid


class ReturnTest(ClaimBase):
    def test_a_return_to_the_build_counts_on_the_object_and_its_car(self):
        rec = {}
        stages.start(rec, "queued", "merge add")
        row = stages.record(rec, "build", "merge drop", "CI красный")
        self.assertTrue(row["return"])
        self.assertEqual(rec["returns"], 1)
        stages.record(rec, "acceptance", "stack post")
        stages.record(rec, "queued", "merge add")
        self.assertNotIn("return", stages.record(rec, "acceptance", "merge drop"))  # back to the acceptance: no return
        self.assertEqual(rec["returns"], 1)
        self.ok("tracks", "add", "--title", "Поезд")
        self.ok("tracks", "car", "add", "tr-0001", "--title", "парсер: поле", "--brief", self.brief)
        self.ok("tracks", "car", "stack", "tr-0001:0", "--start")
        self.ok("stack", "set", "st-0001", "--pr", "401")
        for n in (1, 2):
            self.ok("stack", "post", "st-0001")
            self.ok("stack", "start", "st-0001", "--reason", "доработать %d" % n)  # noqa: UP031
        c = Tracks(self.t.state, self.t.cfg).car("tr-0001:0")[1]
        o = self.stk().get("st-0001")
        self.assertEqual((c["returns"], o["returns"]), (2, 2))
        self.assertEqual([r["why"] for r in c["stages"] if r.get("return")], ["доработать 1", "доработать 2"])
        out = self.ok("car", "show", "tr-0001:0")
        self.assertIn("Брак: возвратов в сборку — 2; последний ", out)
        self.assertIn("(доработать 2; stack start) [брак: возврат в сборку]", out)
        j = json.loads(self.ok("car", "show", "st-0001", "--json"))
        self.assertEqual((j["returns"], j["claims"], j["claim_links"], j["repair"], j["fixes"]), (2, 0, [], None, []))
        self.assertEqual([e["why"] for e in j["returns_log"]], ["доработать 1", "доработать 2"])
        board = json.loads(self.ok("board", "--json"))
        it = next(i for c in board["columns"] for i in c["items"] if i["ref"] == "tr-0001:0")
        self.assertEqual((it["returns"], it["claims"], it["repair"]), (2, 0, None))
        self.assertIn("| tr-0001:0 (st-0001) · брак 2 |", self.ok("board"))

    def test_the_returns_from_before_the_stages_are_written_by_queues_migrate(self):
        # as the depot was before the stages: an object back from the post-stack twice, one live, one that left
        self.ok("stack", "add", "--title", "Живой", "--brief", self.brief)
        self.ok("stack", "add", "--title", "Ушёл", "--brief", self.brief)
        s = self.stk()
        for oid in ("st-0001", "st-0002"):
            o = s.get(oid)
            o.pop("stage")
            o.pop("stages")
        s.data["objects"][0].update(where="slot", pr=7)
        left = s.data["objects"].pop(1)
        left.update(pr=8)
        write_json(self.t.state.path("stack.json"), s.data)
        h = self.t.state.path("stack-history.jsonl")
        for oid, ts in (
            ("st-0001", "2015-11-10T10:00:00+02:00"),
            ("st-0001", "2015-11-11T10:00:00+02:00"),
            ("st-0002", "2015-11-11T12:00:00+02:00"),
        ):
            append_jsonl(h, {"ts": ts, "event": "start", "object": oid, "unposted": True, "reason": "владелец: правка"})
        append_jsonl(
            h,
            {
                "ts": "2015-11-11T13:00:00+02:00",
                "event": "done",
                "object": "st-0002",
                "reason": "merged #8",
                "obj": left,
            },
        )
        self.assertEqual(json.loads(self.ok("car", "show", "st-0001", "--json"))["returns"], 2)  # the history says so
        out = self.ok("queues", "migrate")
        self.assertIn("брак: returns записан у 2 вагонов (возвратов в сборку 3): st-0001 2, st-0002 1", out)
        self.assertEqual(self.stk().get("st-0001")["returns"], 2)
        self.assertEqual(self.stk().left_obj("st-0002")["returns"], 1)
        self.assertEqual(self.stk().left_stage("st-0002"), "arrived")  # merged before the stages: on production
        self.assertIn("нечего переносить", self.ok("queues", "migrate"))
        data = json.loads(self.ok("claims", "--json", "--since", "2015-11-11"))
        self.assertEqual((data["total"]["returns"], [c["ref"] for c in data["cars"]]), (2, ["st-0001", "st-0002"]))


class RepairTest(ClaimBase):
    def test_repair_add_of_a_car_out_of_turn(self):
        culprit = self.arrived()
        self.ok("stack", "add", "--title", "Очередь 1")
        self.ok("stack", "add", "--title", "Очередь 2")
        rc, out, err = self.team(
            "--set",
            "stack.queue_max=2",
            "repair",
            "add",
            "--of",
            "#142",
            "--brief",
            self.brief,
            "--why",
            "импорт теряет обложки",
            "--by",
            "владелец",
        )
        self.assertEqual(rc, 0, err)  # a full queue does not refuse a repair
        self.assertIn("st-0004 — ремонт st-0001: голова очереди стека (3/2), автозапуск да, этап эскиз", out)
        self.assertIn("st-0001: рекламация №1 (ремонт st-0004); рекламаций теперь 1", out)
        s = self.stk()
        r = s.queue()[0]
        self.assertEqual(
            (r["id"], r["kind"], r["repairs"], r["bug"], r["autostart"], r["stage"], r["title"]),
            ("st-0004", "repair", culprit, None, True, "sketch", "ремонт: st-0001 Импорт каталога"),
        )
        left = s.left_obj(culprit)
        self.assertEqual(left["claims"], 1)
        x = left["claim_links"][0]
        self.assertEqual((x["n"], x["repair"], x["by"], x["why"]), (1, "st-0004", "владелец", "импорт теряет обложки"))
        self.assertEqual(s.left_stage(culprit), "arrived")
        out = self.ok("car", "show", culprit)
        self.assertIn("Рекламаций: 1:\n- №1 ", out)
        self.assertIn("импорт теряет обложки (ремонт st-0004)", out)
        self.assertIn("Ремонт: чинит st-0001.", self.ok("car", "show", "st-0004"))
        j = json.loads(self.ok("car", "show", "st-0004", "--json"))
        self.assertEqual(j["repair"], {"of": "st-0001", "bug": None})
        self.assertEqual(j["fixes"][0]["of"], "st-0001")
        self.assertIn("| st-0004 · ремонт st-0001 |", self.ok("board"))

    def test_the_checks_are_soft(self):
        self.ok("stack", "add", "--title", "В работе", "--brief", self.brief)
        self.ok("stack", "start", "st-0001")
        rc, _, err = self.team("repair", "add", "--of", "st-0001", "--why", "x")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 на этапе «сборка», не на проде (рекламация — на прибывший вагон; брак до мерджа", err)
        self.assertIn("depot2 repair add --of st-0001 --force --why «…»", err)
        rc, _out, err = self.team("repair", "add", "--of", "st-0001", "--force", "--why", "на проде старая копия")
        self.assertEqual(rc, 0, err)
        r = self.stk().get("st-0002")
        self.assertTrue(r["stages"][0]["forced"])
        self.assertIn("рекламация", r["stages"][0]["gates"])
        self.assertIn("рекламация", self.stk().get("st-0001")["claim_links"][0]["forced"])
        for argv, text in (
            (("--of", "nope_q9"), "нет бага nope_q9 в "),
            (("--of", "wl-0001"), "не выехавший вагон"),
            (("--of", "st-0001", "--bug", "x", "--of", "y_q"), "баг — один"),
            (("--of", "#999"), "#999: ни вагона, ни объекта стека с этим PR"),
        ):
            rc, _, err = self.team("repair", "add", *argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(text, err)

    def test_a_bug_with_no_culprit_then_repair_link(self):
        culprit = self.arrived()
        out = self.ok("repair", "add", "--of", "t1_q1")
        self.assertIn("st-0002 — ремонт (виновник не найден)", out)
        self.assertIn("виновник найдётся — depot2 repair link st-0002 --of", out)
        self.assertIn("BUGS.md: в строке t1_q1, в «куда», — st-0002", out)
        r = self.stk().get("st-0002")
        self.assertEqual(
            (r["repairs"], r["bug"], r["title"]),
            (None, "t1_q1", "ремонт: t1_q1 — Книга выдаётся с первого клика, раньше было два"),
        )
        self.assertIn(
            "Ремонт: виновник не найден (depot2 repair link st-0002 --of …), баг t1_q1 (BUGS.md).",
            self.ok("car", "show", "st-0002"),
        )
        out = self.ok("repair", "link", "st-0002", "--of", culprit)
        self.assertIn("st-0001: рекламация №1 (ремонт st-0002): ремонт: t1_q1", out)
        self.assertEqual(self.stk().get("st-0002")["repairs"], "st-0001")
        x = self.stk().left_obj("st-0001")["claim_links"][0]
        self.assertEqual((x["repair"], x["bug"]), ("st-0002", "t1_q1"))
        rc, _, err = self.team("repair", "link", "st-0002", "--of", culprit)
        self.assertIn("st-0002 уже ремонт st-0001", err)
        other = self.arrived("Другой", "313")
        rc, _, err = self.team("repair", "link", "st-0002", "--of", other)
        self.assertEqual(rc, 1)
        self.assertIn(f"st-0002 уже ремонт st-0001: перенести рекламацию на {other}", err)
        out = self.ok("repair", "link", "st-0002", "--of", other, "--force", "--why", "виновник — другой PR")
        self.assertIn(f"st-0001: рекламация с ремонтом st-0002 перенесена на {other}; рекламаций теперь 0", out)
        old = self.stk().left_obj("st-0001")
        self.assertEqual((old["claims"], old["claim_links"][0]["moved"]["to"]), (0, other))  # kept, marked
        self.assertEqual(self.stk().left_obj(other)["claims"], 1)

    def test_a_repair_inside_another_car(self):
        culprit = self.arrived()
        self.ok("tracks", "add", "--title", "Каталог")
        self.ok("tracks", "car", "add", "tr-0001", "--title", "карточка книги", "--brief", self.brief)
        rc, _, err = self.team("repair", "link", "tr-0001:0", "--of", "#142")  # noqa: RUF059
        self.assertIn("tr-0001:0 — не вагон-ремонт: что за дефект он починил — --why «…»", err)
        for why in ("дубли авторов после импорта", "пустая графа года"):
            out = self.ok("repair", "link", "tr-0001:0", "--of", "#142", "--why", why)
        self.assertIn(
            "st-0001: рекламация №2 (ремонт tr-0001:0, внутри вагона): пустая графа года; рекламаций теперь 2", out
        )
        _rc, _, err = self.team("repair", "link", "tr-0001:0", "--of", "#142", "--why", "пустая графа года")
        self.assertIn("уже есть рекламация с ремонтом tr-0001:0 и этой причиной", err)
        self.assertIn("Чинит рекламации: st-0001 №1; st-0001 №2.", self.ok("car", "show", "tr-0001:0"))
        self.assertNotIn("kind", Tracks(self.t.state, self.t.cfg).car("tr-0001:0")[1])  # an ordinary car stays one
        self.assertEqual(self.stk().left_obj(culprit)["claims"], 2)


class OutOfTurnTest(ClaimBase):
    def setUp(self):
        super().setUp()
        self.t.cfg["gates"] = json.loads(json.dumps(GATES_ALL))  # the DoR checks the brief

    def team(self, *argv):
        return super().team("--set", "gates.dor=" + json.dumps(GATES_ALL["dor"]), *argv)

    def test_a_freed_slot_takes_a_repair_before_the_trains(self):
        culprit = self.arrived()
        self.ok("tracks", "add", "--title", "Поезд")
        self.ok("tracks", "car", "add", "tr-0001", "--title", "вагон", "--brief", self.brief)
        self.ok("trains", "add", "--name", "poezd", "--title", "Поезд", "--car", "tr-0001:0")
        self.ok("trains", "state", "poezd", "impl")
        for n in range(3):  # three of the four slots busy: one is free
            self.ok("stack", "add", "--title", "занят %d" % n, "--brief", self.brief)  # noqa: UP031
            self.ok("stack", "start", "st-%04d" % (n + 2))  # noqa: UP031
        self.ok("repair", "add", "--of", culprit)  # no brief: the DoR holds it, the train's car goes
        nx = Trains(self.t.state, self.t.cfg).next()
        self.assertEqual(([o["id"] for o in nx["repairs"]], [p["car"]["n"] for p in nx["picks"]]), ([], ["0"]))
        self.ok("stack", "set", "st-0005", "--brief", self.brief)
        nx = Trains(self.t.state, self.t.cfg).next()
        self.assertEqual(([o["id"] for o in nx["repairs"]], nx["picks"]), (["st-0005"], []))
        self.assertEqual([o["id"] for o in nx["stack"]], ["st-0005"])
        self.assertEqual((nx["upcoming"][0]["train"]["name"], nx["upcoming"][0]["track"]["id"]), ("poezd", "tr-0001"))
        out = self.ok("trains", "next")
        self.assertIn(
            "Вне очереди — ремонт: st-0005 «ремонт: st-0001 Импорт каталога» — `depot2 stack start st-0005`.", out
        )
        self.assertNotIn("Потом из очереди стека", out)
        self.assertEqual(json.loads(self.ok("trains", "--json"))["next"]["repairs"], ["st-0005"])
        self.ok("stack", "set", "st-0005", "--autostart", "no")  # the owner's word, as any object
        self.assertEqual(Trains(self.t.state, self.t.cfg).next()["repairs"], [])


class ReportTest(ClaimBase):
    def test_the_report_by_period_md_and_json(self):
        self.assertIn("Ни брака, ни рекламаций за всё время.", self.ok("claims"))
        culprit = self.arrived()
        self.ok("repair", "add", "--of", culprit, "--bug", "t1_q2", "--why", "кнопка исчезает")
        self.ok("tracks", "add", "--title", "Поезд")
        self.ok("tracks", "car", "add", "tr-0001", "--title", "вагон", "--brief", self.brief)
        self.ok("tracks", "car", "stack", "tr-0001:0", "--start")
        self.ok("stack", "set", "st-0003", "--pr", "401")
        self.ok("stack", "post", "st-0003")
        self.ok("stack", "start", "st-0003", "--reason", "доработать: подписи")
        md = self.ok("claims", "--md")
        self.assertIn("## Брак и рекламации — за всё время", md)
        self.assertIn("Итого: брак 1, рекламаций 1 — у 2 вагонов; ремонтов 1 (в работе 1).", md)
        self.assertIn("| tr-0001 Поезд | 1 | 1 | 0 |", md)
        self.assertIn("| вне треков (стек) | 1 | 0 | 1 |", md)
        self.assertIn("| st-0001 | Импорт каталога | #142 | 0 | 1 | рекламация №1 ", md)
        self.assertIn("кнопка исчезает (ремонт st-0002, баг t1_q2) |", md)
        self.assertIn("| st-0002 | ремонт: st-0001 Импорт каталога | эскиз | st-0001 | t1_q2 (новый) |", md)
        data = json.loads(self.ok("claims", "--json"))
        self.assertEqual(sorted(data), ["cars", "generated", "repairs", "since", "total", "tracks", "until", "v"])
        self.assertEqual(data["v"], 1)
        self.assertEqual(
            [(c["ref"], len(c["returns"]), len(c["claims"])) for c in data["cars"]],
            [("st-0001", 0, 1), ("tr-0001:0", 1, 0)],
        )
        self.assertEqual(data["cars"][1]["returns"][0]["why"], "доработать: подписи")
        self.assertEqual(json.loads(self.ok("claims", "--json", "--until", "2000-01-01"))["total"]["cars"], 0)
        rc, _, err = self.team("claims", "--since", "12.11")
        self.assertEqual(rc, 1)
        self.assertIn("--since — дата YYYY-MM-DD", err)
        self.assertEqual(claims.read_bugs({"repair": {"bugs": self.bugs}})["t1_q2"]["status"], "новый")
        self.assertEqual(claims.read_bugs({"repair": {"bugs": os.path.join(self.t.dir, "нет.md")}}), {})


if __name__ == "__main__":
    unittest.main()
