"""No network: gh is tests/fake_gh.py ($FAKE_GH_STATE)."""

import json
import os
import shutil
import tempfile
import unittest

from helpers import slurp
from test_board import GateBase

from depot2 import marks, migrate
from depot2.stack import Stack
from depot2.store import read_json, read_jsonl, write_json
from depot2.tracks import SCHEMA, Tracks

LABELS = {"5", "26", "1", "20", "11", "19", "4", "5a", "23"}

BODY = """## Что сделано
- 5: ок — это не раздел проверки

## Проверка по принципам

Строки по каждому принципу:

### Приоритет 1 — ворота
- 5: ок — как в библиотеке
- **26**: не касается
- `1: ok, связи`
- Принцип 20: нарушен — правка без журнала; взамен запись в журнал
- 20: ок — повтор, считается худшая

### Приоритет 2 — балл вагона
- 11: ОК
- 19 — n/a
- 4: может быть
- 5а: bad — нет источника
- 99: ок
* 23. na
```
- 4: нарушен
```
**Приоритет 2, ещё**
- 4: нарушена — удалено молча

## Тесты
- 11: нарушен — уже не раздел
"""


class ParseTest(unittest.TestCase):
    def test_every_kind_of_line(self):
        p = marks.parse(BODY, LABELS)
        self.assertTrue(p["found"])
        got = {k: (v["mark"], v["text"]) for k, v in p["marks"].items()}
        self.assertEqual(got, {"5": ("ok", "как в библиотеке"), "26": ("na", None), "1": ("ok", "связи"),
                               "20": ("bad", "правка без журнала; взамен запись в журнал"), "11": ("ok", None),
                               "19": ("na", None), "5a": ("bad", "нет источника"), "23": ("na", None),
                               "4": ("bad", "удалено молча")})
        self.assertEqual(p["marks"]["5"]["line"], 9)  # the line in the body (the one before the section is not read)
        self.assertEqual(p["junk"], [[18, "- 4: может быть"]])
        self.assertEqual(p["repeats"], ["20"])
        self.assertEqual(p["unknown"], ["99"])

    def test_the_words(self):
        for word, mark in (("ок", "ok"), ("ОК", "ok"), ("ok", "ok"), ("OK.", "ok"), ("не касается", "na"),
                           ("Не  касается.", "na"), ("n/a", "na"), ("na", "na"), ("нарушен", "bad"), ("нарушено", "bad"),
                           ("bad", "bad"), ("около", None), ("может", None), ("", None)):
            self.assertEqual(marks.mark_of(word), mark, word)
        for line, want in (("- 20: ок", ("ok", None)), ("- 20: ок — так", ("ok", "так")), ("- 20 — ок: так", ("ok", "так")),
                           ("- 20. не касается", ("na", None)), ("- 20) n/a, никак", ("na", "никак")),
                           ("+ 20: нарушен — что; взамен то", ("bad", "что; взамен то")), ("- 20 bad", ("bad", None))):
            p = marks.parse("## Проверка по принципам\n" + line, {"20"})
            self.assertEqual((p["marks"]["20"]["mark"], p["marks"]["20"]["text"]), want, line)
        for line in ("- 20: около дела", "- 20: okay", "- 20:", "- 20 может"):  # a number, no mark word: junk
            self.assertEqual(marks.parse("## Проверка по принципам\n" + line, {"20"})["junk"], [[2, line]], line)
        for line in ("- двадцать: ок", "20: ок", "- Приоритет 1 — ворота", "текст 20: ок"):  # not a mark line at all
            p = marks.parse("## Проверка по принципам\n" + line, {"20"})
            self.assertEqual((p["marks"], p["junk"]), ({}, []), line)

    def test_a_number_twice_the_worse_counts(self):
        for lines, want in ((["- 4: ок", "- 4: ок — снова"], ("ok", None)), (["- 4: ок", "- 4: не касается"], ("na", None)),
                            (["- 4: нарушен — x", "- 4: ок"], ("bad", "x")), (["- 4: n/a", "- 4: bad"], ("bad", None))):
            p = marks.parse("## Проверка по принципам\n" + "\n".join(lines), {"4"})
            self.assertEqual((p["marks"]["4"]["mark"], p["marks"]["4"]["text"]), want, lines)
            self.assertEqual(p["repeats"], ["4"])

    def test_the_sections_bounds(self):
        self.assertEqual(marks.parse("## Сверка\n- 4: ок\n", {"4"}), {"found": False, "marks": {}, "junk": [],
                                                                       "repeats": [], "unknown": []})
        self.assertEqual(marks.parse(None)["found"], False)
        # a bold label starts it; the next bold label that names no priority, or any heading, ends it
        p = marks.parse("**Проверка по принципам**\n- 4: ок\n**Приоритет 2**\n- 5: ок\n**Решения сборщика**\n- 1: ок\n",
                        {"1", "4", "5"})
        self.assertEqual(sorted(p["marks"]), ["4", "5"])
        p = marks.parse("**Проверка по принципам:**\n- 4: ок\n### Итог\n- 5: ок\n", {"4", "5"})
        self.assertEqual(sorted(p["marks"]), ["4"])
        # a heading of the same level that names a priority is inside; one of a higher level ends it
        p = marks.parse("### Проверка по принципам\n## Приоритет 1 — ворота\n- 4: ок\n## Тесты\n- 5: ок\n", {"4", "5"})
        self.assertEqual(sorted(p["marks"]), ["4"])
        p = marks.parse("## Проверка по принципам\n#### Детали\n- 4: ок\n# Конец\n- 5: ок\n", {"4", "5"})
        self.assertEqual(sorted(p["marks"]), ["4"])
        # without labels every number is read; Windows line ends too
        p = marks.parse("## Проверка по принципам\r\n- 77: ок\r\n")
        self.assertEqual((sorted(p["marks"]), p["unknown"]), (["77"], []))


PL = [{"id": "pc-0005", "label": "5", "title": "Как в библиотеке.", "section": "основа", "priority": 1},
      {"id": "pc-0026", "label": "26", "title": "Запись.", "section": "основа", "priority": 1},
      {"id": "pc-0011", "label": "11", "title": "Две минуты.", "section": "механики", "priority": 2},
      {"id": "pc-0004", "label": "4", "title": "Молча.", "section": "механики", "priority": 2},
      {"id": "pc-0005a", "label": "5a", "title": "Источник.", "section": "механики", "priority": 2}]


def m(mark, by="PR #301", text=None):
    return {"mark": mark, "text": text, "by": by, "at": "2015-11-19T12:00:00+02:00"}


class VerdictTest(unittest.TestCase):
    def test_the_gates_and_the_score(self):
        v = marks.evaluate({"5": m("ok"), "26": m("na"), "11": m("ok"), "4": m("bad"), "5a": m("ok")}, PL)
        self.assertTrue(v["passed"] and v["gate"]["passed"] and v["read"] and v["listed"])
        self.assertEqual((v["score"], v["max"], v["counts"], v["unchecked"]),
                         (1, 3, {"ok": 2, "na": 0, "bad": 1, "none": 0}, []))
        self.assertEqual(v["unmet"], [])
        self.assertEqual(v["text"], "ворота пройдены · балл +1 из 3 (ок 2 · не касается 0 · нарушен 1)")
        self.assertEqual(v["cell"], "ворота пройдены, балл +1 из 3")
        v = marks.evaluate({"5": m("bad"), "11": m("na")}, PL)
        self.assertFalse(v["passed"])
        self.assertEqual((v["gate"]["missing"], v["gate"]["bad"]), (["26"], ["5"]))
        self.assertEqual(v["unmet"], ["нет строки — 26; нарушен — 5"])
        self.assertEqual((v["score"], v["unchecked"]), (0, ["4", "5a"]))
        self.assertEqual(v["text"], "ворота не пройдены: нет строки — 26; нарушен — 5 · балл 0 из 3 (ок 0 · не касается "
                                    "1 · нарушен 0); не проверены: 4, 5a")
        self.assertEqual(v["cell"], "ворота не пройдены: нет строки — 26; нарушен — 5, балл 0 из 3, не проверено 2")
        v = marks.evaluate({"5": m("ok"), "26": m("ok"), "11": m("bad"), "4": m("bad"), "5a": m("bad")}, PL)
        self.assertEqual((v["score"], marks.score_ru(v, full=False)), (-3, "балл −3 из 3"))
        self.assertEqual([marks.signed(n) for n in (2, 0, -1)], ["+2", "0", "−1"])

    def test_nothing_read_nothing_listed(self):
        v = marks.evaluate({}, PL)
        self.assertEqual((v["read"], v["passed"], v["gate"]["missing"], v["cell"]), (False, False, ["5", "26"],
                                                                                    "не проверен"))
        self.assertIn("не проверен: строк по принципам нет", v["text"])
        v = marks.evaluate({"5": m("ok")}, [])  # the third check empty: nothing to check, the gate passes
        self.assertEqual((v["listed"], v["passed"], v["unmet"], v["max"]), (False, True, [], 0))
        self.assertIsNone(marks.compact(v))
        self.assertEqual(v["cell"], "не нужны (третья проверка пуста)")
        # a mark of a principle no longer in the check is not counted
        v = marks.evaluate({"99": m("bad"), "5": m("ok"), "26": m("ok")}, PL)
        self.assertEqual((v["passed"], v["counts"]["bad"]), (True, 0))

    def test_the_english_section_and_marks(self):
        p = marks.parse("## Principles check\n### Priority 1 — the gates\n- 5: ok — as in the library\n- 26: n/a\n"
                        "- Principle 4: violated — deleted silently; instead a log\n## Tests\n- 11: bad\n", LABELS)
        self.assertEqual({k: (v["mark"], v["text"]) for k, v in p["marks"].items()},
                         {"5": ("ok", "as in the library"), "26": ("na", None),
                          "4": ("bad", "deleted silently; instead a log")})
        self.assertEqual([marks.mark_of(w) for w in ("violated", "Violated.", "n/a", "нарушена")], ["bad", "bad", "na", "bad"])
        self.assertTrue(marks.is_hand("вручную") and marks.is_hand("by hand") and not marks.is_hand("PR #3"))

    def test_a_read_and_a_hand_mark(self):
        old = {"5": m("bad", by=marks.hand(), text="руками"), "26": m("ok", by="PR #300"), "4": m("ok", by="PR #300")}
        parsed = marks.parse("## Проверка по принципам\n- 5: ок\n- 26: нарушен — x\n- 11: ок\n", {"5", "26", "11", "4"})
        new = marks.from_pr(old, parsed, 301, at="T")
        self.assertEqual(new["5"], old["5"])  # the hand mark wins over the PR's line
        self.assertEqual(new["26"], {"mark": "bad", "text": "x", "by": "PR #301", "at": "T"})
        self.assertEqual(new["11"]["by"], "PR #301")
        self.assertNotIn("4", new)  # the earlier read's mark: this read has no line for it
        self.assertEqual(marks.by_hand(new, "4", "na", "  не  тот\nслучай ", at="T")["4"],
                         {"mark": "na", "text": "не тот случай", "by": marks.hand(), "at": "T"})
        self.assertEqual((marks.norm_label("05", "А"), marks.norm_label(20)), ("5a", "20"))


PFILE = """# Принципы (проба)

## Основа
5. **Модель как в библиотеке.** Тело.
26. **Запись не теряется.** Тело.
## Механики
11. **Первые две минуты.** Тело.
4. **Ничего не удаляется молча.** Тело.
5a. **Источник у каждой графы.** Тело.
## Как мы работаем
6. **Маленькие шаги.** Тело.
"""

GOOD = """## Проверка по принципам
### Приоритет 1 — ворота
- 5: ок — как в библиотеке
- 26: не касается
### Приоритет 2 — балл вагона
- 11: ок
- 4: нарушен — удалено молча; взамен корзина
"""
MISSING = "## Проверка по принципам\n- 5: ок\n- 11: ок\n"
BROKEN = "## Проверка по принципам\n- 5: нарушен — экземпляр без издания; взамен связь\n- 26: ок\n- 4: может быть\n"


class DepotBase(GateBase):
    """Every gate item on (test_board.GateBase), a depot with five principles of the third check (5, 26 of priority 1;
    11, 4, 5a of priority 2) and 6 outside it, and gh's PRs in $FAKE_GH_STATE."""

    def setUp(self):
        super(DepotBase, self).setUp()
        self.files = tempfile.mkdtemp(prefix="team-marks-")
        path = os.path.join(self.files, "PRINCIPLES.md")
        with open(path, "w", encoding="utf-8") as f:
            f.write(PFILE)
        self.ok("principles", "import", path)
        for pc in ("pc-0005", "pc-0026", "pc-0011", "pc-0004", "pc-0005a"):
            self.ok("principles", "set", pc, "--check", "yes")
        self.gh = os.path.join(self.files, "gh.json")
        self.prs({})
        self.env0 = os.environ.get("FAKE_GH_STATE")
        os.environ["FAKE_GH_STATE"] = self.gh

    def tearDown(self):
        if self.env0 is None:
            os.environ.pop("FAKE_GH_STATE", None)
        else:
            os.environ["FAKE_GH_STATE"] = self.env0
        for d, _, names in os.walk(self.files):
            for n in names:
                os.chmod(os.path.join(d, n), 0o644)
        shutil.rmtree(self.files, ignore_errors=True)
        super(DepotBase, self).tearDown()

    def prs(self, bodies, branch="feat"):
        write_json(self.gh, {"prs": {str(n): {"number": n, "title": "PR %d" % n, "state": "OPEN", "headRefName": branch,
                                              "baseRefName": "main", "isDraft": False, "body": b}
                                     for n, b in bodies.items()}})

    def team(self, *argv):
        return super(DepotBase, self).team("--set", "merge.worktrees=" + self.files, *argv)

    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, "%s: %s" % (" ".join(argv), err))
        return out

    def handed(self, oid):
        return ["--ci", "green", "--heavy", "n/a", "--scan", "clean", "--shots", "n/a"]

    def car_in_slot(self):
        self.ok("tracks", "add", "--title", "Поиск")
        self.ok("tracks", "car", "add", "tr-0001", "--title", "поиск", "--brief", self.brief)
        self.ok("tracks", "car", "stack", "tr-0001:0", "--start")
        self.ok("stack", "set", "st-0001", "--pr", "301", "--branch", "feat")

    def car(self, ref="tr-0001:0"):
        return Tracks(self.t.state, self.t.cfg).car(ref)[1]


class PostTest(DepotBase):
    def test_stack_post_reads_the_pr_and_the_gates_refuse_with_the_list(self):
        self.car_in_slot()
        self.prs({301: MISSING})
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001"))
        self.assertEqual(rc, 1)
        self.assertIn("ворота: st-0001 → «приёмка» не пройдены: DoD (вход в приёмку) — принципы, ворота: нет строки — "
                      "26. Пройти всё равно — только с причиной: depot2 stack post st-0001 --force --why «…»", err)
        self.assertIn("принципы: PR #301, раздел «Проверка по принципам» — строк 2 (ок 2 · не касается 0 · нарушен 0)", out)
        self.assertNotIn("marks", self.car())  # a refused post writes nothing, the marks neither
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["where"], "slot")
        self.prs({301: BROKEN})
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001"))
        self.assertEqual(rc, 1)
        self.assertIn("принципы, ворота: нарушен — 5.", err)
        self.assertIn("не разобрана строка 4: «- 4: может быть»", out)
        self.prs({301: GOOD})
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001"))
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001: ворота пройдены · балл 0 из 3 (ок 1 · не касается 0 · нарушен 1); не проверены: 5a", out)
        c = self.car()
        self.assertEqual({k: (v["mark"], v["by"]) for k, v in c["marks"].items()},
                         {"5": ("ok", "PR #301"), "26": ("na", "PR #301"), "11": ("ok", "PR #301"),
                          "4": ("bad", "PR #301")})
        self.assertEqual(c["marks"]["4"]["text"], "удалено молча; взамен корзина")
        self.assertNotIn("marks", Stack(self.t.state, self.t.cfg).get("st-0001"))  # a car's marks are its own
        row = [h for h in Tracks(self.t.state, self.t.cfg).history() if h["event"] == "marks"][-1]
        self.assertEqual((row["car"], row["by"], row["pr"], row["score"], row["passed"]), ("0", "stack post", 301, 0, True))
        self.assertEqual(self.car()["stage"], "acceptance")

    def test_force_why_passes_and_is_recorded_and_an_object_with_no_car_keeps_its_own(self):
        self.ok("stack", "add", "--title", "без вагона", "--brief", self.brief, "--pr", "302", "--branch", "feat")
        self.ok("stack", "start", "st-0001")
        self.prs({302: BROKEN})
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001") + ["--force"])
        self.assertIn("--force — только с причиной", err)
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001") + ["--force", "--why", "владелец: ок"])
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001: ворота пройдены с --force (владелец: ок): DoD (вход в приёмку) — принципы, ворота: "
                      "нарушен — 5", out)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual(o["stages"][-1]["gates"], {"DoD": ["принципы, ворота: нарушен — 5"]})
        self.assertTrue(o["stages"][-1]["forced"])
        self.assertEqual(sorted(o["marks"]), ["26", "5"])  # no car: the object holds them

    def test_a_pr_that_cannot_be_read_leaves_the_marks(self):
        self.car_in_slot()
        self.prs({})  # gh does not know PR 301
        self.ok("tracks", "car", "check", "tr-0001:0", "5", "ок")
        self.ok("tracks", "car", "check", "tr-0001:0", "26", "не", "касается")  # two words as two args
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001"))
        self.assertEqual(rc, 0, err)
        self.assertIn("принципы: PR #301 не прочитан: gh pr view: exit 1: no pull requests found for 301; отметки — как "
                      "были", out)
        self.assertEqual({k: v["by"] for k, v in self.car()["marks"].items()}, {"5": "вручную", "26": "вручную"})

    def test_no_principles_no_read(self):
        """The third check empty (or the DoD without `principles`): nothing to read, no gh call, no marks."""
        for pc in ("pc-0005", "pc-0026", "pc-0011", "pc-0004", "pc-0005a"):
            self.ok("principles", "set", pc, "--check", "no")
        self.car_in_slot()
        os.environ["FAKE_GH_STATE"] = os.path.join(self.files, "none.json")  # a gh call would fail: no such file
        rc, out, err = self.team("stack", "post", "st-0001", *self.handed("st-0001"))
        self.assertEqual(rc, 0, err)
        self.assertNotIn("marks", self.car())
        self.assertNotIn("принципы", out.split("\n\n")[0])


class MergeAddTest(DepotBase):
    def test_merge_add_reads_the_pr_and_gates_it(self):
        self.car_in_slot()
        self.prs({301: GOOD})
        self.ok("stack", "post", "st-0001", *self.handed("st-0001"))
        self.prs({301: MISSING})  # the PR's section lost a line after the post
        rc, out, err = self.team("merge", "add", "301", "--word", "301 — мердж")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: PR #301 → «на путях» не пройдены: DoD (вход в приёмку) — принципы, ворота: нет строки — "
                      "26. Пройти всё равно — только с причиной: depot2 merge add 301 --word «…» --force --why «…»", err)
        self.assertIn("\nпринципы: PR #301, раздел «Проверка по принципам» — строк 2", err)  # the PR's lines under it
        self.assertEqual(read_json(self.t.state.path("merge-queue.json")), None)  # nothing queued
        self.assertEqual(self.car()["marks"]["26"]["mark"], "na")  # nor the marks written
        # a hand mark wins over the PR: the gate passes
        out = self.ok("tracks", "car", "check", "st-0001", "26", "n/a", "--line", "записей тут нет")
        self.assertIn("tr-0001:0: принцип 26 — не касается вручную: записей тут нет", out)
        out = self.ok("merge", "add", "301", "--word", "301 — мердж")
        self.assertIn("tr-0001:0: ворота пройдены · балл +1 из 3", out)
        e = read_json(self.t.state.path("merge-queue.json"))["entries"][-1]
        self.assertEqual((e["principles"]["passed"], e["principles"]["score"], e["principles"]["unchecked"]),
                         (True, 1, ["4", "5a"]))
        self.assertNotIn("forced", e)
        self.assertNotIn("4", self.car()["marks"])  # this read has no line for 4: the earlier read's mark goes

    def test_merge_add_forced_is_recorded(self):
        self.car_in_slot()
        self.prs({301: BROKEN})
        rc, out, err = self.team("merge", "add", "301", "--word", "301 — мердж", "--force", "--why", "владелец: едет")
        self.assertEqual(rc, 0, err)
        self.assertIn("ворота пройдены с --force (владелец: едет): DoD (вход в приёмку) — принципы, ворота: нарушен — 5",
                      out)
        e = read_json(self.t.state.path("merge-queue.json"))["entries"][-1]
        self.assertEqual(e["forced"], {"why": "владелец: едет", "gates": {"DoD": ["принципы, ворота: нарушен — 5"]}})
        h = read_jsonl(self.t.state.path("merge-history.jsonl"))[-1]
        self.assertEqual((h["event"], h["forced"], h["why"]), ("add", True, "владелец: едет"))
        row = self.car()["stages"][-1]
        self.assertEqual((row["stage"], row["forced"]), ("queued", True))
        self.assertIn("--force: владелец: едет", row["why"])
        self.assertEqual(self.car()["marks"]["5"]["mark"], "bad")


class CheckCommandTest(DepotBase):
    def test_check_shows_reads_and_sets_by_hand(self):
        self.car_in_slot()
        self.ok("wishes", "add", "--title", "хотелка", "--words", "хочу")
        out = self.ok("tracks", "car", "check", "tr-0001:0")
        self.assertIn("## Оценка tr-0001:0 по принципам", out)
        self.assertIn("Ворота (приоритет 1): не пройдены: нет строки — 5, 26.", out)
        self.assertIn("Балл (приоритет 2): 0 из 3 (ок 0 · не касается 0 · нарушен 0); не проверены: 11, 4, 5a.", out)
        self.assertIn("| 5. Модель как в библиотеке. | 1 — ворота | не проверен | — | — |", out)
        self.assertNotIn("Маленькие шаги", out)  # 6 is no check principle
        self.prs({301: GOOD})
        out = self.ok("tracks", "car", "check", "st-0001", "--from-pr")  # the object names its car
        self.assertIn("| 4. Ничего не удаляется молча. | 2 — балл | нарушен | удалено молча; взамен корзина "
                      "| PR #301 |", out)
        self.assertIn("Откуда: PR #301.", out)
        out = self.ok("tracks", "car", "check", "tr-0001:0", "5a", "ок", "--line", "источник есть")
        self.assertIn("Балл (приоритет 2): +1 из 3 (ок 2 · не касается 0 · нарушен 1).", out)
        self.assertIn("Откуда: PR #301; вручную: 5a.", out)
        self.assertEqual(self.car()["marks"]["5a"], dict(self.car()["marks"]["5a"], mark="ok", text="источник есть",
                                                         by="вручную"))
        for argv, why in ((["tr-0001:0", "6", "ок"], "принципа 6 нет в третьей проверке; есть: 5, 26, 11, 4, 5a"),
                          (["tr-0001:0", "5", "может"], "отметка — ок, не касается или нарушен"),
                          (["tr-0001:0", "5"], "отметка — ок, не касается или нарушен"),
                          (["tr-0001:0", "5", "ок", "--from-pr"], "либо --from-pr"),
                          (["tr-0001:0", "--line", "x"], "--line и --pr — только с отметкой"),
                          (["wl-0001"], "оценку по принципам ставят вагону трека или живому объекту стека"),
                          (["tr-0001:9"], "tr-0001 has no car 9")):
            rc, _, err = self.team("tracks", "car", "check", *argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(why, err, argv)
        self.ok("tracks", "car", "add", "tr-0001", "--title", "без PR")
        rc, _, err = self.team("tracks", "car", "check", "tr-0001:1", "--from-pr")
        self.assertIn("tr-0001:1 has no PR", err)


class ViewsTest(DepotBase):
    def test_every_view_shows_the_marks(self):
        self.car_in_slot()
        self.ok("trains", "add", "--name", "search", "--title", "Поиск", "--car", "tr-0001:0")
        self.prs({301: GOOD})
        self.ok("tracks", "car", "check", "tr-0001:0", "--from-pr")
        cell = "ворота пройдены, балл 0 из 3, не проверено 1"
        show = self.ok("car", "show", "st-0001")
        self.assertIn("Сдано агентом: CI не сдано · Heavy не сдано · скан не сдано · шоты не сдано · принципы: %s." % cell,
                      show)
        self.assertIn("Оценка по принципам: ворота пройдены · балл 0 из 3 (ок 1 · не касается 0 · нарушен 1); не "
                      "проверены: 5a.", show)
        self.assertIn("- 4 (балл) Ничего не удаляется молча. — нарушен: удалено молча; взамен корзина (PR #301)", show)
        self.assertNotIn("- 5a", show)
        j = json.loads(self.ok("car", "show", "tr-0001:0", "--json"))
        self.assertEqual((j["principles"]["score"], j["principles"]["max"], j["principles"]["passed"]), (0, 3, True))
        self.assertEqual([(i["label"], i["mark"]) for i in j["principles"]["items"]],
                         [("5", "ok"), ("26", "na"), ("11", "ok"), ("4", "bad"), ("5a", None)])
        self.assertIn("Оценка по принципам", show)
        self.assertIn("- С1 st-0001 (tr-0001:0): %s" % cell, self.ok("stack", "--md"))
        self.assertIn("- 0: %s" % cell, self.ok("track", "tr-0001"))
        self.assertIn("- tr-0001:0: %s" % cell, self.ok("train", "search"))
        tk = json.loads(self.ok("tracks", "--json"))
        self.assertEqual(tk["tracks"][0]["cars"][0]["principles"]["cell"], cell)
        self.assertEqual(json.loads(self.ok("train", "search", "--json"))["cars"][0]["principles"]["score"], 0)
        b = json.loads(self.ok("board", "--json"))
        self.assertEqual(b["slots"]["cells"][0]["principles"]["cell"], cell)
        item = [i for c in b["columns"] for i in c["items"] if i["ref"] == "tr-0001:0"][0]
        self.assertEqual((item["principles"]["passed"], item["gate"]["unmet"]),
                         (True, ["CI: не сдано", "Heavy: не сдано", "скан: не сдано", "шоты: не сдано"]))
        self.ok("stack", "add", "--title", "второй", "--brief", self.brief)
        self.ok("stack", "start", "st-0002")
        self.assertIn("- С2 st-0002: не проверен", self.ok("stack", "--md"))
        b = json.loads(self.ok("board", "--json"))
        item = [i for c in b["columns"] for i in c["items"] if i["ref"] == "st-0002"][0]
        self.assertIn("принципы, ворота: нет строки — 5, 26", item["gate"]["unmet"])
        self.assertIn("принципы, ворота: нет строки — 5, 26", self.ok("board"))
        # the web view knows the fields and labels them (its labels: the catalog's messages it is served with)
        from depot2 import webui
        page = webui.page_html(slurp(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "depot2",
                                                  "web", "index.html")), "ru")
        for label in ("Оценка по принципам", "marksTable", '"marks", "principles"', "принципы: "):
            self.assertIn(label, page)

    def test_nothing_shows_without_the_third_check(self):
        for pc in ("pc-0005", "pc-0026", "pc-0011", "pc-0004", "pc-0005a"):
            self.ok("principles", "set", pc, "--check", "no")
        self.car_in_slot()
        self.assertNotIn("Оценка по принципам", self.ok("stack", "--md") + self.ok("car", "show", "st-0001") +
                         self.ok("track", "tr-0001"))
        self.assertIsNone(json.loads(self.ok("car", "show", "st-0001", "--json"))["principles"])
        self.assertIsNone(json.loads(self.ok("tracks", "--json"))["tracks"][0]["cars"][0]["principles"])


class MigrateMarksTest(GateBase):

    def depot2(self, state):
        stk = Stack(state, self.t.cfg)
        o = stk.add("со старой проверкой", pr=301)
        stk.add("без")
        d = read_json(state.path("stack.json"))
        d["objects"][0]["dod"] = {"legacy_check": {"state": "ok", "ts": "2015-11-18T10:00:00+02:00"},
                                  "scan": {"state": "clean", "ts": "2015-11-18T10:00:00+02:00"}}
        write_json(state.path("stack.json"), d)
        Tracks(state, self.t.cfg).add("Трек")
        for f in ("tracks.json", "trains.json"):
            write_json(state.path(f), dict(read_json(state.path(f)) or {"trains": []}, schema=2))
        return o["id"]

    def test_the_legacy_words_go_once(self):
        oid = self.depot2(self.t.state)
        self.assertEqual(migrate.version(self.t.state), 2)
        self.assertTrue(migrate.pending(self.t.state))
        before = {f: slurp(self.t.state.path(f)) for f in ("stack.json", "tracks.json", "trains.json")}
        lines, n = migrate.run(self.t.state, self.t.cfg, dry_run=True)
        self.assertEqual((n, lines[0]), (1, "DRY RUN: ничего не записано"))
        self.assertIn("сданные слова «legacy check» сняты с объектов стека: %s (ok)" % oid, lines)
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in before}, before)  # a dry run writes nothing
        rc, out, err = self.team("stack", "--md")  # any command migrates first
        self.assertEqual(rc, 0, err)
        self.assertIn("миграция данных депо: схема 2 → 3", err)
        self.assertEqual(migrate.version(self.t.state), SCHEMA)
        self.assertEqual(read_json(self.t.state.path("tracks.json"))["schema"], SCHEMA)
        o = Stack(self.t.state, self.t.cfg).get(oid)
        self.assertEqual(o["dod"], {"scan": {"state": "clean", "ts": "2015-11-18T10:00:00+02:00"}})
        row = [h for h in read_jsonl(self.t.state.path("tracks-history.jsonl")) if h["event"] == "migrate"]
        self.assertEqual([(h["schema"], h.get("legacy_check")) for h in row], [(3, {oid: "ok"}), (4, None), (5, None)])
        snap = {f: slurp(self.t.state.path(f)) for f in before}
        self.assertEqual(migrate.run(self.t.state, self.t.cfg),
                         (["миграция данных депо: нечего переносить (схема %d)" % SCHEMA], 0))
        self.assertEqual({f: slurp(self.t.state.path(f)) for f in before}, snap)
        self.assertIn("сданные слова «legacy check» сняты с объектов стека: таких нет",
                      migrate.step_marks(self.t.state, dry_run=True))
        # the old flag is gone with the item
        with self.assertRaises(SystemExit):
            self.team("stack", "set", oid, "--legacy_check", "ok")


if __name__ == "__main__":
    unittest.main()
