"""The cars' files (`depot2 artifacts`, the config's artifacts.dir): a folder per car, named by its branch — `agents
start` makes it (owner only) and records it, nothing deletes or rewrites anything there; the listing: off, not there,
empty; a folder's car by its agent, its branch or its name, a folder without a car; a link, a dot-name and `..` never
read; nothing written by a listing; the queues' row. Every name in it is made up."""

import json
import os
import shutil
import stat
import tempfile
from unittest import mock

from test_agents import AgentsBase
from test_extras import Base

from depot2 import artifacts
from depot2.store import read_jsonl

T0 = 1447322400  # 2015-11-12 10:00 UTC


def tree(root):
    """Every path under root with its size, mtime, mode and kind, links as links (what must stay as it was)."""
    out = {}
    for d, dirs, files in os.walk(root):
        for n in dirs + files:
            p = os.path.join(d, n)
            st = os.lstat(p)
            out[os.path.relpath(p, root)] = (st.st_size, st.st_mtime_ns, st.st_mode, os.path.islink(p))
    return out


def mode(path):
    return stat.S_IMODE(os.lstat(path).st_mode)


class ArtifactsTest(Base):
    def setUp(self):
        super(ArtifactsTest, self).setUp()
        self.art = tempfile.mkdtemp(prefix="depot2-art-")
        self.outside = tempfile.mkdtemp(prefix="depot2-art-out-")
        with open(os.path.join(self.outside, "secret.txt"), "w") as f:
            f.write("private\n")
        for argv in (("tracks", "add", "--title", "Поиск"), ("tracks", "car", "add", "tr-0001", "--title", "индекс"),
                     ("tracks", "car", "add", "tr-0001", "--title", "выдача"),
                     ("tracks", "car", "set", "tr-0001:1", "--branch", "results-page")):
            rc, _, err = self.team(*argv)
            self.assertEqual(rc, 0, (argv, err))

    def tearDown(self):
        shutil.rmtree(self.art, ignore_errors=True)
        shutil.rmtree(self.outside, ignore_errors=True)
        super(ArtifactsTest, self).tearDown()

    def put(self, rel, size=1, at=0):
        p = os.path.join(self.art, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        os.utime(p, (T0 + at, T0 + at))
        return p

    def arts(self, *argv, where=None):
        return self.team("--set", "artifacts.dir=" + (self.art if where is None else where), "artifacts", *argv)

    def ok(self, *argv, **kw):
        rc, out, err = self.arts(*argv, **kw)
        self.assertEqual(rc, 0, err)
        return out

    def fill(self):
        self.put("tr-0001-0/shots/a.png", 2048, at=60)
        self.put("tr-0001-0/build.log", 10, at=30)
        self.put("sketch-tr-0001-0/notes.txt", 5, at=10)
        self.put("results-page/run.log", 3, at=90)
        self.put("plan-results-page/plan.md", 4, at=40)
        self.put("tr-0001-0-retry/run.log", 1, at=5)
        self.put("spike-x/idea.txt", 7, at=20)
        os.makedirs(os.path.join(self.art, "empty-one"))
        os.utime(os.path.join(self.art, "empty-one"), (T0 - 10, T0 - 10))  # an empty folder: by its own date

    def test_off_and_a_folder_that_is_not_there(self):
        rc, out, err = self.team("artifacts")
        self.assertEqual(rc, 0, err)
        self.assertIn("artifacts.dir не задан", out)
        j = json.loads(self.team("artifacts", "--json")[1])
        self.assertEqual((j["on"], j["exists"], j["folders"]), (False, False, []))
        rc, _, err = self.team("artifacts", "--car", "tr-0001:0")
        self.assertEqual(rc, 1)
        self.assertIn("artifacts.dir не задан", err)
        row = json.loads(self.team("queues", "--json")[1])[-1]
        self.assertEqual((row["key"], row["queue"], row["open"], row["head"], row["command"]),
                         ("artifacts", "Файлы вагонов", "выключено (artifacts.dir не задан)", [], "artifacts"))
        gone = os.path.join(self.art, "not-yet")
        self.assertIn("папки %s ещё нет: её создаст первый `agents start`" % gone, self.ok(where=gone))
        self.assertEqual(json.loads(self.ok("--json", where=gone))["exists"], False)
        rc, out, _ = self.team("--set", "artifacts.dir=" + gone, "queues", "--json")
        self.assertEqual(json.loads(out)[-1]["open"], "папки ещё нет (её создаст agents start)")
        self.assertEqual(self.arts("--car", "x", where=gone)[0], 1)
        self.assertFalse(os.path.exists(gone))  # a listing never makes it
        for bad in ("relative/dir", "5", "[]"):
            rc, _, err = self.team("--set", "artifacts.dir=" + bad, "artifacts")
            self.assertEqual(rc, 1, bad)
            self.assertIn("artifacts.dir must be a folder's path from / or ~", err)
        self.assertEqual(self.team("--set", "artifacts.dir=", "artifacts")[0], 0)  # empty: off

    def test_an_empty_folder(self):
        self.assertIn("Папок ещё нет", self.ok())
        j = json.loads(self.ok("--json"))
        self.assertEqual((j["on"], j["exists"], j["folders"], j["files"], j["bytes"]), (True, True, [], 0, 0))

    def test_the_folders_their_cars_and_counts(self):
        self.fill()
        j = json.loads(self.ok("--json"))
        self.assertEqual((j["dir"], j["path"], j["files"], j["bytes"], j["cut"]),
                         (self.art, os.path.realpath(self.art), 7, 2078, False))
        got = [(f["folder"], f["ref"], f["stage"], f["files"], f["bytes"]) for f in j["folders"]]
        self.assertEqual(got, [("results-page", "tr-0001:1", "bogie", 1, 3),       # the car's branch
                               ("tr-0001-0", "tr-0001:0", "bogie", 2, 2058),       # the name: tr-NNNN-<car>
                               ("plan-results-page", "tr-0001:1", "bogie", 1, 4),  # a plan's word before the branch
                               ("spike-x", None, None, 1, 7),                      # no car
                               ("sketch-tr-0001-0", "tr-0001:0", "bogie", 1, 5),   # a word before the name
                               ("tr-0001-0-retry", "tr-0001:0", "bogie", 1, 1),    # the longest car the track has
                               ("empty-one", None, None, 0, 0)])                   # the newest first
        f = j["folders"][1]
        self.assertEqual((f["title"], f["stage_ru"], f["size"], f["newest"][:10]), ("индекс", "тележка", "2.0 КБ", "2015-11-12"))
        self.assertEqual(j["folders"][-1]["newest"], None)
        out = self.ok()
        self.assertIn("| Папка | Вагон | Этап | Файлов | Размер | Новейший |", out)
        self.assertIn("| tr-0001-0 | tr-0001:0 индекс | тележка | 2 | 2.0 КБ |", out)
        self.assertIn("| spike-x | без вагона | — | 1 | 7 Б |", out)
        self.assertIn("папок 7, файлов 7, 2.0 КБ", out)
        self.assertIn("Папку вагона создаёт `agents start`; депо там ничего не меняет и не удаляет.", out)
        self.assertEqual(self.ok("--md"), out)
        with mock.patch.object(artifacts, "MAX_WALK", 3):  # a listing that stops early says so
            j = json.loads(self.ok("--json"))
            self.assertTrue(j["cut"])
            self.assertIn("Список остановлен после 3 записей", self.ok())

    def test_the_files_of_a_folder_or_of_a_car(self):
        self.fill()
        j = json.loads(self.ok("--car", "tr-0001-0", "--json"))
        self.assertEqual((j["car"], [f["folder"] for f in j["folders"]]), ("tr-0001-0", ["tr-0001-0"]))
        f = j["folders"][0]
        self.assertEqual([(e["name"], e["bytes"]) for e in f["entries"]], [("shots/a.png", 2048), ("build.log", 10)])
        self.assertEqual((f["more"], f["entries"][0]["size"]), (0, "2.0 КБ"))
        j = json.loads(self.ok("--car", "tr-0001:0", "--json"))  # a car: every folder of it, by name
        self.assertEqual([f["folder"] for f in j["folders"]], ["sketch-tr-0001-0", "tr-0001-0", "tr-0001-0-retry"])
        j = json.loads(self.ok("--car", "tr-0001:1", "--json"))
        self.assertEqual([f["folder"] for f in j["folders"]], ["plan-results-page", "results-page"])
        out = self.ok("--car", "results-page")
        self.assertIn("## results-page — tr-0001:1 «выдача», тележка", out)
        self.assertIn("| run.log | 3 Б | 2015-11-12", out)
        self.assertIn("без вагона", self.ok("--car", "spike-x"))
        with mock.patch.object(artifacts, "MAX_FILES", 1):
            f = json.loads(self.ok("--car", "tr-0001-0", "--json"))["folders"][0]
            self.assertEqual(([e["name"] for e in f["entries"]], f["more"]), (["shots/a.png"], 1))
            self.assertIn("… и ещё 1: показаны 1 самых новых.", self.ok("--car", "tr-0001-0"))
        for ref, msg in (("tr-0001:9", "нет папки вагона tr-0001:9"), ("nope", "нет папки nope"),
                         ("st-0001", "нет папки вагона st-0001")):
            rc, _, err = self.arts("--car", ref)
            self.assertEqual(rc, 1, ref)
            self.assertIn(msg, err)

    def test_a_link_a_dot_name_and_dotdot_are_never_read(self):
        self.fill()
        os.symlink(self.outside, os.path.join(self.art, "link-out"))
        os.symlink(os.path.join(self.outside, "secret.txt"), os.path.join(self.art, "spike-x", "leak.txt"))
        os.symlink(self.outside, os.path.join(self.art, "spike-x", "leak-dir"))
        self.put(".hidden/secret.txt")
        self.put("spike-x/.cache", 100)
        before = tree(self.art)
        j = json.loads(self.ok("--json"))
        names = [f["folder"] for f in j["folders"]]
        self.assertNotIn("link-out", names)
        self.assertNotIn(".hidden", names)
        spike = [f for f in j["folders"] if f["folder"] == "spike-x"][0]
        self.assertEqual((spike["files"], spike["bytes"]), (1, 7))
        listed = json.loads(self.ok("--car", "spike-x", "--json"))
        self.assertEqual([e["name"] for e in listed["folders"][0]["entries"]], ["idea.txt"])
        for ask in ("link-out", ".hidden", "..", ".", "../" + os.path.basename(self.outside), "spike-x/leak-dir",
                    self.outside, "spike-x/../.."):
            rc, out, err = self.arts("--car", ask)
            self.assertEqual(rc, 1, ask)
            self.assertNotIn("secret", out + err)
        for argv in ((), ("--json",), ("--car", "spike-x"), ("--car", "tr-0001:0", "--json")):
            self.assertNotIn("secret", self.ok(*argv))
        self.team("--set", "artifacts.dir=" + self.art, "queues")
        self.assertEqual(tree(self.art), before)  # nothing written, moved or deleted there
        self.assertEqual(os.listdir(self.outside), ["secret.txt"])

    def test_the_queues_row(self):
        self.fill()
        for i, name in enumerate(sorted(os.listdir(self.art))):
            os.utime(os.path.join(self.art, name), (T0 + i, T0 + i))
        for name, at in (("spike-x", 300), ("tr-0001-0", 200), ("results-page", 100)):
            os.utime(os.path.join(self.art, name), (T0 + at, T0 + at))
        rows = json.loads(self.team("--set", "artifacts.dir=" + self.art, "queues", "--json")[1])
        row = rows[-1]
        self.assertEqual((row["key"], row["open"], row["head"], row["priority"]),
                         ("artifacts", "папок: 7", ["spike-x", "tr-0001-0", "results-page"], None))
        self.assertIn("папку вагона создаёт `agents start`, пишут в неё сами прогоны", row["means"])
        self.assertIn("только по слову владельца", row["means"])
        out = self.team("--set", "artifacts.dir=" + self.art, "queues", "--md")[1]
        self.assertIn("| Файлы вагонов | папок: 7 | — | spike-x; tr-0001-0; results-page | `depot2 artifacts --md` |", out)

    def test_a_folder_name(self):
        self.assertEqual([artifacts.folder_name("tr-0001:2", role) for role in ("sketch", "plan", "build", "docs", "mergefix")],
                         ["sketch-tr-0001-2", "plan-tr-0001-2", "tr-0001-2", "tr-0001-2", "tr-0001-2"])
        self.assertEqual(artifacts.folder_name("tr-0001:2", "build", "feature/cover cache"), "feature-cover-cache")
        self.assertEqual(artifacts.folder_name("tr-0001:2", "sketch", "results-page"), "sketch-results-page")
        for odd in ("..", ".hidden", "/", "", None, "x" * 201):  # no name of its own: the ref
            self.assertEqual(artifacts.folder_name("tr-0001:2-b", "build", odd), "tr-0001-2-b", odd)


class RunFolderTest(AgentsBase):
    """`agents start` makes the run's folder: artifacts.dir and the car's folder, 0700, printed and recorded; a folder
    that is there is left as it is; one that cannot be made stops nothing; nothing else in depot2 touches it."""

    def setUp(self):
        super(RunFolderTest, self).setUp()
        self.top = tempfile.mkdtemp(prefix="depot2-runs-")
        self.art = os.path.join(self.top, "share", "cars")  # two levels that are not there yet

    def tearDown(self):
        shutil.rmtree(self.top, ignore_errors=True)
        super(RunFolderTest, self).tearDown()

    def team(self, *argv):
        return super(RunFolderTest, self).team("--set", "artifacts.dir=" + self.art, *argv)

    def started(self, ref, role):
        rc, out, err = self.team("agents", "start", ref, "--role", role, "--usage", "0.5")
        self.assertEqual(rc, 0, err)
        return out

    def test_the_start_makes_the_folder_once_and_leaves_it_as_it_is(self):
        out = self.started("tr-0001:1", "build")
        self.assertEqual(out.splitlines()[0], "ag-0001")
        self.assertEqual(out.splitlines()[-1], "файлы прогона: %s/tr-0001-1/" % self.art)
        folder = os.path.join(self.art, "tr-0001-1")
        self.assertTrue(os.path.isdir(folder))
        self.assertEqual((mode(self.art), mode(folder)), (0o700, 0o700))
        self.assertEqual(self.agents().get("ag-0001")["files"], "tr-0001-1")
        start = [h for h in read_jsonl(self.t.state.path("agents-history.jsonl")) if h["event"] == "start"][0]
        self.assertEqual(start["files"], "tr-0001-1")
        self.assertIn("- файлы прогона: tr-0001-1 (в artifacts.dir)", self.team("agents", "show", "ag-0001")[1])
        self.assertEqual(json.loads(self.team("agents", "--json")[1])["agents"][0]["files"], "tr-0001-1")
        with open(os.path.join(folder, "build.log"), "w") as f:
            f.write("ok\n")
        os.chmod(folder, 0o750)
        os.utime(folder, (T0, T0))
        before = tree(self.top)
        rc, _, err = self.team("agents", "stop", "ag-0001", "--why", "проба")
        self.assertEqual(rc, 0, err)
        for argv in (("agents", "--all"), ("agents", "show", "ag-0001"), ("artifacts",), ("artifacts", "--json"),
                     ("artifacts", "--car", "tr-0001:1"), ("queues",)):
            self.assertEqual(self.team(*argv)[0], 0, argv)
        self.assertEqual(tree(self.top), before)  # a stop, the settling and the listings leave it as it was
        self.assertIn("файлы прогона: %s/tr-0001-1/" % self.art, self.started("tr-0001:1", "build"))
        self.assertEqual(tree(self.top), before)  # the second start: the same folder, its mode, dates and files
        self.assertEqual(self.agents().get("ag-0002")["files"], "tr-0001-1")

    def test_a_sketch_a_plan_and_a_branch_name_their_folders(self):
        self.assertIn("файлы прогона: %s/sketch-tr-0001-2/" % self.art, self.started("tr-0001:2", "sketch"))
        rc, _, err = self.team("tracks", "car", "set", "tr-0001:3", "--branch", "feature/cover-cache")
        self.assertEqual(rc, 0, err)
        self.assertIn("файлы прогона: %s/plan-feature-cover-cache/" % self.art, self.started("tr-0001:3", "plan"))
        self.assertEqual(sorted(os.listdir(self.art)), ["plan-feature-cover-cache", "sketch-tr-0001-2"])
        j = json.loads(self.team("artifacts", "--json")[1])
        self.assertEqual(sorted((f["folder"], f["ref"]) for f in j["folders"]),
                         [("plan-feature-cover-cache", "tr-0001:3"), ("sketch-tr-0001-2", "tr-0001:2")])
        # the agent's record names its folder's car, whatever the folder is called
        os.mkdir(os.path.join(self.art, "cover-notes"))
        p = self.t.state.path("agents.json")
        with open(p, encoding="utf-8") as f:
            ags = json.load(f)
        ags["agents"][0]["files"] = "cover-notes"
        with open(p, "w", encoding="utf-8") as f:
            json.dump(ags, f)
        j = json.loads(self.team("artifacts", "--json")[1])
        self.assertIn(("cover-notes", "tr-0001:2"), [(f["folder"], f["ref"]) for f in j["folders"]])

    def test_a_file_in_its_place_stops_nothing(self):
        os.makedirs(self.art)
        with open(os.path.join(self.art, "tr-0001-1"), "w") as f:
            f.write("a file\n")
        before = tree(self.top)
        out = self.started("tr-0001:1", "build")
        self.assertIn("⚠ папка прогона %s/tr-0001-1/ не создана:" % self.art, out)
        self.assertIn("(старт всё равно записан)", out)
        self.assertNotIn("files", self.agents().get("ag-0001"))
        self.assertEqual(tree(self.top), before)
        os.remove(os.path.join(self.art, "tr-0001-1"))
        os.symlink(self.top, os.path.join(self.art, "tr-0001-2"))  # a link is no folder of a run either
        self.assertIn("⚠ папка прогона %s/tr-0001-2/ не создана:" % self.art, self.started("tr-0001:2", "build"))

    def test_off_makes_nothing(self):
        with mock.patch.object(artifacts, "_make", side_effect=AssertionError("made a folder")):
            rc, out, err = AgentsBase.team(self, "agents", "start", "tr-0001:1", "--role", "build", "--usage", "0.5")
        self.assertEqual(rc, 0, err)
        self.assertNotIn("файлы прогона", out)
        self.assertNotIn("files", self.agents().get("ag-0001"))
        self.assertEqual(os.listdir(self.top), [])
