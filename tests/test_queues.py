"""The overview of every queue (`depot2 queues`), the team's stack, and the queues of before — wishes, to-think,
todo — that the data migration makes tracks of the same name (schema 5 → 6, migrate.py). Every entry is made up."""
import json
import os

from helpers import TempState, slurp
from test_extras import Base

from depot2 import migrate
from depot2.queues import ROWS
from depot2.stack import Stack, StackError
from depot2.store import read_json, read_jsonl, write_json
from depot2.tracks import SCHEMA, Tracks
from depot2.trains import Trains
from depot2.tstack import TeamStack


def write(d, name, text):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


TS = "2015-11-04T10:00:00+02:00"


def entry(eid, status, **fields):
    """An entry of a queue of before, as a depot of schema 5 kept it."""
    e = {"id": eid, "added": TS, "date": TS[:10], "status": status, "links": [], "notes": [], "priority": 5}
    e.update(fields)
    return e


def old_queue(state, name, entries):
    """<name>.json and <name>-history.jsonl as the queue left them: one `add` row an entry."""
    write_json(state.path(name + ".json"), {"next": len(entries) + 1, "entries": entries})
    with open(state.path(name + "-history.jsonl"), "w", encoding="utf-8") as f:
        for e in entries:
            f.write(json.dumps({"ts": TS, "event": "add", "entry": e["id"]}, ensure_ascii=False) + "\n")


def at_schema(state, n):
    for f, empty in (("tracks.json", {"next": 1, "tracks": []}), ("trains.json", {"trains": []})):
        d = read_json(state.path(f)) or dict(empty)
        d["schema"] = n
        write_json(state.path(f), d)


class QBase(Base):
    def team(self, *argv):
        return super(QBase, self).team("--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent", *argv)


class TeamStackTest(QBase):
    def test_two_slots_and_four_waiting_beside_the_agents_stack(self):
        ts = TeamStack(self.t.state, self.t.cfg)
        self.assertEqual((ts.slots_n, ts.queue_max), (2, 4))
        topic = write(self.out, "topic.md", "# Тема\n")
        a = ts.add("discuss", input_path=topic, who="Копилка", size="S")
        self.assertEqual((a["id"], a["who"], a["title"]), ("tm-0001", "keeper", "обсуждение: topic.md"))
        ts.add("review-10")
        ts.add("question", title="вопрос про лимиты")
        ts.add("reflect", depends=["tm-0001"])
        with self.assertRaises(StackError) as e:
            ts.add("export")
        self.assertEqual(str(e.exception), "очередь команды полна (4)")
        with self.assertRaises(StackError):
            ts.add("dance")
        ts.start("tm-0001")
        ts.start("tm-0002")
        with self.assertRaises(StackError):
            ts.start("tm-0003")  # 2 slots
        with self.assertRaises(StackError):
            ts.start("tm-0004")
        # the agents' stack is a different file: untouched
        self.assertEqual(Stack(self.t.state, self.t.cfg).objects(), [])
        self.assertTrue(os.path.exists(os.path.join(self.t.dir, "team-stack.json")))
        self.assertEqual(ts.command(ts.get("tm-0001")),
                         "depot2 round --kind discuss --input %s --only keeper --item tm-0001 --dry-run" % topic)
        # the spend from the round's own tokens: 10 + 200000 × 1.25 + 3e6 × 0.1 + 40000 × 5 = 0.75 MTok
        ts.record_round("tm-0001", "2015-11-06-discuss-solo-keeper", {"total_usd": 0.4321, "tokens": {
            "input": 10, "cache_write": 200000, "cache_read": 3000000, "output": 40000, "total": 3240010}})
        md = TeamStack(self.t.state, self.t.cfg).md()
        self.assertIn("| № | Где | Что | Приоритет | Вид | Кто | Вход | Раунд | Статус | Effort (ожидаемо / потрачено) | "
                      "Зависит от |", md)
        self.assertIn("| С1 | слот | tm-0001 обсуждение: topic.md | P5 | обсуждение | Копилка | topic.md | "
                      "2015-11-06-discuss-solo-keeper | раунд прошёл: 2015-11-06-discuss-solo-keeper | "
                      "≈ 0.3%UW (3 MTok) / 0.075%UW (0.75 MTok) | — |", md)
        self.assertNotIn("$", md)
        self.assertIn("| **Ждёт слота (2/4)** |", md)
        ts.done("tm-0001", "отчёт показан")
        ts.start("tm-0004")

    def test_the_cli_and_a_dependency_on_the_agents_stack(self):
        self.team("stack", "add", "--title", "PR импорта")
        rc, out, err = self.team("tstack", "add", "--kind", "review", "--depends", "st-0001", "--title", "обзор импорта")
        self.assertEqual(rc, 0, err)
        self.assertIn("tm-0001 added to the team queue (1/4)", out)
        rc, _, err = self.team("tstack", "start", "tm-0001")
        self.assertIn("depends on st-0001, not done yet", err)
        self.team("stack", "start", "st-0001")
        self.team("stack", "done", "st-0001", "--reason", "merged")
        rc, out, err = self.team("tstack", "start", "tm-0001")
        self.assertEqual(rc, 0, err)
        self.assertIn("run: depot2 round --kind review --item tm-0001 --dry-run", out)
        rc, out, err = self.team("tstack", "set", "tm-0001", "--who", "counter", "--cost", "0.5")
        self.assertEqual(rc, 0, err)
        # the old ≈$ flag: converted once into the spend, 0.5 / 2.5 = 0.2 MTok, with a deprecation note
        self.assertIn("warning: --cost is deprecated (the spend is in MTok now): read as --spent-mtok 0.2, "
                      "0.02%UW (0.2 MTok) (0.5 / usage.usd_per_mtok 2.5)", err)
        self.assertEqual(TeamStack(self.t.state, self.t.cfg).get("tm-0001")["who"], "counter")
        self.assertEqual(TeamStack(self.t.state, self.t.cfg).get("tm-0001")["spent_mtok"], 0.2)
        self.assertEqual(self.team("tstack", "set", "tm-0001", "--cost", "0.5", "--spent-mtok", "1")[0], 1)
        data = json.loads(self.team("tstack", "--json")[1])
        self.assertEqual((data["slots"], data["queue_max"], data["kind"]), (2, 4, "team"))
        self.assertEqual(self.team("tstack", "done", "tm-0001", "--reason", "ok")[0], 0)
        self.assertEqual(self.team("--set", "team_stack.slots=3", "tstack")[1].count("свободен"), 3)




class OverviewTest(QBase):
    def test_queues_md_shows_every_queue(self):
        self.team("stack", "add", "--title", "Импорт 3")
        self.team("tstack", "add", "--kind", "discuss", "--title", "лимиты")
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("Поиск")
        tr.car_add("tr-0001", "фильтры", size="M")
        tr.add("todo")
        tr.car_add("tr-0002", "хвост импорта")
        trains = Trains(self.t.state, self.t.cfg)
        trains.add("poisk", "Поиск", cars=["tr-0001:0"], slots="all-free")
        trains.set_state("poisk", "impl")
        self.team("decide", "all", "--text", "делаем по мере нужды")
        rc, out, err = self.team("queues", "--md")
        self.assertEqual(rc, 0, err)
        self.assertIn("| Очередь | Открыто | Приоритет | Сверху | Команда |", out)
        self.assertIn("| Стек агентов | слоты 0/4, пост-стек 0/4, ждёт 1/10 | — | st-0001 Импорт 3 | `depot2 stack --md` |", out)
        self.assertIn("| Мердж-очередь | 0 | — | — | `depot2 merge --md` |", out)  # right under the agents' stack
        self.assertIn("| Стек команды | слоты 0/2, ждёт 1/4 | — | tm-0001 лимиты | `depot2 tstack --md` |", out)
        self.assertIn("| Поезда | 1 (имплементация 1) | — | можно сейчас: tr-0001:0; poisk (имплементация) | "
                      "`depot2 trains --md` |", out)
        self.assertIn("| Треки | 2 открыто из 2 | — | tr-0001 Поиск (1); tr-0002 todo (1) | `depot2 tracks --md` |", out)
        self.assertIn("| Агенты | 0 в работе | — | — | `depot2 agents --md` |", out)
        self.assertIn("| Бюллетени | 0 | — | — |", out)
        self.assertIn("| Решения | всего 1 | — | dec-000001 делаем по мере нужды | `depot2 decisions --md` |", out)
        self.assertNotIn("Хотелки", out)
        self.assertEqual(self.team("queues")[1], out)
        rows = json.loads(self.team("queues", "--json")[1])
        self.assertEqual([r["queue"] for r in rows],
                         ["Стек агентов", "Мердж-очередь", "Стек команды", "Поезда", "Треки", "Агенты", "Бюллетени",
                          "Решения", "Принципы"])
        self.assertEqual([r["key"] for r in rows], list(ROWS))
        self.assertEqual(sorted(rows[0]), ["command", "head", "key", "means", "open", "priority", "queue"])
        self.assertTrue(rows[1]["means"].startswith("PR, впущенные владельцем, с его словом дословно"))
        self.assertIn("вагон уходит с доски прибывшим или списанным", rows[4]["means"])
        self.assertEqual([r["priority"] for r in rows], [None] * 9)


class PriorityMigrateTest(QBase):
    """Schema 3 → 4 read a todo's, a to-think entry's and a wish's priority from its word or its `prio: …` note; schema
    6 then makes the open ones cars, with that priority."""

    def setUp(self):
        super(PriorityMigrateTest, self).setUp()
        self.team("stack", "add", "--title", "старый объект")  # a command first: the queue files below make one migrate
        note = "2015-11-05: prio: %s"
        old_queue(self.t.state, "todo", [
            entry("td-0001", "open", text="один", notes=[note % "high — текст на проде"]),
            entry("td-0002", "open", text="два", notes=[note % "later — по заголовку", note % "Normal — передумали"]),
            entry("td-0003", "open", text="три", notes=["2015-11-05: см. td-0001; prio: high тут не в начале"]),
            entry("td-0004", "open", text="четыре", prio="high", notes=[note % "later — старая заметка"]),
            entry("td-0005", "done", text="пять", notes=[note % "later — закрыт", "2015-11-05: сделан"])])
        old_queue(self.t.state, "think", [entry("th-0001", "waiting", topic="подсказки"),
                                          entry("th-0002", "waiting", topic="обложки", notes=[note % "later — к релизу"])])
        old_queue(self.t.state, "wishes", [entry("wl-0001", "waiting", title="обложки", area="product", prio="high",
                                                 notes=[note % "normal — по вычитке"])])
        for name in ("todo", "think", "wishes"):  # the depot as schema 3 left them: the words, or no field at all
            d = read_json(self.t.state.path(name + ".json"))
            for e in d["entries"]:
                e.pop("priority")
            write_json(self.t.state.path(name + ".json"), d)
        d = read_json(self.t.state.path("stack.json"))
        for o in d["objects"]:
            o.pop("priority")
            o.pop("autostart")
        write_json(self.t.state.path("stack.json"), d)
        write_json(self.t.state.path("trains.json"), {"schema": 3, "trains": []})

    def files(self):
        return {n: slurp(os.path.join(self.t.dir, n)) for n in ("todo.json", "todo-history.jsonl", "think.json",
                                                                 "stack.json", "stack-history.jsonl")}

    def test_dry_run_then_once_and_idempotent(self):
        before = self.files()
        rc, out, err = self.team("migrate", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("todo: prio → priority у 5 (high 8, normal 5, later 2; без слова — 5): P8 2 · P5 2 · P2 1; "
                      "из заметки «prio: …»: td-0001, td-0002, td-0005", out)
        self.assertIn("«подумать»: prio → priority у 2 (high 8, normal 5, later 2; без слова — 5): P5 1 · P2 1; "
                      "из заметки «prio: …»: th-0002", out)
        self.assertIn("хотелки: prio → priority у 1 (high 8, normal 5, later 2; без слова — 5): P8 1", out)
        self.assertEqual(self.files(), before)  # wrote nothing
        rc, out, err = self.team("queues", "migrate", "--dry-run")  # any command migrates the data first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 3 → 4", err)
        self.assertIn("миграция данных депо: схема 5 → 6", err)
        self.assertIn("DRY RUN: перенёс бы 1 записей и объектов", out)
        self.assertIn("- stack: autostart = нет записан явно у 1 объектов: st-0001", out)
        rc, out, err = self.team("queues", "migrate")
        self.assertEqual(rc, 0, err)
        self.assertIn("перенесено: 1 записей и объектов", out)
        arch = read_json(self.t.state.path("todo-v5.json"))
        self.assertEqual({e["id"]: e["priority"] for e in arch["entries"]}, {
            "td-0001": 8, "td-0002": 5, "td-0003": 5, "td-0004": 8, "td-0005": 2})
        self.assertEqual([n[12:] for n in arch["entries"][1]["notes"]], ["prio: later — по заголовку",
                                                                        "prio: Normal — передумали"])  # the notes stay
        tr = Tracks(self.t.state, self.t.cfg)
        cars = {(c.get("from") or [None])[0]: c for t in tr.tracks() for c in t["cars"]}
        self.assertEqual({k: c["priority"] for k, c in cars.items()}, {
            "wl-0001": 8, "th-0001": 5, "th-0002": 2, "td-0001": 8, "td-0002": 5, "td-0003": 5, "td-0004": 8})
        self.assertIs(Stack(self.t.state, self.t.cfg).get("st-0001")["autostart"], False)
        h = [h for h in read_jsonl(self.t.state.path("todo-history-v5.jsonl")) if h["event"] == "migrate_priority"]
        self.assertEqual([(x["prio"], x["from_notes"]) for x in h], [(
            {"td-0001": "high", "td-0002": "normal", "td-0003": None, "td-0004": "high", "td-0005": "later"},
            ["td-0001", "td-0002", "td-0005"])])
        snap = {n: slurp(os.path.join(self.t.dir, n)) for n in ("tracks.json", "todo-v5.json", "stack.json")}
        rc, out, _ = self.team("queues", "migrate")
        self.assertIn("перенесено: 0 записей и объектов", out)
        self.assertIn("нечего переносить: миграция уже прошла", out)
        self.assertEqual({n: slurp(os.path.join(self.t.dir, n)) for n in snap}, snap)


class QueuesToTracksTest(QBase):
    """Schema 5 → 6: an open entry is a car on the track of its queue's name, a closed or a taken one stays in the
    archive; an old ref still reads the archive."""

    def setUp(self):
        super(QueuesToTracksTest, self).setUp()
        st = self.t.state
        tr = Tracks(st, self.t.cfg)
        tr.add("Поиск")
        tr.car_add("tr-0001", "полка", src="wl-0002")  # a wish taken into a car, before
        tr.add("to-think: о чём подумать")
        tr.car_add("tr-0002", "уже на треке", n="2")
        rows = [{"stage": "bogie", "from": None, "at": TS, "by": "wishes add", "why": None},
                {"stage": "sketch", "from": "bogie", "at": TS, "by": "car stage", "why": "бриф пишется"}]
        old_queue(st, "wishes", [
            entry("wl-0001", "waiting", title="обложки видны сразу", words="обложки должны быть заметнее",
                  area="product", priority=8, stage="sketch", stages=rows,
                  asks=[{"n": 1, "q": "Какие обложки?", "at": TS, "by": "car ask", "answer": None, "answered": None}]),
            entry("wl-0002", "taken", title="пустая полка", area="product", taken_to="tr-0001:0"),
            entry("wl-0003", "declined", title="автоимпорт", area="tools", notes=["2015-11-05: не в v1"])])
        old_queue(st, "think", [
            entry("th-0001", "decided", topic="тёмная тема", decision="dec-000001"),
            entry("th-0002", "waiting", topic="подсказки по мере нужды", questions=["Показывать сразу?"], by="Краски",
                  related=["X1"]),
            entry("th-0003", "in_round", topic="лимиты", round="2015-11-05-discuss")])
        old_queue(st, "todo", [
            entry("td-0001", "open", text="maxQueryMs не сбрасывается", where=["lib/search.ts:1029"],
                  version="2.4.0, main 3c9e1d7", source="агент", kind="perf", links=["#12", "dec-000001"],
                  notes=["2015-11-05: виден на больших выдачах"]),
            entry("td-0002", "done", text="старый лог", kind="cleanup"),
            entry("td-0003", "in_pr", text="повторный импорт", kind="bug", pr=195),
            entry("td-0004", "open", title="напомнить", text="про повторный импорт", kind="reminder",
                  trigger="после выкатки поиска", priority=2),
            entry("td-0005", "wont", text="устарело", kind="text")])
        at_schema(st, 5)

    def snap(self):
        return {f: slurp(self.t.state.path(f)) for f in sorted(os.listdir(self.t.dir)) if f.endswith((".json", ".jsonl"))}

    def test_a_dry_run_says_what_goes_where_and_writes_nothing(self):
        before = self.snap()
        rc, out, err = self.team("migrate", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 5 → 6 (очереди хотелок, «подумать» и todo → одноимённые треки", out)
        self.assertIn("хотелки: записей 3 — открытые 1 → вагоны нового трека «wishes» (P8 1); закрытые и взятые (2) "
                      "остаются в архиве wishes-v5.json", out)
        self.assertIn("«подумать»: записей 3 — открытые 2 → вагоны tr-0002 «to-think: о чём подумать» (P5 2); закрытые "
                      "и взятые (1) остаются в архиве think-v5.json", out)
        self.assertIn("  первые: th-0002 → tr-0002:4, th-0003 → tr-0002:3", out)  # its own number 2 is taken
        self.assertIn("todo: записей 5 — открытые 3 → вагоны нового трека «todo» (P5 2 · P2 1); закрытые и взятые (2) "
                      "остаются в архиве todo-v5.json", out)
        self.assertIn("  первые: td-0001 → новый трек:1, td-0003 → новый трек:3, td-0004 → новый трек:4", out)
        self.assertEqual(self.snap(), before)
        self.assertTrue(migrate.pending(self.t.state))

    def test_open_entries_become_cars_the_rest_an_archive(self):
        rc, out, err = self.team("tracks")  # any command migrates first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 5 → 6", err)
        self.assertEqual((migrate.version(self.t.state), SCHEMA), (6, 6))
        for name in ("wishes", "think", "todo"):
            self.assertFalse(os.path.exists(self.t.state.path(name + ".json")), name)
            self.assertFalse(os.path.exists(self.t.state.path(name + "-history.jsonl")), name)
        tr = Tracks(self.t.state, self.t.cfg)
        self.assertEqual([(t["id"], t["title"]) for t in tr.tracks()],
                         [("tr-0001", "Поиск"), ("tr-0002", "to-think: о чём подумать"), ("tr-0003", "wishes"),
                          ("tr-0004", "todo")])
        self.assertEqual([c["n"] for c in tr.get("tr-0002")["cars"]], ["2", "4", "3"])
        self.assertEqual([(c["n"], c["from"]) for c in tr.get("tr-0004")["cars"]],
                         [("1", ["td-0001"]), ("3", ["td-0003"]), ("4", ["td-0004"])])
        self.assertIn("перенесена сюда миграцией данных (схема 6)", tr.get("tr-0004")["notes"][0])
        # a wish: its title, priority, date, stage and its history of stages, its open question
        _, w = tr.car("tr-0003:1")
        self.assertEqual((w["title"], w["priority"], w["added"], w["stage"], w["status"]),
                         ("обложки видны сразу", 8, TS, "sketch", "waiting"))
        self.assertEqual([r["stage"] for r in w["stages"]], ["bogie", "sketch"])
        self.assertEqual(w["asks"][0]["q"], "Какие обложки?")
        self.assertTrue(w["notes"][0].endswith("из wl-0001 — хотелка от 2015-11-04: обложки видны сразу; слова "
                                               "владельца: «обложки должны быть заметнее»"))
        # a todo: its text, kind, version, place, source and links in the note, its own notes after it
        _, c = tr.car("tr-0004:1")
        self.assertEqual((c["title"], c["stage"], c["stages"][0]["why"]),
                         ("maxQueryMs не сбрасывается", "bogie", "из td-0001 (прежние очереди → треки)"))
        self.assertTrue(c["notes"][0].endswith("из td-0001 — todo от 2015-11-04: maxQueryMs не сбрасывается (вид: перф; "
                                               "версия: 2.4.0, main 3c9e1d7; где: lib/search.ts:1029; источник: агент) "
                                               "(ссылки: #12, dec-000001)"))
        self.assertEqual(c["notes"][1], "2015-11-05: виден на больших выдачах")
        self.assertIn("(вид: баг; в PR #195)", tr.car("tr-0004:3")[1]["notes"][0])
        _, r = tr.car("tr-0004:4")
        self.assertEqual((r["title"], r["priority"]), ("напомнить", 2))
        self.assertIn("напомнить: про повторный импорт (вид: напоминание; когда: после выкатки поиска)", r["notes"][0])
        _, q = tr.car("tr-0002:4")
        self.assertIn("подсказки по мере нужды; вопросы: 1) Показывать сразу? (поднял: Краски) (связано: X1)",
                      q["notes"][0])
        self.assertIn("лимиты (в раунде 2015-11-05-discuss)", tr.car("tr-0002:3")[1]["notes"][0])
        # the archive: every entry as it was, a moved one with its car; the history of before, and one row more
        arch = read_json(self.t.state.path("todo-v5.json"))
        self.assertEqual([(e["id"], e["status"], e.get("moved_to")) for e in arch["entries"]],
                         [("td-0001", "open", "tr-0004:1"), ("td-0002", "done", None), ("td-0003", "in_pr", "tr-0004:3"),
                          ("td-0004", "open", "tr-0004:4"), ("td-0005", "wont", None)])
        hist = read_jsonl(self.t.state.path("todo-history-v5.jsonl"))
        self.assertEqual([h["event"] for h in hist], ["add"] * 5 + ["migrate"])
        self.assertEqual(hist[-1]["moved"], {"td-0001": "tr-0004:1", "td-0003": "tr-0004:3", "td-0004": "tr-0004:4"})
        row = [h for h in tr.history() if h["event"] == "migrate"][-1]
        self.assertEqual((row["schema"], row["queues"]["think"]["track"], row["queues"]["wishes"]["moved"]),
                         (6, "tr-0002", {"wl-0001": "tr-0003:1"}))
        self.assertEqual([h["track"] for h in tr.history() if h["event"] == "add" and h.get("by")],
                         ["tr-0003", "tr-0004"])
        # an old ref reads the archive: where a car came from, and where an entry went
        out = self.team("car", "show", "tr-0004:1")[1]
        self.assertIn("Пришёл из: td-0001 — todo «maxQueryMs не сбрасывается» (перенесён в tr-0004:1).", out)
        self.assertIn("Пришёл из: wl-0002 — хотелка «пустая полка» (взята).", self.team("car", "show", "tr-0001:0")[1])
        j = json.loads(self.team("car", "show", "tr-0003:1", "--json")[1])
        self.assertEqual((j["kind"], j["stage"], j["from"][0]["status_ru"]), ("car", "sketch", "перенесён в tr-0003:1"))
        self.assertIn("1. Какие обложки?", self.team("car", "show", "tr-0003:1")[1])
        # the board shows them as the cars they are; a new leftover is the next number of its track
        self.assertIn("tr-0004:4", [i["ref"] for col in json.loads(self.team("board", "--json")[1])["columns"]
                                    for i in col["items"]])
        rc, out, err = self.team("tracks", "car", "add", "tr-0004", "--title", "ещё хвост", "--note", "где: lib/x.ts:9")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0004:5 added", out)
        self.assertRegex(tr.__class__(self.t.state, self.t.cfg).car("tr-0004:5")[1]["notes"][0],
                         r"^\d{4}-\d\d-\d\d: где: lib/x.ts:9$")
        # the commands of before are gone
        for cmd in ("wishes", "wish", "think", "todo"):
            with self.assertRaises(SystemExit):
                self.team(cmd)
        self.assertEqual(self.team("migrate")[1].strip(), "миграция данных депо: нечего переносить (схема 6)")

    def test_a_run_cut_short_moves_nothing_twice(self):
        keep = {f: slurp(self.t.state.path(f)) for f in ("wishes.json", "think.json", "todo.json",
                                                          "todo-history.jsonl")}
        self.assertEqual(self.team("tracks")[0], 0)
        cars = slurp(self.t.state.path("tracks.json"))
        for f, text in keep.items():  # the cars are written, the files of before are back: the run was cut short
            write(self.t.dir, f, text)
        at_schema(self.t.state, 5)
        rc, out, err = self.team("migrate")
        self.assertEqual(rc, 0, err)
        self.assertIn("  из них 3 уже вагоны (прерванный прогон): оставлены", out)
        self.assertEqual(read_json(self.t.state.path("tracks.json"))["tracks"], json.loads(cars)["tracks"])
        self.assertFalse(os.path.exists(self.t.state.path("todo.json")))
        self.assertEqual(read_json(self.t.state.path("todo-v5.json"))["entries"][0]["moved_to"], "tr-0004:1")

    def test_one_track_of_the_name_is_taken_and_the_others_are_named(self):
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("todo: хвосты")
        tr.add("TODO")
        at_schema(self.t.state, 5)
        rc, out, err = self.team("migrate", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("вагоны tr-0003 «todo: хвосты»", out)
        self.assertIn("  ещё треки с этим именем, оставлены как есть: tr-0004", out)

    def test_a_depot_without_them_has_nothing_to_move(self):
        for name in ("wishes", "think", "todo"):
            os.remove(self.t.state.path(name + ".json"))
        rc, out, err = self.team("migrate")
        self.assertEqual(rc, 0, err)
        self.assertIn("очередей хотелок, «подумать» и todo нет: нечего переносить", out)
        self.assertEqual(len(Tracks(self.t.state, self.t.cfg).tracks()), 2)
        self.assertEqual(migrate.version(self.t.state), 6)


class QueuesOnlyTest(QBase):
    def test_a_depot_that_kept_only_the_queues_migrates_too(self):
        """No trains.json (no track, no train ever written): the queue files alone make the step due."""
        old_queue(self.t.state, "todo", [entry("td-0001", "open", text="хвост", kind="bug")])
        self.assertFalse(os.path.exists(self.t.state.path("trains.json")))
        self.assertTrue(migrate.pending(self.t.state))
        rc, out, err = self.team("tracks")
        self.assertEqual(rc, 0, err)
        self.assertIn("todo: записей 1 — открытые 1 → вагоны нового трека «todo»", err)
        self.assertEqual(Tracks(self.t.state, self.t.cfg).car("tr-0001:1")[1]["from"], ["td-0001"])
        self.assertFalse(migrate.pending(self.t.state))
        self.assertTrue(os.path.exists(self.t.state.path("todo-v5.json")))


class QueuesToTracksGitTest(QBase):
    """The depot's git takes the archive and the removal of the files of before in one commit."""

    def test_the_removal_is_committed(self):
        t = TempState(git=True)
        self.addCleanup(t.close)
        old_queue(t.state, "todo", [entry("td-0001", "open", text="хвост", kind="bug")])
        at_schema(t.state, 5)
        t.state.git("add", "--", ".")
        t.state.git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "schema 5")
        lines, n = migrate.run(t.state, t.cfg)
        t.state.commit("team: migrate the depot to schema 6")
        self.assertEqual(n, 1)
        self.assertEqual(t.state.git("status", "--porcelain").stdout, "")
        files = t.state.git("ls-files").stdout.split()
        self.assertNotIn("todo.json", files)
        self.assertNotIn("todo-history.jsonl", files)
        self.assertIn("todo-v5.json", files)
        self.assertIn("todo-history-v5.jsonl", files)
        self.assertEqual(Tracks(t.state, t.cfg).car("tr-0001:1")[1]["title"], "хвост")
