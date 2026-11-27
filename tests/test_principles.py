"""The DoD's `principles` (the car's marks): test_marks.py."""

import json
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path

from helpers import BRIEF_TEXT, GATES_ALL, HERE, slurp
from test_board import GateBase
from test_extras import Base

from depot2 import bundle, gates
from depot2.principles import (
    Principles,
    banner_line,
    diff_lines,
    owner_words,
    parse,
    render_text,
    strip_banner,
    today_headings,
)
from depot2.stack import Stack
from depot2.store import read_json, write_json
from depot2.tracks import Tracks

FIXTURE = os.path.join(HERE, "principles-handwritten.md")  # written by hand, before the depot
FIXTURE_RENDERED = os.path.join(HERE, "principles-rendered.md")  # rendered by an older depot
GATES_DAY = "2015-11-18"  # the rule's day GatesTest sets (gates.checks_since; the config's default is null)
NEW_HEADS = ["## Приоритет 1 — ворота", "## Приоритет 2 — балл вагона"]
BANNER = banner_line()  # the run's language: helpers.py sets ru

SAMPLE = """# Принципы (проба)

Вступление.

## Основа
2. **Второй** (владелец 2015-11-18: «слово один»; книга «Не слово»). Тело.
   - **Третья проверка** (владелец 2015-11-18: «рамка»). Пять пунктов ниже.
   - **Пункт один.** Текст.
   - **Пункт два.** Текст.

## Механики
1. **Первый.** Владелец, 2015-11-06: «мы так решили».
   - вложение
     - глубже (wl-0004, dec-000012)
5a. **Буква (принят 2015-11-06).** Без слов.
## Голос и тексты
3. Без жирного (2015-11-05: «да»).
"""


def write(d, name, text):
    path = os.path.join(d, name)
    with Path(path).open("w", encoding="utf-8") as f:
        f.write(text)
    return path


def writable(path):
    return bool(Path(path).stat().st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))


class PBase(Base):
    def setUp(self):
        super().setUp()
        self.files = tempfile.mkdtemp(prefix="team-principles-")

    def tearDown(self):
        for d, _, names in os.walk(self.files):
            for n in names:
                Path(d, n).chmod(0o644)
        shutil.rmtree(self.files, ignore_errors=True)
        super().tearDown()

    def ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, "{}: {}".format(" ".join(argv), err))
        return out

    def pc(self):
        return Principles(self.t.state, self.t.cfg)


class ImportTest(PBase):
    def round_trip(self, src):
        path = os.path.join(self.files, "PRINCIPLES.md")
        shutil.copy(src, path)
        Path(path).chmod(0o644)
        original = "\n".join(strip_banner(slurp(path))) + "\n"  # a file rendered before carries the banner already
        out = self.ok("principles", "import", path, "--dry-run")
        self.assertIn("DRY RUN: would import", out)
        self.assertIn(
            "the render equals the file line for line (%d lines), but the banner «Собран из депо» and the "  # noqa: UP031
            "sections' headings in today's words" % len(original.splitlines()),
            out,
        )
        self.assertFalse(os.path.exists(os.path.join(self.t.dir, "principles.json")))  # the dry run wrote nothing
        self.assertEqual(strip_banner(slurp(path)), original.splitlines())
        self.assertTrue(writable(path))
        self.ok("principles", "import", path)
        rendered = slurp(path)
        self.assertFalse(writable(path))  # read-only between renders
        got = rendered.splitlines()
        self.assertEqual(
            got[2],
            "> Собран из депо (`depot2 principles render`); править через `depot2 principles` "
            "(add, set, replace, drop), не руками: между рендерами файл только для чтения.",
        )
        self.assertEqual(got[3], "")
        # the banner and the headings' words are the only difference
        self.assertEqual(got[:2] + got[4:], today_headings(original.splitlines()))
        self.assertEqual(strip_banner(rendered), today_headings(original.splitlines()))
        self.assertEqual([x for x in got if x.startswith("## ")][:2], NEW_HEADS)
        self.assertTrue(rendered.endswith("\n"))
        return path, original

    def test_a_hand_written_file_round_trips_without_loss(self):
        path, original = self.round_trip(FIXTURE)
        p = self.pc()
        ids = [e["id"] for e in p.ordered()]
        self.assertEqual(len(ids), 19)  # 18 numbers and 3a
        self.assertEqual(ids[:3], ["pc-0018", "pc-0017", "pc-0001"])
        self.assertIn("## Основа", original.splitlines())  # the headings of before: they name the same sections
        self.assertEqual(ids[4:7], ["pc-0003", "pc-0003a", "pc-0004"])
        e = p.get("pc-0018")
        self.assertEqual(
            (e["n"], e["section"], e["date"], e["check"], e["order"], e["gap"]), (18, "core", "2015-04-09", True, 1, 1)
        )
        self.assertEqual(e["title"], "Поиск отвечает на вопрос читателя")
        self.assertEqual(
            e["words"][:2],
            [
                {"date": "2015-04-09", "text": "читатель спрашивает про книгу, а не про нашу базу"},
                {"date": "2015-04-09", "text": "эти признаки — третья проверка, после слов и сверки"},
            ],
        )
        self.assertEqual(len(e["subs"]), 4)
        self.assertEqual([x["id"] for x in p.entries() if x["check"]], ["pc-0018"])
        self.assertEqual(p.get("pc-0003a")["suffix"], "a")
        self.assertIn("     - 2015-04-06: карточка издания", "\n".join(original.splitlines()))
        self.assertTrue(any(s.startswith("  2015-04-06") for s in p.get("pc-0005")["subs"]))  # nested, indent kept
        self.assertTrue(any(s.startswith("    2015-04-14") for s in p.get("pc-0005")["subs"]))
        self.assertEqual(p.get("pc-0009")["words"], [{"date": "2015-04-02", "text": "Покажи на книге"}])
        self.assertEqual(p.get("pc-0002")["words"], [])
        self.assertEqual(p.data["next"], 19)
        self.assertEqual(p.target(), path)
        # a second import of the same file is refused, the depot as it was
        Path(path).chmod(0o644)
        before = slurp(os.path.join(self.t.dir, "principles.json"))
        rc, _, err = self.team("principles", "import", path)
        self.assertEqual(rc, 1)
        self.assertIn("was imported already", err)
        self.assertEqual(slurp(os.path.join(self.t.dir, "principles.json")), before)
        # … and another copy of it too: its numbers are in the depot
        other = write(self.files, "again.md", original)
        rc, _, err = self.team("principles", "import", other)
        self.assertEqual(rc, 1)
        self.assertIn("pc-0018 is in the depot already", err)
        hist = [h["event"] for h in p.history()]
        self.assertEqual(hist, ["import"])

    def test_the_head_is_replaced_and_the_rest_stays(self):
        path, original = self.round_trip(FIXTURE)
        text = "Правила каталога. Меняет их только владелец: через depot2 principles add и set, с датой."
        self.ok("principles", "head", "--text", text)
        got = strip_banner(slurp(path))
        want = today_headings(original.splitlines())
        self.assertEqual(got[:4], [want[0], "", text, ""])
        self.assertNotEqual(want[2], text)
        self.assertEqual(got[4:], want[4:])  # every principle line for line, as before

    def test_a_rendered_file_round_trips(self):
        path, original = self.round_trip(FIXTURE_RENDERED)
        p = self.pc()
        by = {}
        for e in p.ordered():
            by.setdefault(e["section"], []).append(e["n"] if not e["suffix"] else "%d%s" % (e["n"], e["suffix"]))  # noqa: UP031
        self.assertEqual(by["core"], [17, 4, 1])
        self.assertEqual(by["mechanics"], [2, 3, "3a", 5, 6, 7, 19, 20])
        self.assertEqual(len(p.ordered()), 20)
        self.assertEqual(
            [x for x in strip_banner(slurp(path)) if x.startswith("## ")],
            [*NEW_HEADS, "## Как мы работаем", "## Как мы проверяем", "## Голос и тексты"],
        )
        self.assertIn("## Основа", original.splitlines())
        self.assertIsNone(p.drift())  # rendered: no hand edit
        Path(path).chmod(0o644)
        shutil.copy(FIXTURE_RENDERED, path)
        self.assertIsNone(p.drift())
        self.assertIn("rendered from the depot", self.ok("principles", "render"))
        self.assertEqual([x for x in slurp(path).splitlines() if x.startswith("## ")][:2], NEW_HEADS)

    def test_the_grammar_of_a_small_file(self):
        head, items = parse(SAMPLE)
        self.assertEqual(head, ["# Принципы (проба)", "", "Вступление.", ""])
        by = {(i["n"], i["suffix"]): i for i in items}
        two = by[(2, "")]
        self.assertEqual(
            (two["title"], two["text"], two["gap"], two["check"]),
            ("Второй", "(владелец 2015-11-18: «слово один»; книга «Не слово»). Тело.", 1, True),
        )
        self.assertEqual(
            two["words"], [{"date": "2015-11-18", "text": "слово один"}, {"date": "2015-11-18", "text": "рамка"}]
        )
        one = by[(1, "")]
        self.assertEqual(one["subs"], ["вложение", "  глубже (wl-0004, dec-000012)"])  # the indent past three kept
        self.assertEqual(one["words"], [{"date": "2015-11-06", "text": "мы так решили"}])
        self.assertEqual(one["links"], ["wl-0004", "dec-000012"])
        self.assertEqual(by[(5, "a")]["gap"], 0)
        three = by[(3, "")]
        self.assertEqual(
            (three["title"], three["text"], three["section"]), ("", "Без жирного (2015-11-05: «да»).", "voice")
        )
        self.assertEqual(three["words"], [{"date": "2015-11-05", "text": "да"}])
        rendered = render_text(head, items, banner=False).splitlines()
        self.assertIsNone(diff_lines(today_headings(SAMPLE.splitlines()), rendered))
        self.assertEqual([x for x in rendered if x.startswith("## ")], [*NEW_HEADS, "## Голос и тексты"])
        self.assertEqual(
            today_headings(
                ["## Основа", "## механики", "## Приоритет 1", "## балл", "## Голос и тексты", "## Разное", "текст"]
            ),
            NEW_HEADS + NEW_HEADS + ["## Голос и тексты", "## Разное", "текст"],
        )

    def test_the_owner_words(self):
        self.assertEqual(
            owner_words("**Т** (владелец 2015-11-15; раньше, 2015-11-12: «а», после отзывов: «б»).", "x"),
            [{"date": "2015-11-12", "text": "а"}, {"date": "2015-11-12", "text": "б"}],
        )
        self.assertEqual(
            owner_words("**Т (владелец 2015-11-06, ещё; уточнено 2015-11-15):** «в»", "x"),
            [{"date": "2015-11-06", "text": "в"}],
        )
        self.assertEqual(
            owner_words("2015-11-05, пятое: письмо (src/x.ts), «нет». Владелец: «да».", "x"),
            [{"date": "2015-11-05", "text": "да"}],
        )
        self.assertEqual(owner_words("понимать «нет» (2015-11-06, отклонено «нет»)", "x"), [])

    def test_a_lossy_file_is_refused_and_nothing_written(self):
        for text, why in (
            (SAMPLE + "Просто абзац.\n", "строка не принцип"),
            (SAMPLE.replace("## Механики", "## Разное"), "section must be one of"),
            (SAMPLE.replace("## Основа\n", "## Основа\n\n"), "пустая строка сразу после заголовка"),
            (SAMPLE.replace("   - **Пункт два.**", "  - **Пункт два.**"), "строка не принцип"),
        ):
            path = write(self.files, "bad.md", text)
            rc, _, err = self.team("principles", "import", path)
            self.assertEqual(rc, 1, text)
            self.assertIn(why, err)
            self.assertFalse(os.path.exists(os.path.join(self.t.dir, "principles.json")))
            self.assertTrue(writable(path))
        # a render that would part from the file (no space after the bold title) is refused the same way
        path = write(self.files, "bad.md", SAMPLE.replace("**Первый.** Владелец", "**Первый.**Владелец"))
        rc, _, err = self.team("principles", "import", path)
        self.assertEqual(rc, 1)
        self.assertIn("импорт с потерями, ничего не записано: строка 12", err)


class QueueTest(PBase):
    def setUp(self):
        super().setUp()
        self.path = write(self.files, "PRINCIPLES.md", SAMPLE)
        self.ok("principles", "import", self.path)

    def test_a_copy_of_the_depot_never_renders_the_live_file(self):
        from depot2 import config

        before = slurp(self.path)
        live, config.DEFAULT_STATE = config.DEFAULT_STATE, os.path.join(self.files, "team")  # the target in its tree
        try:
            out = self.ok("principles", "set", "pc-0001", "--text", "Тело на копии.")
            self.assertIn(
                f"{self.path}: not rendered: it is the live depot's and this depot ({self.t.dir}) another", out
            )
            self.assertIn("principles render --out PATH", out)
            self.assertEqual(slurp(self.path), before)
            self.assertEqual(self.pc().get("pc-0001")["text"], "Тело на копии.")  # the depot's own data changed
            self.assertIn("not rendered", self.ok("principles", "render"))
            self.ok("principles", "set", "pc-0001", "--title", "Первый на копии")
            self.assertEqual(slurp(self.path), before)
            out = os.path.join(self.files, "elsewhere.md")
            self.ok("principles", "render", "--out", out)
            self.assertIn("Тело на копии.", slurp(out))
            config.DEFAULT_STATE = self.t.dir  # the live depot itself renders it (its drift checked again)
            self.assertEqual(self.team("principles", "render")[0], 1)
            self.assertIn("rendered from the depot", self.ok("principles", "render", "--overwrite"))
            self.assertIn("Тело на копии.", slurp(self.path))
        finally:
            config.DEFAULT_STATE = live

    def test_add_set_replace_drop_and_the_history(self):
        out = self.ok(
            "principles",
            "add",
            "--section",
            "основа",
            "--title",
            "Новый",
            "--words",
            "2015-11-17: «скажу»",
            "--words",
            "ещё",
            "--date",
            "2015-11-18",
            "--text",
            "Тело.",
            "--sub",
            "один",
            "--sub",
            "  вложен",
            "--link",
            "dec-000400",
        )
        self.assertIn("pc-0006 added (principle 6)", out)  # SAMPLE's numbers go up to 5 (5a): the next is 6
        p = self.pc()
        self.assertEqual(p.data["next"], 7)
        e = p.get("pc-0006")
        self.assertEqual(e["words"], [{"date": "2015-11-17", "text": "скажу"}, {"date": "2015-11-18", "text": "ещё"}])
        self.assertEqual((e["order"], e["gap"], e["status"], e["check"]), (2, 1, "active", False))
        text = slurp(self.path).splitlines()
        i = text.index("6. **Новый** (владелец 2015-11-17: «скажу»; 2015-11-18: «ещё»). Тело.")
        self.assertEqual(text[i + 1 : i + 5], ["   - один", "     - вложен", "", "## Приоритет 2 — балл вагона"])
        self.assertFalse(writable(self.path))
        # set: fields replaced (the row keeps what they were), a sub changed and one dropped, a word appended
        self.ok(
            "principles",
            "set",
            "pc-0006",
            "--text",
            "Тело два.",
            "--sub-set",
            "1",
            "первый",
            "--sub-drop",
            "2",
            "--words",
            "2015-11-18: третье",
            "--section",
            "работа",
        )
        e = self.pc().get("pc-0006")
        self.assertEqual((e["text"], e["subs"], e["section"]), ("Тело два.", ["первый"], "work"))
        row = self.pc().history()[-1]
        self.assertEqual(row["event"], "set")
        self.assertEqual(row["before"], {"text": "Тело.", "subs": ["один", "  вложен"], "section": "core", "order": 2})
        self.assertIn(
            "6. **Новый** (владелец 2015-11-17: «скажу»; 2015-11-18: «ещё»; 2015-11-18: «третье»). Тело два.",
            slurp(self.path),
        )
        self.assertIn("## Как мы работаем\n6. **Новый**", slurp(self.path))  # a section of its own now
        rc, _, err = self.team("principles", "set", "pc-0006", "--section", "нет такого")
        self.assertEqual(rc, 1)
        self.assertIn("section must be one of", err)
        # replace: out of the file, in --all; the other one knows it
        self.ok("principles", "replace", "pc-0006", "--by", "pc-0001", "--reason", "слились")
        self.assertNotIn("6. **Новый**", slurp(self.path))
        self.assertNotIn("## Как мы работаем", slurp(self.path))  # an empty section is not rendered
        self.assertEqual(self.pc().get("pc-0001")["replaces"], ["pc-0006"])
        self.assertNotIn("pc-0006", self.ok("principles"))
        self.assertIn("заменён → pc-0001", self.ok("principles", "--all"))
        for argv, why in (
            (["replace", "pc-0001", "--by", "pc-0001"], "cannot replace itself"),
            (["replace", "pc-0002", "--by", "pc-0006"], "pc-0006 is заменён"),
            (["set", "pc-0006", "--text", "x"], "only an active principle changes"),
            (["set", "pc-0001"], "nothing to set"),
        ):
            rc, _, err = self.team("principles", *argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(why, err)
        # drop: out of the file, its reason kept; never twice
        self.ok("principles", "drop", "pc-0005a", "--reason", "проба")
        self.assertNotIn("5a.", slurp(self.path))
        self.assertEqual(self.pc().get("pc-0005a")["reason"], "проба")
        rc, _, err = self.team("principles", "drop", "pc-0005a", "--reason", "ещё")
        self.assertEqual(rc, 1)
        # a number is never reused: the next one is 7, not 6 again
        self.ok("principles", "add", "--section", "тексты", "--title", "Ещё один")
        self.assertEqual(self.pc().entries()[-1]["id"], "pc-0007")
        self.assertEqual(
            [h["event"] for h in self.pc().history()], ["import", "add", "set", "replaced", "dropped", "add"]
        )

    def test_the_file_is_never_edited_by_hand(self):
        Path(self.path).chmod(0o644)
        write(self.files, "PRINCIPLES.md", slurp(self.path).replace("Без слов.", "Правка руками."))
        rc, _, err = self.team("principles", "set", "pc-0001", "--note", "x")
        self.assertEqual(rc, 1)
        self.assertIn("was edited by hand (строка", err)
        self.assertEqual(self.pc().get("pc-0001")["notes"], [])  # nothing changed
        rc, _, err = self.team("principles", "render")
        self.assertEqual(rc, 1)
        self.assertIn("principles render --overwrite", err)
        self.assertIn("Правка руками.", slurp(self.path))
        self.assertIn("rendered from the depot", self.ok("principles", "render", "--overwrite"))
        self.assertNotIn("Правка руками.", slurp(self.path))
        self.assertFalse(writable(self.path))
        self.assertIn("already as the depot has it", self.ok("principles", "render"))
        # elsewhere, and to stdout
        other = os.path.join(self.files, "copy.md")
        self.ok("principles", "render", "--out", other)
        self.assertEqual(slurp(other), slurp(self.path))
        self.assertTrue(writable(other))  # only the depot's own file is kept read-only
        self.assertEqual(self.ok("principles", "render", "--out", "-"), slurp(self.path))

    def test_the_head_changes_through_the_depot(self):
        """`principles head --text`: the import kept the head verbatim; after it the depot owns it, as the rest."""
        self.assertEqual(self.ok("principles", "head"), "# Принципы (проба)\n\nВступление.\n")
        out = self.ok("principles", "head", "--text", "Новые принципы — через depot2 principles add.")
        self.assertIn("the head of PRINCIPLES.md replaced", out)
        self.assertIn("rendered again", out)
        lines = slurp(self.path).splitlines()
        self.assertEqual(
            lines[:7],
            [
                "# Принципы (проба)",
                "",
                BANNER,
                "",
                "Новые принципы — через depot2 principles add.",
                "",
                "## Приоритет 1 — ворота",
            ],
        )
        self.assertFalse(writable(self.path))
        rows = [h for h in self.pc().history() if h["event"] == "head"]
        self.assertEqual(
            [(r["before"], r["text"]) for r in rows],
            [
                (
                    "# Принципы (проба)\n\nВступление.\n",
                    "# Принципы (проба)\n\nНовые принципы — через depot2 principles add.\n",
                )
            ],
        )
        self.assertIn("already as the depot has it", self.ok("principles", "render"))
        # a new title and two paragraphs; the principles below stay as they were
        below = slurp(self.path).splitlines()[6:]
        self.ok("principles", "head", "--text", "# Принципы (новые)\n\nАбзац один.\n\nАбзац два.\n")
        lines = slurp(self.path).splitlines()
        self.assertEqual(
            lines[:9],
            ["# Принципы (новые)", "", BANNER, "", "Абзац один.", "", "Абзац два.", "", "## Приоритет 1 — ворота"],
        )
        self.assertEqual(lines[8:], below)
        self.assertEqual(len([h for h in self.pc().history() if h["event"] == "head"]), 2)
        # refused: the same head, an empty one, a section, the banner
        for text, why in (
            ("# Принципы (новые)\n\nАбзац один.\n\nАбзац два.", "nothing to set"),
            ("  \n", "empty"),
            ("Текст.\n## Основа", "section"),
            (BANNER, "banner"),
        ):
            rc, _, err = self.team("principles", "head", "--text", text)
            self.assertEqual(rc, 1, text)
            self.assertIn(why, err)
        self.assertEqual(len([h for h in self.pc().history() if h["event"] == "head"]), 2)
        # a hand edit is refused, as with every command, until --overwrite
        Path(self.path).chmod(0o644)
        write(self.files, "PRINCIPLES.md", slurp(self.path).replace("Абзац два.", "Правка руками."))
        rc, _, err = self.team("principles", "head", "--text", "Абзац три.")
        self.assertEqual(rc, 1)
        self.assertIn("was edited by hand (строка", err)
        self.assertEqual(self.pc().head()[-2], "Абзац два.")  # nothing changed
        self.ok("principles", "head", "--text", "Абзац три.", "--overwrite")
        self.assertEqual(slurp(self.path).splitlines()[:6], ["# Принципы (новые)", "", BANNER, "", "Абзац три.", ""])
        self.assertNotIn("Правка руками.", slurp(self.path))

    def test_the_checks_come_from_the_principles_by_priority(self):
        out = self.ok("principles", "checks")
        self.assertIn("1. **Слова владельца**", out)
        self.assertIn("2. **«Сверка с кодом»**", out)
        self.assertIn(
            "Каждый эскиз, бриф, PR и раунд команды проходит три проверки; у принципов приоритета 1 — "
            "ворота, у остальных — балл вагона.\n",
            out,
        )
        self.assertIn(
            "3. **Проверка по принципам** — по строке на каждый принцип: `- 20: ок — как сделано`, `- 26: не "
            "касается`, `- 4: нарушен — что; взамен …`.\n",
            out,
        )
        self.assertIn(
            "   - **Приоритет 1 — ворота** — у каждого строка, и ни один не «нарушен» — иначе вагон не идёт в "
            "приёмку и в мердж:\n     - 2. Второй (pc-0002)\n",
            out,
        )
        self.assertIn(
            "   - **Приоритет 2 — балл вагона** — ок +1, не касается 0, нарушен -1; нет строки — «не "
            "проверен»:\n     - (нет: `depot2 principles set pc-… --check yes`)\n",
            out,
        )
        self.assertIn("В теле PR — раздел «Проверка по принципам»", out)
        self.assertNotIn("(владелец", out)  # the checks quote no owner's words
        self.assertNotIn("«рамка»", out)  # nor the words of a principle in them
        j = json.loads(self.ok("principles", "checks", "--json"))
        self.assertEqual((j["v"], [c["n"] for c in j["checks"]]), (2, [1, 2, 3]))
        third = j["checks"][2]
        self.assertEqual(third["title"], "Проверка по принципам")
        self.assertEqual(
            third["principles"], [{"id": "pc-0002", "label": "2", "title": "Второй", "section": "core", "priority": 1}]
        )
        self.assertEqual(
            [(p["priority"], p["title"], p["labels"]) for p in third["priorities"]],
            [(1, "Приоритет 1 — ворота", ["2"]), (2, "Приоритет 2 — балл вагона", [])],
        )
        self.ok("principles", "set", "pc-0001", "--check", "yes")
        self.ok("principles", "set", "pc-0005a", "--check", "yes")
        self.ok("principles", "set", "pc-0003", "--check", "yes")
        j = json.loads(self.ok("principles", "checks", "--json"))
        self.assertEqual(
            [(p["label"], p["priority"]) for p in j["checks"][2]["principles"]],
            [("2", 1), ("1", 2), ("5a", 2), ("3", 2)],
        )
        self.assertEqual([p["labels"] for p in j["checks"][2]["priorities"]], [["2"], ["1", "5a", "3"]])
        self.assertIn("     - 5a. Буква (принят 2015-11-06). (pc-0005a)\n", self.ok("principles", "checks"))
        self.assertIn(
            "(3) проверка по принципам — приоритет 1 — ворота: 2; приоритет 2 — балл вагона: 1, 5a, 3.",
            self.pc().round_line(),
        )
        for pc in ("pc-0002", "pc-0001", "pc-0005a", "pc-0003"):
            self.ok("principles", "set", pc, "--check", "no")
        self.assertEqual(self.ok("principles", "checks").count("(нет: `depot2 principles set pc-… --check yes`)"), 2)
        self.assertIn("(3) проверка по принципам — принципов с check: true нет.", self.pc().round_line())

    def test_an_older_depots_section_codes_are_read_as_todays(self):
        path = self.t.state.path("principles.json")
        data = read_json(path)
        old = {"core": "основа", "mechanics": "механики", "voice": "тексты"}
        for e in data["entries"]:
            e["section"] = old[e["section"]]
        write_json(path, data)
        self.assertEqual([e["section"] for e in self.pc().ordered()], ["core", "mechanics", "mechanics", "voice"])
        self.assertIsNone(self.pc().drift())  # the same render
        self.ok("principles", "set", "pc-0003", "--section", "проверка")  # an old id typed: today's code written
        self.assertEqual(sorted({e["section"] for e in read_json(path)["entries"]}), ["checks", "core", "mechanics"])
        self.ok("principles", "set", "pc-0003", "--section", "Voice and texts")  # a title in English
        self.assertEqual(self.pc().get("pc-0003")["section"], "voice")

    def test_the_views(self):
        md = self.ok("principles")
        self.assertIn("| Принцип | Раздел | Дата | Слова владельца | Подпункты | Статус |", md)
        self.assertIn(
            "| pc-0002 · 2. Второй · ③ ворота | Приоритет 1 — ворота | 2015-11-18 | «слово один» (2015-11-18) (+1) "
            "| 3 | действует |",
            md,
        )
        self.assertIn("Действуют 4 из 4", md)
        j = json.loads(self.ok("principles", "--json"))
        self.assertEqual(j["v"], 1)
        self.assertEqual([s["key"] for s in j["sections"]], ["core", "mechanics", "work", "checks", "voice"])
        self.assertEqual([s["title"] for s in j["sections"]][:2], ["Приоритет 1 — ворота", "Приоритет 2 — балл вагона"])
        self.assertEqual(
            [(i["label"], i["priority"]) for sec in j["sections"] for i in sec["items"]],
            [("2", 1), ("1", 2), ("5a", 2), ("3", 2)],
        )
        self.assertEqual([s["count"] for s in j["sections"]], [1, 2, 0, 0, 1])
        self.assertEqual(j["checks"], ["pc-0002"])
        self.assertEqual(j["counts"], {"active": 4, "replaced": 0, "dropped": 0})
        self.assertEqual(j["file"], self.path)
        five = j["sections"][1]["items"][1]
        self.assertEqual((five["id"], five["label"], five["status_ru"]), ("pc-0005a", "5a", "действует"))
        self.assertEqual(five["lines"], ["5a. **Буква (принят 2015-11-06).** Без слов."])
        self.assertEqual(
            j["sections"][1]["items"][0]["lines"],
            [
                "1. **Первый.** Владелец, 2015-11-06: «мы так решили».",
                "   - вложение",
                "     - глубже (wl-0004, dec-000012)",
            ],
        )
        show = self.ok("principles", "show", "pc-0002")
        self.assertIn("# pc-0002 — принцип 2 (действует)", show)
        self.assertIn("  - 2015-11-18: «рамка»", show)
        self.assertIn("откуда: PRINCIPLES.md:6", show)
        # the queues' overview
        out = self.ok("queues", "--md")
        self.assertIn("| Принципы | 4 действуют | — | ③ pc-0002 2. Второй | `depot2 principles --md` |", out)


class GatesTest(GateBase):
    def setUp(self):
        super().setUp()
        # the rule's day, set explicitly (the config's default is null): in-process, and for the commands by the
        # state's config.json (a --set or another config.json overrides it)
        self.t.cfg["gates"]["checks_since"] = GATES_DAY
        write_json(self.t.state.path("config.json"), {"gates": {"checks_since": GATES_DAY}})

    def test_the_dor_wants_the_checks_in_the_brief(self):
        bare = write(self.t.dir, "bare.md", "# бриф\n\nбез разделов\n")
        half = write(self.t.dir, "half.md", "# бриф\n\n**Сверка с кодом.** есть\n")
        s = Stack(self.t.state, self.t.cfg)
        o1 = s.add("голый", brief=bare)
        o2 = s.add("половина", brief=half)
        o3 = s.add("полный", brief=self.brief)
        self.assertEqual(gates.checks_missing(bare), ["Сверка с кодом", "Проверка по принципам"])
        self.assertEqual(gates.checks_missing(half), ["Проверка по принципам"])
        self.assertEqual(gates.checks_missing(self.brief), [])
        for head in (
            "### Проверка по принципам",
            "**Проверка по принципам (кратко)**",
            "## ПРОВЕРКА ПО  ПРИНЦИПАМ ДИЗАЙНА",
            "## Проверка по принципам",
        ):
            self.assertEqual(
                gates.checks_missing(write(self.t.dir, "h.md", f"## Сверка с кодом\n\n{head}\n")), [], head
            )
        self.assertEqual(
            gates.checks_missing(write(self.t.dir, "h.md", "## Сверка с кодом\n\n## Принципы\n")),
            ["Проверка по принципам"],
        )
        self.assertIn(
            "в брифе нет разделов трёх проверок: «Проверка по принципам» (depot2 principles checks)",
            gates.dor_unmet(s, None, obj=s.get(o2["id"])),
        )
        self.assertEqual(gates.dor_unmet(s, None, obj=s.get(o3["id"])), [])
        rc, _, err = self.team("stack", "start", o1["id"])
        self.assertEqual(rc, 1)
        self.assertIn(
            "DoR (вход в сборку) — в брифе нет разделов трёх проверок: «Сверка с кодом», «Проверка по принципам»", err
        )
        self.assertIn("--force --why", err)
        board = self.team("board")[1]
        self.assertIn("DoR: в брифе нет разделов трёх проверок", board)
        rc, _out, err = self.team("stack", "start", o1["id"], "--force", "--why", "старый эскиз без разделов")
        self.assertEqual(rc, 0, err)
        row = Stack(self.t.state, self.t.cfg).get(o1["id"])["stages"][-1]
        self.assertEqual(
            row["gates"]["DoR"],
            [
                "в брифе нет разделов трёх проверок: «Сверка с кодом», «Проверка по "
                "принципам» (depot2 principles checks)"
            ],
        )
        self.assertEqual(self.team("stack", "start", o3["id"])[0], 0)
        # without the brief item, a car with no brief is told so by `checks` itself
        self.assertIn(
            "три проверки: нет брифа, нечего проверить",
            gates.dor_unmet(s, None, obj=s.add("без брифа"), items=["checks"]),
        )

    def backdate(self, day, cars=(), objects=()):
        """The cars (`tr-NNNN:N`) and the stack objects came in on `day` (as the cars before the rule did)."""
        if cars:
            path = self.t.state.path("tracks.json")
            d = read_json(path)
            for t in d["tracks"]:
                for c in t["cars"]:
                    if "{}:{}".format(t["id"], c["n"]) in cars:
                        c["added"] = day + "T12:00:00+02:00"
            write_json(path, d)
        if objects:
            path = self.t.state.path("stack.json")
            d = read_json(path)
            for o in d["objects"]:
                if o["id"] in objects:
                    o["added"] = day + "T12:00:00+02:00"
            write_json(path, d)

    def test_an_old_car_passes_the_checks_with_a_quiet_note(self):
        bare = write(self.t.dir, "bare.md", "# старый эскиз\n\nбез разделов\n")
        self.team("tracks", "add", "--title", "Поезд")
        self.team("tracks", "car", "add", "tr-0001", "--title", "старый", "--brief", bare)
        self.team("tracks", "car", "add", "tr-0001", "--title", "новый", "--brief", bare)
        self.team("stack", "add", "--title", "старый объект", "--brief", bare)
        self.team("stack", "add", "--title", "новый объект", "--brief", bare)
        self.backdate("2015-11-10", cars=["tr-0001:0"], objects=["st-0001"])
        self.team(
            "trains",
            "add",
            "--name",
            "poezd",
            "--title",
            "Поезд",
            "--car",
            "tr-0001:0",
            "--car",
            "tr-0001:1",
            "--slots",
            "max 4",
        )
        self.assertEqual(
            self.team("trains", "state", "poezd", "impl", "--force", "--why", "новый — по DoR в слоте")[0], 0
        )
        s, tr = Stack(self.t.state, self.t.cfg), Tracks(self.t.state, self.t.cfg)
        t, old = tr.car("tr-0001:0")
        _, new = tr.car("tr-0001:1")
        self.assertEqual((gates.added_day(car=old), gates.checks_since(self.t.cfg)), ("2015-11-10", "2015-11-18"))
        self.assertEqual(gates.dor_unmet(s, tr, car=old, track=t), [])
        self.assertEqual(gates.dor_notes(s, car=old), ["три проверки: эскиз до правила"])
        self.assertEqual(
            gates.dor_unmet(s, tr, car=new, track=t),
            [
                "в брифе нет разделов трёх проверок: «Сверка с "
                "кодом», «Проверка по принципам» (depot2 principles "
                "checks)"
            ],
        )
        self.assertEqual(gates.dor_notes(s, car=new), [])
        self.assertEqual(gates.dor_unmet(s, None, obj=s.get("st-0001")), [])
        self.assertEqual(gates.dor_notes(s, obj=s.get("st-0001")), ["три проверки: эскиз до правила"])
        self.assertIn("в брифе нет разделов трёх проверок", " ".join(gates.dor_unmet(s, None, obj=s.get("st-0002"))))
        self.assertEqual(
            gates.gate_text(
                {"unmet": ["открытые вопросы: 1"], "notes": ["три проверки: эскиз до правила"]},
                "пройдены",
                "не пройдены: ",
            ),
            "не пройдены: открытые вопросы: 1 · три проверки: эскиз до правила",
        )  # a note never reads as unmet
        # an old car whose brief has the sections: nothing to note
        full = dict(old, brief=self.brief)
        self.assertEqual((gates.dor_unmet(s, tr, car=full, track=t), gates.dor_notes(s, car=full)), ([], []))
        # car show and the board say it quietly
        out = self.team("car", "show", "tr-0001:0")[1]
        self.assertIn("Ворота дальше — DoR (вход в сборку): пройдены (три проверки: эскиз до правила).", out)
        j = json.loads(self.team("car", "show", "tr-0001:0", "--json")[1])
        self.assertEqual((j["gate"]["unmet"], j["gate"]["notes"]), ([], ["три проверки: эскиз до правила"]))
        self.assertIn(
            "Ворота дальше — DoR (вход в сборку): не пройдены: в брифе нет разделов трёх проверок",
            self.team("car", "show", "tr-0001:1")[1],
        )
        board = self.team("board")[1]
        self.assertRegex(
            board, r"\| tr-0001:0 \| P5 \| старый \|[^\n]*\| DoR пройдены \(три проверки: эскиз до правила\) \|"
        )
        self.assertRegex(
            board,
            r"\| st-0001 \| P5 \| старый объект \|[^\n]*\| DoR пройдены \(три проверки: эскиз до "
            r"правила\) \|",
        )
        self.assertRegex(board, r"\| tr-0001:1 \| P5 \| новый \|[^\n]*\| DoR: в брифе нет разделов трёх проверок")
        nx = json.loads(self.team("trains", "next", "--json")[1])
        self.assertEqual([g["car"] for g in nx["gated"]], ["tr-0001:1"])
        # the old ones start with no --force; the new one is refused, and --force --why still passes it
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:0", "--start")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.team("stack", "start", "st-0001")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("forced", Stack(self.t.state, self.t.cfg).get("st-0001")["stages"][-1])
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:1", "--start")
        self.assertEqual(rc, 1)
        self.assertIn("DoR (вход в сборку) — в брифе нет разделов трёх проверок", err)
        rc, _, err = self.team("tracks", "car", "stack", "tr-0001:1", "--start", "--force", "--why", "эскиз в работе")
        self.assertEqual(rc, 0, err)
        _, c = Tracks(self.t.state, self.t.cfg).car("tr-0001:1")
        row = Stack(self.t.state, self.t.cfg).get(c["st"])["stages"][-1]
        self.assertEqual(
            (row["forced"], row["gates"]["DoR"]),
            (
                True,
                [
                    "в брифе нет разделов трёх проверок: «Сверка с кодом», «Проверка по принципам» (depot2 principles checks)"
                ],
            ),
        )

    def test_the_rules_day_is_read_from_the_config(self):
        self.assertIsNone(GATES_ALL["checks_since"])  # the config's default (helpers: before the suites' own): null
        self.assertEqual(gates.checks_since(self.t.cfg), GATES_DAY)  # the day setUp set
        bare = write(self.t.dir, "bare.md", "# эскиз\n\nбез разделов\n")
        self.team("tracks", "add", "--title", "Поезд")
        self.team("tracks", "car", "add", "tr-0001", "--title", "вагон", "--brief", bare)
        self.backdate("2015-11-10", cars=["tr-0001:0"])
        t, c = Tracks(self.t.state, self.t.cfg).car("tr-0001:0")
        later = json.loads(json.dumps(self.t.cfg))
        later["gates"]["checks_since"] = "2015-11-01"  # the rule moved back: the car counts
        self.assertIn(
            "в брифе нет разделов трёх проверок",
            " ".join(gates.dor_unmet(Stack(self.t.state, later), None, car=c, track=t)),
        )
        self.assertEqual(gates.dor_notes(Stack(self.t.state, later), car=c), [])
        self.assertEqual(gates.dor_unmet(Stack(self.t.state, self.t.cfg), None, car=c, track=t), [])
        # by the command line and by the state's config.json (setUp's: the car is old)
        self.assertIn("пройдены (три проверки: эскиз до правила)", self.team("car", "show", "tr-0001:0")[1])
        rc, out, _ = self.team("--set", "gates.checks_since=2015-11-01", "car", "show", "tr-0001:0")
        self.assertIn("не пройдены: в брифе нет разделов трёх проверок", out)
        write_json(self.t.state.path("config.json"), {"gates": {"checks_since": "2015-11-01"}})
        self.assertIn("не пройдены: в брифе нет разделов трёх проверок", self.team("car", "show", "tr-0001:0")[1])
        write_json(self.t.state.path("config.json"), {"gates": {"checks_since": "2999-01-01"}})
        self.team("tracks", "car", "add", "tr-0001", "--title", "сегодняшний", "--brief", bare)
        self.assertIn("пройдены (три проверки: эскиз до правила)", self.team("car", "show", "tr-0001:1")[1])
        rc, _, err = self.team("--set", "gates.checks_since=soon", "board")
        self.assertNotEqual(rc, 0)
        self.assertIn("gates.checks_since must be a day, YYYY-MM-DD", err)


class BundleTest(PBase):
    def test_the_round_bundle_carries_the_principles(self):
        b = bundle.build("discuss", self.t.cfg, self.input, state=self.t.state)
        self.assertNotIn("Принципы", b.text(10**6))  # none in the depot yet: nothing, nothing said cut
        path = os.path.join(self.files, "PRINCIPLES.md")
        shutil.copy(FIXTURE, path)
        Path(path).chmod(0o644)
        self.ok("principles", "import", path)
        b = bundle.build("discuss", self.t.cfg, self.input, state=self.t.state)
        text = b.text(10**6)
        self.assertIn("## Принципы дизайна и три проверки (depot2 principles)", text)
        self.assertIn(
            "Три проверки (`depot2 principles checks`): (1) слова владельца, (2) «Сверка с кодом», (3) "
            "проверка по принципам — приоритет 1 — ворота: 18; приоритет 2 — балл вагона: нет.",
            text,
        )
        self.assertIn("## Приоритет 1 — ворота\n18. **Поиск отвечает на вопрос читателя**", text)
        self.assertIn("17. **Каталог открыт без входа.**", text)
        self.assertIn("16. **Письмо читателю — одно дело", text)
        self.assertNotIn("Собран из депо", text)
        self.assertNotIn("принципы:", " ".join(b.cuts))
        # capped like the other parts
        self.t.cfg["bundle"]["principles_max_chars"] = 2000
        b = bundle.build("discuss", self.t.cfg, self.input, state=self.t.state)
        self.assertTrue(any(c.startswith("принципы: оставлено 2000 из") for c in b.cuts), b.cuts)
        # a round builds its bundle with the state
        self.t.cfg["bundle"]["principles_max_chars"] = 24000
        r, _ = self.play({})
        self.assertIn(
            "## Принципы дизайна и три проверки (depot2 principles)", slurp(os.path.join(r["dir"], "bundle.md"))
        )


class BriefTextTest(unittest.TestCase):
    def test_the_brief_the_old_suites_use_passes_the_checks(self):
        d = tempfile.mkdtemp(prefix="team-brief-")
        try:
            self.assertEqual(gates.checks_missing(write(d, "b.md", BRIEF_TEXT)), [])
        finally:
            shutil.rmtree(d, ignore_errors=True)
