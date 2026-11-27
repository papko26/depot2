import json
import os
import subprocess
import tempfile

from helpers import slurp
from test_extras import Base

from depot2.queues import QueueError, Think, Todo, Wishes, repo_version
from depot2.stack import Stack, StackError
from depot2.store import read_json, write_json
from depot2.tracks import Tracks
from depot2.trains import Trains
from depot2.tstack import TeamStack


def write(d, name, text):
    path = os.path.join(d, name)
    with open(path, "w", encoding="utf-8") as f:
        f.write(text)
    return path


class QBase(Base):
    def team(self, *argv):
        return super().team("--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent", *argv)


class WishesTest(QBase):
    def test_add_take_drop_and_the_table(self):
        w = Wishes(self.t.state)
        a = w.add("обложки видны сразу", words="обложки должны быть заметнее в выдаче", date="2015-11-05")
        b = w.add("автоимпорт", date="2015-11-04", priority="later")
        c = w.add(
            "пустая полка", words="пустые полки выглядят как ошибка", priority=8, date="2015-11-05", area="product"
        )
        with self.assertRaises(QueueError):
            w.add("x", area="kitchen")
        md = Wishes(self.t.state).md()
        self.assertIn("## Очередь хотелок", md)
        self.assertIn("| № | Хотелка | Дата | Слова владельца | Область | Приоритет | Статус |", md)
        self.assertLess(md.index(c["id"]), md.index(a["id"]))  # high first, later last
        self.assertLess(md.index(a["id"]), md.index(b["id"]))
        self.assertIn(
            "| 1 | wl-0003 пустая полка | 2015-11-05 | пустые полки выглядят как ошибка | продукт | P8 | ждёт |", md
        )
        Stack(self.t.state, self.t.cfg).add("Полка")
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("Поиск")
        tr.car_add("tr-0001", "полка", size="M")
        rc, out, err = self.team("wish", "take", "wl-0003", "--to", "tr-0001:0")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Wishes(self.t.state).get("wl-0003")["taken_to"], "tr-0001:0")
        self.assertEqual(self.team("wishes", "take", "wl-0001", "--to", "st-0404")[0], 1)
        self.assertEqual(self.team("wishes", "take", "wl-0001", "--to", "st-0001")[0], 0)
        self.assertEqual(self.team("wishes", "take", "wl-0001", "--to", "st-0001")[0], 1)  # taken already
        rc, out, _ = self.team("wishes", "drop", "wl-0002", "--reason", "не в v1")
        self.assertIn("wl-0002 → отклонена", out)
        self.assertIn("| — | (пусто) |", self.team("wishes")[1])
        rc, out, _ = self.team("wishes", "--all")
        self.assertIn("взята → tr-0001:0", out)
        self.assertIn("отклонена", out)
        rc, out, _ = self.team(
            "wishes", "add", "--title", "кнопка экспорта", "--words", "значок «CSV»", "--area", "product"
        )
        self.assertIn("wl-0004 added", out)
        rc, out, _ = self.team("wishes", "set", "wl-0004", "--prio", "high", "--note", "в вагон 4")
        self.assertEqual(Wishes(self.t.state).get("wl-0004")["priority"], 8)
        self.assertIn("в вагон 4", self.team("wishes", "show", "wl-0004")[1])
        self.assertEqual([h["event"] for h in Wishes(self.t.state).history()][-2:], ["add", "set"])


class ThinkTest(QBase):
    def test_add_round_decide_drop(self):
        rc, out, err = self.team(
            "think",
            "add",
            "--topic",
            "подсказки по мере нужды",
            "--question",
            "Показывать сразу?",
            "--by",
            "Краски",
            "--related",
            "X1",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn("| 1 | th-0001 подсказки по мере нужды |", out)
        self.assertIn("| 1: Показывать сразу? | Краски | X1 | P5 | ждёт |", out)
        self.assertEqual(self.team("think", "round", "th-0001", "--round", "2015-11-05-discuss")[0], 1)  # no such round
        os.makedirs(os.path.join(self.t.state.rounds_dir(), "2015-11-05-discuss"))
        with open(os.path.join(self.t.state.rounds_dir(), "2015-11-05-discuss", "round.json"), "w") as f:
            f.write("{}")
        rc, out, _ = self.team("think", "round", "th-0001", "--round", "2015-11-05-discuss")
        self.assertIn("в раунде 2015-11-05-discuss", out)
        TeamStack(self.t.state, self.t.cfg).add("discuss", title="тема")
        self.assertEqual(self.team("think", "round", "th-0001", "--round", "tm-0001")[0], 0)
        self.assertEqual(self.team("think", "decide", "th-0001", "--dec", "dec-000999")[0], 1)  # unknown
        self.team("decide", "all", "--text", "делаем по мере нужды")
        rc, out, err = self.team("think", "decide", "th-0001", "--dec", "dec-000001")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Think(self.t.state).get("th-0001")["decision"], "dec-000001")
        self.team("think", "add", "--topic", "обложки")
        self.assertEqual(self.team("think", "drop", "th-0002", "--reason", "в хотелки")[0], 0)
        self.assertIn("решено dec-000001", self.team("think", "--all")[1])


class TodoTest(QBase):
    def test_add_pr_done_wont(self):
        rc, out, err = self.team(
            "todo",
            "add",
            "--text",
            "maxQueryMs не сбрасывается",
            "--where",
            "lib/search.ts:1029",
            "--version",
            "2.4.0, main 3c9e1d7",
            "--source",
            "агент slow-search",
            "--kind",
            "perf",
            "--date",
            "2015-11-05",
        )
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "| 1 | td-0001 maxQueryMs не сбрасывается | 2015-11-05 | 2.4.0, main 3c9e1d7 | lib/search.ts:1029 | "
            "агент slow-search | перф | P5 | открыт |",
            out,
        )
        self.team(
            "todo",
            "add",
            "--text",
            "напомнить про повторный импорт",
            "--kind",
            "reminder",
            "--trigger",
            "после выкатки поиска",
            "--version",
            "2.4.0",
        )
        self.assertIn("(когда: после выкатки поиска)", self.team("todo")[1])
        with self.assertRaises(SystemExit):  # argparse refuses a kind it does not know
            self.team("todo", "add", "--text", "x", "--kind", "nope")
        rc, out, _ = self.team("todo", "pr", "td-0001", "--pr", "195")
        self.assertIn("в PR #195", out)
        self.team("todo", "add", "--text", "третий", "--version", "v")
        rc, out, _ = self.team("todo", "done", "td-0001", "td-0003", "--reason", "#195 слит")
        self.assertIn("td-0001 → сделан", out)
        self.assertIn("td-0003 → сделан", out)
        self.assertEqual(self.team("todo", "wont", "td-0002", "--reason", "устарело")[0], 0)
        self.assertEqual(Todo(self.t.state).entries(open_only=True), [])
        self.assertEqual(self.team("todo", "set", "td-0002", "--where", "lib/x.ts:1")[0], 0)
        self.assertEqual(Todo(self.t.state).get("td-0002")["where"], ["lib/x.ts:1"])

    def test_a_closed_entry_stays_closed_and_several_ids_all_or_none(self):
        for text in ("первый", "второй"):
            self.team("todo", "add", "--text", text, "--version", "v")
        self.assertEqual(self.team("todo", "done", "td-0001", "--reason", "слит")[0], 0)
        for argv in (
            ("todo", "pr", "td-0001", "--pr", "9"),
            ("todo", "wont", "td-0001", "--reason", "x"),
            ("todo", "done", "td-0001", "--reason", "ещё раз"),
        ):
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(
                "error: td-0001 is «сделан»: it is closed and stays so (only an open one — открыт, в PR — "
                "changes); the history keeps it, new work is a new entry: depot2 todo add …",
                err,
            )
        self.assertEqual(Todo(self.t.state).get("td-0001")["status"], "done")
        rows = len(Todo(self.t.state).history())
        rc, out, err = self.team("todo", "done", "td-0002", "td-0009", "--reason", "частично")
        self.assertEqual((rc, out), (1, ""))
        self.assertIn("no td-0009 in todo", err)
        self.assertEqual(
            (Todo(self.t.state).get("td-0002")["status"], len(Todo(self.t.state).history())), ("open", rows)
        )
        th = Think(self.t.state).add("вопрос")
        Think(self.t.state).decide(th["id"], reason="решили")
        self.assertEqual(self.team("think", "round", th["id"], "--round", "r1")[0], 1)
        self.assertEqual(Think(self.t.state).get(th["id"])["status"], "decided")
        w = Wishes(self.t.state)
        w.add("знак")
        w.to_status("wl-0001", "taken", taken_to="st-0001")
        self.assertEqual(self.team("wishes", "drop", "wl-0001", "--reason", "передумал")[0], 1)
        self.assertEqual(self.team("wishes", "done", "wl-0001", "--reason", "сбылась")[0], 0)  # a taken wish came true
        self.assertEqual(Wishes(self.t.state).get("wl-0001")["status"], "done")

    def test_the_version_comes_from_the_main_clone(self):
        d = tempfile.mkdtemp(prefix="team-clone-")
        write(d, "package.json", '{"name": "app", "version": "2.4.0"}\n')

        def git(*a):
            subprocess.run(["git", "-C", d, *list(a)], check=True, capture_output=True)

        git("init", "-q")
        git("add", "package.json")
        git("-c", "user.name=t", "-c", "user.email=t@localhost", "commit", "-q", "-m", "x")
        git("update-ref", "refs/remotes/origin/main", "HEAD")
        sha = subprocess.run(
            ["git", "-C", d, "rev-parse", "--short=7", "HEAD"], stdout=subprocess.PIPE, text=True
        ).stdout.strip()
        self.assertEqual(repo_version({"deep_repo": {"clone": d}}), f"2.4.0, main {sha}")
        self.assertIsNone(repo_version({"deep_repo": {"clone": ""}}))
        rc, _out, err = super(QBase, self).team("--set", "deep_repo.clone=" + d, "todo", "add", "--text", "x")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Todo(self.t.state).get("td-0001")["version"], f"2.4.0, main {sha}")


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
        self.assertEqual(
            ts.command(ts.get("tm-0001")),
            f"depot2 round --kind discuss --input {topic} --only keeper --item tm-0001 --dry-run",
        )
        # the spend from the round's own tokens: 10 + 200000 x 1.25 + 3e6 x 0.1 + 40000 x 5 = 0.75 MTok
        ts.record_round(
            "tm-0001",
            "2015-11-06-discuss-solo-keeper",
            {
                "total_usd": 0.4321,
                "tokens": {
                    "input": 10,
                    "cache_write": 200000,
                    "cache_read": 3000000,
                    "output": 40000,
                    "total": 3240010,
                },
            },
        )
        md = TeamStack(self.t.state, self.t.cfg).md()
        self.assertIn(
            "| № | Где | Что | Приоритет | Вид | Кто | Вход | Раунд | Статус | Effort (ожидаемо / потрачено) | "
            "Зависит от |",
            md,
        )
        self.assertIn(
            "| С1 | слот | tm-0001 обсуждение: topic.md | P5 | обсуждение | Копилка | topic.md | "
            "2015-11-06-discuss-solo-keeper | раунд прошёл: 2015-11-06-discuss-solo-keeper | "
            "≈ 0.3%UW (3 MTok) / 0.075%UW (0.75 MTok) | — |",
            md,
        )
        self.assertNotIn("$", md)
        self.assertIn("| **Ждёт слота (2/4)** |", md)
        ts.done("tm-0001", "отчёт показан")
        ts.start("tm-0004")

    def test_the_cli_and_a_dependency_on_the_agents_stack(self):
        self.team("stack", "add", "--title", "PR импорта")
        rc, out, err = self.team(
            "tstack", "add", "--kind", "review", "--depends", "st-0001", "--title", "обзор импорта"
        )
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
        self.assertIn(
            "warning: --cost is deprecated (the spend is in MTok now): read as --spent-mtok 0.2, "
            "0.02%UW (0.2 MTok) (0.5 / usage.usd_per_mtok 2.5)",
            err,
        )
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
        trains = Trains(self.t.state, self.t.cfg)
        trains.add("poisk", "Поиск", cars=["tr-0001:0"], slots="all-free")
        trains.set_state("poisk", "impl")
        self.team("wishes", "add", "--title", "обложки", "--prio", "high")
        self.team("think", "add", "--topic", "подсказки")
        self.team("todo", "add", "--text", "maxQueryMs", "--kind", "perf", "--version", "2.4.0")
        rc, out, err = self.team("queues", "--md")
        self.assertEqual(rc, 0, err)
        self.assertIn("| Очередь | Открыто | Приоритет | Сверху | Команда |", out)
        self.assertIn(
            "| Стек агентов | слоты 0/4, пост-стек 0/4, ждёт 1/10 | — | st-0001 Импорт 3 | `depot2 stack --md` |", out
        )
        self.assertIn("| Стек команды | слоты 0/2, ждёт 1/4 | — | tm-0001 лимиты | `depot2 tstack --md` |", out)
        self.assertIn(
            "| Поезда | 1 (имплементация 1) | — | можно сейчас: tr-0001:0; poisk (имплементация) | "
            "`depot2 trains --md` |",
            out,
        )
        self.assertIn("| Треки | 1 открыто из 1 | — | tr-0001 Поиск | `depot2 tracks --md` |", out)
        self.assertIn("| Бюллетени | 0 | — | — |", out)
        self.assertIn("| Мердж-очередь | 0 | — | — | `depot2 merge --md` |", out)  # right under the agents' stack
        self.assertIn("| Хотелки | 1 | P8 1 | wl-0001 обложки |", out)
        self.assertIn("| На подумать | 1 | P5 1 | th-0001 подсказки |", out)
        self.assertIn("| Todo | 1 (перф 1) | P5 1 | td-0001 maxQueryMs |", out)
        self.assertEqual(self.team("queues")[1], out)
        self.assertEqual(
            [r["queue"] for r in json.loads(self.team("queues", "--json")[1])],
            [
                "Стек агентов",
                "Мердж-очередь",
                "Стек команды",
                "Поезда",
                "Треки",
                "Бюллетени",
                "Хотелки",
                "На подумать",
                "Todo",
                "Принципы",
            ],
        )


class PrioTest(QBase):
    def test_todo_and_think_get_a_priority_sorted_the_most_important_first(self):
        self.team("todo", "add", "--text", "первый", "--version", "v")
        self.team("todo", "add", "--text", "второй", "--version", "v", "--prio", "later")
        rc, out, err = self.team(
            "todo", "add", "--text", "третий", "--version", "v", "--prio", "high", "--kind", "text"
        )
        self.assertEqual(rc, 0, err)
        self.assertEqual([e["priority"] for e in Todo(self.t.state).entries()], [5, 2, 8])
        self.assertIn("| № | Что | Дата | Версия | Где | Источник | Вид | Приоритет | Статус |", out)
        self.assertRegex(out, r"\| 1 \| td-0003 третий \|.*\| текст \| P8 \| открыт \|")
        self.assertRegex(out, r"\| 2 \| td-0001 первый \|.*\| P5 \| открыт \|")
        self.assertRegex(out, r"\| 3 \| td-0002 второй \|.*\| P2 \| открыт \|")
        out = self.team("todo", "set", "td-0002", "--priority", "8")[1].split("## Очередь todo")[1]
        self.assertLess(out.index("td-0002"), out.index("td-0003"))  # both 8: the queue's own order
        self.assertLess(out.index("td-0003"), out.index("td-0001"))
        self.team("todo", "done", "td-0003", "--reason", "сделан")
        rc, out, _ = self.team("todo", "--all")  # a closed one sorts after the open ones
        self.assertLess(out.index("td-0001"), out.index("td-0003"))
        with self.assertRaises(SystemExit):
            self.team("todo", "set", "td-0001", "--prio", "urgent")
        self.assertEqual(self.team("todo", "set", "td-0001", "--title", "x")[0], 0)
        # to-think the same
        self.team("think", "add", "--topic", "обложки")
        rc, out, err = self.team("think", "add", "--topic", "подсказки", "--prio", "high")
        self.assertEqual(rc, 0, err)
        self.assertIn("| № | Тема | Дата | Вопросы | Поднял | Связано | Приоритет | Статус |", out)
        self.assertIn(
            "| 1 | th-0002 подсказки | {} | — | — | — | P8 | ждёт |".format(Think(self.t.state).get("th-0002")["date"]),
            out,
        )
        self.assertEqual(self.team("think", "set", "th-0001", "--prio", "later")[0], 0)
        self.assertEqual(Think(self.t.state).get("th-0001")["priority"], 2)
        rc, out, _ = self.team("queues", "--md")
        self.assertIn("| На подумать | 2 | P8 1 · P2 1 | th-0002 подсказки; th-0001 обложки |", out)
        self.assertIn("| Todo | 2 (баг 2) | P8 1 · P5 1 | td-0002 второй; td-0001 x |", out)
        self.assertEqual(
            [r["priority"] for r in json.loads(self.team("queues", "--json")[1])][-3:],
            [{"8": 1, "2": 1}, {"8": 1, "5": 1}, None],
        )  # the principles have none


class WishDoneTest(QBase):
    def test_a_wish_is_done_not_dropped(self):
        w = Wishes(self.t.state)
        w.add("автоимпорт", date="2015-11-04")
        w.add("обложки", date="2015-11-05")
        with self.assertRaises(SystemExit):  # --reason is required
            self.team("wishes", "done", "wl-0001")
        rc, out, err = self.team("wishes", "done", "wl-0001", "--reason", "#205 слит")
        self.assertEqual(rc, 0, err)
        self.assertIn("wl-0001 → сделано", out)
        e = Wishes(self.t.state).get("wl-0001")
        self.assertEqual(e["status"], "done")
        self.assertTrue(e["notes"][-1].endswith("#205 слит"))
        self.assertNotIn("wl-0001", self.team("wishes")[1])  # off the open table
        self.assertIn("| wl-0001 автоимпорт |", self.team("wishes", "--all")[1].replace("| 2 ", "| "))
        self.assertIn("| сделано |", self.team("wishes", "--all")[1])
        self.assertEqual(self.team("wishes", "done", "wl-0001", "--reason", "ещё раз")[0], 1)
        self.team("wishes", "drop", "wl-0002", "--reason", "не в v1")
        self.assertEqual(Wishes(self.t.state).get("wl-0002")["status"], "declined")  # drop stays its own
        self.assertEqual(self.team("queues", "--json")[0], 0)


class MigrateTest(QBase):
    def setUp(self):
        super().setUp()
        td = Todo(self.t.state)
        for text in ("один", "два", "три", "четыре", "пять"):
            td.add(text, version="v")
        th = Think(self.t.state)
        th.add("подсказки")
        th.add("обложки")
        self.team("todo", "set", "td-0001", "--note", "prio: high — текст на проде")
        self.team("todo", "set", "td-0002", "--note", "prio: later — по заголовку")
        self.team("todo", "set", "td-0002", "--note", "prio: Normal — передумали")  # the newest wins
        self.team("todo", "set", "td-0003", "--note", "см. td-0001; prio: high тут не в начале")
        self.team("todo", "set", "td-0005", "--note", "prio: later — закрыт")
        self.team("todo", "done", "td-0005", "--reason", "сделан")
        self.team("think", "set", "th-0002", "--note", "prio: later — к релизу")
        self.team("todo", "set", "td-0004", "--note", "prio: later — старая заметка")  # … the field wins
        self.team("wishes", "add", "--title", "обложки")
        self.team("wishes", "set", "wl-0001", "--note", "prio: normal — по вычитке")
        self.team("stack", "add", "--title", "старый объект")
        stk = Stack(self.t.state, self.t.cfg)
        del stk.get("st-0001")["autostart"]
        stk._save("test", "st-0001")
        # the depot as schema 3 left it: the words, or no field at all (the todo and to-think entries from before it)
        for name, words in (("todo", {"td-0004": "high"}), ("think", {}), ("wishes", {"wl-0001": "high"})):
            d = read_json(self.t.state.path(name + ".json"))
            for e in d["entries"]:
                e.pop("priority")
                if e["id"] in words:
                    e["prio"] = words[e["id"]]
            write_json(self.t.state.path(name + ".json"), d)
        for f in ("stack.json",):
            d = read_json(self.t.state.path(f))
            for o in d["objects"]:
                o.pop("priority")
            write_json(self.t.state.path(f), d)
        write_json(self.t.state.path("trains.json"), {"schema": 3, "trains": []})

    def files(self):
        return {
            n: slurp(os.path.join(self.t.dir, n))
            for n in ("todo.json", "todo-history.jsonl", "think.json", "stack.json", "stack-history.jsonl")
        }

    def test_dry_run_then_once_and_idempotent(self):
        before = self.files()
        rc, out, err = self.team("migrate", "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn(
            "todo: prio → priority у 5 (high 8, normal 5, later 2; без слова — 5): P8 2 · P5 2 · P2 1; "
            "из заметки «prio: …»: td-0001, td-0002, td-0005",
            out,
        )
        self.assertIn(
            "«подумать»: prio → priority у 2 (high 8, normal 5, later 2; без слова — 5): P5 1 · P2 1; "
            "из заметки «prio: …»: th-0002",
            out,
        )
        self.assertIn("хотелки: prio → priority у 1 (high 8, normal 5, later 2; без слова — 5): P8 1", out)
        self.assertEqual(self.files(), before)  # wrote nothing
        rc, out, err = self.team("queues", "migrate", "--dry-run")  # any command migrates the data first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 3 → 4", err)
        self.assertIn("DRY RUN: перенёс бы 1 записей и объектов", out)
        self.assertIn("- stack: autostart = нет записан явно у 1 объектов: st-0001", out)
        rc, out, err = self.team("queues", "migrate")
        self.assertEqual(rc, 0, err)
        self.assertIn("перенесено: 1 записей и объектов", out)
        td = Todo(self.t.state)
        self.assertEqual(
            {e["id"]: e["priority"] for e in td.entries()},
            {"td-0001": 8, "td-0002": 5, "td-0003": 5, "td-0004": 8, "td-0005": 2},
        )
        self.assertEqual(
            [n[12:] for n in td.get("td-0002")["notes"]], ["prio: later — по заголовку", "prio: Normal — передумали"]
        )  # the notes stay
        self.assertEqual(Think(self.t.state).get("th-0002")["priority"], 2)
        self.assertEqual(Wishes(self.t.state).get("wl-0001")["priority"], 8)
        self.assertIs(Stack(self.t.state, self.t.cfg).get("st-0001")["autostart"], False)
        h = [h for h in td.history() if h["event"] == "migrate_priority"]
        self.assertEqual(
            [(x["prio"], x["from_notes"]) for x in h],
            [
                (
                    {"td-0001": "high", "td-0002": "normal", "td-0003": None, "td-0004": "high", "td-0005": "later"},
                    ["td-0001", "td-0002", "td-0005"],
                )
            ],
        )
        after = self.files()
        rc, out, _ = self.team("queues", "migrate")
        self.assertIn("перенесено: 0 записей и объектов", out)
        self.assertIn("нечего переносить: миграция уже прошла", out)
        self.assertEqual(self.files(), after)
