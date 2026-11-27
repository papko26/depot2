"""The demo depot (examples/demo/make-demo.sh) builds and reads back: its board, trains, stack and queues; its command
line speaks English by default and Russian on request.
"""

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import helpers

ROOT = str(Path(helpers.HERE).parent)
SCRIPT = str(Path(ROOT, "examples", "demo", "make-demo.sh"))
MAIN = str(Path(ROOT, "depot2", "__main__.py"))
CYRILLIC = re.compile("[\u0400-\u04ff]")
VIEWS = (
    "board",
    "board --md",
    "tracks",
    "track tr-0001",
    "trains",
    "trains next",
    "stack",
    "stack --md",
    "tstack",
    "ballots",
    "wishes",
    "think",
    "todo",
    "queues",
    "principles",
    "principles checks",
    "decisions",
    "agents",
    "merge",
    "ledger",
    "show builder",
    "car show tr-0001:1",
    "priority tr-0001:1",
    "claims",
    "orders",
    "brainstorm list",
    "backup --status",
    "--help",
    "stack --help",
)


class DemoTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp(prefix="depot2-demo-test-")
        cls.state = str(Path(cls.tmp, "demo"))
        p = subprocess.run(["sh", SCRIPT, cls.state], capture_output=True, text=True)
        if p.returncode != 0:
            raise AssertionError(f"make-demo.sh failed: {p.stdout} {p.stderr}")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def depot(self, *args, lang="ru"):
        env = dict(os.environ, DEPOT2_BACKUP="off")
        if lang:
            env["DEPOT2_LANG"] = lang
        else:
            env.pop("DEPOT2_LANG", None)
        p = subprocess.run(
            [sys.executable, MAIN, "--state", self.state, *list(args)], capture_output=True, text=True, env=env
        )
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    def test_english_by_default_and_russian_on_request(self):
        for view in VIEWS:
            out = self.depot(*view.split(), lang="en")
            self.assertIsNone(CYRILLIC.search(out), f"{view} printed Russian in English: {out}")
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
        self.assertEqual(titles, ["Search", "Accounts"])
        trains = json.loads(self.depot("trains", "--json"))
        self.assertEqual(sorted(t["name"] for t in trains["trains"]), ["accounts", "search-v1"])

    def test_the_board_and_the_stack(self):
        board = self.depot("board")
        self.assertIn("Search: the index and the query parser", board)
        self.assertIn("Fix the flaky date test", self.depot("stack"))

    def test_the_queues_principles_and_decisions(self):
        self.assertIn("Dark mode for the reader", self.depot("wishes"))
        self.assertIn("Should search be offline-first?", self.depot("think"))
        self.assertIn("debug level", self.depot("todo"))
        self.assertIn("The answer first", self.depot("principles"))
        self.assertIn("50 MB", self.depot("decisions"))

    def test_the_project_config_is_the_demos(self):
        cfg = json.loads(self.depot("config"))
        self.assertEqual(
            (cfg["project"]["name"], cfg["repo"], cfg["stack"]["slots"]), ("Shelfmark", "example-org/shelfmark", 2)
        )


if __name__ == "__main__":
    unittest.main()
