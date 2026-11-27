import hashlib
import http.client
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import helpers
from helpers import HERE
from test_board import ALL, GateBase

from depot2 import board as board_mod
from depot2 import characters, i18n, webui
from depot2 import principles as principles_mod

NOW = "2015-11-12T12:00:00+02:00"
CYRILLIC = re.compile("[\u0400-\u04ff]")


def page_messages(html):
    """The page's messages, as the served page carries them in its #messages block."""
    m = re.search(r'<script type="application/json" id="messages">(.*?)</script>', html, re.DOTALL)
    return json.loads(m.group(1))


class Served:
    """A server on port 0 in a thread; get() returns (status, headers, body)."""

    def __init__(self, state, sets, **kw) -> None:
        kw.setdefault("follow_code", False)  # a server in a thread never re-executes the test run (ReexecTest does)
        self.srv = webui.Server(state, sets=sets, port=0, **kw)
        self.port = self.srv.server_address[1]
        self.th = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.th.start()

    def request(self, method, path, host=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=20)
        try:
            c.putrequest(method, path, skip_host=True)
            c.putheader("Host", host or "127.0.0.1:%d" % self.port)  # noqa: UP031
            c.endheaders()
            r = c.getresponse()
            return r.status, dict(r.getheaders()), r.read()
        finally:
            c.close()

    def get(self, path, **kw):
        return self.request("GET", path, **kw)

    def json(self, path):
        code, _, body = self.get(path)
        return code, json.loads(body.decode("utf-8"))

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Peer:
    """A request as `Server.authorize` sees it, from any address: the one gate, asked without a socket."""

    def __init__(self, addr, host) -> None:
        self.client_address, self.headers, self.sent = (addr, 50000), {"Host": host}, []

    def send_json(self, code, data, allow=False):
        self.sent.append(code)


def tree_hash(root):
    out = {}
    for d, _, files in os.walk(root):
        for n in files:
            p = str(Path(d, n))
            with Path(p).open("rb") as f:
                out[os.path.relpath(p, root)] = hashlib.sha256(f.read()).hexdigest()
    return out


class WebBase(GateBase):
    def setUp(self):
        super().setUp()
        self.scratch = tempfile.mkdtemp(prefix="team-web-scratch-")
        for d in ("briefs", "design", "notes", "drafts"):
            Path(self.scratch, d).mkdir(parents=True)
        self.web_brief = str(Path(self.scratch, "briefs", "search.md"))
        with Path(self.web_brief).open("w", encoding="utf-8") as f:
            f.write("# Поиск\n\n- **жирный** и `код`\n\n<script>alert(1)</script>\n")
        with Path(self.scratch, "design", "sketch.md").open("w", encoding="utf-8") as f:
            f.write("# Эскиз\n")
        self.secret = str(Path(self.scratch, "notes", "secret.md"))
        with Path(self.secret).open("w") as f:
            f.write("private\n")
        os.symlink(self.secret, Path(self.scratch, "briefs", "out.md"))
        self.team("stack", "add", "--title", "Поиск", "--brief", self.web_brief, "--size", "M")
        self.team("stack", "add", "--title", "Мысль")
        self.team("stack", "add", "--title", "Наружу", "--brief", str(Path(self.scratch, "briefs", "out.md")))
        self.team("tracks", "add", "--title", "Поезд")
        self.team("tracks", "car", "add", "tr-0001", "--title", "вагон 0", "--brief", self.web_brief)
        self.team("tracks", "car", "add", "tr-0001", "--title", "вагон 1", "--after", "0")
        self.team(
            "trains", "add", "--name", "search", "--title", "Поиск-поезд", "--car", "tr-0001:0", "--car", "tr-0001:1"
        )
        self.team("wishes", "add", "--title", "Хочу поиск", "--words", "поиск бы")
        self.team("stack", "start", "st-0001")
        self.sets = [
            "orders.results_dir=" + self.t.cfg["orders"]["results_dir"],
            "brainstorm.topics_dir=" + self.t.cfg["brainstorm"]["topics_dir"],
            *ALL[1::2],
        ]
        self.dirs = (str(Path(self.scratch, "briefs")), str(Path(self.scratch, "design")))
        self.web = Served(self.t.state, self.sets, read_dirs=self.dirs, doc_base=self.scratch, docs_base=self.scratch)

    def tearDown(self):
        self.web.close()
        shutil.rmtree(self.scratch, ignore_errors=True)
        super().tearDown()


class WebTest(WebBase):
    def test_the_page(self):
        code, h, body = self.web.get("/")
        self.assertEqual(code, 200)
        self.assertTrue(h["Content-Type"].startswith("text/html"))
        html = body.decode("utf-8")
        self.assertIn("<title>depot2</title>", html)
        self.assertIn('data-tab="principles"', html)
        self.assertIn('id="view-principles"', html)
        for tab in ("tracks", "trains", "board", "merge", "agents", "principles", "queues"):
            self.assertIn(f'data-tab="{tab}"', html)
            self.assertIn(f'id="view-{tab}"', html)
        self.assertIn(
            'data-tab="board" aria-selected="false" data-t="tab.board">Board</button>\n    <button type="button" role="tab" '
            'data-tab="merge" aria-selected="false" data-t="tab.merge">Merge queue</button>\n    <button type="button" '
            'role="tab" data-tab="agents" aria-selected="false" data-t="tab.agents">Agents</button>',
            html,
        )
        for part in ('"/api/agents?all=1"', 'data-go="agents"', "function renderAgents()", "function renderBusy(sl)"):
            self.assertIn(part, html)
        self.assertIn('data-tab="principles" aria-selected="false" data-t="tab.principles">', html)
        self.assertIn('data-tab="queues" aria-selected="false" data-t="tab.queues">', html)
        msgs = page_messages(html)  # the static texts filled from the catalog at load
        self.assertEqual(
            (msgs["tab.board"], msgs["tab.merge"], msgs["tab.agents"], msgs["tab.principles"], msgs["tab.queues"]),
            ("Доска", "Мердж-очередь", "Агенты", "Решения и принципы", "Очереди"),
        )
        for part in ('id="pc-list"', 'id="dec-list"', '"/api/decisions"', '"/api/queue/"', 'data-side="dec"'):
            self.assertIn(part, html)
        self.assertNotIn('id="dec-q"', html)
        for part in (
            '<div class="find" role="search">',
            '<input type="search" id="q"',
            '<select id="sort"',
            "function hit(fields)",
            "function arrange(rows, sort)",
            '"q=" + enc(ui.q)',
            '"sort=" + enc(ui.sort)',
            '"<mark>"',
            'e.key === "/"',
            '["own", T("sort.own")]',
            '["match", T("sort.match")]',
            '["prio", T("sort.prio")]',
            '["new", T("sort.new")]',
            '["old", T("sort.old")]',
            '["stage", T("sort.stage")]',
            '["group", T("sort.group")]',
        ):
            self.assertIn(part, html)
        self.assertEqual(
            [msgs["sort." + k] for k in ("own", "match", "prio", "new", "old", "stage", "group")],
            [
                "обычный порядок",
                "по совпадению",
                "по приоритету",
                "сначала новые",
                "сначала старые",
                "по этапу",
                "по треку и поезду",
            ],
        )
        self.assertNotRegex(html, r"fetch\([^)]*method")  # nothing but GET
        self.assertIn('id="code"', html)  # the code that serves the page, in its header
        self.assertIn('"/api/tracks"', html)
        self.assertNotRegex(html, r"https?://(?!github\.com/)[a-z]")  # nothing from outside: no CDN, no font
        self.assertEqual((msgs["copy.name"], msgs["copy.done"]), ("копировать {name}", "скопировано"))
        for part in (
            '<symbol id="cp-i"',
            "function cp(name)",
            '<use href="#cp-i">',
            "'\" aria-label=\"' + label + '\" title=\"' + label",
            "navigator.clipboard.writeText",
            'execCommand("copy")',
            'T("copy.done")',
            '<div id="cp-tip" class="cp-tip" role="status" aria-live="polite">',
        ):
            self.assertIn(part, html)
        self.assertNotIn(" style=", html)
        click = html[html.index('document.addEventListener("click"') :]
        self.assertLess(
            click.index('e.target.closest("#panel .rlink[data-ref]")'),
            click.index('e.target.closest("#panel")) return;'),
        )
        csp = h["Content-Security-Policy"]
        self.assertIn("default-src 'none'", csp)
        self.assertIn("script-src 'sha256-", csp)
        self.assertEqual(h["Cache-Control"], "no-store")

    def test_the_page_takes_its_texts_from_the_catalog(self):
        """The served page: <html lang> and the #messages block in the run's language (the catalogs' `web.` keys), no
        Russian outside that block, and the CSP hashes the one script that runs, not the data block.
        """
        _code, h, body = self.web.get("/")
        html = body.decode("utf-8")
        self.assertIn('<html lang="ru">', html)
        self.assertEqual(page_messages(html), i18n.web_messages("ru"))
        block = html[html.index('<script type="application/json" id="messages">') :]
        block = block[: block.index("</script>")]
        self.assertNotIn("<code>", block)  # every < of a message is \u003c in the block
        self.assertIn("\\u003ccode>depot2 decide\\u003c/code>", block)
        self.assertIn("<code>depot2 decide</code>", page_messages(html)["dec.note"])
        self.assertIsNone(CYRILLIC.search(html.replace(block, "")))
        main = html[html.index("<script>") + len("<script>") :]
        main = main[: main.index("</script>")]
        want = "'sha256-{}'".format(webui.base64.b64encode(hashlib.sha256(main.encode("utf-8")).digest()).decode())
        script_src = h["Content-Security-Policy"].split("script-src ")[1].split(";")[0]
        self.assertEqual(script_src, want)
        with Path(webui.PAGE).open(encoding="utf-8") as f:
            en = webui.page_html(f.read(), "en")
        self.assertIn('<html lang="en">', en)
        self.assertEqual(page_messages(en)["tab.board"], "Board")
        self.assertIsNone(CYRILLIC.search(en))
        self.assertEqual(webui.csp_for(en), webui.csp_for(html))  # the messages change no hash

    def test_a_depot_without_a_team(self):
        """No `characters` in the config: every view still answers; the team's queues are empty, the who-list is `all`."""
        web = Served(
            self.t.state,
            [*self.sets, "characters=null"],
            read_dirs=self.dirs,
            doc_base=self.scratch,
            docs_base=self.scratch,
        )
        try:
            for p in [
                "/",
                "/api/meta",
                "/api/board",
                "/api/board?all=1",
                "/api/trains",
                "/api/tracks",
                "/api/stack",
                "/api/merge",
                "/api/merge/report",
                "/api/car/st-0001",
                "/api/car/tr-0001%3A1",
                "/api/principles",
                "/api/decisions",
                "/api/agents?all=1",
            ] + [f"/api/queue/{n}{a}" for n, _, _ in webui.QUEUES for a in ("", "?all=1")]:
                code, _, body = web.get(p)
                self.assertEqual(code, 200, (p, body[:300]))
            self.assertEqual(characters.IDS, [])
            _, m = web.json("/api/meta")
            q = {x["name"]: x for x in m["queues"]}
            for name in ("tstack", "orders", "brainstorms"):
                self.assertEqual(q[name]["labels"]["who"], {"all": "все"}, name)
            _, ts = web.json("/api/queue/tstack")
            self.assertEqual((ts["in_slots"], ts["queue"]), ([], []))
        finally:
            web.close()
            characters.configure(helpers.TEAM)

    def test_every_api_answer_is_its_json_command(self):
        pairs = [
            ("/api/board", ["board", "--json"]),
            ("/api/board?view=stack", ["board", "--view", "stack", "--json"]),
            ("/api/board?all=1", ["board", "--all", "--json"]),
            ("/api/trains", ["trains", "--json"]),
            ("/api/train/search", ["train", "search", "--json"]),
            ("/api/tracks", ["tracks", "--json"]),
            ("/api/track/tr-0001", ["track", "tr-0001", "--json"]),
            ("/api/stack", ["stack", "--json"]),
            ("/api/merge", ["merge", "--json"]),
            ("/api/principles", ["principles", "--json"]),
            ("/api/principles?all=1", ["principles", "--all", "--json"]),
        ]
        for ref in ("st-0001", "st-0002", "tr-0001:0", "tr-0001:1", "wl-0001"):
            pairs.append(("/api/car/" + ref.replace(":", "%3A"), ["car", "show", ref, "--json"]))
        self.team(
            "principles",
            "add",
            "--section",
            "основа",
            "--title",
            "Проба",
            "--text",
            "тело",
            "--check",
            "yes",
            "--sub",
            "**Третья проверка.** рамка",
            "--sub",
            "пункт",
        )
        self.team("principles", "add", "--section", "работа", "--title", "Снятый", "--words", "так")
        self.team("principles", "drop", "pc-0002", "--reason", "проба")
        with (
            mock.patch.object(board_mod, "now_iso", lambda: NOW),
            mock.patch.object(principles_mod, "now_iso", lambda: NOW),
        ):
            for path, argv in pairs:
                code, got = self.web.json(path)
                self.assertEqual(code, 200, (path, got))
                rc, out, err = self.team(*argv)
                self.assertEqual(rc, 0, err)
                self.assertEqual(got, json.loads(out), path)
        _, tr = self.web.json("/api/trains")
        t = tr["trains"][0]
        self.assertEqual(
            (t["name"], t["state_ru"], t["start_ru"]), ("search", "дизайн", "сразу")
        )  # as `trains --md` says
        self.assertEqual(sorted(t["effort"]), ["expected", "spent", "text", "unpriced"])
        self.assertEqual(sorted(tr["effort"]), ["done", "live", "note"])
        self.assertEqual(
            [(c["ref"], c["track"], c["status_ru"]) for c in t["cars"]],
            [("tr-0001:0", "tr-0001", "готов"), ("tr-0001:1", "tr-0001", "ждёт: 0 (ждёт)")],
        )
        self.assertEqual(sorted(t["cars"][0]["effort"]), ["expected", "spent", "text"])
        _, tk = self.web.json("/api/tracks")
        self.assertEqual(
            [(c["ref"], c["train"]) for c in tk["tracks"][0]["cars"]],
            [("tr-0001:0", "search"), ("tr-0001:1", "search")],
        )
        _, car = self.web.json("/api/car/st-0001")
        self.assertEqual(car["records"]["object"]["id"], "st-0001")
        self.assertIsNone(car["records"]["car"])

    def fill_queues(self):
        """One of every queue, open and closed: todo, think, ballots, the team stack, orders, brainstorms (and the wish)."""
        from depot2.extras import Brainstorms, Orders
        from depot2.stack import Ballots

        for argv in (
            ("todo", "add", "--text", "хвост", "--prio", "high"),
            ("todo", "add", "--text", "сделан"),
            ("todo", "done", "td-0002", "--reason", "готово"),
            ("think", "add", "--topic", "Подумать о поиске"),
            ("tstack", "add", "--kind", "discuss", "--title", "Обсудить поиск"),
            ("decide", "all", "--text", "Первое: вагон tr-0001:0 и #301"),
            ("decide", "all", "--text", "Второе: поиск по dec-000001"),
        ):
            rc, _, err = self.team(*argv)
            self.assertEqual(rc, 0, (argv, err))
        bq = Ballots(self.t.state)
        bq.add({"id": "2015-11-19-discuss", "kind": "discuss", "finished": NOW}, ["Q1", "Q2"], "Тема")
        bq.add({"id": "2015-11-18-ballot", "kind": "ballot", "finished": NOW}, ["B1"], "Старая")
        bq.close("2015-11-18-ballot", "answered")
        Orders(self.t.state).add(
            "ord-0001", "queued", character="builder", round="r1", command="node tools/catalog/import.mjs"
        )
        Orders(self.t.state).add("ord-0002", "settled", character="painter", round="r1", command="node tools/x.mjs")
        Brainstorms(self.t.state).add("bs-0001", "approved", initiator="counter", round="r1", topic="Тема")

    def test_the_decisions_and_the_queues_are_their_json_commands(self):
        self.fill_queues()
        pairs = [
            ("/api/decisions", ["decisions", "--json"]),
            (
                "/api/decisions?q=%D0%9F%D0%9E%D0%98%D0%A1%D0%9A&limit=1",
                ["decisions", "--grep", "ПОИСК", "--limit", "1", "--json"],
            ),
            ("/api/decisions?limit=0", ["decisions", "--limit", "0", "--json"]),
            ("/api/queue/tstack", ["tstack", "--json"]),
        ]
        for name, argv in (
            ("todo", ["todo"]),
            ("think", ["think"]),
            ("wishes", ["wishes"]),
            ("ballots", ["ballots"]),
            ("orders", ["orders"]),
            ("brainstorms", ["brainstorm", "list"]),
        ):
            pairs += [
                ("/api/queue/" + name, [*argv, "--json"]),
                (f"/api/queue/{name}?all=1", [*argv, "--all", "--json"]),
            ]
        for path, argv in pairs:
            code, got = self.web.json(path)
            self.assertEqual(code, 200, (path, got))
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 0, err)
            self.assertEqual(got, json.loads(out), path)
        _, d = self.web.json("/api/decisions")
        self.assertEqual([r["id"] for r in d["decisions"]], ["dec-000002", "dec-000001"])  # newest first
        self.assertEqual(
            (d["decisions"][1]["links"]["cars"], d["decisions"][1]["links"]["prs"]), (["tr-0001:0"], [301])
        )
        _, d = self.web.json("/api/decisions?q=%D0%BF%D0%BE%D0%B8%D1%81%D0%BA")
        self.assertEqual(
            ([r["id"] for r in d["decisions"]], d["found"], d["total"], d["grep"]), (["dec-000002"], 1, 2, "поиск")
        )
        self.assertEqual([e["id"] for e in self.web.json("/api/queue/todo")[1]], ["td-0001"])
        self.assertEqual([e["id"] for e in self.web.json("/api/queue/todo?all=1")[1]], ["td-0001", "td-0002"])
        self.assertEqual([e["round"] for e in self.web.json("/api/queue/ballots")[1]], ["2015-11-19-discuss"])
        self.assertEqual([e["id"] for e in self.web.json("/api/queue/orders")[1]], ["ord-0001"])
        self.assertEqual(self.web.json("/api/queue/tstack")[1]["queue"][0]["title"], "Обсудить поиск")
        _, m = self.web.json("/api/meta")
        self.assertEqual([q["name"] for q in m["queues"]], [n for n, _, _ in webui.QUEUES])
        self.assertEqual(
            [q["name"] for q in m["queues"]], ["todo", "think", "wishes", "ballots", "tstack", "orders", "brainstorms"]
        )
        q = {x["name"]: x for x in m["queues"]}
        self.assertEqual(
            (q["todo"]["title"], q["todo"]["command"], q["todo"]["open"]), ("Todo", "depot2 todo", ["open", "in_pr"])
        )
        self.assertEqual(
            (q["brainstorms"]["command"], q["orders"]["open"], q["tstack"]["open"]),
            ("depot2 brainstorm list", ["queued", "done"], []),
        )
        self.assertEqual(
            (
                q["todo"]["status_ru"]["wont"],
                q["ballots"]["status_ru"]["presenting"],
                q["todo"]["labels"]["kind"]["bug"],
                q["tstack"]["labels"]["who"]["all"],
                m["priority"]["max"],
            ),
            ("не будем", "показываю", "баг", "все", 10),
        )
        for name in q:  # every queue's open statuses have their names
            self.assertTrue(set(q[name]["open"]) <= set(q[name]["status_ru"]), name)

    def test_returns_and_claims_show_with_their_labels(self):
        """Car 3's fields: in the API as `car show --json` and `board --json` have them; the page labels them."""
        html = self.web.get("/")[2].decode("utf-8")
        for label in (
            "Брак и рекламации",
            "Брак: возвратов в сборку",
            "Рекламации (дефект на проде)",
            "Чинит рекламации",
            "брак: возврат в сборку",
            "виновник не найден",
            '"returns", "claims", "claim_links", "kind"',
        ):
            self.assertIn(label, html)
        self.team("stack", "set", "st-0001", "--pr", "301")
        self.assertEqual(self.team("stack", "post", "st-0001", "--force", "--why", "тест")[0], 0)
        self.assertEqual(self.team("stack", "start", "st-0001", "--reason", "доработать")[0], 0)
        rc, _out, err = self.team("repair", "add", "--of", "st-0002", "--force", "--why", "на проде дефект")
        self.assertEqual(rc, 0, err)
        _, car = self.web.json("/api/car/st-0001")
        self.assertEqual((car["returns"], car["claims"]), (1, 0))
        self.assertTrue([r for r in car["stages"] if r.get("return")])
        _, car = self.web.json("/api/car/st-0002")
        self.assertEqual((car["claims"], car["claim_links"][0]["repair"]), (1, "st-0004"))
        _, rep = self.web.json("/api/car/st-0004")
        self.assertEqual((rep["repair"], rep["records"]["object"]["kind"]), ({"of": "st-0002", "bug": None}, "repair"))
        _, b = self.web.json("/api/board")
        items = {i["ref"]: i for c in b["columns"] for i in c["items"]}
        self.assertEqual(
            (items["st-0001"]["returns"], items["st-0002"]["claims"], items["st-0004"]["repair"]["of"]),
            (1, 1, "st-0002"),
        )

    def test_a_moved_car_by_its_old_ref(self):
        html = self.web.get("/")[2].decode("utf-8")
        for label in (
            "перенесены отсюда: ",
            "старый номер: вагон перенесён, теперь ",
            '[T("panel.moved"), ',
            '"moved_from", "priority"]',
        ):
            self.assertIn(label, html)
        rc, _out, err = self.team("tracks", "car", "move", "tr-0001:1", "--n", "5", "--why", "номер")
        self.assertEqual(rc, 0, err)
        _, t = self.web.json("/api/track/tr-0001")
        p = t["moved_out"][0]
        self.assertEqual((p["n"], p["to"], p["now"], p["why"]), ("1", "tr-0001:5", "tr-0001:5", "номер"))
        _, car = self.web.json("/api/car/tr-0001:1")
        self.assertEqual((car["ref"], car["car"]), ("tr-0001:1", "tr-0001:5"))
        self.assertEqual(car["records"]["car"]["moved_from"][0]["from"], "tr-0001:1")
        _, trains = self.web.json("/api/trains")
        self.assertEqual(next(tn["cars"] for tn in trains["trains"] if tn["name"] == "search")[1]["ref"], "tr-0001:5")

    def test_the_agents_are_their_json_command_and_never_booked_here(self):
        fx = str(Path(HERE, "transcripts", "-work-depot", "sess-0001", "subagents"))
        live, done = (str(Path(self.scratch, n + ".jsonl")) for n in ("live", "done"))
        shutil.copy(Path(fx, "agent-b1e2d3c4f5a6b7c80.jsonl"), live)
        shutil.copy(Path(fx, "agent-a0f1c2d3e4b5a6978.jsonl"), done)
        for ref, aid, path in (("tr-0001:0", "b1e2d3c4f5a6b7c80", live), ("tr-0001:1", "a0f1c2d3e4b5a6978", done)):
            rc, out, err = self.team(
                "agents", "start", ref, "--role", "sketch", "--usage", "0.5", "--force", "--why", "проба"
            )
            self.assertEqual(rc, 0, err)
            rc, _, err = self.team("agents", "attach", out.splitlines()[0], aid, "--transcript", path)
            self.assertEqual(rc, 0, err)
        shutil.copy(Path(fx, "agent-a0f1c2d3e4b5a6978.meta.json"), done[: -len(".jsonl")] + ".meta.json")  # done now
        before = tree_hash(self.t.dir)
        code, got = self.web.json("/api/agents?all=1")
        self.assertEqual(code, 200, got)
        self.assertEqual(
            [(a["id"], a["state"], a["live"]) for a in got["agents"]],
            [("ag-0001", "silent", True), ("ag-0002", "finished", False)],
        )
        self.assertEqual(got["agents"][1]["spent_mtok"], 0.1525)
        self.assertNotIn("booked", got["agents"][1])
        self.assertEqual([a["id"] for a in self.web.json("/api/agents")[1]["agents"]], ["ag-0001"])
        self.assertEqual(tree_hash(self.t.dir), before)  # nothing booked, nothing written
        self.team("agents")  # the tool books it
        from depot2 import agents as agents_mod

        with mock.patch.object(agents_mod, "now_iso", lambda: NOW):
            for path, argv in (
                ("/api/agents", ["agents", "--json"]),
                ("/api/agents?all=1", ["agents", "--all", "--json"]),
            ):
                code, got = self.web.json(path)
                rc, out, err = self.team(*argv)
                self.assertEqual(rc, 0, err)
                self.assertEqual(got, json.loads(out), path)
        self.assertEqual(got["agents"][1]["booked"]["mtok"], 0.1525)

    def test_a_car_from_a_queue_and_where_the_entry_went(self):
        html = self.web.get("/")[2].decode("utf-8")
        for label in (
            '[T("panel.from"), ',
            '[T("panel.taken_to"), j.taken_to ? linked(j.taken_to)',
            'e.status === "taken" && e.taken_to',
            '"Пришёл из"',
            '"Взят в"',
        ):
            self.assertIn(label, html)
        rc, out, err = self.team("wishes", "add", "--title", "из очереди в трек", "--area", "tools")
        self.assertEqual(rc, 0, err)
        wid = out.split()[0]
        rc, out, err = self.team("wishes", "take", wid, "--to", "tr-0001")
        self.assertEqual(rc, 0, err)
        ref = out.split()[2].rstrip(":")
        _, car = self.web.json("/api/car/" + ref.replace(":", "%3A"))
        self.assertEqual(
            [(x["ref"], x["kind_ru"], x["title"]) for x in car["from"]], [(wid, "хотелка", "из очереди в трек")]
        )
        _, entry = self.web.json("/api/car/" + wid)
        self.assertEqual(entry["taken_to"], ref)
        self.assertEqual(
            (car["status"], car["status_ru"], entry["status"], entry["status_ru"]),
            ("waiting", "ждёт", "taken", "взята"),
        )
        for part in (
            "esc(j.status_ru || j.status)",
            "esc(p.e.status_ru || p.e.status)",
            'said(T("panel.owner_words"), [].concat(entry.words || []))',
            'said(T("panel.questions"), qs)',
            'said(T("panel.text"), [].concat(entry.text || []))',
            '"Слова владельца"',
            '"Вопросы"',
            '"Текст"',
        ):
            self.assertIn(part, html)
        _, meta = self.web.json("/api/meta")
        self.assertEqual(
            next(q for q in meta["queues"] if q["name"] == "ballots")["labels"]["kind"]["discuss"],
            "обсуждение с командой",
        )

    def fill_merge(self):
        """A merge queue of every kind (made up): two merged ones (the second with the plugin's lines), one riding (merged,
        its deploy on the way), one stopped at CI, one waiting; one dropped before its merge, one dropped after it.
        """
        from depot2 import merge

        q = merge.MergeQueue(self.t.state)

        def put(n, status, **kw):
            e = dict(
                {
                    "id": q.new_id(),
                    "pr": n,
                    "title": "PR %d" % n,  # noqa: UP031
                    "branch": "feat-%d" % n,  # noqa: UP031
                    "worktree": "/nowhere",
                    "st": None,
                    "car": None,
                    "status": status,
                    "word": "%d — merge it" % n,  # noqa: UP031
                    "added": NOW,
                    "step": None,
                    "log": ["2015-11-12T11:00:00 added"],
                },
                **kw,
            )
            q.data["entries"].append(e)
            return e

        put(
            401,
            "merged",
            merged={"sha": "a" * 40, "ts": "2015-11-12T10:00:00+02:00"},
            deploy={"run": 7, "conclusion": "success", "deploy_job": "success", "site": "200"},
            smoke={"ok": True, "summary": "checks 3/3 ok"},
        )
        put(
            402,
            "merged",
            merged={"sha": "b" * 40, "ts": "2015-11-12T11:00:00+02:00"},
            smoke={"ok": None, "summary": "off"},
            usage={"lines": ["release notes: 3 lines", "  index size  12.4 MB  "], "plugins": [], "ts": NOW},
        )
        put(403, "dropped", dropped={"reason": "not needed", "ts": NOW})
        put(
            404,
            "riding",
            step="deploy",
            step_since=NOW,
            head="c" * 40,
            car="tr-0001:0",
            merged={"sha": "d" * 40, "ts": NOW},
        )
        put(405, "stopped", stop={"step": "ci", "ts": NOW, "reason": "CI failed: test_dates"}, st="st-0002")
        put(406, "waiting", word="406: «yes», as said")
        put(
            407,
            "dropped",
            merged={"sha": "e" * 40, "ts": NOW},
            dropped={"reason": "its deploy rides with the next", "ts": NOW},
        )  # merged, then dropped
        q.save("test", "mq-0001")

    def test_the_merge_queue_tab_is_its_json_commands(self):
        """The tab's data: `merge --json` (the open entries in their order, the runner, the last merge) and `merge report
        --json --last N` (the last merges with the project's after_merge lines, as written); its words in /api/meta.
        """
        self.fill_merge()
        for path, argv in (
            ("/api/merge", ["merge", "--json"]),
            ("/api/merge/report", ["merge", "report", "--json"]),
            ("/api/merge/report?last=10", ["merge", "report", "--last", "10", "--json"]),
            ("/api/merge/report?last=2", ["merge", "report", "--last", "2", "--json"]),
        ):
            code, got = self.web.json(path)
            self.assertEqual(code, 200, (path, got))
            rc, out, err = self.team(*argv)
            self.assertEqual(rc, 0, err)
            self.assertEqual(got, json.loads(out), path)
        _, j = self.web.json("/api/merge")
        self.assertEqual(
            [(e["pr"], e["status"], e["status_ru"]) for e in j["entries"]],
            [(404, "riding", "едет"), (405, "stopped", "остановлен"), (406, "waiting", "ждёт")],
        )
        self.assertEqual((j["runner_pid"], j["runner_idle"]), (None, {"open": 3, "stopped": 405}))
        self.assertEqual((j["last_merged"]["pr"], j["last_merged"]["ts"]), (404, NOW))  # the newest merge, its time
        self.assertEqual(j["entries"][2]["word"], "406: «yes», as said")  # verbatim
        _, rep = self.web.json("/api/merge/report?last=10")
        self.assertEqual(
            [e["pr"] for e in rep], [401, 402, 404]
        )  # merged, in the queue's order (404 rides on: its deploy)
        # (407, merged and then dropped, is not among them, as in `merge report`)
        self.assertEqual(
            rep[1]["usage"]["lines"], ["release notes: 3 lines", "  index size  12.4 MB  "]
        )  # as the hook wrote them
        self.assertEqual([e["pr"] for e in self.web.json("/api/merge/report")[1]], [404])  # one, as the command
        rc, md, err = self.team("merge", "report", "--last", "10")  # the Markdown report: the same merges
        self.assertEqual(rc, 0, err)
        self.assertEqual(re.findall(r"^#(\d+) PR \d+ — смерджен", md, re.MULTILINE), ["401", "402", "404"])
        self.assertNotIn("#407", md)
        _, m = self.web.json("/api/meta")
        mm = m["merge"]
        self.assertEqual(mm["steps"], ["sync", "ci", "merge", "deploy", "smoke", "usage"])
        self.assertEqual(
            (mm["step_ru"]["ci"], mm["status_ru"]["stopped"], mm["smoke_ru"]["skipped"]),
            ("CI на голове", "остановлен", "пропущен"),
        )
        self.assertTrue(mm["means"].startswith("PR по слову владельца"))
        html = self.web.get("/")[2].decode("utf-8")
        for part in (
            'id="view-merge"',
            '"/api/merge/report?last=" + MERGES_SHOWN',
            "const MERGES_SHOWN = 10;",
            "function renderMerge()",
            'T("mq.todo", { pr: esc(e.pr) })',
            'if (j.runner_pid) return T("mq.runner_rides");',
            'lines.map((x) => "<li>" + hl(x)',
            'data-go="merge" data-mq="',
            "if (go.dataset.mq) showEntry(go.dataset.mq);",
            ".mcard[data-ref]",
            "if (it.merge && !mqRef.has(it.merge)) mqRef.set(it.merge, it.ref);",
            "(x.h || esc(x.t))",
        ):
            self.assertIn(part, html)
        self.assertNotIn("runner_pid +", html)  # the runner's pid is never shown, only whether it rides
        msgs = page_messages(html)
        self.assertIn("<code>depot2 merge resume {pr}</code>", msgs["mq.todo"])
        self.assertIn("<code>depot2 merge drop {pr} --reason …</code>", msgs["mq.todo"])
        self.assertNotIn("{pid}", msgs["mq.runner_rides"] + msgs["mq.runner_idle"] + msgs["mq.runner_empty"])
        self.assertEqual(msgs["tab.merge"], "Мердж-очередь")

    def test_not_found_and_only_get(self):
        for path in (
            "/api/car/st-0999",
            "/api/car/tr-0001:9",
            "/api/car/zz",
            "/api/car/..%2F..%2Fetc",
            "/api/train/tr-0009",
            "/api/train/x",
            "/api/train/Search",
            "/api/track/tr-0009",
            "/api/track/search",
            "/api/nope",
            "/nope",
            "/api/board?view=zz",
            "/api/queue/nope",
            "/api/queue/..%2Fstack",
            "/api/queue/stack",
            "/api/queue",
            "/api/queue/trains",
            "/api/decisions?limit=x",
            "/api/decisions?limit=-1",
            "/api/merge/report?last=0",
            "/api/merge/report?last=x",
            "/api/merge/report?last=-2",
            "/api/merge/nope",
            "/api/merge/report/1",
        ):
            code, j = self.web.json(path)
            self.assertEqual(code, 404, path)
            self.assertIn("error", j)
        for m in ("POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            code, h, _ = self.web.request(m, "/api/board")
            self.assertEqual(code, 405, m)
            if m != "HEAD":
                self.assertEqual(h.get("Allow"), "GET")

    def test_the_brief_and_the_paths(self):
        code, j = self.web.json("/api/brief/st-0001")
        self.assertEqual(code, 200, j)
        self.assertEqual((j["path"], j["cut"]), (self.web_brief, False))
        self.assertIn("<script>alert(1)</script>", j["text"])  # as text: the page escapes it
        self.assertEqual(self.web.json("/api/brief/tr-0001%3A0")[0], 200)
        self.assertEqual(self.web.json("/api/brief/st-0002")[0], 404)  # no brief
        code, j = self.web.json("/api/brief/st-0003")  # its brief is a link out of the read dirs
        self.assertEqual(code, 404)
        self.assertNotIn("private", json.dumps(j))
        self.assertEqual(
            self.web.json("/api/doc?path=design/sketch.md"),
            (200, {"path": "design/sketch.md", "text": "# Эскиз\n", "cut": False}),
        )
        refused = [
            "../briefs/search.md",
            "briefs/../notes/secret.md",
            "notes/secret.md",
            "drafts/x.md",
            self.secret,
            "%2F" + self.secret.lstrip("/"),
            "~/x.md",
            "briefs/out.md",
            "briefs",
            "briefs/search.txt",
            "briefs//search.md",
            "briefs/./search.md",
            "briefs%5Csearch.md",
            "",
            "briefs/search.md%00",
        ]
        for rel in refused:
            code, j = self.web.json("/api/doc?path=" + rel)
            self.assertEqual(code, 404, rel)
            self.assertNotIn("private", json.dumps(j))
        self.assertEqual(self.web.json("/api/doc?path=briefs/search.md")[0], 200)

    def test_loopback_only(self):
        self.assertEqual(self.web.srv.server_address[0], "127.0.0.1")
        for host in ("0.0.0.0", "", "192.168.1.10", "::"):
            with self.assertRaises(ValueError):
                webui.Server(self.t.state, port=0, host=host)
        code, _, body = self.web.get("/api/board", host="evil.example:%d" % self.web.port)  # DNS rebinding  # noqa: UP031
        self.assertEqual(code, 403)
        self.assertNotIn(b"columns", body)
        self.assertEqual(self.web.get("/api/meta", host="localhost:%d" % self.web.port)[0], 200)  # noqa: UP031
        ip = outside_ip()
        if ip:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                with self.assertRaises(OSError):
                    s.connect((ip, self.web.port))
            finally:
                s.close()

    def test_nothing_written_nothing_run(self):
        self.fill_queues()
        self.fill_merge()
        before = tree_hash(self.t.dir)
        runs = []

        def no_popen(*a, **kw):
            runs.append(a)
            raise AssertionError(f"a handler ran a command: {a!r}")

        paths = [
            "/",
            "/api/meta",
            "/api/board",
            "/api/board?all=1",
            "/api/board?view=stack",
            "/api/trains",
            "/api/train/search",
            "/api/tracks",
            "/api/track/tr-0001",
            "/api/stack",
            "/api/merge",
            "/api/merge/report",
            "/api/merge/report?last=5",
            "/api/car/st-0001",
            "/api/car/tr-0001%3A1",
            "/api/car/wl-0001",
            "/api/brief/st-0001",
            "/api/doc?path=design/sketch.md",
            "/api/decisions",
            "/api/decisions?q=x&limit=1",
            "/api/agents",
            "/api/agents?all=1",
        ] + [f"/api/queue/{n}{a}" for n, _, _ in webui.QUEUES for a in ("", "?all=1")]
        with mock.patch.object(subprocess, "Popen", no_popen), mock.patch.object(os, "system", no_popen):
            for p in paths:
                self.assertEqual(self.web.get(p)[0], 200, p)
        self.assertEqual(runs, [])
        self.assertEqual(tree_hash(self.t.dir), before)

    def test_a_state_read_mid_write(self):
        p = str(Path(self.t.dir, "stack.json"))
        with Path(p).open(encoding="utf-8") as f:
            good = f.read()
        with Path(p).open("w", encoding="utf-8") as f:
            f.write(good[: len(good) // 2])
        with mock.patch.object(webui, "RETRY_S", 0.01):
            code, j = self.web.json("/api/board")
        self.assertEqual(code, 503)
        self.assertTrue(j["retry"])
        self.assertIn("повторит при следующем обновлении", j["error"])
        self.assertEqual(self.web.json("/api/meta")[0], 200)  # the server is still up
        with Path(p).open("w", encoding="utf-8") as f:
            f.write(good)
        self.assertEqual(self.web.json("/api/board")[0], 200)

    def test_meta(self):
        code, j = self.web.json("/api/meta")
        self.assertEqual(code, 200)
        self.assertEqual(
            (j["name"], j["repo"], j["route"][0], j["route"][-1]),
            ("depot2", "example-org/example-project", "bogie", "arrived"),
        )
        self.assertEqual(len(j["stages"]), 10)
        self.assertEqual(j["route"][3:6], ["build", "docs", "acceptance"])
        self.assertEqual(j["moves"]["docs"], ["acceptance", "queued", "build", "scrapped"])
        c = j["code"]  # the code that serves it: the page's header shows it
        self.assertEqual(sorted(c), ["broken", "commit", "date", "dirty", "pid", "stamp", "started"])
        self.assertEqual((c["stamp"], c["pid"], c["broken"]), (webui.code_stamp(), os.getpid(), None))
        self.assertTrue(c["commit"] is None or len(c["commit"]) >= 7)

    def test_a_depot_waiting_for_its_migration_is_answered_503_and_never_written(self):
        """The view never writes: a depot from before the tracks and the trains (schema 1) waits for its first command."""
        from depot2.migrate import SCHEMA
        from depot2.store import write_json

        old = {"next": 2, "trains": [{"id": "tr-0001", "title": "Поезд", "status": "running", "cars": []}]}
        write_json(self.t.state.path("trains.json"), old)
        before = tree_hash(self.t.dir)
        for path in (
            "/api/board",
            "/api/tracks",
            "/api/trains",
            "/api/stack",
            "/api/car/st-0001",
            "/api/decisions",
            "/api/queue/todo",
        ):
            code, j = self.web.json(path)
            self.assertEqual(code, 503, path)
            self.assertTrue(j["migrate"] and j["retry"], path)
            self.assertIn("данные депо ждут миграции (схема 1 → %d)" % SCHEMA, j["error"])  # noqa: UP031
        self.assertEqual(self.web.json("/api/meta")[0], 200)  # the page's constants: so it can say so
        self.assertEqual(tree_hash(self.t.dir), before)


class ReexecTest(unittest.TestCase):
    def test_a_code_change_re_executes_the_server_on_its_socket(self):
        tmp = tempfile.mkdtemp(prefix="team-web-reexec-")
        try:
            code = str(Path(tmp, "code"))
            Path(code).mkdir(parents=True)
            src = str(Path(HERE).parent)
            shutil.copytree(Path(src, "depot2"), Path(code, "depot2"), ignore=shutil.ignore_patterns("__pycache__"))
            state = str(Path(tmp, "state"))
            Path(state).mkdir(parents=True)
            env = dict(os.environ, DEPOT2_BACKUP="off", DEPOT2_FSYNC="0")
            env.pop(webui.FD_ENV, None)
            p = subprocess.Popen(
                [sys.executable, str(Path(code, "depot2", "__main__.py")), "--state", state, "web", "--port", "0"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                universal_newlines=True,
            )
            try:
                port = int(p.stdout.readline().split("127.0.0.1:")[1].split("/")[0])

                def get(path):
                    c = http.client.HTTPConnection("127.0.0.1", port, timeout=20)
                    try:
                        c.request("GET", path, headers={"Host": "127.0.0.1:%d" % port})  # noqa: UP031
                        r = c.getresponse()
                        return r.status, dict(r.getheaders()), r.read()
                    finally:
                        c.close()

                code0, _, body = get("/api/meta")
                self.assertEqual(code0, 200)
                meta0 = json.loads(body)["code"]
                self.assertIsNone(meta0["commit"])  # a copy outside git: its stamp only
                # new code on disk: a file of it changes (as a pull of main does)
                with Path(code, "depot2", "webui.py").open("a") as f:
                    f.write("\n# a change\n")
                import time

                time.sleep(webui.CODE_CHECK_S + 0.2)
                st, h, _ = get("/api/meta")
                self.assertEqual((st, h["Location"]), (307, "/api/meta"))  # the old process sends the browser back
                for _ in range(50):
                    try:
                        st, _, body = get("/api/meta")
                        if st == 200:
                            break
                    except (ConnectionError, OSError):
                        pass
                    time.sleep(0.1)
                self.assertEqual(st, 200)
                meta1 = json.loads(body)["code"]
                self.assertEqual(meta1["pid"], meta0["pid"])  # the same process, exec'd
                self.assertNotEqual(meta1["stamp"], meta0["stamp"])
                self.assertEqual(meta1["stamp"], webui.code_stamp(code))
                self.assertEqual(get("/api/tracks")[0], 200)  # an API of the new code, on the same port
                # code that does not compile is not run: the old one keeps serving and says why
                with Path(code, "depot2", "webui.py").open("a") as f:
                    f.write("\ndef broken(:\n")
                time.sleep(webui.CODE_CHECK_S + 0.2)
                st, _, body = get("/api/meta")
                self.assertEqual(st, 200)
                meta2 = json.loads(body)["code"]
                self.assertEqual(meta2["stamp"], meta1["stamp"])
                self.assertIn("depot2/webui.py", meta2["broken"])
            finally:
                p.terminate()
                out, _err = p.communicate(timeout=10)
            self.assertIn("код изменился — перезапускаюсь", out)
            self.assertIn("перезапущен на новом коде (код вне git", out)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


class WebHostsTest(WebBase):
    """The config's `web.hosts`: more names of this computer for the Host check, each with the server's port. The
    peer check and the socket stay as they were: loopback only.
    """

    def served(self, *sets):
        w = Served(
            self.t.state, self.sets + list(sets), read_dirs=self.dirs, doc_base=self.scratch, docs_base=self.scratch
        )
        self.addCleanup(w.close)
        return w

    def test_an_empty_list_is_as_before(self):
        self.assertEqual(self.t.cfg["web"], {"hosts": []})  # the default
        for w in (self.web, self.served("web.hosts=[]")):
            p = w.port
            self.assertEqual((w.srv.names, w.srv.dropped, w.srv.urls), ([], [], []))
            self.assertEqual(w.srv.hosts, {"127.0.0.1:%d" % p, "localhost:%d" % p, "[::1]:%d" % p})  # noqa: UP031
            for host, code in (
                ("127.0.0.1:%d" % p, 200),  # noqa: UP031
                ("localhost:%d" % p, 200),  # noqa: UP031
                ("mybox.local:%d" % p, 403),  # noqa: UP031
                ("mybox.local", 403),
                ("evil.example:%d" % p, 403),  # noqa: UP031
            ):
                self.assertEqual(w.get("/api/meta", host=host)[0], code, host)

    def test_a_listed_name_with_the_servers_port(self):
        w = self.served('web.hosts=["mybox.local", "Depot.Example"]')
        p = w.port
        self.assertEqual(w.srv.urls, ["http://mybox.local:%d/" % p, "http://depot.example:%d/" % p])  # noqa: UP031
        self.assertEqual(w.get("/", host="mybox.local:%d" % p)[0], 200)  # noqa: UP031
        for host in (
            "mybox.local:%d" % p,  # noqa: UP031
            "MYBOX.Local:%d" % p,  # noqa: UP031
            "depot.example:%d" % p,  # noqa: UP031
            "127.0.0.1:%d" % p,  # noqa: UP031
            "localhost:%d" % p,  # noqa: UP031
        ):
            code, j = w.get("/api/meta", host=host)[0::2]
            self.assertEqual((code, json.loads(j.decode("utf-8"))["name"]), (200, "depot2"), host)
        for host in (
            "mybox.local:%d" % (p + 1),  # noqa: UP031
            "mybox.local:80",
            "mybox.local",
            "mybox.local:%d:1" % p,  # noqa: UP031
            "other.local:%d" % p,  # noqa: UP031
            "evil.example:%d" % p,  # noqa: UP031
            "xmybox.local:%d" % p,  # noqa: UP031
            "mybox.local.evil.example:%d" % p,  # noqa: UP031
            "mybox.local.:%d" % p,  # noqa: UP031
        ):
            code, _, body = w.get("/api/board", host=host)
            self.assertEqual(code, 403, host)
            self.assertNotIn(b"columns", body)
        self.assertEqual(w.srv.server_address[0], "127.0.0.1")  # a name never moves the socket

    def test_a_peer_from_outside_is_refused_whatever_the_name(self):
        hosts = 'web.hosts=["mybox.local"]'
        w = self.served(hosts)
        p = w.port
        names = ("mybox.local:%d" % p, "127.0.0.1:%d" % p, "localhost:%d" % p)  # noqa: UP031
        for addr in ("192.168.1.10", "10.0.0.5", "8.8.8.8", "0.0.0.0", "2001:db8::1", "::ffff:192.168.1.10"):
            for host in names:
                peer = Peer(addr, host)
                self.assertFalse(w.srv.authorize(peer), (addr, host))
                self.assertEqual(peer.sent, [403], (addr, host))
        for addr in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            for host in names:
                peer = Peer(addr, host)
                self.assertTrue(w.srv.authorize(peer), (addr, host))
                self.assertEqual(peer.sent, [])
            self.assertFalse(w.srv.authorize(Peer(addr, "other.local:%d" % p)), addr)  # noqa: UP031
        for host in ("0.0.0.0", "mybox.local", "192.168.1.10", "::"):  # and no name is an address to listen on
            with self.assertRaises(ValueError):
                webui.Server(self.t.state, sets=[*self.sets, hosts], port=0, host=host)
        ip = outside_ip()
        if ip:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2)
            try:
                with self.assertRaises(OSError):
                    s.connect((ip, p))
            finally:
                s.close()

    def test_what_is_not_a_host_name_is_dropped(self):
        bad = [
            "",
            " ",
            "mybox local",
            " mybox.local",
            "mybox.local ",
            "mybox.local\n",
            "mybox.local:8765",
            "http://mybox.local",
            "mybox.local/",
            "a/b",
            "a\\b",
            "*",
            "*.local",
            "[::1]",
            "::1",
            "mybox..local",
            ".local",
            "mybox.local.",
            "-mybox.local",
            "mybox-.local",
            "my_box.local",
            "депо.local",
            "mybox.lo\u212aal",
            "a" * 64 + ".local",
            ".".join(["a" * 60] * 5),
            8765,
            None,
            True,
            ["mybox.local"],
            {"a": 1},
        ]
        self.assertEqual(webui.extra_hosts(bad), ([], bad))
        good = ["Mybox.LOCAL", "mybox.local", "localhost", "127.0.0.1", "a-b.c1.example", "a" * 63 + ".local"]
        self.assertEqual(webui.extra_hosts(good), (["mybox.local", "a-b.c1.example", "a" * 63 + ".local"], []))
        for value, want in (
            (None, ([], [])),
            ([], ([], [])),
            ("mybox.local", (["mybox.local"], [])),
            (5, ([], [5])),
            ({"mybox.local": 1}, ([], [{"mybox.local": 1}])),
        ):
            self.assertEqual(webui.extra_hosts(value), want, value)
        w = self.served('web.hosts=["mybox.local:8765", "bad name", "ok.local", "*"]')
        p = w.port
        self.assertEqual((w.srv.names, w.srv.dropped), (["ok.local"], ["mybox.local:8765", "bad name", "*"]))
        self.assertEqual(w.get("/api/meta", host="ok.local:%d" % p)[0], 200)  # noqa: UP031
        for host in (
            "mybox.local:%d" % p,  # noqa: UP031
            "mybox.local:8765",
            "mybox.local:8765:%d" % p,  # noqa: UP031
            "bad name:%d" % p,  # noqa: UP031
            "*:%d" % p,  # noqa: UP031
        ):
            self.assertEqual(w.get("/api/meta", host=host)[0], 403, host)
        self.assertEqual(self.served("web=mybox.local").srv.names, [])  # not an object: no names, no crash

    def test_the_names_are_read_from_the_config_at_the_start(self):
        path = str(Path(self.t.dir, "config.json"))
        self.assertFalse(Path(path).exists())
        with Path(path).open("w", encoding="utf-8") as f:
            json.dump({"web": {"hosts": ["mybox.local"]}}, f)
        w = self.served()
        self.assertEqual(w.get("/api/meta", host="mybox.local:%d" % w.port)[0], 200)  # noqa: UP031
        self.assertEqual(self.web.get("/api/meta", host="mybox.local:%d" % self.web.port)[0], 403)  # started before  # noqa: UP031
        Path(path).unlink()
        self.assertEqual(w.get("/api/meta", host="mybox.local:%d" % w.port)[0], 200)  # until it is restarted  # noqa: UP031
        again = self.served()
        self.assertEqual(again.get("/api/meta", host="mybox.local:%d" % again.port)[0], 403)  # noqa: UP031


class WebCliTest(unittest.TestCase):
    def test_cli_web_prints_the_address_and_serves(self):
        d = tempfile.mkdtemp(prefix="team-web-cli-")
        try:
            env = dict(os.environ, DEPOT2_BACKUP="off", DEPOT2_FSYNC="0")
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(HERE).parent / "depot2" / "__main__.py"),
                    "--state",
                    d,
                    "web",
                    "--port",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                universal_newlines=True,
            )
            try:
                line = p.stdout.readline()
                self.assertRegex(line, r"^depot2: http://127\.0\.0\.1:\d+/ — только показ")
                port = int(line.split("127.0.0.1:")[1].split("/")[0])
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.request("GET", "/api/meta")
                r = c.getresponse()
                self.assertEqual((r.status, json.loads(r.read())["name"]), (200, "depot2"))
                c.close()
            finally:
                p.terminate()
                out, err = p.communicate(timeout=10)
            self.assertNotIn("web.hosts", out + err)  # no extra names: the one line, as before
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def test_the_web_command_prints_the_extra_names(self):
        d = tempfile.mkdtemp(prefix="team-web-cli-")
        try:
            env = dict(os.environ, DEPOT2_BACKUP="off", DEPOT2_FSYNC="0")
            p = subprocess.Popen(
                [
                    sys.executable,
                    str(Path(HERE).parent / "depot2" / "__main__.py"),
                    "--state",
                    d,
                    "--set",
                    'web.hosts=["Mybox.local", "bad name"]',
                    "web",
                    "--port",
                    "0",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                universal_newlines=True,
            )
            try:
                first, second = p.stdout.readline(), p.stdout.readline()
                self.assertRegex(first, r"^depot2: http://127\.0\.0\.1:\d+/ — только показ")
                port = int(first.split("127.0.0.1:")[1].split("/")[0])
                self.assertEqual(
                    second,
                    "depot2: и по именам из web.hosts (этот же компьютер): "  # noqa: UP031
                    "http://mybox.local:%d/\n" % port,
                )
                c = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
                c.putrequest("GET", "/api/meta", skip_host=True)
                c.putheader("Host", "mybox.local:%d" % port)  # noqa: UP031
                c.endheaders()
                r = c.getresponse()  # answered: the start's lines are all printed by now
                self.assertEqual((r.status, json.loads(r.read())["name"]), (200, "depot2"))
                c.close()
            finally:
                p.terminate()
                _, err = p.communicate(timeout=10)
            self.assertIn('warning: web.hosts: не имя хоста, пропущено: "bad name"', err)
        finally:
            shutil.rmtree(d, ignore_errors=True)


def outside_ip():
    """This machine's address on a network (not loopback), or None."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()
    return None if ip.startswith("127.") or ip == "0.0.0.0" else ip


def webui_state(root):
    from depot2.store import State

    return State(root)


if __name__ == "__main__":
    unittest.main()
