import os
import unittest

from helpers import TempState, slurp

from depot2 import schema
from depot2.persona import Persona
from depot2.store import WriteCheckError, sha256_text


class SchemaTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.cfg = self.t.cfg

    def tearDown(self):
        self.t.close()

    def good(self):
        return {"points": [{"id": "B49", "position": "agree", "stake": 2, "comment": "да"}],
                "main_point": "главное", "inner_thought": "мысль"}

    def test_valid_answer(self):
        s = schema.round1("ballot", ["B49", "B50"], self.cfg)
        self.assertEqual(schema.validate(self.good(), s), [])

    def test_invalid_answers(self):
        s = schema.round1("ballot", ["B49"], self.cfg)
        bad = self.good()
        bad["points"][0]["position"] = "maybe"
        self.assertTrue(any("not in" in e for e in schema.validate(bad, s)))
        bad = self.good()
        bad["points"][0]["stake"] = -1
        self.assertTrue(any("below" in e for e in schema.validate(bad, s)))
        bad = self.good()
        bad["points"][0]["stake"] = True  # a bool is not an integer
        self.assertTrue(schema.validate(bad, s))
        bad = self.good()
        bad["points"][0]["id"] = "B1"
        self.assertTrue(schema.validate(bad, s))
        bad = self.good()
        del bad["inner_thought"]
        self.assertIn("$: missing inner_thought", schema.validate(bad, s))
        bad = self.good()
        bad["extra"] = 1
        self.assertIn("$: unexpected extra", schema.validate(bad, s))
        bad = self.good()
        bad["inner_thought"] = "x" * 301
        self.assertTrue(any("longer" in e for e in schema.validate(bad, s)))
        bad = self.good()
        bad["deep_request"] = {"level": 1, "reason": "больше ходов"}  # the paid pass is gone
        self.assertIn("$: unexpected deep_request", schema.validate(bad, s))

    def test_no_deep_request_in_any_schema(self):
        for kind in ("ballot", "review", "discuss"):
            for pids in ([], ["B49"]):
                self.assertNotIn("deep_request", schema.round1(kind, pids, self.cfg)["properties"], kind)
        self.assertNotIn("deep_request", str(schema.round2(["B49"], self.cfg, ["builder"])))

    def test_kind_fields(self):
        r = schema.round1("review", [], self.cfg)
        self.assertIn("verdict", r["properties"])
        self.assertIn("proposals", r["properties"])
        self.assertNotIn("points", r["properties"])
        b = schema.round1("ballot", ["B49"], self.cfg)
        self.assertNotIn("verdict", b["properties"])
        self.assertNotIn("proposals", b["properties"])

    def test_schema_is_the_same_for_every_character(self):
        # The schema becomes a tool definition, which is part of the cached prefix.
        self.assertEqual(schema.round2(["B49"], self.cfg, ["builder", "painter"]),
                         schema.round2(["B49"], self.cfg, ["builder", "painter"]))


class PortraitTest(unittest.TestCase):
    def setUp(self):
        self.t = TempState()
        self.p = Persona(self.t.state, "painter")

    def tearDown(self):
        self.t.close()

    def test_replace_keeps_every_version(self):
        v1 = self.p.replace_portrait("Я Краски. Первая версия.", "seed", cap=1000)
        v2 = self.p.replace_portrait("Я Краски. Вторая версия.", "reflect", cap=1000, call_id="c2")
        self.assertEqual((v1["version"], v2["version"]), (1, 2))
        self.assertEqual(v2["previous"], 1)
        self.assertEqual(self.p.portrait(), "Я Краски. Вторая версия.\n")
        old = slurp(os.path.join(self.p.dir, v1["path"]))
        self.assertEqual(old, "Я Краски. Первая версия.\n")
        self.assertEqual(len(v1["sha256"]), 64)
        self.assertEqual(v1["sha256"], sha256_text(old))
        self.assertEqual(self.p.verify(), 2)

    def test_history_is_never_overwritten(self):
        self.p.replace_portrait("one", "seed", cap=100)
        with open(os.path.join(self.p.history_dir, "v0002.md"), "w") as f:
            f.write("squatter")
        with self.assertRaises(FileExistsError):
            self.p.replace_portrait("two", "reflect", cap=100)
        self.assertEqual(self.p.portrait(), "one\n")

    def test_a_hand_edited_portrait_is_refused(self):
        self.p.replace_portrait("one", "seed", cap=100)
        with open(self.p.portrait_path, "w") as f:
            f.write("edited outside the tool")
        with self.assertRaises(WriteCheckError):
            self.p.replace_portrait("two", "reflect", cap=100)
        with self.assertRaises(WriteCheckError):
            self.p.verify()

    def test_english_summary_is_versioned_too(self):
        v = self.p.replace_portrait("Я Краски.", "reflect", cap=100, summary_en="Kraski, she loves clean colours.")
        self.assertEqual(self.p.summary_en(), "Kraski, she loves clean colours.\n")
        self.assertEqual(len(v["summary_en_sha256"]), 64)
        self.p.replace_portrait("Я Краски, вторая.", "reflect", cap=100)
        self.assertIsNone(self.p.summary_en())  # the newest version has none
        with open(os.path.join(self.p.dir, v["summary_en_path"]), "w") as f:
            f.write("tampered")
        with self.assertRaises(WriteCheckError):
            self.p.verify()

    def test_cap(self):
        text = ("Первое предложение. " * 30).strip()
        v = self.p.replace_portrait(text, "reflect", cap=100)
        self.assertTrue(v["truncated"])
        self.assertLessEqual(len(self.p.portrait()), 101)
        self.assertTrue(self.p.portrait().rstrip().endswith("."))


if __name__ == "__main__":
    unittest.main()
