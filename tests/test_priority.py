
import json
import os

from helpers import slurp
from test_tracks import PAST, TracksBase

from depot2 import migrate
from depot2 import priority as pri
from depot2.queues import Think, Todo, Wishes
from depot2.stack import Stack
from depot2.store import read_json, read_jsonl, write_json
from depot2.tracks import SCHEMA, Tracks
from depot2.tstack import TeamStack


class PBase(TracksBase):
    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, err)
        return out

    def refused(self, *argv):
        """A refusal: the tool's error (exit 1), or the parser's (exit 2, a bad --priority); its words."""
        import contextlib
        import io

        from depot2 import cli as team
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = team.main(["--state", self.t.dir, "--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent", *list(argv)])
            except SystemExit as x:
                rc = x.code
        self.assertIn(rc, (1, 2), out.getvalue())
        return err.getvalue()


class ParseTest(PBase):
    def test_the_range_the_words_and_the_default(self):
        self.assertEqual([pri.parse(x) for x in (1, "10", "P7", "p3", " 5 ", "high", "normal", "later", "высокий")],
                         [1, 10, 7, 3, 5, 8, 5, 2, 8])
        for bad in (0, 11, "11", "P0", "urgent", "", "5.5", True):
            with self.assertRaises(pri.PriorityError):
                pri.parse(bad)
        self.assertEqual((pri.of({}), pri.of({"priority": 9}), pri.cell({"priority": 9})), (5, 9, "P9"))
        self.assertEqual(pri.counts_cell(pri.counts([{"priority": 2}, {"priority": 8}, {}, {"priority": 8}])),
                         "P8 2 · P5 1 · P2 1")
        self.assertEqual(pri.ordered([{"id": 1}, {"id": 2, "priority": 9}, {"id": 3}, {"id": 4, "priority": 9}]),
                         [{"id": 2, "priority": 9}, {"id": 4, "priority": 9}, {"id": 1}, {"id": 3}])  # ties: in order


class SetTest(PBase):
    def setUp(self):
        super().setUp()
        tk = self.tracks()
        tk.add("Каталог")
        tk.add("Второй")

    def test_every_add_and_set_takes_it_and_every_table_shows_it(self):
        self.assertIn("tr-0001:1 added, P7", self.ok("tracks", "car", "add", "tr-0001", "--title", "приоритет",
                                                    "--n", "1", "--priority", "7"))
        self.assertIn("tr-0001:2 added, P5", self.ok("tracks", "car", "add", "tr-0001", "--title", "без"))
        self.assertIn("katalog added (position 1, «дизайн», P9)", self.ok(
            "trains", "add", "--name", "katalog", "--title", "Каталог", "--car", "tr-0001:1", "--priority", "9"))
        self.assertIn("st-0001 added to the queue (1/10), autostart no, P3",
                      self.ok("stack", "add", "--title", "чужой", "--priority", "P3"))
        self.assertIn("tm-0001 added to the team queue (1/4), P6",
                      self.ok("tstack", "add", "--kind", "question", "--priority", "6"))
        self.ok("wishes", "add", "--title", "обложки", "--prio", "high")      # the old word: a synonym
        self.ok("think", "add", "--topic", "подсказки", "--priority", "4")
        self.ok("todo", "add", "--text", "хвост", "--version", "v", "--prio", "later")
        self.assertEqual([e["priority"] for q in (Wishes, Think, Todo) for e in q(self.t.state).entries()], [8, 4, 2])
        for q in (Wishes, Think, Todo):
            self.assertNotIn("prio", q(self.t.state).entries()[0])
        # set, on every kind
        self.ok("tracks", "car", "set", "tr-0001:2", "--priority", "2")
        self.ok("trains", "set", "katalog", "--priority", "1")
        self.ok("stack", "set", "st-0001", "--priority", "10")
        self.ok("tstack", "set", "tm-0001", "--priority", "1")
        self.ok("wishes", "set", "wl-0001", "--priority", "3")
        self.ok("think", "set", "th-0001", "--prio", "normal")
        self.ok("todo", "set", "td-0001", "--priority", "9")
        tk = self.tracks()
        self.assertEqual([c["priority"] for c in tk.get("tr-0001")["cars"]], [7, 2])
        self.assertEqual(tk.trains.get("katalog")["priority"], 1)
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["priority"], 10)
        self.assertEqual(TeamStack(self.t.state, self.t.cfg).get("tm-0001")["priority"], 1)
        self.assertEqual([e["priority"] for q in (Wishes, Think, Todo) for e in q(self.t.state).entries()], [3, 5, 9])
        # out of range: the parser's error, nothing written
        before = slurp(self.t.state.path("tracks.json"))
        self.assertIn("priority is 1-10", self.refused("tracks", "car", "set", "tr-0001:2", "--priority", "11"))
        self.assertIn("priority is a whole number", self.refused("todo", "set", "td-0001", "--prio", "urgent"))
        self.assertEqual(slurp(self.t.state.path("tracks.json")), before)
        # the tables
        self.assertIn("| № | Поезд | Приоритет | Состояние |", self.ok("trains", "--md"))
        self.assertIn("| 1 | katalog «Каталог» | P1 | дизайн |", self.ok("trains", "--md"))
        out = self.ok("train", "katalog")
        self.assertIn("Состояние: дизайн. Приоритет: P1.", out)
        self.assertIn("| 1 | tr-0001:1 | P7 | tr-0001 Каталог | приоритет |", out)
        out = self.ok("track", "tr-0001")
        self.assertIn("| Вагон | Приоритет | Что |", out)
        self.assertIn("| 2 | P2 | без |", out)
        out = self.ok("stack", "--md")
        self.assertIn("| № | Где | Задача | Приоритет | Ветка / PR |", out)
        self.assertIn("| 1 | очередь | st-0001 чужой | P10 |", out)
        self.assertIn("Приоритет: приоритет 1-10 (10 — важнее всего", out)
        self.assertIn("| 1 | очередь | tm-0001 вопрос команде | P1 |", self.ok("tstack", "--md"))
        self.assertIn("| 1 | td-0001 хвост | ", self.ok("todo", "--md"))
        self.assertIn("| P9 | открыт |", self.ok("todo", "--md"))
        self.assertIn("| P3 | ждёт |", self.ok("wishes", "--md"))
        self.assertIn("| Хотелки | 1 | P3 1 |", self.ok("queues", "--md"))
        self.assertEqual([r["priority"] for r in json.loads(self.ok("queues", "--json"))][-4:-1],
                         [{"3": 1}, {"5": 1}, {"9": 1}])
        self.assertIn("Приоритет: P7 (1-10, 10 — важнее всего; `depot2 priority tr-0001:1 N`).",
                      self.ok("car", "show", "tr-0001:1"))
        self.assertEqual(json.loads(self.ok("car", "show", "wl-0001", "--json"))["priority"], 3)
        self.assertEqual(json.loads(self.ok("tracks", "--json"))["tracks"][0]["cars"][0]["priority"], 7)
        self.assertEqual(json.loads(self.ok("trains", "--json"))["trains"][0]["priority"], 1)
        self.assertEqual(json.loads(self.ok("stack", "--json"))["queue"][0]["priority"], 10)
        board = json.loads(self.ok("board", "--json"))
        items = {it["ref"]: it for c in board["columns"] for it in c["items"]}
        self.assertEqual((items["tr-0001:1"]["priority"], items["st-0001"]["priority"], items["wl-0001"]["priority"]),
                         (7, 10, 3))
        self.assertIn("| Вагон | Приоритет | Что |", self.ok("board", "--md"))

    def test_the_one_command_for_every_kind_and_one_car_one_priority(self):
        tk = self.tracks()
        tk.car_add("tr-0001", "вагон", n="1")
        self.trains().add("katalog", "Каталог", cars=["tr-0001:1"])
        Stack(self.t.state, self.t.cfg).add("чужой")
        TeamStack(self.t.state, self.t.cfg).add("question")
        Wishes(self.t.state).add("обложки")
        Think(self.t.state).add("подсказки")
        Todo(self.t.state).add("хвост", version="v")
        for ref, n in (("katalog", 9), ("tr-0001:1", 8), ("st-0001", 7), ("tm-0001", 6), ("wl-0001", 4),
                       ("th-0001", 3), ("td-0001", 2)):
            self.assertIn(": приоритет P5 → P%d" % n, self.ok("priority", ref, str(n)))  # noqa: UP031
            self.assertIn("%s: приоритет P%d (1-10" % (ref, n), self.ok("priority", ref))  # noqa: UP031
        self.assertIn("приоритет P2 (был тот же)", self.ok("priority", "td-0001", "2"))
        self.assertIn("трек (тема)", self.refused("priority", "tr-0001", "5"))
        self.assertIn("priority is 1-10", self.refused("priority", "wl-0001", "0"))
        self.assertIn("no train nope", self.refused("priority", "nope", "5"))
        # a car and its object: one car — whichever is named, both get it
        out = self.ok("tracks", "car", "stack", "tr-0001:1")
        self.assertIn("st-0002 in the stack queue", out)
        stk = Stack(self.t.state, self.t.cfg)
        self.assertEqual(stk.get("st-0002")["priority"], 8)  # the new object takes its car's
        self.assertIn("tr-0001:1 (объект st-0002): приоритет P8 → P10", self.ok("priority", "st-0002", "10"))
        self.assertEqual((self.tracks().car("tr-0001:1")[1]["priority"],
                          Stack(self.t.state, self.t.cfg).get("st-0002")["priority"]), (10, 10))
        self.ok("tracks", "car", "set", "tr-0001:1", "--priority", "4")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0002")["priority"], 4)
        out = self.ok("stack", "set", "st-0002", "--priority", "6")
        self.assertIn("tr-0001:1: приоритет P6 (вагон и его объект st-0002)", out)
        self.assertEqual(self.tracks().car("tr-0001:1")[1]["priority"], 6)
        # it travels: a car moved to another track keeps it, its old ref finds it
        self.ok("stack", "done", "st-0002", "--reason", "назад", "--car", "back")
        self.ok("tracks", "car", "move", "tr-0001:1", "--to", "tr-0002", "--why", "тест")
        self.assertEqual(self.tracks().car("tr-0002:1")[1]["priority"], 6)
        self.assertIn("tr-0001:1 → tr-0002:1: приоритет P6 → P7", self.ok("priority", "tr-0001:1", "7"))
        # a linked object takes the car's
        Stack(self.t.state, self.t.cfg).add("ещё один", priority=2)
        self.ok("tracks", "car", "set", "tr-0002:1", "--st", "st-0003")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0003")["priority"], 7)

    def test_take_gives_the_new_car_the_entry_s_priority(self):
        Wishes(self.t.state).add("обложки", priority=9)
        Todo(self.t.state).add("хвост", version="v", priority=2)
        Stack(self.t.state, self.t.cfg).add("объект", priority=8)
        TeamStack(self.t.state, self.t.cfg).add("question", priority=3)
        self.ok("wishes", "take", "wl-0001", "--to", "tr-0001")
        self.ok("stack", "take", "st-0001", "--to", "tr-0001")
        self.ok("tstack", "take", "tm-0001", "--to", "tr-0001")
        self.assertEqual([c["priority"] for c in self.tracks().get("tr-0001")["cars"]], [9, 8, 3])
        out = self.ok("todo", "take", "td-0001", "--to", "tr-0001:0")  # a car that is: it keeps its own
        self.assertIn("приоритет вагона — его, P9 (у td-0001 был P2)", out)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["priority"], 9)
        self.assertEqual(Todo(self.t.state).get("td-0001")["priority"], 2)


class NextByPriorityTest(PBase):
    def test_the_trains_then_the_cars_then_the_stack_ties_as_before(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("первый в очереди", autostart=True)
        stk.add("второй, но важнее", autostart=True, priority=9)
        tk = self.tracks()
        a, b = tk.add("А"), tk.add("Б")
        for n in "0123":
            tk.car_add(a["id"], "a" + n, size="S")
        tk.car_add(b["id"], "b0", size="S")
        self.ride("tr-0001", "first", slots="all-free")
        self.ride("tr-0002", "second", slots="all-free")
        nx = self.trains().next(PAST)  # all at 5: as before, the queue order and the train's order
        self.assertEqual(["{}:{}".format(p["track"]["id"], p["car"]["n"]) for p in nx["picks"]],
                         ["tr-0001:0", "tr-0001:1", "tr-0001:2", "tr-0001:3"])
        self.trains().set("second", priority=8)        # the second train is more important: it goes first
        tk = self.tracks()
        tk.car_set("tr-0001:2", priority=7)           # within a train, by the car's priority
        tk.car_set("tr-0001:3", priority=7)
        nx = self.trains().next(PAST)
        self.assertEqual(["{}:{}".format(p["track"]["id"], p["car"]["n"]) for p in nx["picks"]],
                         ["tr-0002:0", "tr-0001:2", "tr-0001:3", "tr-0001:0"])
        lines = "\n".join(self.trains().next_lines(PAST))
        self.assertIn("tr-0002:0 «b0» (поезд second P8, вагон P5)", lines)
        self.assertIn("Порядок: ремонт вне очереди, потом поезда в «имплементации» по приоритету поезда", lines)
        # the stack's autostart queue: by priority, ties in its order
        self.trains().set("first", priority=1)
        self.trains().set_state("first", "design")
        self.trains().set_state("second", "design")
        nx = self.trains().next(PAST)
        self.assertEqual([o["id"] for o in nx["stack"]], ["st-0002", "st-0001"])
        self.assertIn("st-0002 (P9) — `depot2 stack start st-0002`", "\n".join(
            __import__("depot2.trains", fromlist=["x"]).freed_slot_lines(self.t.state, self.t.cfg)))


class MigrateTest(PBase):
    """Schema 3 → 4: every record gets `priority`; the queues' `prio` words 8 / 5 / 2 (a `prio:` note, else 5), the
    field off, kept in the queues' history rows; the gate last; a second run changes nothing."""

    FILES = ("tracks.json", "trains.json", "stack.json", "team-stack.json", "wishes.json", "think.json", "todo.json")

    def depot3(self, state):
        """A depot as schema 3 left it: no `priority` anywhere, the queues' `prio` words."""
        tk = Tracks(state, self.t.cfg)
        tk.add("Трек")
        tk.car_add("tr-0001", "вагон")
        tk.car_stack("tr-0001:0")
        self.trains().add("katalog", "Каталог", cars=["tr-0001:0"])
        Stack(state, self.t.cfg).add("чужой")
        TeamStack(state, self.t.cfg).add("question")
        Wishes(state).add("высокая")
        Wishes(state).add("потом")
        Think(state).add("обычная")
        for text in ("из поля", "из заметки", "без ничего", "поле и заметка"):
            Todo(state).add(text, version="v")
        prio = {"wl-0001": "high", "wl-0002": "later", "th-0001": "normal", "td-0001": "later", "td-0004": "high"}
        for f in self.FILES:
            d = read_json(state.path(f))
            for key in ("tracks", "trains", "objects", "entries"):
                for r in d.get(key) or []:
                    r.pop("priority", None)
                    for c in r.get("cars") or [] if key == "tracks" else []:
                        c.pop("priority", None)
                    if r.get("id") in prio:
                        r["prio"] = prio[r["id"]]
            if f == "todo.json":
                d["entries"][1]["notes"] = ["2015-11-06: prio: high — из заметки"]
                d["entries"][3]["notes"] = ["2015-11-06: prio: later — поле главнее"]
            if f in ("tracks.json", "trains.json"):
                d["schema"] = 3
            write_json(state.path(f), d)

    def test_the_words_the_notes_the_default_once(self):
        self.depot3(self.t.state)
        self.assertEqual(migrate.version(self.t.state), 3)
        before = {f: slurp(self.t.state.path(f)) for f in self.FILES}
        lines, n = migrate.run(self.t.state, self.t.cfg, dry_run=True)
        self.assertEqual((n, lines[0]), (1, "DRY RUN: ничего не записано"))
        self.assertIn("todo: prio → priority у 4 (high 8, normal 5, later 2; без слова — 5): P8 2 · P5 1 · P2 1; "
                      "из заметки «prio: …»: td-0002", lines)
        self.assertIn("приоритет 5 (по умолчанию): поездов 1, вагонов треков 1, объектов стека 2, стека команды 1 "
                      "(объект вагона — как у его вагона)", lines)
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in self.FILES}, before)  # a dry run writes nothing
        rc, out, err = self.team("todo", "--md")  # any command migrates first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 3 → 4", err)
        self.assertEqual(migrate.version(self.t.state), SCHEMA)
        self.assertEqual(read_json(self.t.state.path("tracks.json"))["schema"], SCHEMA)
        got = {e["id"]: e["priority"] for q in (Wishes, Think, Todo) for e in q(self.t.state).entries()}
        self.assertEqual(got, {"wl-0001": 8, "wl-0002": 2, "th-0001": 5, "td-0001": 2, "td-0002": 8, "td-0003": 5,
                               "td-0004": 8})
        self.assertFalse(any("prio" in e for q in (Wishes, Think, Todo) for e in q(self.t.state).entries()))
        tk = self.tracks()
        self.assertEqual((tk.car("tr-0001:0")[1]["priority"], tk.trains.get("katalog")["priority"]), (5, 5))
        self.assertEqual([o["priority"] for o in Stack(self.t.state, self.t.cfg).objects()], [5, 5])
        self.assertEqual(TeamStack(self.t.state, self.t.cfg).get("tm-0001")["priority"], 5)
        rows = [h for h in read_jsonl(self.t.state.path("todo-history.jsonl")) if h["event"] == "migrate_priority"]
        self.assertEqual([(h["prio"], h["from_notes"]) for h in rows],
                         [({"td-0001": "later", "td-0002": "high", "td-0003": None, "td-0004": "high"}, ["td-0002"])])
        row = [h for h in read_jsonl(self.t.state.path("tracks-history.jsonl")) if h.get("schema") == 4]
        self.assertEqual([h["priority"]["todo"] for h in row], [4])
        self.assertIn("| P8 | открыт |", out)
        snap = {f: slurp(self.t.state.path(f)) for f in self.FILES}
        self.assertEqual(migrate.run(self.t.state, self.t.cfg),
                         (["миграция данных депо: нечего переносить (схема %d)" % SCHEMA], 0))  # noqa: UP031
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in self.FILES}, snap)

    def run_team(self, state_dir, *argv):
        import subprocess
        import sys
        here = os.path.dirname(os.path.abspath(__file__))
        p = subprocess.run([sys.executable, os.path.join(os.path.dirname(here), "depot2"), "--state", state_dir, "--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent", *list(argv)],
                           capture_output=True, text=True, env=dict(os.environ, DEPOT2_BACKUP="off"))
        return p.returncode, p.stdout, p.stderr


if __name__ == "__main__":
    import unittest
    unittest.main()
