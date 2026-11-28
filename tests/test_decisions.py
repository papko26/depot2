"""`decide` writes, this only reads."""

import hashlib
import json
import os
from pathlib import Path

from test_extras import Base

from depot2 import characters
from depot2 import decisions as dec


def tree_hash(root):
    out = {}
    for d, _, files in os.walk(root):
        for n in files:
            with Path(d, n).open("rb") as f:
                out[os.path.relpath(Path(d, n), root)] = hashlib.sha256(f.read()).hexdigest()
    return out


LINKED = (
    "Вагон tr-0904:16: и tr-0907:2x-alt. Трек tr-0904, PR #946 и (#12), см. dec-000001, wl-0007, td-0045; "
    "mq-tr-0009 и xtr-0001 — не ссылки, st-0001x тоже; снова tr-0904:16."
)


class DecisionsTest(Base):
    def decide(self, who, text, *more):
        rc, _out, err = self.team("decide", who, "--text", text, *more)
        self.assertEqual(rc, 0, err)

    def json(self, *argv):
        rc, out, err = self.team("decisions", "--json", *argv)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def setUp(self):
        super().setUp()
        self.decide("all", "Первое: вкладка «Очереди»")
        self.decide("builder", "Второе: строитель про импорт", "--round", "2015-11-19-discuss", "--point", "Q2")
        self.decide("all", LINKED)

    def test_newest_first_with_the_date_who_and_the_links(self):
        j = self.json()
        self.assertEqual((j["v"], j["total"], j["found"], j["count"], j["grep"], j["limit"]), (1, 3, 3, 3, None, None))
        rows = j["decisions"]
        self.assertEqual([r["id"] for r in rows], ["dec-000003", "dec-000002", "dec-000001"])
        r = rows[1]
        self.assertEqual(
            (r["seq"], r["character"], r["who"], r["round"], r["point"]),
            (2, "builder", characters.name("builder"), "2015-11-19-discuss", "Q2"),
        )
        self.assertEqual(r["date"], r["ts"][:10])
        self.assertEqual(rows[2]["who"], "все")
        self.assertEqual((rows[2]["round"], rows[2]["point"]), (None, None))  # always there, null when none
        self.assertEqual(
            rows[0]["links"],
            {
                "cars": ["tr-0904:16", "tr-0907:2x-alt"],
                "tracks": ["tr-0904"],
                "items": ["wl-0007", "td-0045"],
                "decisions": ["dec-000001"],
                "prs": [946, 12],
            },
        )
        self.assertEqual(rows[1]["links"], {k: [] for k in dec.LINK_KINDS})
        with Path(self.t.state.decisions_path).open(encoding="utf-8") as f:
            stored = [json.loads(x) for x in f]
        for k in ("id", "seq", "ts", "character", "text"):  # the record as it is stored, nothing renamed
            self.assertEqual(rows[2][k], stored[0][k])

    def test_the_filter_is_every_word_in_any_case_then_the_limit(self):
        self.assertEqual([r["id"] for r in self.json("--grep", "ВКЛАДКА очереди")["decisions"]], ["dec-000001"])
        self.assertEqual([r["id"] for r in self.json("--grep", "вкладка импорт")["decisions"]], [])
        self.assertEqual([r["id"] for r in self.json("--grep", "dec-000002")["decisions"]], ["dec-000002"])
        self.assertEqual(
            [r["id"] for r in self.json("--grep", characters.name("builder"))["decisions"]], ["dec-000002"]
        )
        self.assertEqual([r["id"] for r in self.json("--grep", "q2")["decisions"]], ["dec-000002"])  # the point
        self.assertEqual([r["id"] for r in self.json("--grep", "все")["decisions"]], ["dec-000003", "dec-000001"])
        j = self.json("--grep", "все", "--limit", "1")
        self.assertEqual(
            ([r["id"] for r in j["decisions"]], j["found"], j["count"], j["total"]), (["dec-000003"], 2, 1, 3)
        )
        self.assertEqual(self.json("--limit", "0")["decisions"], [])
        self.assertEqual(self.json("--grep", "   ")["found"], 3)  # no words: no filter
        rc, _, err = self.team("decisions", "--limit", "-1")
        self.assertEqual(rc, 1)
        self.assertIn("--limit", err)

    def test_the_list(self):
        rc, out, err = self.team("decisions")
        self.assertEqual(rc, 0, err)
        lines = out.splitlines()
        self.assertEqual(lines[0], "## Решения владельца (3 из 3)")
        self.assertTrue(lines[2].startswith("- **dec-000003** · "))
        self.assertIn(" · все — Вагон tr-0904:16: и ", lines[2])
        self.assertTrue(
            lines[2].endswith("(связи: tr-0904:16, tr-0907:2x-alt, tr-0904, wl-0007, td-0045, dec-000001, #946, #12)"),
            lines[2],
        )
        self.assertIn(" · {} · раунд 2015-11-19-discuss п. Q2 — Второе".format(characters.name("builder")), lines[3])
        rc, out, _ = self.team("decisions", "--md", "--grep", "импорт")
        self.assertTrue(out.startswith("## Решения владельца (1 из 3, по «импорт»: найдено 1)\n"))
        rc, out, _ = self.team("decisions", "--grep", "нет такого")
        self.assertIn("(пусто)", out)

    def test_it_only_reads(self):
        before = tree_hash(self.t.dir)
        for argv in ([], ["--json"], ["--grep", "импорт", "--limit", "1", "--json"]):
            self.assertEqual(self.team("decisions", *argv)[0], 0)
        self.assertEqual(tree_hash(self.t.dir), before)


class NoDecisionsTest(Base):
    def test_a_depot_without_decisions(self):
        rc, out, err = self.team("decisions", "--json")
        self.assertEqual(rc, 0, err)
        self.assertEqual(
            json.loads(out), {"v": 1, "total": 0, "found": 0, "count": 0, "grep": None, "limit": None, "decisions": []}
        )
        self.assertIn("(пусто)", self.team("decisions")[1])
