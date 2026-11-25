"""A depot without a team (the config's `characters` null, the default), in English: a car goes the whole way from
its track to production through the stack, the post-stack and the merge queue; the team's commands answer «no team
configured»; and nothing it prints is Russian."""

import os
import re
import unittest

import helpers
from test_merge import MergeBase

from depot2 import characters, config, i18n
from depot2.stack import Stack
from depot2.store import write_json
from depot2.tracks import Tracks

CYRILLIC = re.compile("[Ѐ-ӿ]")
NO_TEAM = "no team configured — set `characters`"


class NoTeamTest(MergeBase):
    def setUp(self):
        characters.configure(None)  # the seeding of TempState below finds no team
        super().setUp()
        os.environ[i18n.ENV] = "en"
        write_json(os.path.join(self.t.dir, "config.json"), {"characters": None})
        self.t.state.commit("test: no team")
        self.t.cfg = config.load(self.t.dir)
        self.t.cfg["deep_repo"]["clone"] = ""
        self.assertEqual((self.t.cfg["characters"], characters.IDS, i18n.language()), (None, [], "en"))

    def tearDown(self):
        super().tearDown()  # the environment as it was: the suites' Russian
        i18n.configure()
        characters.configure(helpers.TEAM)

    def run_ok(self, *argv):
        rc, out, err = self.team(*argv)
        self.assertEqual(rc, 0, "%s: %s %s" % (argv, out, err))
        self.assertIsNone(CYRILLIC.search(out + err), "%s printed Russian: %s %s" % (argv, out, err))
        return out

    def test_a_car_goes_all_the_way_to_production(self):
        self.run_ok("tracks", "add", "--title", "Search")
        self.run_ok("tracks", "car", "add", "tr-0001", "--title", "Search: the index", "--size", "S")
        self.run_ok("tracks", "car", "stack", "tr-0001:0", "--start")
        obj = Stack(self.t.state, self.t.cfg).in_slots()[0]
        self.assertEqual(obj["stage"], "build")
        self.pr(301, "feat-a", {"a.txt": "A\n"}, stack=False)
        self.run_ok("stack", "set", obj["id"], "--branch", "feat-a", "--pr", "301")
        self.run_ok("stack", "post", obj["id"], "--ci", "green")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get(obj["id"])["stage"], "acceptance")
        self.run_ok("merge", "add", "301", "--word", "301 merge")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get(obj["id"])["stage"], "queued")
        for view in ("board", "stack", "trains", "merge", "tracks", "car show tr-0001:0"):
            self.run_ok(*view.split())
        out = self.run_ok("merge", "run")
        car = Tracks(self.t.state, self.t.cfg).car("tr-0001:0")[1]
        self.assertEqual((car["status"], [(r["from"], r["stage"]) for r in car["stages"]][-2:]),
                         ("merged", [("queued", "rolling"), ("rolling", "arrived")]))
        self.assertIn("tr-0001:0: rolling → arrived", out)
        for view in ("board", "stack", "merge", "car show tr-0001:0", "queues", "principles", "decisions",
                     "backup --status"):
            self.run_ok(*view.split())

    def test_the_team_commands_say_there_is_no_team(self):
        for argv in (("round", "--kind", "discuss", "--input", "x.md", "--dry-run"), ("ledger",),
                     ("payout", "builder", "1", "--reason", "x"), ("reflect",), ("brainstorm", "list"), ("show", "builder"),
                     ("tstack",), ("gender", "builder", "f"), ("orders",)):
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 1, argv)
            self.assertIn(NO_TEAM, err, argv)
            self.assertIsNone(CYRILLIC.search(out + err), argv)
        self.run_ok("decide", "all", "--text", "Keep the index in one file.")  # the owner's decisions need no team


if __name__ == "__main__":
    unittest.main()
