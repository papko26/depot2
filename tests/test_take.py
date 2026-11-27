import json

from test_queues import QBase

from depot2.queues import Think, Todo, Wishes
from depot2.stack import Stack
from depot2.tracks import Tracks
from depot2.tstack import TeamStack


class TakeBase(QBase):
    def setUp(self):
        super().setUp()
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("Некритичные баги")
        tr.car_add("tr-0001", "поиск по пустой строке")  # car 0: the next free name is 1

    def tracks(self):
        return Tracks(self.t.state, self.t.cfg)

    def raw(self, name):
        with open(self.t.state.path(name), encoding="utf-8") as f:
            return f.read()

    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, err)
        return out


class QueueTakeTest(TakeBase):
    def test_a_wish_becomes_a_car_on_the_track(self):
        Wishes(self.t.state).add(
            "заметки о выпуске", words="Список изменений — коротко", date="2015-11-04", area="release"
        )
        out = self.ok("wishes", "take", "wl-0001", "--to", "tr-0001", "--size", "M")
        self.assertIn("wl-0001 → tr-0001:1: новый вагон «заметки о выпуске» (тележка)", out)
        self.assertIn("## Трек tr-0001 Некритичные баги", out)  # the car on its track
        self.assertIn("| — | (пусто) |", out)  # the open wishes: none left
        _t, c = self.tracks().car("tr-0001:1")
        self.assertEqual(
            (c["title"], c["size"], c["status"], c["stage"], c["from"]),
            ("заметки о выпуске", "M", "waiting", "bogie", ["wl-0001"]),
        )
        self.assertEqual(len(c["notes"]), 1)
        self.assertRegex(
            c["notes"][0],
            r"^\d{4}-\d{2}-\d{2}: из wl-0001 — хотелка от 2015-11-04: заметки о выпуске; "
            r"слова владельца: «Список изменений — коротко»$",
        )
        e = Wishes(self.t.state).get("wl-0001")
        self.assertEqual((e["status"], e["taken_to"]), ("taken", "tr-0001:1"))
        self.assertIn("взята → tr-0001:1 | тележка → тележка |", self.team("wishes", "--all")[1])
        # both ways: the car says where it came from, the entry where it went
        self.assertIn("Пришёл из: wl-0001 — хотелка «заметки о выпуске» (взята).", self.ok("car", "show", "tr-0001:1"))
        self.assertIn("Взята → tr-0001:1: тележка.", self.ok("car", "show", "wl-0001"))
        j = json.loads(self.ok("car", "show", "tr-0001:1", "--json"))
        self.assertEqual(
            j["from"],
            [
                {
                    "ref": "wl-0001",
                    "kind": "wish",
                    "kind_ru": "хотелка",
                    "title": "заметки о выпуске",
                    "status_ru": "взята",
                }
            ],
        )
        self.assertEqual(json.loads(self.ok("car", "show", "wl-0001", "--json"))["taken_to"], "tr-0001:1")
        self.assertEqual(json.loads(self.ok("car", "show", "tr-0001:0", "--json"))["from"], [])
        # the history
        h = self.tracks().history()[-1]
        self.assertEqual((h["event"], h["car"], h["from"]), ("car_add", "1", "wl-0001"))
        out = self.ok("car", "show", "tr-0001:1")
        self.assertIn("\nЗаметки:\n- {}\n".format(c["notes"][0]), out)
        self.assertLess(out.index("Заметки:"), out.index("История этапов:"))
        self.ok("tracks", "car", "stack", "tr-0001:1")
        oid = self.tracks().car("tr-0001:1")[1]["st"]
        self.ok("stack", "set", oid, "--note", "ждёт ответа владельца")
        notes = self.ok("car", "show", "tr-0001:1").split("Заметки:\n")[1].split("\n\n")[0].splitlines()
        self.assertEqual(notes[0], "- " + c["notes"][0])
        self.assertRegex(notes[1], r"^- \d{4}-\d{2}-\d{2}: ждёт ответа владельца$")
        self.assertNotIn("Заметки:", self.ok("car", "show", "tr-0001:0"))
        self.assertEqual(
            (Wishes(self.t.state).history()[-1]["event"], Wishes(self.t.state).history()[-1]["taken_to"]),
            ("taken", "tr-0001:1"),
        )
        # a second take: refused, nothing written
        before = self.raw("tracks.json")
        rc, _, err = self.team("wishes", "take", "wl-0001", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("wl-0001 is взята → tr-0001:1: only an open entry is taken", err)
        self.assertEqual(self.raw("tracks.json"), before)

    def test_a_to_think_entry_with_its_name_and_title(self):
        Think(self.t.state).add(
            "перенос «подумать» в трек", questions=["Одной командой?", "А todo?"], date="2015-11-21"
        )
        out = self.ok("think", "take", "th-0001", "--to", "tr-0001", "--n", "7", "--title", "take для всех очередей")
        self.assertIn("th-0001 → tr-0001:7: новый вагон «take для всех очередей» (тележка)", out)
        c = self.tracks().car("tr-0001:7")[1]
        self.assertEqual((c["title"], c["from"]), ("take для всех очередей", ["th-0001"]))
        self.assertIn(
            "из th-0001 — «подумать» от 2015-11-21: перенос «подумать» в трек; вопросы: 1) Одной командой?; 2) А todo?",
            c["notes"][0],
        )
        e = Think(self.t.state).get("th-0001")
        self.assertEqual((e["status"], e["taken_to"]), ("taken", "tr-0001:7"))
        self.assertEqual(Think(self.t.state).entries(open_only=True), [])
        self.assertIn("взято → tr-0001:7 | тележка → тележка |", self.team("think", "--all")[1])
        self.assertIn("Взято → tr-0001:7: тележка.", self.ok("car", "show", "th-0001"))
        self.assertIn(
            "Пришёл из: th-0001 — «подумать» «перенос «подумать» в трек» (взято).", self.ok("car", "show", "tr-0001:7")
        )

    def test_a_todo_with_its_version_and_place(self):
        Todo(self.t.state).add(
            "maxQueryMs не сбрасывается между запросами",
            where=["lib/search.ts:1029"],
            version="2.4.0, main 3c9e1d7",
            date="2015-11-05",
            source="агент",
            kind="perf",
        )
        out = self.ok("todo", "take", "td-0001", "--to", "tr-0001")
        self.assertIn("td-0001 → tr-0001:1: новый вагон «maxQueryMs не сбрасывается между запросами» (тележка)", out)
        c = self.tracks().car("tr-0001:1")[1]
        self.assertIn(
            "из td-0001 — todo от 2015-11-05: maxQueryMs не сбрасывается между запросами (вид: перф; версия: 2.4.0, "
            "main 3c9e1d7; где: lib/search.ts:1029; источник: агент)",
            c["notes"][0],
        )
        e = Todo(self.t.state).get("td-0001")
        self.assertEqual((e["status"], e["taken_to"]), ("taken", "tr-0001:1"))
        self.assertIn("| взят → tr-0001:1 |", self.team("todo", "--all")[1])
        self.assertEqual(json.loads(self.ok("car", "show", "tr-0001:1", "--json"))["from"][0]["kind"], "todo")
        # a todo's long text is cut for the title; the note keeps it whole
        Todo(self.t.state).add("x" * 150, version="v")
        self.ok("todo", "take", "td-0002", "--to", "tr-0001")
        c = self.tracks().car("tr-0001:2")[1]
        self.assertEqual(len(c["title"]), 100)
        self.assertIn("x" * 150, c["notes"][0])

    def test_a_taken_name_and_the_other_refusals_write_nothing(self):
        Todo(self.t.state).add("второй", version="v")
        files = ("tracks.json", "todo.json")
        before = [self.raw(f) for f in files]
        for argv, said in (
            (("--n", "0"), "номер занят: tr-0001:0 — поиск по пустой строке (ждёт); свободный номер — --n 1"),
            (("--to", "tr-0404"), "no track tr-0404"),
            (("--to", "tr-0001:5"), "tr-0001 has no car 5"),
            (
                ("--to", "tr-0001:0", "--n", "3"),
                "--n, --size и --title — для нового вагона: --to tr-NNNN; tr-0001:0 — вагон, что уже есть",
            ),
            (("--to", "st-0404"), "unknown stack object st-0404"),
            (("--to", "nowhere"), "--to is tr-NNNN (a new car on it), tr-NNNN:<car>, st-NNNN or tm-NNNN"),
            (("--size", "XXL"), "a size is one of L, M, S, doc"),
        ):
            to = () if "--to" in argv else ("--to", "tr-0001")
            rc, _, err = self.team("todo", "take", "td-0001", *(to + argv))
            self.assertEqual(rc, 1, argv)
            self.assertIn(said, err)
            self.assertEqual([self.raw(f) for f in files], before, argv)
        self.assertEqual(Todo(self.t.state).get("td-0001")["status"], "open")

    def test_the_old_wishes_take_forms_still_work(self):
        w = Wishes(self.t.state)
        for title in ("в объект стека", "в стек команды", "в вагон, что есть"):
            w.add(title, date="2015-11-05")
        Stack(self.t.state, self.t.cfg).add("Полка")
        TeamStack(self.t.state, self.t.cfg).add("discuss", title="тема")
        self.assertIn("wl-0001 → st-0001", self.ok("wish", "take", "wl-0001", "--to", "st-0001"))
        self.assertIn("wl-0002 → tm-0001", self.ok("wishes", "take", "wl-0002", "--to", "tm-0001"))
        out = self.ok("wishes", "take", "wl-0003", "--to", "tr-0001:0")
        self.assertIn("wl-0003 → tr-0001:0: в вагон «поиск по пустой строке» (его заметка и from)", out)
        self.assertEqual(
            [Wishes(self.t.state).get(i)["taken_to"] for i in ("wl-0001", "wl-0002", "wl-0003")],
            ["st-0001", "tm-0001", "tr-0001:0"],
        )
        c = self.tracks().car("tr-0001:0")[1]
        self.assertEqual((c["from"], c["title"]), (["wl-0003"], "поиск по пустой строке"))
        self.assertIn("из wl-0003 — хотелка от 2015-11-05: в вагон, что есть", c["notes"][-1])
        self.assertEqual(self.tracks().history()[-1]["event"], "car_from")
        self.assertEqual(len(self.tracks().car("tr-0001:0")[0]["cars"]), 1)  # no car made for these


class StackTakeTest(TakeBase):
    def test_an_object_becomes_a_car_and_stays_its_object(self):
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("обложка в карточке, а в списке нет", brief="/tmp/brief-cover.md", size="S")
        stk.add("счётчик пропал")
        stk.add("кнопка экспорта")
        self.ok("stack", "start", "st-0002")
        self.ok("stack", "start", "st-0003")
        self.ok("stack", "set", "st-0003", "--pr", "77")
        self.ok("stack", "post", "st-0003")
        out = self.ok("stack", "take", "st-0001", "--to", "tr-0001")
        self.assertIn(
            "st-0001 → tr-0001:1: новый вагон «обложка в карточке, а в списке нет» (ждёт, эскиз); st-0001 — его "
            "объект: «tr-0001:1 Некритичные баги — обложка в карточке, а в списке нет»",
            out,
        )
        t, c = self.tracks().car("tr-0001:1")  # noqa: RUF059
        self.assertEqual(
            (c["st"], c["status"], c["stage"], c["size"], c["brief"], c["from"]),
            ("st-0001", "waiting", "sketch", "S", "/tmp/brief-cover.md", ["st-0001"]),
        )
        self.assertIn("из st-0001 — объект стека (в очереди; добавлен ", c["notes"][0])
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual(
            (o["where"], o["title"]), ("queue", "tr-0001:1 Некритичные баги — обложка в карточке, а в списке нет")
        )
        self.assertIn("tr-0001:1 (объект st-0001)", self.ok("car", "show", "st-0001"))
        # a slot: the car is in work; the post-stack: its PR waits for the owner's merge
        self.ok("stack", "take", "st-0002", "--to", "tr-0001", "--title", "счётчик")
        self.assertEqual(self.tracks().car("tr-0001:2")[1]["status"], "running")
        self.ok("stack", "take", "st-0003", "--to", "tr-0001")
        _t, c = self.tracks().car("tr-0001:3")
        self.assertEqual((c["status"], c["pr"]), ("pr", 77))
        self.assertTrue(self.tracks().posted(c))
        # refused: a car's object already, one that left
        rc, _, err = self.team("stack", "take", "st-0001", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 — уже объект вагона tr-0001:1", err)
        rc, _, err = self.team("stack", "take", "st-0001", "--to", "tr-0001:1")
        self.assertIn("--to — трек tr-NNNN (на нём новый вагон)", err)
        # the link works as any car's: done --car merged closes the car
        self.ok("stack", "done", "st-0002", "--reason", "merged #78", "--car", "merged")
        self.assertEqual(self.tracks().car("tr-0001:2")[1]["status"], "merged")
        stk = Stack(self.t.state, self.t.cfg)
        stk.add("ушёл")
        self.ok("stack", "done", "st-0004", "--reason", "снят")
        rc, _, err = self.team("stack", "take", "st-0004", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0004 вышел из стека", err)

    def test_a_team_item_becomes_a_car_and_leaves(self):
        ts = TeamStack(self.t.state, self.t.cfg)
        ts.add("question", title="спросить команду про take", input_path="/tmp/q.md")
        ts.add("discuss", title="идёт сейчас")
        self.ok("tstack", "start", "tm-0002")
        out = self.ok("tstack", "take", "tm-0001", "--to", "tr-0001", "--size", "S")
        self.assertIn(
            "tm-0001 → tr-0001:1: новый вагон «спросить команду про take» (тележка); tm-0001 вышел из стека команды",
            out,
        )
        c = self.tracks().car("tr-0001:1")[1]
        self.assertEqual((c["from"], c["size"], c["st"], c["stage"]), (["tm-0001"], "S", None, "bogie"))
        self.assertIn(
            "из tm-0001 — стек команды (в очереди, вид: вопрос команде; кто: все; вход: /tmp/q.md; добавлен ",
            c["notes"][0],
        )
        self.assertNotIn("tm-0001", [o["id"] for o in TeamStack(self.t.state, self.t.cfg).objects()])
        h = TeamStack(self.t.state, self.t.cfg).history()[-1]
        self.assertEqual((h["event"], h["reason"]), ("done", "взят в вагон tr-0001:1"))
        self.assertIn(
            "Пришёл из: tm-0001 — стек команды «спросить команду про take» (вышел из стека).",
            self.ok("car", "show", "tr-0001:1"),
        )
        rc, _, err = self.team("tstack", "take", "tm-0002", "--to", "tr-0001")
        self.assertEqual(rc, 1)
        self.assertIn("tm-0002 в слоте команды: идёт его раунд", err)


class MigrateFromTest(TakeBase):
    def test_the_cars_taken_into_before_get_from_once(self):
        tr = self.tracks()
        tr.add("Справка")
        tr.car_add("tr-0002", "заметки о выпуске", note="2015-11-21: из хотелки wl-0002 (по слову владельца)")
        w = Wishes(self.t.state)
        for title in ("в вагон", "в трек, вагон по заметке", "в трек, вагона нет"):
            w.add(title, date="2015-11-05")
        # as `wishes take` wrote them before: only the mark
        w.to_status("wl-0001", "taken", taken_to="tr-0001:0")
        w.to_status("wl-0002", "taken", taken_to="tr-0002")
        w.to_status("wl-0003", "taken", taken_to="tr-0001")
        out = self.ok("queues", "migrate", "--dry-run")
        self.assertIn("вагоны: откуда пришли (from) — записано у 2: tr-0001:0 ← wl-0001, tr-0002:0 ← wl-0002", out)
        self.assertIn(
            "взяты в трек, не в вагон: 1 → вагон, что их называет: wl-0002 → tr-0002:0; вагон не найден, "
            "остались как были: wl-0003 → tr-0001 (ни один вагон трека его не называет)",
            out,
        )
        self.assertNotIn("from", self.tracks().car("tr-0001:0")[1])  # the dry run wrote nothing
        self.ok("queues", "migrate")
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["from"], ["wl-0001"])
        self.assertEqual(self.tracks().car("tr-0002:0")[1]["from"], ["wl-0002"])
        w = Wishes(self.t.state)
        self.assertEqual(
            [w.get(i)["taken_to"] for i in ("wl-0001", "wl-0002", "wl-0003")], ["tr-0001:0", "tr-0002:0", "tr-0001"]
        )
        self.assertEqual(w.history()[-1]["taken_to"], {"wl-0002": {"was": "tr-0002", "now": "tr-0002:0"}})
        self.assertIn(
            "Пришёл из: wl-0002 — хотелка «в трек, вагон по заметке» (взята).", self.ok("car", "show", "tr-0002:0")
        )
        self.assertIn("перенесено: 0", self.ok("queues", "migrate"))  # a second run changes nothing
