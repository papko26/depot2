"""The demo depot (examples/demo/make-demo.sh) builds and reads back: its board, trains, stack and queues; its command
line speaks English by default and Russian on request."""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

import helpers  # noqa: F401

ROOT = os.path.dirname(helpers.HERE)
SCRIPT = os.path.join(ROOT, "examples", "demo", "make-demo.sh")
MAIN = os.path.join(ROOT, "depot2", "__main__.py")
CYRILLIC = re.compile("[\u0400-\u04ff]")
VIEWS = ("board", "board --md", "tracks", "track tr-0001", "trains", "trains next", "stack", "stack --md", "tstack",
         "ballots", "queues", "principles", "principles checks", "decisions", "agents",
         "merge", "ledger", "show builder", "car show tr-0001:1", "priority tr-0001:1", "claims", "orders",
         "brainstorm list", "backup --status", "artifacts", "artifacts --car tr-0001:1", "rules", "--help", "stack --help")


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="depot2-demo-test-")
        cls.state = os.path.join(cls.tmp, "demo")
        p = subprocess.run(["sh", SCRIPT, cls.state], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           universal_newlines=True)
        if p.returncode != 0:
            raise AssertionError("make-demo.sh failed: %s %s" % (p.stdout, p.stderr))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def depot(self, *args, lang="ru"):
        env = dict(os.environ, DEPOT2_BACKUP="off")
        if lang:
            env["DEPOT2_LANG"] = lang
        else:
            env.pop("DEPOT2_LANG", None)
        p = subprocess.run([sys.executable, MAIN, "--state", self.state] + list(args), stdout=subprocess.PIPE,
                           stderr=subprocess.PIPE, universal_newlines=True, env=env)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_english_by_default_and_russian_on_request(self):
        for view in VIEWS:
            out = self.depot(*view.split(), lang="en")
            self.assertIsNone(CYRILLIC.search(out), "%s printed Russian in English: %s" % (view, out))
        self.assertIn("## Stack", self.depot("stack", "--md", lang=None))  # no $DEPOT2_LANG, no `language`: English
        self.assertIn("## Стек", self.depot("stack", "--md", lang="ru"))
        self.assertIn("## Стек", self.depot("--set", "language=ru", "stack", "--md", lang=None))  # the config's key
        self.assertIn("## Stack", self.depot("--set", "language=ru", "stack", "--md", lang="en"))  # the env beats it
        self.assertIn("| S1 | slot |", self.depot("stack", "--md", lang="en"))
        self.assertIn("| С1 | слот |", self.depot("stack", "--md", lang="ru"))
        self.assertIn("build", self.depot("board", lang="en"))
        self.assertIn("сборка", self.depot("board", lang="ru"))

    def test_the_tracks_and_the_trains(self):
        tracks = json.loads(self.depot("tracks", "--json"))
        titles = [t["title"] for t in tracks["tracks"]]
        self.assertEqual(titles, ["Search", "Accounts", "todo"])
        trains = json.loads(self.depot("trains", "--json"))
        self.assertEqual(sorted(t["name"] for t in trains["trains"]), ["accounts", "search-v1"])

    def test_the_board_and_the_stack(self):
        board = self.depot("board")
        self.assertIn("Search: the index and the query parser", board)
        self.assertIn("Fix the flaky date test", self.depot("stack"))

    def test_the_queues_principles_and_decisions(self):
        self.assertIn("debug level", self.depot("track", "tr-0003"))
        self.assertIn("Dark mode for the reader", self.depot("track", "tr-0002"))
        self.assertIn("Should search be offline-first?", self.depot("car", "show", "tr-0001:0"))
        self.assertIn("| Tracks | 3 open of 3 |", self.depot("queues", lang="en"))
        self.assertIn("The answer first", self.depot("principles"))
        self.assertIn("50 MB", self.depot("decisions"))

    def test_the_cars_files_and_the_rules(self):
        out = self.depot("artifacts", lang="en")
        self.assertIn("| tr-0001-1 | tr-0001:1 Search: the index and the query parser |", out)
        self.assertIn("| spike-cover-cache | no car |", out)
        self.assertIn("| sketch-tr-0001-0 | tr-0001:0 Search: a design note on ranking |", out)
        self.assertIn("bench/query-times.csv", self.depot("artifacts", "--car", "tr-0001:1", lang="en"))
        self.assertIn("| The cars' files | folders: 3 |", self.depot("queues", lang="en"))
        j = json.loads(self.depot("rules", "--json"))
        self.assertEqual([(f["title"], f["shown"]) for f in j["files"]], [("Rules for the builders of Shelfmark", True)])

    def test_the_project_config_is_the_demos(self):
        cfg = json.loads(self.depot("config"))
        self.assertEqual((cfg["project"]["name"], cfg["repo"], cfg["stack"]["slots"]),
                         ("Shelfmark", "example-org/shelfmark", 2))


if __name__ == "__main__":
    unittest.main()
