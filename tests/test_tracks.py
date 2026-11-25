
import datetime
import json
import os

from test_extras import Base

from depot2.stack import Stack
from depot2.tracks import TrackError, Tracks
from depot2.trains import Trains

PAST = datetime.datetime(2015, 11, 1, 12, 0).astimezone()
FUTURE = datetime.datetime(2015, 11, 9, 12, 0).astimezone()


class TracksBase(Base):

    def setUp(self):
        super(TracksBase, self).setUp()
        self.t.cfg["stack"].update(slots=4, queue_max=10)
        self.t.cfg["usage"]["mtok_per_pct"] = 12.0

    def tracks(self):
        return Tracks(self.t.state, self.t.cfg)

    def trains(self):
        return Trains(self.t.state, self.t.cfg)

    def ride(self, tid, name=None, slots="max 1", **kw):
        tr = self.trains()
        t = tr.tracks.get(tid)
        name = name or "train-%s" % tid[-1]
        tr.add(name, t["title"], cars=["%s:%s" % (tid, c["n"]) for c in t["cars"] if c["status"] not in ("merged",
                                                                                                      "dropped")],
               slots=slots, **kw)
        tr.set_state(name, "impl")
        return name

    def team(self, *argv):  # never the main clone (repo_version), never a real model
        return super(TracksBase, self).team("--set", "deep_repo.clone=", "--set", "executor_bin=/nonexistent",
                                            "--set", "usage.mtok_per_pct=12", *argv)


class TracksTest(TracksBase):
    def test_add_cars_after_and_the_tables(self):
        tr = self.tracks()
        a = tr.add("Поиск")
        tr.car_add(a["id"], "дизайн-док", size="doc", spent_mtok=4.6, status="merged")
        tr.car_add(a["id"], "индекс и фильтры", size="L", pr=189, status="pr", spent_mtok=15)
        tr.car_add(a["id"], "файл синонимов", after=["1"], size="M")
        tr.car_add(a["id"], "пустая выдача", after=["1", "2"], est_mtok=13.2)
        b = tr.add("Читалка", est_mtok="120-156")
        tr.car_add(b["id"], "макет", n="R0", size="M")
        tr.car_add(b["id"], "вёрстка", n="R1", after=["R0"], size="L")
        tr.add("Экспорт")
        self.assertEqual([c["n"] for c in tr.get("tr-0001")["cars"]], ["0", "1", "2", "3"])
        # refs must name something; a car never waits for itself
        with self.assertRaises(TrackError):
            tr.car_add(a["id"], "x", after=["9"])
        with self.assertRaises(TrackError):
            tr.car_set("tr-0001:2", after=["2"])
        with self.assertRaises(TrackError):
            tr.car_add(a["id"], "y", after=["st-0404"])
        with self.assertRaises(TrackError):
            tr.car_add(a["id"], "dup", n="2")
        with self.assertRaises(TrackError):  # a car's bare name is a car of its track, never a train
            tr.car_add(a["id"], "z", after=["export-n-print"])
        md = Tracks(self.t.state, self.t.cfg).md(now=PAST)  # read back from tracks.json
        self.assertIn("| № | Трек | Вагонов (готово / всего) | Effort (ожидаемо / потрачено) | Состояние | Этапы вагонов |",
                      md)
        self.assertIn("| 1 | tr-0001 Поиск | 1 / 4 | ≈ 3.9%UW (46.8 MTok) / 1.63%UW (19.6 MTok) | открыто 3; "
                      "вне поездов 3 |", md)
        self.assertIn("| 2 | tr-0002 Читалка | 0 / 2 | ≈ 10–13%UW (120–156 MTok) / — (по вагонам ≈ 2.4%UW (28.8 MTok)) | "
                      "открыто 2; вне поездов 2 |", md)
        self.assertIn("| 3 | tr-0003 Экспорт | 0 / 0 | — | вагонов нет |", md)
        self.assertIn("1%UW ≈ 12 MTok (usage.mtok_per_pct). Размеры, %UW: S 0.3, doc 0.4, M 1, L 1.4.", md)
        cm = tr.car_md("tr-0001", now=PAST)
        self.assertIn("## Трек tr-0001 Поиск", cm)
        self.assertIn("| Вагон | Приоритет | Что | После | Размер: effort (оценка / факт) | Ветка / PR | Стек | Поезд | Статус |", cm)
        self.assertIn("| 0 | P5 | дизайн-док | — | doc: ≈ 0.4%UW (4.8 MTok) / 0.38%UW (4.6 MTok) | — | — | — | слит |", cm)
        self.assertIn("| 1 | P5 | индекс и фильтры | — | L: ≈ 1.4%UW (16.8 MTok) / 1.25%UW (15 MTok) | PR #189 | — | — | "
                      "PR открыт |", cm)
        self.assertIn("| 2 | P5 | файл синонимов | 1 | M: ≈ 1%UW (12 MTok) / — | — | — | — | ждёт: 1 (PR открыт) |", cm)
        self.assertIn("| 3 | P5 | пустая выдача | 1, 2 | ≈ 1.1%UW (13.2 MTok) / — | — | — | — | ждёт: 1 (PR открыт), 2 "
                      "(ждёт) |", cm)
        events = [h["event"] for h in tr.history()]
        self.assertEqual(events.count("add"), 3)
        self.assertEqual(events.count("car_add"), 6)
        self.assertEqual({h["track"] for h in tr.history() if h["event"] == "add"}, {"tr-0001", "tr-0002", "tr-0003"})
        # a track is done when it has cars and every one is closed; a ref to it waits until then
        self.assertEqual(tr.ref_state("tr-0001"), (False, "tr-0001 Поиск (открыто вагонов: 3)"))
        self.assertEqual(tr.ref_state("tr-0003")[0], False)  # no cars: not done
        for n in ("1", "2", "3"):
            tr.car_set("tr-0001:%s" % n, status="dropped" if n == "3" else "merged")
        self.assertEqual(tr.ref_state("tr-0001"), (True, "tr-0001 Поиск (пройден)"))
        self.assertNotIn("tr-0001 Поиск", Tracks(self.t.state, self.t.cfg).md(now=PAST))  # done: only with --all
        self.assertIn("Пройденных треков не показано: 1", Tracks(self.t.state, self.t.cfg).md(now=PAST))
        self.assertIn("| tr-0001 Поиск | 3 / 3 (+1 снят) |", Tracks(self.t.state, self.t.cfg).md(include_done=True))

    def test_the_effort_map_is_the_config(self):
        self.t.cfg["usage"]["sizes"]["M"] = 1.2
        tr = self.tracks()
        a = tr.add("Т")
        tr.car_add(a["id"], "x", size="M")
        tr.car_add(a["id"], "y", size="S", spent_mtok=3.0, status="merged")
        tr.car_set("tr-0001:0", spent_mtok=15.0)
        self.assertEqual(tr.effort(tr.get(a["id"]))[:2], (18.0, 18.0))  # 14.4 + 3.6 expected; 15 + 3 spent
        with self.assertRaises(ValueError):
            tr.car_add(a["id"], "z", size="XXL")
        # A new calibration: a stored spend keeps its MTok (its % moves); a size keeps its % (its MTok moves).
        self.t.cfg["usage"]["mtok_per_pct"] = 6.0
        self.assertEqual(tr.effort_cell(tr.get(a["id"])), "≈ 1.5%UW (9 MTok) / 3%UW (18 MTok)")
        rc, out, err = self.team("--set", "usage.sizes.M=0.7", "--set", "usage.mtok_per_pct=10", "config")
        self.assertEqual((json.loads(out)["usage"]["sizes"]["M"], json.loads(out)["usage"]["mtok_per_pct"]), (0.7, 10))
        self.assertEqual(self.team("--set", "usage.mtok_per_pct=0", "tracks")[0], 1)


class StackLinkTest(TracksBase):
    def setUp(self):
        super(StackLinkTest, self).setUp()
        tr = self.tracks()
        a = tr.add("Поиск")
        tr.car_add(a["id"], "индекс", size="L", branch="search-index")
        tr.car_add(a["id"], "фильтры", size="M")
        tr.add("Ждёт")
        self.ride("tr-0001", "poisk", slots="max 1")

    def test_stack_start_set_and_done_move_the_car(self):
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:0", "--start")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0 → st-0001 in the stack queue", out)
        self.assertIn("tr-0001:0 → в работе (st-0001 в слоте)", out)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["where"], o["title"], o["branch"]), ("slot", "tr-0001:0 Поиск — индекс", "search-index"))
        # max 1: the second car does not start while the first holds the train's slot
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:1", "--start")
        self.assertEqual(rc, 1)
        self.assertIn("поезд poisk держит слотов уже 1 из 1 (0)", err)
        self.assertEqual(self.team("trains", "done", "poisk")[0], 1)  # a car is in work
        rc, out, err = self.team("stack", "set", "st-0001", "--pr", "201", "--status", "PR открыт, CI")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0 → PR #201", out)
        car = self.tracks().car("tr-0001:0")[1]
        self.assertEqual((car["status"], car["pr"]), ("pr", 201))
        rc, out, err = self.team("stack", "done", "st-0001", "--reason", "merged #201")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 is the object of tr-0001:0 (PR открыт): say what the car becomes: --car merged", err)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "pr")
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["where"], "slot")
        rc, out, err = self.team("stack", "done", "st-0001", "--reason", "merged #201", "--car", "merged")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:0 → слит (merged #201)", out)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "merged")
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--spent-mtok", "16.2")
        self.assertIn("| 0 | P5 | индекс | — | L: ≈ 1.4%UW (16.8 MTok) / 1.35%UW (16.2 MTok) | search-index / PR #201 | "
                      "st-0001 | poisk | слит |", out)  # its train keeps it in its list
        # The second car: into the queue first, then `stack start`; dropped by its flag. The last car closed: the
        # train arrives by itself.
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:1")
        self.assertEqual(rc, 0, err)
        self.assertIn("готов (в очереди стека)", out)
        self.assertIn("`depot2 stack start st-0002`", self.team("trains", "next")[1])
        self.assertIn("tr-0001:1 → в работе", self.team("stack", "start", "st-0002")[1])
        rc, out, err = self.team("stack", "done", "st-0002", "--reason", "dropped: not needed", "--car", "dropped")
        self.assertIn("tr-0001:1 → снят", out)
        self.assertIn("поезд poisk → «прибыл» (последний вагон tr-0001:1 закрыт)", out)
        self.assertEqual(self.trains().get("poisk")["state"], "arrived")
        # Only the car's link: a stack object that is no car changes no track.
        self.team("stack", "add", "--title", "просто задача")
        rc, out, _ = self.team("stack", "start", "st-0003")
        self.assertNotIn("tr-", out.split("## Стек")[0])
        # --car overrides the reason; a car of no train goes into a slot by hand
        tr = self.tracks()
        b = tr.add("Ещё")
        tr.car_add(b["id"], "x", size="S")
        self.assertEqual(self.team("tracks", "car", "stack", "tr-0003:0", "--start")[0], 0)
        rc, out, _ = self.team("stack", "done", "st-0004", "--reason", "closed, superseded by #300", "--car", "merged")
        self.assertIn("tr-0003:0 → слит", out)

    def test_a_car_whose_object_is_in_the_stack_moves_with_it(self):
        self.assertEqual(self.team("tracks", "car", "stack", "tr-0001:0", "--start")[0], 0)
        self.assertEqual(self.team("tracks", "car", "stack", "tr-0001:1")[0], 0)
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--status", "merged")
        self.assertEqual(rc, 1)
        self.assertIn("error: tr-0001:0: its stack object st-0001 is in the stack, the status goes with it: depot2 "
                      "stack done st-0001 --car merged|dropped|back --reason «…», stack start st-0001 or stack park "
                      "st-0001 --reason «…»", err)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["status"], "running")
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:0", "--status", "running", "--note", "x")[0], 0)
        # its PR and branch set on the car are its object's too (`stack post` reads the object's PR first)
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:0", "--pr", "601", "--branch", "b601")[0], 0)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["pr"], o["branch"]), (601, "b601"))
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--st", "st-0002")
        self.assertEqual(rc, 1)
        self.assertIn("tr-0001:0 is linked to st-0001, which is in the stack: done with it first", err)
        self.assertEqual(self.team("stack", "done", "st-0001", "--reason", "слит #201", "--car", "merged")[0], 0)
        tr = self.tracks()
        other = tr.car_add(tr.add("Ещё")["id"], "x", size="S")
        ref = "%s:%s" % (other[0]["id"], other[1]["n"])
        rc, out, err = self.team("tracks", "car", "set", ref, "--st", "st-0001")
        self.assertEqual(rc, 1)
        self.assertIn("st-0001 is or was the stack object of tr-0001:0: a car links only to an object of its own", err)
        # a merged car whose object stayed in the queue (the API's write, as the hand's was before): no start, no post
        self.tracks().car_set("tr-0001:1", status="merged")
        finished = self.tracks().car("tr-0001:1")[1]["finished"]
        for act in (("start", "st-0002"), ("post", "st-0002", "--reason", "CI")):
            rc, out, err = self.team("stack", *act)
            self.assertEqual(rc, 1, act)
            self.assertIn("st-0002 is the object of tr-0001:1, which is слит: no stack %s — the object is left over, "
                          "close it: depot2 stack done st-0002 --car merged --reason «…»" % act[0], err)
        self.assertEqual(self.tracks().car("tr-0001:1")[1]["status"], "merged")
        self.assertEqual(self.team("stack", "done", "st-0002", "--reason", "остался", "--car", "merged")[0], 0)
        self.assertEqual(self.tracks().car("tr-0001:1")[1]["finished"], finished)  # its day kept
        self.assertIn("picks", json.loads(self.team("trains", "--json", "next")[1]))  # the parent's --json too

    def test_the_spend_from_a_transcript_and_on_both_sides_of_the_link(self):
        path = os.path.join(self.out, "agent.output")
        lines = [
            {"type": "assistant", "message": {"id": "msg_a", "usage": {
                "input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 1000000,
                "output_tokens": 500}}},
            {"type": "assistant", "message": {"id": "msg_a", "usage": {  # the same message, a second block
                "input_tokens": 1000, "cache_creation_input_tokens": 2000, "cache_read_input_tokens": 1000000,
                "output_tokens": 500}}},
            {"type": "user", "message": {"content": "no usage here"}},
            {"type": "assistant", "message": {"id": "msg_b", "usage": {"cache_read_input_tokens": 2000000,
                                                                          "output_tokens": 1000}}}]
        with open(path, "w") as f:
            f.write("\n".join(json.dumps(x) for x in lines) + "\nnot json\n")
        # 1000 + 2000 × 1.25 + 1e6 × 0.1 + 500 × 5, then 2e6 × 0.1 + 1000 × 5
        from depot2.effort import from_transcript
        self.assertEqual(from_transcript(path), 0.311)
        self.team("tracks", "car", "stack", "tr-0001:0", "--start")
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--transcript", path)
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["spent_mtok"], 0.311)
        self.assertEqual(Stack(self.t.state, self.t.cfg).get("st-0001")["spent_mtok"], 0.311)  # its stack object too
        rc, out, err = self.team("stack", "set", "st-0001", "--spent-mtok", "18")
        self.assertIn("tr-0001:0: потрачено 1.5%UW (18 MTok)", out)
        self.assertEqual(self.tracks().car("tr-0001:0")[1]["spent_mtok"], 18.0)
        self.assertIn("| ≈ 1.4%UW (16.8 MTok) / 1.5%UW (18 MTok) |", out)  # the stack's effort column
        self.assertEqual(self.team("stack", "set", "st-0001", "--spent-mtok", "1", "--transcript", path)[0], 1)

    def test_a_train_in_design_and_a_full_stack(self):
        tr = self.tracks()
        tr.car_add("tr-0002", "рано", size="S")
        self.trains().add("pozzhe", "Позже", cars=["tr-0002:0"])
        with self.assertRaises(ValueError) as e:
            self.tracks().car_stack("tr-0002:0", start=True)
        self.assertIn("поезд pozzhe — «дизайн»: его вагоны берут слот только в «имплементации»", str(e.exception))
        self.t.cfg["stack"]["slots"] = 0
        with self.assertRaises(TrackError) as e:  # the slots: a soft gate (gates.py), refused before anything is written
            self.tracks().car_stack("tr-0001:0", start=True)
        self.assertIn("no free slot (0/0)", str(e.exception))
        self.assertEqual(Stack(self.t.state, self.t.cfg).objects(), [])  # nothing added on a refusal
        # a full queue does not refuse a car that goes straight into a slot
        self.t.cfg["stack"].update(slots=4, queue_max=1)
        Stack(self.t.state, self.t.cfg).add("заняла очередь")
        obj, _ = self.tracks().car_stack("tr-0001:0", start=True)
        self.assertEqual(obj["where"], "slot")

    def test_a_lock_on_a_cars_stack_object_holds_it(self):
        tr = self.tracks()
        tr.car_stack("tr-0001:0")
        Stack(self.t.state, self.t.cfg).lock("st-0001", "keeper", "замер выдачи", "дорого", "r", "tx-000001", 10)
        self.assertEqual([p["car"]["n"] for p in self.trains().next(PAST)["picks"]], ["1"])  # car 0 is held, 1 goes
        self.assertIn("ждёт: замок lk-0001 (замер выдачи)", self.tracks().car_md("tr-0001", now=PAST))
        with self.assertRaises(TrackError):
            self.tracks().car_stack("tr-0001:0", start=True)

    def test_the_cli_tables(self):
        rc, out, err = self.team("tracks")
        self.assertEqual(rc, 0, err)
        self.assertIn("## Треки", out)
        self.assertIn("| 1 | tr-0001 Поиск | 0 / 2 |", out)
        self.assertIn("в поездах: poisk", out)
        self.assertEqual(self.team("tracks", "--md")[1], out)
        rc, out, _ = self.team("track", "tr-0001", "--md")
        self.assertIn("## Трек tr-0001 Поиск", out)
        self.assertIn("| 0 | P5 | индекс | — | L: ≈ 1.4%UW (16.8 MTok) / — | search-index | — | poisk | готов |", out)
        data = json.loads(self.team("tracks", "--json")[1])
        self.assertEqual([c["ref"] for c in data["tracks"][0]["cars"]], ["tr-0001:0", "tr-0001:1"])
        self.assertEqual(data["tracks"][0]["cars"][0]["train"], "poisk")
        self.assertEqual(json.loads(self.team("track", "tr-0001", "--json")[1])["id"], "tr-0001")
        rc, out, err = self.team("tracks", "add", "--title", "Новый", "--at", "1")
        self.assertEqual(rc, 0, err)
        self.assertEqual([t["id"] for t in self.tracks().tracks()], ["tr-0003", "tr-0001", "tr-0002"])
        self.assertEqual(self.team("tracks", "set", "tr-0003", "--at", "3", "--note", "в конец")[0], 0)
        self.assertEqual([t["id"] for t in self.tracks().tracks()], ["tr-0001", "tr-0002", "tr-0003"])
        self.assertEqual(self.team("tracks", "car", "add", "tr-0003", "--title", "a", "--after", "nope")[0], 1)
        self.assertEqual(self.team("tracks", "car", "add", "tr-0003", "--title", "a", "--size", "S")[0], 0)
        self.assertEqual(self.team("tracks", "car", "add", "tr-0003", "--title", "b", "--size", "S", "--at", "1")[0], 0)
        self.assertEqual([c["title"] for c in self.tracks().get("tr-0003")["cars"]], ["b", "a"])
        self.assertEqual(self.team("tracks", "car", "move", "tr-0003:1", "--at", "2")[0], 0)
        self.assertEqual([c["title"] for c in self.tracks().get("tr-0003")["cars"]], ["a", "b"])


class ImportTest(TracksBase):
    SPEC = {"tracks": [
        {"title": "Поиск", "notes": ["бывший st-0006"],
         "cars": [{"n": "0", "title": "дизайн-док", "size": "doc", "spent_mtok": 4.6, "status": "merged"},
                  {"n": "1", "title": "индекс и фильтры", "size": "L", "spent_mtok": 15, "status": "pr", "pr": 189},
                  {"n": "2", "title": "файл синонимов", "after": ["1"], "size": "M"}]},
        {"title": "Читалка", "est_pct": "10-13",
         "cars": [{"n": "R0", "title": "макет", "size": "M"}, {"n": "R1", "title": "вёрстка", "after": ["R0"]}]},
        {"title": "Экспорт", "cars": [{"title": "док", "after": ["tr-0002:R1"]}]}]}

    def write(self, spec):
        path = os.path.join(self.out, "tracks-import.json")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(spec, f, ensure_ascii=False)
        return path

    def test_dry_run_then_the_import_once(self):
        path = self.write(self.SPEC)
        rc, out, err = self.team("tracks", "import", path, "--dry-run")
        self.assertEqual(rc, 0, err)
        self.assertIn("DRY RUN: would import 3 tracks, 6 cars", out)
        self.assertIn("## Трек tr-0002 Читалка", out)
        self.assertFalse(os.path.exists(os.path.join(self.t.dir, "tracks.json")))
        rc, out, err = self.team("tracks", "import", path)
        self.assertEqual(rc, 0, err)
        tr = self.tracks()
        self.assertEqual([t["title"] for t in tr.tracks()], ["Поиск", "Читалка", "Экспорт"])
        self.assertEqual(tr.get("tr-0001")["notes"], ["бывший st-0006"])
        self.assertEqual(tr.get("tr-0002")["est_mtok"], [120.0, 156.0])  # stored in MTok
        self.assertEqual(tr.car("tr-0001:1")[1]["spent_mtok"], 15.0)
        self.assertEqual(tr.history()[-1]["event"], "import")
        self.assertEqual(tr.history()[-1]["tracks"], ["tr-0001", "tr-0002", "tr-0003"])
        rc, _, err = self.team("tracks", "import", path)
        self.assertEqual(rc, 1)
        self.assertIn("the import is one-off", err)

    def test_a_bad_ref_writes_nothing(self):
        spec = json.loads(json.dumps(self.SPEC))
        spec["tracks"][1]["cars"][1]["after"] = ["R9"]
        rc, _, err = self.team("tracks", "import", self.write(spec))
        self.assertEqual(rc, 1)
        self.assertIn("tr-0002 Читалка: tr-0002 has no car R9", err)
        self.assertFalse(os.path.exists(os.path.join(self.t.dir, "tracks.json")))
        spec = json.loads(json.dumps(self.SPEC))
        spec["tracks"][0]["status"] = "running"  # a planning key of before is refused, never dropped unseen
        rc, _, err = self.team("tracks", "import", self.write(spec))
        self.assertEqual(rc, 1)
        self.assertIn("unknown key status", err)


class ParkCarTest(TracksBase):
    """`stack park` of a car's object: the object leaves the stack, the car waits on its track with its
    PR (status pr), its slot is free; when its `after` is met its PR is ready to merge."""

    def setUp(self):
        super(ParkCarTest, self).setUp()
        tr = self.tracks()
        a = tr.add("Аккаунты")
        tr.car_add(a["id"], "док v2", n="0b", size="doc")
        tr.car_add(a["id"], "пароли", n="M", after=["0b"], size="M", branch="passwords-0.1")
        tr.car_add(a["id"], "book-cards", n="1", after=["M"], size="L")
        tr.car_set("tr-0001:0b", status="ready")
        self.ride("tr-0001", "akkaunty", slots="max 1")
        tr = self.tracks()
        tr.car_set("tr-0001:M", no_after=True)
        tr.car_stack("tr-0001:M", start=True)
        tr.car_set("tr-0001:M", after=["0b"])
        self.team("stack", "set", "st-0001", "--pr", "202", "--status", "PR #202 готов; блокирован паровозом")
        Stack(self.t.state, self.t.cfg).add("ждёт паролей", depends=["st-0001"], autostart=True)

    def test_park_frees_the_slot_and_the_pr_waits_on_the_track(self):
        self.assertEqual(self.trains().next(PAST)["picks"], [])  # max 1: M holds the train's slot
        rc, out, err = self.team("stack", "park", "st-0001", "--reason", "блокирован: вливать после 0b")
        self.assertEqual(rc, 0, err)
        self.assertIn("st-0001 parked: it left the stack, the slot is free (0/4)", out)
        self.assertIn("tr-0001:M → PR #202 открыт, запаркован на треке (st-0001 вышел из стека, слот свободен)", out)
        tr = self.trains()
        c = tr.tracks.car("tr-0001:M")[1]
        self.assertEqual((c["status"], c["pr"], c["branch"], c["st"]), ("pr", 202, "passwords-0.1", "st-0001"))
        self.assertTrue(tr.tracks.parked(c))
        self.assertEqual(tr.busy(tr.get("akkaunty")), [])
        self.assertNotIn("st-0001", [o["id"] for o in Stack(self.t.state, self.t.cfg).objects()])
        self.assertEqual(tr.tracks.ref_state("#202"), (False, "PR #202 (не слит)"))
        self.assertFalse(tr.tracks.ref_state("st-0001")[0])
        self.assertEqual(self.team("stack", "start", "st-0002")[0], 1)
        nx = tr.next(PAST)
        self.assertEqual([p["car"]["n"] for p in nx["picks"]], ["0b"])  # the train's slot is free for 0b
        self.assertEqual(nx["stack"], [])
        cm = tr.tracks.car_md("tr-0001", now=PAST)
        self.assertIn("| M | P5 | пароли | 0b | M: ≈ 1%UW (12 MTok) / — | passwords-0.1 / PR #202 | st-0001 | akkaunty | "
                      "PR открыт, запаркован: ждёт: 0b (готов) |", cm)
        self.assertIn("Запаркованы (слот не держат): tr-0001:M — PR #202 запаркован, ждёт: 0b (готов).", cm)
        self.assertEqual(self.team("trains", "done", "akkaunty")[0], 1)  # a parked PR is not merged
        # 0b lands: M's PR is ready to merge, and a merge needs no slot
        self.tracks().car_set("tr-0001:0b", status="merged")
        tr = self.trains()
        lines = "\n".join(tr.next_lines(PAST))
        self.assertIn("Готово к мерджу (слот не нужен): tr-0001:M — PR #202 готов к мерджу (после слова владельца).",
                      lines)
        self.assertIn("PR открыт, запаркован: готов к мерджу (после слова владельца) |", tr.tracks.car_md("tr-0001"))
        self.assertIn("Готово к мерджу (слот не нужен): tr-0001:M", "\n".join(tr.next_lines(PAST, train=tr.get("akkaunty"))))
        self.assertIn("к мерджу: tr-0001:M", self.team("queues")[1])
        self.assertEqual(json.loads(self.team("trains", "next", "--json")[1])["parked"],
                         [{"car": "tr-0001:M", "pr": 202, "ready": True, "waits": []}])
        # work is needed after all: the same object comes back into the stack, its id kept
        rc, out, err = self.team("tracks", "car", "stack", "tr-0001:M")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:M → st-0001 back in the stack queue (PR #202)", out)
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["where"], o["pr"], o["branch"]), ("queue", 202, "passwords-0.1"))
        self.assertEqual(self.tracks().car("tr-0001:M")[1]["status"], "ready")
        self.assertIn("`depot2 stack start st-0001`", self.team("trains", "next")[1])
        self.team("stack", "start", "st-0001")
        self.assertEqual(self.tracks().car("tr-0001:M")[1]["status"], "running")
        self.team("stack", "done", "st-0001", "--reason", "merged #202", "--car", "merged")
        tr = self.tracks()
        self.assertEqual(tr.ref_state("#202"), (True, "PR #202 (слит)"))
        self.assertTrue(tr.ref_state("st-0001")[0])
        self.assertEqual(self.team("stack", "start", "st-0002")[0], 0)

    def test_park_refusals(self):
        Stack(self.t.state, self.t.cfg).lock("st-0001", "keeper", "замер", "дорого", "r", "tx-000001", 10)
        rc, _, err = self.team("stack", "park", "st-0001", "--reason", "x")
        self.assertEqual(rc, 1)
        self.assertIn("is locked (lk-0001): lift it first", err)
        self.tracks().car_add("tr-0001", "без PR", n="X", size="S")
        self.tracks().car_stack("tr-0001:X", start=True)
        rc, _, err = self.team("stack", "park", "st-0003", "--reason", "x")
        self.assertEqual(rc, 1)
        self.assertIn("has no PR: a car is parked with its PR", err)


class PrRefTest(TracksBase):
    def test_a_pr_ref_is_met_only_when_merged(self):
        stk = Stack(self.t.state, self.t.cfg)
        for title, pr in (("слит", 301), ("снят", 302), ("не слит", 303)):
            stk.add(title, pr=pr)
        stk.done("st-0001", "merged #301")
        stk.done("st-0002", "closed: superseded")
        stk.done("st-0003", "ушёл из стека, PR #303 открыт, НЕ слит")
        tr = self.tracks()
        self.assertEqual([tr.ref_state("#%d" % n)[0] for n in (301, 302, 303, 304)], [True, False, False, False])
        a = tr.add("Т")
        tr.car_add(a["id"], "x", pr=304, status="merged")
        self.assertTrue(self.tracks().ref_state("#304")[0])


class CarNotesTest(TracksBase):
    def test_a_car_note_appends_and_replace_is_explicit(self):
        tr = self.tracks()
        a = tr.add("Аккаунты")
        tr.car_add(a["id"], "пароли", n="M", note="первая заметка")
        tr.car_add(a["id"], "другой", n="1")
        # a car from before: one string, `note`; it reads as the first line
        c = tr.car("tr-0001:1")[1]
        c.pop("notes")
        c["note"] = "старая строка"
        tr._save("test", a["id"])
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--note", "вторая")
        self.assertEqual(rc, 0, err)
        c = self.tracks().car("tr-0001:1")[1]
        self.assertEqual(c["notes"][0], "старая строка")
        self.assertRegex(c["notes"][1], r"^20\d\d-\d\d-\d\d: вторая$")
        self.assertNotIn("note", c)
        self.team("tracks", "car", "set", "tr-0001:1", "--note", "третья")
        rc, out, _ = self.team("track", "tr-0001", "--md")
        self.assertRegex(out, r"\| 1 \| P5 \| другой \(20\d\d-\d\d-\d\d: третья\) \(\+2\) \|")
        self.assertIn("| M | P5 | пароли (первая заметка) | — |", out)
        self.assertIn("Прежние заметки вагонов (в таблице — последняя):\n- 1: старая строка\n- 1: ", out)
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:1", "--note", " ")[0], 1)
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:1", "--note", "a", "--note-replace", "b")[0], 1)
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--note-replace", "одна")
        self.assertEqual(rc, 0, err)
        self.assertEqual(len(self.tracks().car("tr-0001:1")[1]["notes"]), 1)
        self.assertNotIn("Прежние заметки", out)
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:1", "--note-replace", "")[0], 0)
        self.assertEqual(self.tracks().car("tr-0001:1")[1]["notes"], [])
        self.assertEqual([h.get("note_replaced") for h in self.tracks().history()][-2:], ["одна", ""])


class CarMoveTest(TracksBase):

    def setUp(self):
        super(CarMoveTest, self).setUp()
        tr = self.tracks()
        a = tr.add("Каталог")
        tr.car_add(a["id"], "a", size="S")
        tr.car_add(a["id"], "b", size="S", after=["0"], note="первая заметка", branch="tr-0001-1")
        tr.car_add(a["id"], "c", size="S", after=["1"])
        b = tr.add("Другой")
        tr.car_add(b["id"], "x", size="S", after=["tr-0001:1"])
        trn = self.trains()
        trn.add("katalog", "Каталог", cars=["tr-0001:0", "tr-0001:1", "tr-0001:2"], slots="max 3")
        trn.set_state("katalog", "impl")
        trn.add("next-one", "Следом", after=["tr-0001:1"])

    def test_to_another_track_with_its_history_and_its_refs(self):
        self.assertEqual(self.team("tracks", "car", "stack", "tr-0001:1", "--start", "--force", "--why", "тест")[0], 0)
        self.assertEqual(self.team("stack", "set", "st-0001", "--pr", "301")[0], 0)
        self.assertEqual(self.team("tracks", "car", "set", "tr-0001:1", "--spent-mtok", "3.5")[0], 0)
        before = dict(self.tracks().car("tr-0001:1")[1])
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:1", "--to", "tr-0002", "--why", "тема другая")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0001:1 → tr-0002:1: перенесён со всей историей", out)
        self.assertIn("tr-0002:1: слот и PR при нём (st-0001, PR #301)", out)
        self.assertIn("ждут его под новым номером (after): tr-0001:2, tr-0002:0", out)
        self.assertIn("поезд katalog: tr-0001:1 → tr-0002:1, на том же месте (2)", out)
        self.assertIn("поезд next-one: старт после tr-0001:1 → после tr-0002:1", out)
        self.assertIn("объект стека st-0001: «tr-0002:1 Другой — b»", out)
        self.assertIn("Перенесены отсюда (старый номер находит вагон): 1 → tr-0002:1 «b» (", out)
        tr = self.tracks()
        t, c = tr.car("tr-0002:1")
        self.assertEqual([x["n"] for x in tr.get("tr-0001")["cars"]], ["0", "2"])
        self.assertEqual([x["n"] for x in t["cars"]], ["0", "1"])
        for k in ("title", "notes", "spent_mtok", "pr", "st", "branch", "status", "stage", "stages", "started"):
            self.assertEqual(c.get(k), before.get(k), k)  # the record itself, every field
        self.assertEqual(c["moved_from"][0]["from"], "tr-0001:1")
        self.assertEqual(c["moved_from"][0]["why"], "тема другая")
        self.assertEqual(c["after"], ["tr-0001:0"])  # its bare name on the old track, now full
        self.assertEqual(tr.car("tr-0001:2")[1]["after"], ["tr-0002:1"])
        self.assertEqual(tr.car("tr-0002:0")[1]["after"], ["tr-0002:1"])
        p = tr.get("tr-0001")["moved_out"][0]
        self.assertEqual((p["n"], p["to"], p["title"]), ("1", "tr-0002:1", "b"))
        # the old ref finds it: the reading, an `after`, `car show`, the trains, the stack's link
        self.assertIs(tr.car("tr-0001:1")[1], c)
        self.assertEqual(tr.ref_state("tr-0001:1"), (False, "tr-0001:1 → tr-0002:1 (PR открыт)"))
        self.assertEqual(tr.ref_state("1", tr.get("tr-0001")), (False, "1 → tr-0002:1 (PR открыт)"))
        rc, out, _ = self.team("car", "show", "tr-0001:1")
        self.assertIn("# tr-0002:1 (объект st-0001)", out)
        self.assertIn("tr-0001:1 — старый номер: вагон перенесён, теперь tr-0002:1.", out)
        self.assertIn("Перенесён: tr-0001:1 → tr-0002:1 (", out)
        trn = self.trains()
        self.assertEqual(trn.get("katalog")["cars"], ["tr-0001:0", "tr-0002:1", "tr-0001:2"])
        self.assertEqual(trn.get("next-one")["start"]["after"], ["tr-0002:1"])
        self.assertIs(trn.of_car("tr-0002:1"), trn.get("katalog"))
        o = Stack(self.t.state, self.t.cfg).get("st-0001")
        self.assertEqual((o["where"], o["title"], o["pr"]), ("slot", "tr-0002:1 Другой — b", 301))
        self.assertEqual(tr.car_ref("st-0001"), "tr-0002:1")
        self.assertEqual(Stack(self.t.state, self.t.cfg).car_places()["st-0001"]["car"], "tr-0002:1")
        h = [x for x in tr.history() if x["event"] == "car_moved"]
        self.assertEqual([(x["track"], x["car"]) for x in h], [("tr-0001", "1"), ("tr-0002", "1")])
        self.assertEqual((h[0]["to"], h[1]["from"], h[1]["st"]), ("tr-0002:1", "tr-0001:1", "st-0001"))
        # the stack link follows: the car is done through its object as before
        rc, out, err = self.team("stack", "done", "st-0001", "--reason", "merged #301", "--car", "merged")
        self.assertEqual(rc, 0, err)
        self.assertIn("tr-0002:1 → слит", out)
        self.assertTrue(self.tracks().ref_state("tr-0001:1")[0])
        # the web reads the pointer with where the car stands now
        tj = json.loads(self.team("track", "tr-0001", "--json")[1])
        self.assertEqual((tj["moved_out"][0]["to"], tj["moved_out"][0]["now"]), ("tr-0002:1", "tr-0002:1"))
        # the old name is never given again: the next car is 3, and --n 1 is refused with what holds it
        rc, out, err = self.team("tracks", "car", "add", "tr-0001", "--title", "d")
        self.assertIn("tr-0001:3 added", out)
        rc, out, err = self.team("tracks", "car", "add", "tr-0001", "--title", "e", "--n", "1")
        self.assertEqual(rc, 1)
        self.assertIn("номер занят: tr-0001:1 — вагон «b» перенесён в tr-0002:1; свободный номер — --n 4", err)
        # a ref typed by its old name is written as the new one; the old ref of itself is itself
        tr = self.tracks()
        tr.car_set("tr-0001:3", after=["1"])
        self.assertEqual(tr.car("tr-0001:3")[1]["after"], ["tr-0002:1"])
        with self.assertRaises(TrackError):
            tr.car_set("tr-0002:0", after=["tr-0002:0"])
        self.assertEqual(self.trains().get("katalog")["cars"][1], "tr-0002:1")

    def test_a_new_name_on_its_own_track_and_back(self):
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:2", "--n", "c7")
        self.assertEqual(rc, 0, err)
        tr = self.tracks()
        self.assertEqual([x["n"] for x in tr.get("tr-0001")["cars"]], ["0", "1", "c7"])  # its place kept
        self.assertEqual(tr.car("tr-0001:2")[1]["n"], "c7")
        self.assertEqual(self.trains().get("katalog")["cars"], ["tr-0001:0", "tr-0001:1", "tr-0001:c7"])
        tr.car_add("tr-0001", "d", after=["c7"])
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:c7", "--n", "2", "--at", "1")
        self.assertEqual(rc, 0, err)
        tr = self.tracks()
        t = tr.get("tr-0001")
        self.assertEqual([x["n"] for x in t["cars"]], ["2", "0", "1", "3"])
        self.assertEqual([(p["n"], p["to"]) for p in t["moved_out"]], [("c7", "tr-0001:2")])  # its own old place back
        self.assertEqual(tr.car("tr-0001:3")[1]["after"], ["2"])  # a bare name on the same track stays bare
        self.assertEqual(self.trains().get("katalog")["cars"], ["tr-0001:0", "tr-0001:1", "tr-0001:2"])
        self.assertEqual(tr.car("tr-0001:c7")[1]["title"], "c")
        # moved twice: the old ref follows the chain
        self.assertEqual(self.team("tracks", "car", "move", "tr-0001:2", "--to", "tr-0002", "--n", "5")[0], 0)
        tr = self.tracks()
        self.assertEqual(tr.canon("tr-0001:c7"), "tr-0002:5")
        self.assertEqual(tr.moved_lines(tr.get("tr-0001"))[0][:15], "c7 → tr-0002:5 ")

    def test_refusals_say_why(self):
        tr = self.tracks()
        tr.car_set("tr-0001:0", status="merged")
        tr.car_stage("tr-0001:0", "arrived", "тест")
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:0", "--to", "tr-0002", "--n", "9")
        self.assertEqual(rc, 1)
        self.assertIn("tr-0001:0 не переносится: он прибыл (слит)", err)
        tr = self.tracks()
        t, c = tr.car("tr-0001:2")
        c["stage"] = "rolling"
        tr._save("test", t["id"])
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:2", "--to", "tr-0002", "--n", "9")
        self.assertIn("tr-0001:2 не переносится: он в пути", err)
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:1", "--to", "tr-0002", "--n", "0")
        self.assertEqual(rc, 1)
        self.assertIn("номер занят: tr-0002:0 — x (ждёт); свободный номер — --n 1", err)
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:1", "--to", "tr-0001")
        self.assertIn("tr-0001:1 stays where it is", err)
        self.assertEqual(self.team("tracks", "car", "move", "tr-0001:1")[0], 1)  # neither --at nor --to/--n
        from depot2.merge import FILE, MergeQueue
        from depot2.store import write_json
        write_json(self.t.state.path(FILE), {"next": 2, "entries": [
            {"id": "mq-0001", "pr": 302, "car": "tr-0001:1", "st": None, "status": "stopped"}]})
        self.assertEqual(len(MergeQueue(self.t.state).entries(open_only=True)), 1)
        rc, out, err = self.team("tracks", "car", "move", "tr-0001:1", "--to", "tr-0002", "--n", "9")
        self.assertIn("tr-0001:1 не переносится: его PR #302 в мердж-очереди (mq-0001)", err)
        self.assertNotIn("moved_out", self.tracks().get("tr-0001"))  # nothing written

    def test_a_taken_name_and_a_write_into_a_closed_car(self):
        tr = self.tracks()
        tr.car_set("tr-0001:0", status="merged")
        rc, out, err = self.team("tracks", "car", "add", "tr-0001", "--title", "new", "--n", "0")
        self.assertEqual(rc, 1)
        self.assertIn("номер занят: tr-0001:0 — a (слит); свободный номер — --n 3 (его же даёт команда без --n)", err)
        self.assertEqual(self.tracks().stage_of(self.tracks().car("tr-0001:0")[1]), "rolling")
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--note", "после мерджа")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("warning", out)
        self.assertRegex(self.tracks().car("tr-0001:0")[1]["notes"][-1], r"после мерджа$")
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:1", "--note", "живой")
        self.assertNotIn("warning", out)
        rc, out, err = self.team("car", "stage", "tr-0001:0", "arrived", "--why", "выкатан")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("warning", out)
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:0", "--note", "после выкатки")
        self.assertEqual(rc, 0, err)
        self.assertIn("warning: tr-0001:0 «a» — слит, этап «прибыл»: запись идёт в закрытый вагон; новый вагон — "
                      "depot2 tracks car add tr-0001 --title …", out)
        self.assertRegex(self.tracks().car("tr-0001:0")[1]["notes"][-1], r"после выкатки$")  # written all the same
        tr = self.tracks()
        tr.car_set("tr-0001:2", status="dropped")
        rc, out, err = self.team("tracks", "car", "set", "tr-0001:2", "--note", "после списания")
        self.assertIn("warning: tr-0001:2 «c» — снят, этап «списан»: запись идёт в закрытый вагон", out)
