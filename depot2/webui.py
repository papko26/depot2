"""depot2's web view: `depot2 web [--port N]`.

One page (`web/index.html`: HTML, CSS and JS in one file, no build, nothing from outside) and a small JSON API on
127.0.0.1 only. No authorization, so it never listens outside loopback, and it only shows: GET alone (any other
method is 405), no handler writes to the depot or runs a command. The data are the `--json` functions, called in the
process for every request. The Host header must name this computer (127.0.0.1, localhost, or `web.hosts`). Briefs and
docs are served only as .md files inside `web.docs.dirs`. A change of the code's files re-executes the server on its
socket. The texts are the message catalogs': the server's own through i18n.t (`webui.` keys), the page's as the `web.`
keys it is served with, in the run's language (page_html).
"""

import base64
import contextlib
import hashlib
import http.server
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse

from . import i18n

NAME = "depot2"
HOST = "127.0.0.1"
LOOPBACK = ("127.0.0.1", "::1")
# The names a Host header may carry (with the server's port), before the config's `web.hosts` adds its own.
OWN_NAMES = ("127.0.0.1", "localhost", "[::1]")
# A host name: labels of letters, digits and hyphens between dots (RFC 1123), lower case; no port, no brackets.
RE_HOST = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)*")
PORT = 8765
REFRESH_S = 25
RETRY_S = 0.25
# The only files it reads besides the state: .md briefs and docs, under the config's web.docs (base, dirs).
DOCS_BASE = None
READ_DIRS = None
DOC_BASE = None
PAGE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
CODE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FD_ENV = "DEPOT2_WEB_FD"
CODE_CHECK_S = 1.0  # at most one look at the code's files a second
RESTART_WAIT_S = 0.6  # a request that found new code waits this long for its 307: the old loop stops meanwhile
RE_REF = re.compile(r"^(?:(?:st|wl|th)-\d{4}|tr-\d{4}:[^\s/\\:]{1,24})$")
RE_TRACK = re.compile(r"^tr-\d{4}$")
RE_TRAIN = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
MAX_DOC = 2 * 1024 * 1024
# the queues of the Queues tab: (name, the catalog key of its title, the command that edits it)
QUEUES = (("todo", "webui.queue.todo", "todo"), ("think", "webui.queue.think", "think"),
          ("wishes", "webui.queue.wishes", "wishes"), ("ballots", "webui.queue.ballots", "ballots"),
          ("tstack", "webui.queue.tstack", "tstack"), ("orders", "webui.queue.orders", "orders"),
          ("brainstorms", "webui.queue.brainstorms", "brainstorm list"))
RE_MESSAGES = re.compile(r'(<script type="application/json" id="messages">)(.*?)(</script>)', re.DOTALL)
RE_LANG = re.compile(r'<html lang="[^"]*">')


class NotFound(Exception):
    pass


def now_stamp():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _flag(q, key):
    return i18n.is_word((q.get(key) or [""])[-1], "in.webui.yes")


# --- the data: the `--json` functions --------------------------------------------------------------------

def board(state, cfg, q):
    from .board import VIEWS, Board
    view = (q.get("view") or ["all"])[-1]
    if view not in VIEWS:
        raise NotFound("view: {}".format(", ".join(VIEWS)))
    return Board(state, cfg).to_json(all_=_flag(q, "all"), view=view)


def tracks(state, cfg):
    from .tracks import Tracks
    return Tracks(state, cfg).to_json()


def track(state, cfg, tid):
    from .tracks import TrackError, Tracks
    if not RE_TRACK.match(tid):
        raise NotFound(i18n.t("webui.bad_track", got=repr(tid)))
    tr = Tracks(state, cfg)
    try:
        t = tr.get(tid)
    except TrackError as e:
        raise NotFound(str(e))  # noqa: B904
    return tr.track_json(t)


def trains(state, cfg):
    from .trains import Trains
    return Trains(state, cfg).to_json()


def train(state, cfg, name):
    from .trains import TrainError, Trains
    if not RE_TRAIN.match(name):
        raise NotFound(i18n.t("webui.bad_train", got=repr(name)))
    tr = Trains(state, cfg)
    try:
        t = tr.get(name)
    except TrainError as e:
        raise NotFound(str(e))  # noqa: B904
    return tr.train_json(t)


def car_of(state, cfg, ref):
    from . import stages
    from .stack import StackError
    from .tracks import TrackError
    if not RE_REF.match(ref):
        raise NotFound(i18n.t("webui.bad_ref", got=repr(ref)))
    try:
        return stages.Car(state, cfg, ref)
    except (stages.StageError, TrackError, StackError) as e:
        raise NotFound(str(e))  # noqa: B904


def car(state, cfg, ref):
    return car_of(state, cfg, ref).to_json()


def stack(state, cfg):
    from .stack import Stack
    return Stack(state, cfg).to_json()


def merge_queue(state, cfg):
    from . import merge
    return merge.to_json(state)


def merge_report(state, cfg, q):
    """`merge report --json [--last N]`: the last merges (?last=N, 1 when not given, as the command)."""
    from . import merge
    last = (q.get("last") or [""])[-1].strip()
    if last and not (last.isdigit() and int(last) >= 1):
        raise NotFound(i18n.t("webui.bad_last", got=repr(last)))
    return merge.report_json(state, last=int(last) if last else 1)


def agents(state, cfg, q):
    from .agents import Agents
    return Agents(state, cfg).to_json(include_all=_flag(q, "all"))


def principles(state, cfg, q):
    from .principles import Principles
    return Principles(state, cfg).to_json(open_only=not _flag(q, "all"))


def decisions(state, cfg, q):
    from . import decisions as dec
    limit = (q.get("limit") or [""])[-1].strip()
    if limit and not limit.isdigit():
        raise NotFound(i18n.t("webui.bad_limit", got=repr(limit)))
    return dec.to_json(state, grep=(q.get("q") or [""])[-1].strip() or None, limit=int(limit) if limit else None)


def queue(state, cfg, name, q):
    """A queue's `--json` (QUEUES): the open entries, with ?all=1 every one; the team stack is its slots and queue."""
    from . import extras, queues
    from .stack import Ballots
    from .tstack import TeamStack
    all_ = _flag(q, "all")
    if name in ("todo", "think", "wishes"):
        return getattr(queues, {"todo": "Todo", "think": "Think", "wishes": "Wishes"}[name])(state, cfg).to_json(
            open_only=not all_)
    if name == "ballots":
        return Ballots(state).entries(open_only=not all_)
    if name == "tstack":
        return TeamStack(state, cfg).to_json()
    if name in ("orders", "brainstorms"):
        return (extras.Orders if name == "orders" else extras.Brainstorms)(state).listing(all_=all_)
    raise NotFound(i18n.t("webui.bad_queue", names=", ".join(n for n, _, _ in QUEUES), got=repr(name)))


def plain(words):
    """A family of display words as a dict for JSON, whether a dict or an i18n.Labels (read through .items())."""
    return dict(words.items())


def queue_meta():
    """What the page needs to draw the queues: each one's open statuses and their names, its labels (/api/meta).
    No team (characters.IDS empty): the who-list is `all` alone.
    """
    from . import characters, extras, prompts, queues, tstack
    from .stack import BALLOT_RU
    who = dict({"all": i18n.t("webui.who_all")}, **{c: characters.name(c) for c in characters.IDS})
    q = {"todo": (queues.Todo.OPEN, queues.Todo.STATUS_RU, {"kind": queues.KIND_RU}),
         "think": (queues.Think.OPEN, queues.Think.STATUS_RU, {}),
         "wishes": (queues.Wishes.OPEN, queues.Wishes.STATUS_RU, {"area": queues.AREA_RU}),
         "ballots": (("waiting", "presenting"), BALLOT_RU, {"kind": dict(prompts.KIND_RU.items())}),
         "tstack": ((), {}, {"kind": tstack.KIND_RU, "who": who}),
         "orders": (extras.Orders.OPEN, extras.Orders.STATUS_RU, {"who": who}),
         "brainstorms": (extras.Brainstorms.OPEN, extras.Brainstorms.STATUS_RU, {"who": who})}
    return [{"name": n, "title": i18n.t(title), "command": f"depot2 {c}", "open": list(q[n][0]),
             "status_ru": plain(q[n][1]), "labels": {k: plain(v) for k, v in q[n][2].items()}}
            for n, title, c in QUEUES]


def merge_meta():
    """What the page needs to draw the merge queue's tab (/api/meta): what the queue is, its steps in their order and
    the names of the steps, the statuses and the smoke's verdicts, as `merge --md` writes them.
    """
    from . import merge
    return {"means": merge.means(cap=True), "steps": list(merge.STEPS), "step_ru": plain(merge.STEP_RU),
            "status_ru": plain(merge.STATUS_RU), "smoke_ru": plain(merge.SMOKE_RU)}


def meta(state, cfg, code=None):
    from . import priority as pri
    from . import stages, tracks
    return {"v": 1, "name": NAME, "repo": cfg.get("repo"), "refresh_s": REFRESH_S, "code": code,
            "stages": [{"stage": s, "ru": stages.ru(s), "means": stages.STAGE_MEANS.get(s, "")}
                       for s in stages.STAGES],
            "route": [s for s in stages.STAGES if s != "scrapped"],
            "moves": {s: list(m) for s, m in stages.MOVES.items()},
            "queues": queue_meta(),
            "merge": merge_meta(),
            # a car's status words as tracks.py writes its cells: the page reads a cell that starts with `waiting`
            # as a car with blockers, one that is `ready` alone as a car a free slot could take
            "car_status": plain(tracks.CAR_RU),
            "priority": {"min": pri.MIN, "max": pri.MAX, "default": pri.DEFAULT, "means": pri.means()}}


# --- the code it runs: what it is, whether it changed -----------------------------------------------------------

def code_files(root=CODE_ROOT):
    """The files the server's code is: depot2/*.py, depot2/executors/*.py and depot2/web/* (sorted)."""
    out = []
    for sub, keep in (("depot2", lambda n: n.endswith(".py")), (os.path.join("depot2", "executors"),
                                                                 lambda n: n.endswith(".py")),
                      (os.path.join("depot2", "web"), lambda n: True)):
        d = os.path.join(root, sub)
        if os.path.isdir(d):
            out += [os.path.join(d, n) for n in sorted(os.listdir(d)) if keep(n) and os.path.isfile(os.path.join(d, n))]
    return out


def code_stamp(root=CODE_ROOT):
    """A short hash of the code's files: their names, sizes and mtimes (a change of any, or a file added or gone)."""
    h = hashlib.sha256()
    for f in code_files(root):
        try:
            st = os.stat(f)
            h.update(("%s %d %d\n" % (os.path.relpath(f, root), st.st_size, st.st_mtime_ns)).encode("utf-8"))  # noqa: UP031
        except OSError:
            h.update((f"{f} gone\n").encode())
    return h.hexdigest()[:12]


def code_commit(root=CODE_ROOT):
    """{commit, date, dirty} of the git checkout the code is in, or None outside git."""
    def git(*args):
        return subprocess.run(["git", "-C", root, *list(args)], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              text=True, timeout=5)
    try:
        r = git("log", "-1", "--format=%h %cI")
        if r.returncode != 0 or not r.stdout.strip():
            return None
        h, date = r.stdout.split()
        dirty = bool(git("status", "--porcelain", "--", ".").stdout.strip())
        return {"commit": h, "date": date, "dirty": dirty}
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def code_broken(root=CODE_ROOT):
    for f in code_files(root):
        if not f.endswith(".py"):
            continue
        try:
            with open(f, encoding="utf-8") as fh:
                compile(fh.read(), f, "exec")
        except (OSError, SyntaxError, ValueError) as e:
            return f"{os.path.relpath(f, root)}: {e}"
    return None


def reexec(srv):
    """The same process on the new code: the listening socket kept open across exec, its number in DEPOT2_WEB_FD.
    Never returns.
    """
    srv.wait_answers()
    fd = srv.socket.fileno()
    os.set_inheritable(fd, True)
    env = dict(os.environ)
    env[FD_ENV] = str(fd)
    sys.stdout.flush()
    sys.stderr.flush()
    os.execve(sys.executable, [sys.executable, *sys.argv], env)


# --- the files: only .md under the read dirs ---------------------------------------------------------------

def inside(real, dirs):
    for d in dirs:
        rd = os.path.realpath(os.path.expanduser(d))
        if real.startswith(rd + os.sep):
            return True
    return False


def safe_md(path, dirs, base=DOCS_BASE):
    if not isinstance(path, str) or not path or "\0" in path or len(path) > 1024 or not dirs:
        return None
    p = os.path.expanduser(path.strip())
    if not os.path.isabs(p):
        if not base:
            return None
        p = os.path.join(os.path.expanduser(base), p)
    real = os.path.realpath(p)
    if not real.endswith(".md") or not os.path.isfile(real) or not inside(real, dirs):
        return None
    return real


def url_doc(rel, dirs, base=DOC_BASE):
    """`/api/doc?path=`: a relative path under web.docs.base; never absolute, never `..`, and the real path inside
    the read dirs (web.docs.dirs).
    """
    if not base or not isinstance(rel, str) or not rel or rel.startswith(("/", "~", "\\")) or "\\" in rel or "\0" in rel:
        return None
    parts = rel.split("/")
    if any(x in ("", ".", "..") for x in parts) or not rel.endswith(".md"):
        return None
    joined = os.path.join(os.path.expanduser(base), *parts)
    if not inside(os.path.abspath(joined), dirs):
        return None
    return safe_md(joined, dirs, base)


def read_md(real):
    with open(real, "rb") as f:
        data = f.read(MAX_DOC + 1)
    return data[:MAX_DOC].decode("utf-8", errors="replace"), len(data) > MAX_DOC


def csp_for(html):
    """The page's Content-Security-Policy: its own inline script and style by their hashes, nothing else."""
    def hashes(tag):
        out = []
        for m in re.finditer(rf"<{tag}>(.*?)</{tag}>", html, re.DOTALL):
            h = base64.b64encode(hashlib.sha256(m.group(1).encode("utf-8")).digest()).decode()
            out.append(f"'sha256-{h}'")
        return " ".join(out) or "'none'"
    return ("default-src 'none'; script-src {}; style-src {}; connect-src 'self'; img-src data:; "
            "base-uri 'none'; form-action 'none'; frame-ancestors 'none'".format(hashes("script"), hashes("style")))


def page_html(html, lang=None):
    """The page as served: the run's language in <html lang> and its messages (the catalogs' `web.` keys, English
    filling a gap) in the #messages block. That block is JSON data, never run, so the CSP's script hashes leave it out;
    every `<` in it is written \\u003c, so no text in a message can end or open a tag there.
    """
    lang = lang or i18n.language()
    msgs = json.dumps(i18n.web_messages(lang), ensure_ascii=False, sort_keys=True).replace("<", "\\u003c")
    html = RE_LANG.sub(f'<html lang="{lang}">', html, count=1)
    return RE_MESSAGES.sub(lambda m: m.group(1) + msgs + m.group(3), html, count=1)


# --- the names it answers to -------------------------------------------------------------------------------------

def extra_hosts(value):
    """The config's `web.hosts` → (the names to let in besides OWN_NAMES, lower case, in the list's order; what was
    dropped). A name is a host name and nothing else: an empty string, a space, a slash, a colon (so a port, a URL,
    an IPv6 address), a `*` or anything that is not a string is dropped, never repaired. A lone string is a list of
    one (`--set web.hosts=mybox.local`).
    """
    if value is None:
        return [], []
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, (list, tuple)):
        return [], [value]
    names, dropped = [], []
    for v in value:
        n = v.lower() if isinstance(v, str) and v.isascii() else None
        if n is None or len(n) > 253 or not RE_HOST.fullmatch(n):
            dropped.append(v)
        elif n not in OWN_NAMES and n not in names:
            names.append(n)
    return names, dropped


# --- the server ------------------------------------------------------------------------------------------------

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = NAME
    sys_version = ""

    def __getattr__(self, name):  # every method but GET: 405
        if name.startswith("do_"):
            return self.not_allowed
        raise AttributeError(name)

    def not_allowed(self):
        self.send_json(405, {"error": i18n.t("webui.only_get", method=self.command)}, allow=True)

    def log_message(self, fmt, *args):
        if self.server.verbose:
            sys.stderr.write(f"{self.log_date_time_string()} {fmt % args}\n")

    def headers_common(self, length, ctype, allow=False):
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        if allow:
            self.send_header("Allow", "GET")

    def send_json(self, code, data, allow=False):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.headers_common(len(body), "application/json; charset=utf-8", allow=allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def send_restart(self):
        """307 to the same path: the browser asks again and the listening socket, kept across the re-exec, hands the
        request to the new code.
        """
        body = json.dumps({"restart": True, "error": i18n.t("webui.restart")}, ensure_ascii=False).encode("utf-8")
        self.server.request_restart()
        with self.server.answering():
            time.sleep(RESTART_WAIT_S)  # the browser comes back after the old loop has stopped taking requests
            self._redirect(body)

    def _redirect(self, body):
        self.send_response(307)
        self.headers_common(len(body), "application/json; charset=utf-8")
        self.send_header("Location", self.path)
        self.send_header("Retry-After", "1")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.write(body)
        self.close_connection = True

    def send_page(self):
        try:
            self.server.cfg()  # the run's language as the config says it now
        except Exception:
            pass  # a config that does not load: the page still comes (in the language of the start), its API says why
        with open(PAGE, encoding="utf-8") as f:
            html = page_html(f.read())
        body = html.encode("utf-8")
        self.send_response(200)
        self.headers_common(len(body), "text/html; charset=utf-8")
        self.send_header("Content-Security-Policy", csp_for(html))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.server.authorize(self):
            return None
        if self.server.code_changed():  # new code on disk: this request goes to it, after a re-exec
            return self.send_restart()
        u = urllib.parse.urlsplit(self.path)
        path, q = u.path, urllib.parse.parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self.send_page()
        if not path.startswith("/api/"):
            return self.send_json(404, {"error": i18n.t("webui.no_page", path=path)})
        code, data = self.server.answer(urllib.parse.unquote(path[5:]), q)
        self.send_json(code, data)
        return None


class Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def __init__(self, state, sets=None, port=PORT, host=HOST, read_dirs=READ_DIRS, doc_base=DOC_BASE,
                 docs_base=DOCS_BASE, verbose=False, code_root=CODE_ROOT, follow_code=True):
        if host not in LOOPBACK:  # no flag opens it: there is no authorization yet
            raise ValueError("{} web listens on loopback only ({}), not {!r}".format(NAME, ", ".join(LOOPBACK), host))
        if host == "::1":
            self.address_family = socket.AF_INET6
        self.state, self.sets, self.verbose = state, sets, verbose
        docs = ((self.cfg().get("web") or {}).get("docs") or {}) if read_dirs is None or doc_base is None else {}
        read_dirs = docs.get("dirs") or () if read_dirs is None else read_dirs
        doc_base = docs.get("base") if doc_base is None else doc_base
        docs_base = docs.get("base") if docs_base is None else docs_base
        self.read_dirs, self.doc_base, self.docs_base = tuple(read_dirs), doc_base, docs_base
        # the code it runs: a change of it re-executes the server (follow_code; the unit tests' servers don't)
        self.code_root, self.follow_code = code_root, follow_code
        self.stamp, self.commit = code_stamp(code_root), code_commit(code_root)
        self.started, self.restarting, self.broken = now_stamp(), False, None
        self._checked, self._check_lock = time.time(), threading.Lock()
        self._answering, self._answering_lock = 0, threading.Condition()
        # `web.hosts` is read once, here: what the start printed stays true, and a change of it takes a restart.
        web = self.cfg().get("web")
        self.names, self.dropped = extra_hosts(web.get("hosts") if isinstance(web, dict) else None)
        fd = os.environ.pop(FD_ENV, None)  # a re-exec: the socket the server before it listened on
        self.resumed = fd is not None
        super().__init__((host, port), Handler, bind_and_activate=not self.resumed)
        if self.resumed:
            self.socket.close()
            self.socket = socket.socket(fileno=int(fd))
            os.set_inheritable(self.socket.fileno(), False)
            self.server_address = self.socket.getsockname()[:2]
        p = self.server_address[1]
        self.hosts = {"%s:%d" % (n, p) for n in OWN_NAMES + tuple(self.names)}  # noqa: UP031

    @property
    def url(self):
        h = self.server_address[0]
        return "http://%s:%d/" % (f"[{h}]" if ":" in h else h, self.server_address[1])  # noqa: UP031

    @property
    def urls(self):
        """The same server by the names of `web.hosts`: the same port, still this computer only."""
        return ["http://%s:%d/" % (n, self.server_address[1]) for n in self.names]  # noqa: UP031

    def authorize(self, h):
        """The one place a request is let in (a login goes here later): a loopback peer, a Host that names this
        server (OWN_NAMES and the config's `web.hosts`, with its port). Refused: 403, and nothing is read.
        """
        peer = h.client_address[0]
        host = (h.headers.get("Host") or "").strip().lower()
        if not (peer in LOOPBACK or peer.startswith("::ffff:127.")) or host not in self.hosts:
            h.send_json(403, {"error": i18n.t("webui.localhost_only", host=host or i18n.t("webui.no_host"))})
            return False
        return True

    def cfg(self):
        from . import config
        return config.load(self.state.root, self.sets)

    # --- the code it runs ---

    def code_changed(self):
        """True once the code on disk differs from what this process runs and compiles (looked at once a second);
        code that does not compile is kept out (`broken`, shown in /api/meta).
        """
        if not self.follow_code or self.restarting:
            return self.restarting
        with self._check_lock:
            if time.time() - self._checked < CODE_CHECK_S:
                return False
            self._checked = time.time()
            if code_stamp(self.code_root) == self.stamp:
                self.broken = None
                return False
            self.broken = code_broken(self.code_root)
            return self.broken is None

    def request_restart(self):
        """Stop serving (from another thread: serve_forever waits for it); `depot2 web` then re-executes itself."""
        if not self.restarting:
            self.restarting = True
            threading.Thread(target=self.shutdown, daemon=True).start()

    @contextlib.contextmanager
    def answering(self):
        """A restart's 307 being written: the re-exec waits for it (`wait_answers`)."""
        with self._answering_lock:
            self._answering += 1
        try:
            yield
        finally:
            with self._answering_lock:
                self._answering -= 1
                self._answering_lock.notify_all()

    def wait_answers(self, timeout=5.0):
        with self._answering_lock:
            self._answering_lock.wait_for(lambda: self._answering == 0, timeout)

    def code_info(self):
        return dict(self.commit or {"commit": None, "date": None, "dirty": None}, stamp=self.stamp,
                    started=self.started, pid=os.getpid(), broken=self.broken)

    def code_line(self):
        c = self.commit
        if not c:
            return i18n.t("webui.code_outside_git", stamp=self.stamp)
        f = {"commit": c["commit"], "date": c["date"][:16].replace("T", " ")}
        return i18n.t("webui.code_dirty", **f) if c["dirty"] else i18n.t("webui.code", **f)

    def route(self, name, q):
        st = self.state
        parts = name.split("/", 1)
        head, rest = parts[0], (parts[1] if len(parts) > 1 else None)
        simple = {"board": lambda c: board(st, c, q), "tracks": lambda c: tracks(st, c),
                  "trains": lambda c: trains(st, c), "stack": lambda c: stack(st, c),
                  "merge": lambda c: merge_queue(st, c), "meta": lambda c: meta(st, c, self.code_info()),
                  "doc": lambda c: self.doc(q), "principles": lambda c: principles(st, c, q),
                  "decisions": lambda c: decisions(st, c, q), "agents": lambda c: agents(st, c, q)}
        if rest is None and head in simple:
            return simple[head]
        if rest and head == "track":
            return lambda c: track(st, c, rest)
        if rest and head == "train":
            return lambda c: train(st, c, rest)
        if rest and head == "car":
            return lambda c: car(st, c, rest)
        if rest and head == "brief":
            return lambda c: self.brief(c, rest)
        if rest and head == "queue":
            return lambda c: queue(st, c, rest, q)
        if rest == "report" and head == "merge":
            return lambda c: merge_report(st, c, q)
        return None

    def answer(self, name, q):
        fn = self.route(name, q)
        if fn is None:
            return 404, {"error": i18n.t("webui.no_api", name=name)}
        from . import migrate
        if name.split("/", 1)[0] not in ("meta", "doc") and migrate.pending(self.state):
            return 503, {"error": i18n.t("webui.migrate_pending", old=migrate.version(self.state), new=migrate.SCHEMA),
                         "retry": True, "migrate": True}
        last = None
        for attempt in range(2):
            try:
                return 200, fn(self.cfg())
            except NotFound as e:
                return 404, {"error": str(e)}
            except Exception as e:  # a state read mid-write: once more, then a plain error
                last = e
                if attempt == 0:
                    time.sleep(RETRY_S)
        return 503, {"error": i18n.t("webui.read_failed", kind=type(last).__name__, error=last), "retry": True}

    def brief(self, cfg, ref):
        j = car_of(self.state, cfg, ref).to_json()
        path = j.get("brief")
        if not path:
            raise NotFound(i18n.t("webui.no_brief", ref=ref))
        real = safe_md(path, self.read_dirs, self.docs_base)
        if real is None:
            raise NotFound(i18n.t("webui.brief_not_served", path=path))
        text, cut = read_md(real)
        return {"ref": ref, "path": path, "text": text, "cut": cut}

    def doc(self, q):
        rel = (q.get("path") or [""])[-1]
        real = url_doc(rel, self.read_dirs, self.doc_base)
        if real is None:
            raise NotFound(i18n.t("webui.file_not_served", path=rel))
        text, cut = read_md(real)
        return {"path": rel, "text": text, "cut": cut}
