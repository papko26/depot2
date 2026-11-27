"""The transcripts are small invented fixtures (tests/transcripts/)."""

import datetime
import json
import os
import shutil
import tempfile
from unittest import mock

from helpers import BRIEF_TEXT, GATES_ALL
from test_extras import Base

from depot2 import effort
from depot2.agents import Agents, AgentError, parse_usage, read_transcript, find_transcript
from depot2.executors import cli_agent
from depot2.executors.cli_agent import action_arg
from depot2.stack import Stack
from depot2.store import read_json, read_jsonl
from depot2.tracks import Tracks

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "transcripts")
DONE = "a0f1c2d3e4b5a6978"   # finished: 4 turns (one message logged twice), 0.1525 MTok
LIVE = "b1e2d3c4f5a6b7c80"   # still running: 2 turns, 0.0265 MTok
SUB = os.path.join("-work-depot", "sess-0001", "subagents")
AT = datetime.datetime(2015, 11, 20, 12, 3, 30, tzinfo=datetime.timezone.utc)  # 61 min after LIVE's last line


class AgentsBase(Base):
    def setUp(self):
        super(AgentsBase, self).setUp()
        self.t.cfg["stack"].update(slots=2, queue_max=10)
        self.projects = tempfile.mkdtemp(prefix="team-agents-")
        shutil.copytree(os.path.join(FIXTURES, "-work-depot"), os.path.join(self.projects, "-work-depot"))
        self.t.cfg["agents"]["projects"] = self.projects
        self.brief = os.path.join(self.t.dir, "brief.md")
        with open(self.brief, "w") as f:
            f.write(BRIEF_TEXT)
        tr = Tracks(self.t.state, self.t.cfg)
        tr.add("Депо")
        tr.car_add("tr-0001", "тележка", stage="bogie")                   # 0
        tr.car_add("tr-0001", "эскиз", brief=self.brief, size="M")
        tr.car_add("tr-0001", "второй эскиз", brief=self.brief)          # 2
        tr.car_add("tr-0001", "без брифа", stage="sketch")               # 3

    def tearDown(self):
        super(AgentsBase, self).tearDown()
        shutil.rmtree(self.projects, ignore_errors=True)

    def team(self, *argv):
        return super(AgentsBase, self).team("--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent",
                                            "--set", "agents.projects=" + self.projects, "--set", "stack.slots=2",
                                            *argv)

    def path(self, aid):
        return os.path.join(self.projects, SUB, "agent-%s.jsonl" % aid)

    def car(self, ref):
        return Tracks(self.t.state, self.t.cfg).car(ref)[1]

    def agents(self):
        return Agents(self.t.state, self.t.cfg)

    def start(self, ref, role, *more):
        rc, out, err = self.team("agents", "start", ref, "--role", role, "--usage", "0.5", *more)
        self.assertEqual(rc, 0, err)
        return out.splitlines()[0]


class StartGateTest(AgentsBase):
    def test_the_role_the_window_and_one_live_agent_per_car(self):
        rc, _, err = self.team("agents", "start", "tr-0001:0", "--role", "plan", "--usage", "0.5")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: tr-0001:0 → «агент: проект» не пройдены: роль и этап — роль «проект»: вагон на этапе "
                      "«тележка», а роль берёт этапы: «эскиз», «проект»", err)
        self.assertIn("depot2 agents start tr-0001:0 --role plan --force --why «…»", err)
        rc, _, err = self.team("agents", "start", "tr-0001:1", "--role", "mergefix", "--usage", "0.5")
        self.assertIn("роль «починка к мерджу»: вагон на этапе «эскиз», а роль берёт этапы: «приёмка», «на путях»", err)
        self.assertFalse(os.path.exists(self.t.state.path("agents.json")))  # nothing written by a refusal
        # the usage window: no reading, no pass; at or over usage.stop_at, no pass
        rc, _, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch")
        self.assertEqual(rc, 1)
        self.assertIn("окно расхода — нет показания окна расхода: --usage X (доля или %), стоп 90 % (usage.stop_at)",
                      err)
        rc, _, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch", "--usage", "99.5%")
        self.assertIn("окно расхода — окно 99.5 % ≥ стоп 90 % (usage.stop_at)", err)
        rc, _, err = self.team("--set", "usage.stop_at=0.9", "agents", "start", "tr-0001:0", "--role", "sketch",
                               "--usage", "0.95")
        self.assertIn("окно 95 % ≥ стоп 90 %", err)
        # through: the id alone on the first line, the record
        rc, out, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch", "--usage", "62", "--by", "lead")
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "ag-0001")
        self.assertIn("depot2 agents attach ag-0001 <run id>", out)
        ag = self.agents().get("ag-0001")
        self.assertEqual((ag["car"], ag["role"], ag["by"], ag["state"], ag["usage"], ag["stop_at"]),
                         ("tr-0001:0", "sketch", "lead", "live", 0.62, 0.9))
        self.assertTrue(ag["started"])
        # a second agent on the car: refused while the first is live, with the command that ends it
        rc, _, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch", "--usage", "0.5")
        self.assertEqual(rc, 1)
        self.assertIn("агент на вагоне — ag-0001 (эскиз) уже на вагоне: не привязан (depot2 agents attach ag-0001 "
                      "<run id>); закончить — depot2 agents stop ag-0001 --why «…»", err)
        rc, _, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch", "--usage", "0.5", "--force")
        self.assertIn("--force — только с причиной", err)
        rc, out, err = self.team("agents", "start", "tr-0001:0", "--role", "sketch", "--force", "--why",
                                 "второй взгляд на эскиз")
        self.assertEqual(rc, 0, err)
        self.assertIn("ag-0002: ворота пройдены с --force (второй взгляд на эскиз): окно расхода — нет показания", out)
        ag = self.agents().get("ag-0002")
        self.assertTrue(ag["forced"])
        self.assertEqual(sorted(ag["gates"]), ["agent", "window"])
        hist = read_jsonl(self.t.state.path("agents-history.jsonl"))
        self.assertEqual([(h["event"], h["agent"]) for h in hist], [("start", "ag-0001"), ("start", "ag-0002")])
        self.assertTrue(hist[1]["forced"])
        # a car by its stack object: an object that is no car's is refused
        self.team("stack", "add", "--title", "не вагон")
        rc, _, err = self.team("agents", "start", "st-0001", "--role", "sketch", "--usage", "0.5")
        self.assertIn("st-0001 is no track's car", err)

    def test_usage_reads_a_share_or_percent(self):
        self.assertEqual(parse_usage("0.62"), 0.62)
        self.assertEqual(parse_usage("62"), 0.62)
        self.assertEqual(parse_usage("62%"), 0.62)
        self.assertEqual(parse_usage("1"), 1.0)
        for bad in ("lots", "150", "-3"):
            with self.assertRaises(AgentError):
                parse_usage(bad)
        rc, _, err = self.team("--set", "usage.stop_at=0", "config")
        self.assertIn("usage.stop_at is a share of the usage window", err)


class BuildTest(AgentsBase):
    def test_a_build_goes_into_a_slot_through_the_dor(self):
        dor = ["--set", "gates.dor=" + json.dumps(GATES_ALL["dor"])]
        # no brief: the DoR refuses, beside the agents' own gates, in one line; nothing moves
        rc, _, err = self.team(*(dor + ["agents", "start", "tr-0001:3", "--role", "build"]))
        self.assertEqual(rc, 1)
        self.assertIn("окно расхода — нет показания", err)
        self.assertIn("DoR (вход в сборку) — нет брифа", err)
        self.assertIsNone(self.car("tr-0001:3").get("st"))
        # a brief: into a free slot, as `tracks car stack --start`
        rc, out, err = self.team(*(dor + ["agents", "start", "tr-0001:1", "--role", "build", "--usage", "0.4"]))
        self.assertEqual(rc, 0, err)
        self.assertEqual(out.splitlines()[0], "ag-0001")
        self.assertIn("tr-0001:1 → st-0001 in the stack queue", out)
        self.assertIn("tr-0001:1 → в работе (st-0001 в слоте)", out)
        c = self.car("tr-0001:1")
        self.assertEqual((c["status"], c["stage"], c["st"]), ("running", "build", "st-0001"))
        self.assertEqual([o["id"] for o in Stack(self.t.state, self.t.cfg).in_slots()], ["st-0001"])
        self.assertEqual(self.agents().get("ag-0001")["st"], "st-0001")
        rc, out, _ = self.team("agents")
        self.assertIn("| ag-0001 | tr-0001:1 | сборка | оркестратор |", out)
        self.assertIn("| С1 st-0001 |", out)
        # a car in a slot already: a new build agent once the first is done, no second object
        self.team("agents", "stop", "ag-0001", "--why", "упал на тестах")
        self.assertEqual(self.start("tr-0001:1", "build"), "ag-0002")
        self.assertEqual(len(Stack(self.t.state, self.t.cfg).in_slots()), 1)
        # the slots full: the build's limit
        self.team("stack", "add", "--title", "другое")
        self.team("stack", "start", "st-0002")
        rc, _, err = self.team("agents", "start", "tr-0001:2", "--role", "build", "--usage", "0.4")
        self.assertIn("лимит колонки — no free slot (2/2), stack.slots", err)
        self.assertIsNone(self.car("tr-0001:2").get("st"))

    def test_mergefix_takes_a_pr_in_the_acceptance(self):
        self.team("tracks", "car", "stack", "tr-0001:1", "--start")
        self.team("stack", "set", "st-0001", "--pr", "77")
        rc, _, err = self.team("stack", "post", "st-0001", "--ci", "green")
        self.assertEqual(rc, 0, err)
        self.assertEqual(Tracks(self.t.state, self.t.cfg).stage_of(self.car("tr-0001:1")), "acceptance")
        rc, _, err = self.team("agents", "start", "tr-0001:1", "--role", "build", "--usage", "0.4")
        self.assertIn("роль «сборка»: вагон на этапе «приёмка»", err)
        self.assertEqual(self.start("tr-0001:1", "mergefix"), "ag-0001")
        rc, out, _ = self.team("agents")
        self.assertIn("| П1 st-0001 |", out)

    def test_docs_take_the_car_on_the_docs_in_its_slot(self):
        self.start("tr-0001:1", "build")
        rc, _, err = self.team("agents", "start", "tr-0001:1", "--role", "docs", "--usage", "0.5")
        self.assertEqual(rc, 1)
        self.assertIn("ворота: tr-0001:1 → «агент: документация» не пройдены: роль и этап — роль «документация»: вагон "
                      "на этапе «сборка», а роль берёт этап «документация» (он держит слот сборки): depot2 car stage "
                      "tr-0001:1 docs", err)
        self.assertIn("агент на вагоне — ag-0001 (сборка) уже на вагоне", err)
        rc, _, err = self.team("car", "stage", "tr-0001:1", "docs")
        self.assertEqual(rc, 0, err)
        rc, _, err = self.team("agents", "start", "tr-0001:1", "--role", "docs", "--usage", "0.5")
        self.assertEqual(rc, 1)  # the stage fits now; the build agent still live: a second one, refused
        self.assertNotIn("роль и этап", err)
        self.assertIn("агент на вагоне — ag-0001 (сборка) уже на вагоне", err)
        self.team("agents", "attach", "ag-0001", DONE)  # the build is done: its spend on the car
        self.assertEqual(self.start("tr-0001:1", "docs"), "ag-0002")
        ag = self.agents().get("ag-0002")
        self.assertEqual((ag["role"], ag["st"]), ("docs", "st-0001"))
        self.assertEqual([o["id"] for o in Stack(self.t.state, self.t.cfg).in_slots()], ["st-0001"])  # no new slot
        self.assertEqual(Tracks(self.t.state, self.t.cfg).stage_of(self.car("tr-0001:1")), "docs")
        rc, out, _ = self.team("agents")
        self.assertIn("| ag-0002 | tr-0001:1 | документация | оркестратор |", out)
        self.assertIn("| С1 st-0001 |", out)
        # its spend goes on the same car, after the build's
        self.team("agents", "attach", "ag-0002", LIVE)
        rc, out, err = self.team("agents", "stop", "ag-0002", "--why", "доки готовы")
        self.assertIn("ag-0002 остановлен: на вагон tr-0001:1 записано", out)
        self.assertEqual(self.car("tr-0001:1")["spent_mtok"], 0.179)  # 0.1525 + 0.0265
        rows = [h for h in read_jsonl(self.t.state.path("tracks-history.jsonl")) if h["event"] == "agent_spent"]
        self.assertEqual([(h["agent"], h["car"]) for h in rows], [("ag-0001", "1"), ("ag-0002", "1")])
        # a car put on the docs out of a slot (only --force does it): the slot first, then the docs
        self.team("car", "stage", "tr-0001:2", "docs", "--force", "--why", "поправка")
        rc, _, err = self.team("agents", "start", "tr-0001:2", "--role", "docs", "--usage", "0.5")
        self.assertIn("роль «документация»: этап «документация» держит слот сборки, а tr-0001:2 не в слоте; в слот — "
                      "depot2 tracks car stack tr-0001:2 --start, затем depot2 car stage tr-0001:2 docs", err)


class TranscriptTest(AgentsBase):
    def test_the_reading_turns_spend_actions_and_state(self):
        rd = read_transcript(self.path(DONE))
        self.assertEqual(rd["turns"], 4)  # msg_01 is logged twice (thinking, tool_use): one turn, one usage
        self.assertEqual(rd["mtok"], 0.1525)
        self.assertEqual(rd["mtok"], effort.from_transcript(self.path(DONE)))  # the same sum as everywhere
        self.assertTrue(rd["finished"])
        self.assertEqual(rd["actions_n"], 4)
        self.assertEqual([(a["name"], a["arg"]) for a in rd["actions"]],
                         [("Bash", "List the work folder"), ("Read", "/work/notes.md"), ("Agent", "Count the files"),
                          ("SubagentHandback", "")])
        self.assertNotIn("NEVER SHOWN", json.dumps(rd))  # no prompt, no report, no file text
        self.assertEqual(rd["last_at"], "2015-11-20T10:06:01.000Z")
        live = read_transcript(self.path(LIVE), tail=1)
        self.assertEqual((live["turns"], live["mtok"], live["finished"]), (2, 0.0265, False))
        self.assertEqual(live["actions"], [{"at": "2015-11-20T11:02:00.000Z", "name": "Edit", "arg": "/work/app/main.py"}])
        self.assertNotIn("NEVER SHOWN", json.dumps(live))
        self.assertIsNone(read_transcript(os.path.join(self.projects, "none.jsonl")))
        self.assertEqual(action_arg("Bash", {"command": "git status"}), "git status")  # no description: the command
        self.assertEqual(len(action_arg("Bash", {"description": "x" * 200})), 80)
        ags = self.agents()
        ag = {"id": "ag-0009", "car": "tr-0001:1", "role": "build", "state": "live", "transcript": self.path(LIVE)}
        rd = ags.reading(ag)
        self.assertEqual(ags.status(ag, rd, now=AT - datetime.timedelta(minutes=50))[0], "working")
        self.assertEqual(ags.status(ag, rd, now=AT), ("silent", "молчит 1 ч 01 мин"))
        self.assertEqual(ags.status(dict(ag, transcript=None), None)[0], "unattached")
        self.assertEqual(ags.status(ag, None)[0], "lost")

    def test_a_grown_transcript_is_read_on_from_where_it_stopped(self):
        with open(self.path(DONE), "rb") as f:
            lines = f.read().splitlines(keepends=True)
        path = os.path.join(self.projects, "growing.jsonl")
        seeks = []
        real_open = open

        def spy_open(p, *a, **kw):
            f = real_open(p, *a, **kw)
            if p == path:
                real_seek = f.seek
                f.seek = lambda off, *x: (seeks.append(off), real_seek(off, *x))[1]
            return f

        def fresh():
            cli_agent._CACHE.pop(path, None)
            return read_transcript(path)
        with real_open(path, "wb") as f:
            f.write(b"".join(lines[:5]) + lines[5][:40])  # msg_02 (Read) half written
        with mock.patch("builtins.open", spy_open):
            first = read_transcript(path)
            self.assertIs(read_transcript(path), first)  # unchanged: the reading as it was, the file not opened
        self.assertEqual((first["turns"], first["actions_n"]), (1, 1))  # the half line waits
        with real_open(path, "ab") as f:
            f.write(lines[5][40:] + b"".join(lines[6:8]))
        with mock.patch("builtins.open", spy_open):
            grown = read_transcript(path)
        self.assertEqual(seeks, [0, sum(len(x) for x in lines[:5])])  # on from the last whole line
        self.assertEqual(grown, fresh())
        self.assertEqual([x["name"] for x in grown["actions"]], ["Bash", "Read", "Agent"])
        with real_open(path, "ab") as f:
            f.write(b"".join(lines[8:]).rstrip(b"\n"))  # the last line without its end, whole: read
        self.assertEqual(read_transcript(path), fresh())
        self.assertEqual(read_transcript(path)["mtok"], 0.1525)
        with real_open(path, "wb") as f:  # shorter: read anew
            f.write(b"".join(lines[:3]))
        self.assertEqual(read_transcript(path)["actions_n"], 1)
        with real_open(path, "ab") as f:
            f.write(b"".join(lines[3:8]))
        self.assertEqual([x["name"] for x in read_transcript(path, tail=1)["actions"]], ["Agent"])  # another tail: anew

    def test_attach_finds_the_transcript_by_the_agent_id(self):
        self.assertEqual(find_transcript(self.t.cfg, "agent-" + LIVE), self.path(LIVE))
        with self.assertRaises(AgentError):
            find_transcript(self.t.cfg, "../etc")
        self.start("tr-0001:1", "build")
        self.start("tr-0001:2", "sketch")
        rc, _, err = self.team("agents", "attach", "ag-0001", "a0000000000000000")
        self.assertEqual(rc, 1)
        self.assertIn("no transcript agent-a0000000000000000.jsonl under", err)
        rc, out, err = self.team("agents", "attach", "ag-0001", LIVE)
        self.assertEqual(rc, 0, err)
        self.assertIn("ag-0001 → %s (%s): молчит" % (LIVE, self.path(LIVE)), out)
        self.assertIn("ходов 2, потрачено", out)
        ag = self.agents().get("ag-0001")
        self.assertEqual((ag["agent_id"], ag["transcript"]), (LIVE, self.path(LIVE)))
        rc, _, err = self.team("agents", "attach", "ag-0002", LIVE)
        self.assertIn("%s is ag-0001's already" % LIVE, err)
        rc, _, err = self.team("agents", "attach", "ag-0001", DONE)
        self.assertIn("ag-0001 is attached to %s already" % LIVE, err)
        rc, _, err = self.team("agents", "attach", "ag-0001", LIVE)  # the same link again: nothing changes
        self.assertEqual(rc, 0, err)
        # a transcript given by its path
        other = os.path.join(self.projects, "elsewhere.jsonl")
        shutil.copy(self.path(DONE), other)
        rc, out, err = self.team("agents", "attach", "ag-0002", "c0ffee0000000001", "--transcript", other)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.agents().get("ag-0002")["transcript"], other)


class SpendTest(AgentsBase):
    def test_the_spend_is_booked_on_the_car_once(self):
        Tracks(self.t.state, self.t.cfg).car_set("tr-0001:1", spent_mtok=1.0)  # the sketch's spend, by hand before
        self.start("tr-0001:1", "build")
        rc, out, err = self.team("agents", "attach", "ag-0001", DONE)
        self.assertEqual(rc, 0, err)
        self.assertIn("ag-0001 закончил: на вагон tr-0001:1 записано", out)
        c = self.car("tr-0001:1")
        self.assertEqual(c["spent_mtok"], 1.1525)
        self.assertEqual(c["transcript"], self.path(DONE))
        self.assertEqual(Stack(self.t.state, self.t.cfg).get(c["st"])["spent_mtok"], 1.1525)  # its object shows it
        ag = self.agents().get("ag-0001")
        self.assertEqual((ag["state"], ag["booked"]["mtok"], ag["booked"]["turns"]), ("finished", 0.1525, 4))
        for _ in range(2):  # read again: booked once
            rc, out, err = self.team("agents", "--all")
            self.assertEqual(rc, 0, err)
            self.assertNotIn("записано", out)
        self.assertEqual(self.car("tr-0001:1")["spent_mtok"], 1.1525)
        rows = [h for h in read_jsonl(self.t.state.path("tracks-history.jsonl")) if h["event"] == "agent_spent"]
        self.assertEqual([(h["agent"], h["spent_mtok"], h["total_mtok"]) for h in rows], [("ag-0001", 0.1525, 1.1525)])
        self.assertEqual([h["event"] for h in read_jsonl(self.t.state.path("agents-history.jsonl"))],
                         ["start", "attach", "finish"])
        rc, out, _ = self.team("agents")
        self.assertIn("Живых агентов нет", out)
        rc, out, _ = self.team("agents", "--all")
        self.assertIn("| ag-0001 | tr-0001:1 | сборка |", out)
        self.assertIn("закончил", out)
        # a finished agent frees the car for the next one
        self.assertEqual(self.start("tr-0001:1", "build"), "ag-0002")

    def test_a_refused_start_still_books_an_agent_that_finished(self):
        self.t.state.init_git()
        self.start("tr-0001:2", "sketch")
        self.team("agents", "attach", "ag-0001", LIVE)
        with open(os.path.join(self.projects, SUB, "agent-%s.meta.json" % LIVE), "w") as f:
            json.dump({"finished": True}, f)
        rc, out, err = self.team("agents", "start", "tr-0001:2", "--role", "plan")  # no --usage: refused
        self.assertEqual(rc, 1)
        self.assertIn("ag-0001 закончил: на вагон tr-0001:2 записано", out)
        self.assertEqual(self.car("tr-0001:2")["spent_mtok"], 0.0265)
        left = self.t.state.git("status", "--porcelain", "--", "agents.json", "agents-history.jsonl", "tracks.json",
                                "tracks-history.jsonl").stdout.strip()
        self.assertEqual(left, "")  # committed, not left over
        self.assertIn("team: agents: ag-0001 закончил", self.t.state.git("log", "-1", "--format=%s").stdout)

    def test_an_agent_that_finishes_later_is_booked_by_the_next_read(self):
        self.start("tr-0001:2", "sketch")
        self.team("agents", "attach", "ag-0001", LIVE)
        self.assertIsNone(self.car("tr-0001:2").get("spent_mtok"))
        meta = os.path.join(self.projects, SUB, "agent-%s.meta.json" % LIVE)
        with open(meta, "w") as f:
            json.dump({"finished": True}, f)
        rc, out, err = self.team("agents")
        self.assertEqual(rc, 0, err)
        self.assertIn("ag-0001 закончил: на вагон tr-0001:2 записано", out)
        self.assertEqual(self.car("tr-0001:2")["spent_mtok"], 0.0265)

    def test_stop_records_it_and_books_the_spend_so_far(self):
        self.start("tr-0001:2", "plan")
        self.team("agents", "attach", "ag-0001", LIVE)
        with self.assertRaises(SystemExit):  # argparse: --why is required
            self.team("agents", "stop", "ag-0001")
        rc, out, err = self.team("agents", "stop", "ag-0001", "--why", "ушёл не туда")
        self.assertEqual(rc, 0, err)
        self.assertIn("ag-0001 остановлен (ушёл не туда): процесс останавливает оркестратор", out)
        self.assertIn("ag-0001 остановлен: на вагон tr-0001:2 записано", out)
        self.assertEqual(self.car("tr-0001:2")["spent_mtok"], 0.0265)
        ag = self.agents().get("ag-0001")
        self.assertEqual((ag["state"], ag["stopped"]["why"], ag["stopped"]["by"]), ("stopped", "ушёл не туда",
                                                                                     "оркестратор"))
        rc, _, err = self.team("agents", "stop", "ag-0001", "--why", "ещё раз")
        self.assertIn("ag-0001 is остановлен already", err)
        rc, out, _ = self.team("agents", "show", "ag-0001")
        self.assertIn("## ag-0001 — проект, tr-0001:2", out)
        self.assertIn("- состояние: остановлен", out)
        self.assertIn("ходов: 2; потрачено: ", out)
        self.assertIn("(записано на вагон)", out)
        self.assertIn("Последние действия (2 из 2):", out)
        self.assertIn(" Grep: def settle", out)
        self.assertIn(" Edit: /work/app/main.py", out)
        self.assertNotIn("NEVER SHOWN", out)


class JsonTest(AgentsBase):
    def test_agents_json_and_the_stack_json_agent(self):
        self.start("tr-0001:1", "build")
        self.team("agents", "attach", "ag-0001", LIVE)
        rc, out, err = self.team("agents", "--json")
        self.assertEqual(rc, 0, err)
        j = json.loads(out)
        self.assertEqual((j["v"], j["all"], j["silent_min"], j["stop_at"]), (1, False, 20, 0.9))
        self.assertEqual([r["role"] for r in j["roles"]], ["sketch", "plan", "build", "docs", "mergefix"])
        a = j["agents"][0]
        for k in ("id", "car", "role", "role_ru", "by", "started", "state", "state_ru", "state_text", "live", "turns",
                  "spent_mtok", "spent_text", "last_action", "last_at", "actions", "agent_id", "transcript", "slot"):
            self.assertIn(k, a)
        self.assertEqual((a["id"], a["state"], a["turns"], a["spent_mtok"]), ("ag-0001", "silent", 2, 0.0265))
        self.assertEqual(a["slot"], {"object": "st-0001", "where": "slot", "n": "С1"})
        rc, out, err = self.team("agents", "show", "ag-0001", "--json")
        self.assertEqual(json.loads(out)["last_action"]["name"], "Edit")
        rc, out, err = self.team("stack", "--json")
        o = json.loads(out)["in_slots"][0]
        self.assertEqual((o["id"], o["car"]), ("st-0001", "tr-0001:1"))
        self.assertEqual({k: o["agent"][k] for k in ("id", "role", "state", "turns", "spent_mtok")},
                         {"id": "ag-0001", "role": "build", "state": "silent", "turns": 2, "spent_mtok": 0.0265})
        self.assertEqual(o["agent"]["last_action"]["arg"], "/work/app/main.py")
        rc, out, err = self.team("stack", "--md")
        row = [x for x in out.splitlines() if x.startswith("| С1 | слот | st-0001 ")][0]
        self.assertRegex(row, r"\| агент ag-0001 \(сборка\): молчит [^|]*; последнее — Edit: /work/app/main\.py "
                              r"\(\d\d:\d\d\) \|")
        # done: no live agent on the object
        self.team("agents", "stop", "ag-0001", "--why", "готово")
        o = json.loads(self.team("stack", "--json")[1])["in_slots"][0]
        self.assertIsNone(o["agent"])
        self.assertIsNone(read_json(self.t.state.path("stack.json"))["objects"][0].get("agent"))  # never stored
        row = [x for x in self.team("stack", "--md")[1].splitlines() if x.startswith("| С1 | слот | st-0001 ")][0]
        self.assertNotIn("ag-0001", row)  # no live agent: the status as stored
