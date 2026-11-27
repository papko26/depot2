"""The cars' files: one folder per car under the config's `artifacts.dir` (`agents start` makes it; `depot2 artifacts`
and the web view's Queues tab list it).

A run leaves files besides its pull request: screenshots, builds, logs. They go into one folder per car,
<artifacts.dir>/<the car's branch> (`tr-0001-2`; a sketch's `sketch-tr-0001-2`, a plan's `plan-tr-0001-2`): `agents
start` makes the folder (and artifacts.dir) when it is not there, owner only (0700), and prints it; the runs write
there, and only the owner's word clears a folder. Nothing in depot2 ever deletes or rewrites anything there: a folder
that is there is left as it is. The listing reads names, sizes and dates, never a file's content, and never makes a
folder. A symbolic link is never followed nor listed, a name that starts with a dot is left out, and a folder is
named only as one the listing found, so no path from a command or a request is ever opened.

A folder's car: the car an agent's start made it for (agents.json `files`); else the car (or the stack object) whose
branch is its name, a sketch's or a plan's word before it; else the name read as `[<word>-]tr-NNNN-<car>` →
tr-NNNN:<car> (a car number with a hyphen: the longest the track has); else none.
"""

import os
import re
import time

from . import stages
from .i18n import t
from .stack import _cell
from .store import now_iso, read_json

# A listing walks at most this many entries in all; past it the counts are «at least» (`cut`).
MAX_WALK = 200000
# `--car` and the web view list at most this many files of a folder, the newest first (`more`: the rest).
MAX_FILES = 1000
RE_NAME = re.compile(r"^(?:[A-Za-z0-9_.]+-)*?(tr-\d{4})-(.+)$")
# The folders `agents start` makes (artifacts.dir and a car's folder): its owner's alone, as a run's files may hold
# anything; one that is there keeps its own mode.
MODE = 0o700
# A run's folder: the car's branch, a sketch's and a plan's with their word first (their files apart from the build's).
PREFIX = {"sketch": "sketch-", "plan": "plan-"}
RE_UNSAFE = re.compile(r"[^A-Za-z0-9._+=@-]+")
RE_REF = re.compile(r"^(?:tr-\d{4}:.+|st-\d{4})$")


class ArtifactsError(ValueError):
    pass


def base_name(ref, branch=None):
    """A car's folder name without a role's word: its branch, made a plain name (a `/` and the like become `-`); no
    branch, or one that is not a name: the ref, `tr-0001-2` for tr-0001:2."""
    b = RE_UNSAFE.sub("-", branch or "").strip("-")
    if not b or b.startswith(".") or len(b) > 200:
        b = RE_UNSAFE.sub("-", ref).strip("-")
    return b


def folder_name(ref, role, branch=None):
    """The folder of a run on car `ref` in `role`: `sketch-<base>`, `plan-<base>`, else `<base>` (base_name)."""
    return PREFIX.get(role, "") + base_name(ref, branch)


def _make(path):
    """path made a folder of MODE unless there is one: True when it made it. A file or a link there: OSError."""
    try:
        os.mkdir(path, MODE)
    except FileExistsError:
        if os.path.islink(path) or not os.path.isdir(path):
            raise
        return False
    os.chmod(path, MODE)  # the umask may have taken more than MODE leaves
    return True


def ensure(cfg, name):
    """`agents start`: <artifacts.dir>/<name> made when it is not there, artifacts.dir first (owner only, MODE); what
    is there is left as it is (its mode, its dates, its files). Returns (the folder as the config names it, with a
    trailing /; None) — or (None, why it could not) — or (None, None): artifacts.dir is off."""
    value, top = root(cfg)
    if top is None:
        return None, None
    shown = value.rstrip("/") + "/" + name + "/"
    try:
        if not os.path.isdir(top):
            parent = os.path.dirname(top)
            if parent and not os.path.isdir(parent):
                os.makedirs(parent)
            _make(top)
        _make(os.path.join(top, name))
    except OSError as e:
        return None, t("artifacts.not_made", path=shown, error=e.strerror or e)
    return shown, None


def root(cfg):
    """(the configured value, its real path), or (None, None): off."""
    v = (cfg.get("artifacts") or {}).get("dir")
    if not v:
        return None, None
    return v, os.path.realpath(os.path.expanduser(v))


def iso(ts):
    lt = time.localtime(ts)
    off = time.strftime("%z", lt)
    return time.strftime("%Y-%m-%dT%H:%M:%S", lt) + off[:3] + ":" + off[3:]


def when(s):
    return s[:16].replace("T", " ") if s else "—"


def size_text(n):
    for unit, k in (("gb", 1024 ** 3), ("mb", 1024 ** 2), ("kb", 1024)):
        if n >= k:
            return t("artifacts.size_" + unit, n="%.1f" % (n / float(k)))
    return t("artifacts.size_b", n=n)


def _names(top):
    """The folders right under top, by name: real directories, no link, no dot-name. [(name, its mtime)]."""
    out = []
    try:
        with os.scandir(top) as it:
            for e in it:
                try:
                    if e.name.startswith(".") or e.is_symlink() or not e.is_dir(follow_symlinks=False):
                        continue
                    out.append((e.name, e.stat(follow_symlinks=False).st_mtime))
                except OSError:
                    continue
    except OSError:
        return []
    return sorted(out)


def _walk(top, budget):
    """Every regular file under top, no link followed or listed, no dot-name: ([(name under top, bytes, mtime)], cut).
    budget: a one-item list, the entries the listing may still look at."""
    out, todo = [], [""]
    while todo:
        rel = todo.pop()
        try:
            it = os.scandir(os.path.join(top, rel) if rel else top)
        except OSError:
            continue
        with it:
            for e in it:
                if budget[0] <= 0:
                    return out, True
                budget[0] -= 1
                if e.name.startswith("."):
                    continue
                name = rel + "/" + e.name if rel else e.name
                try:
                    if e.is_symlink():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        todo.append(name)
                    elif e.is_file(follow_symlinks=False):
                        st = e.stat(follow_symlinks=False)
                        out.append((name, st.st_size, st.st_mtime))
                except OSError:
                    continue
    return out, False


class Cars(object):
    """A folder's name → its car: {ref, title, stage, stage_ru}, or None."""

    def __init__(self, state, cfg):
        from .stack import Stack
        from .tracks import Tracks
        self.stk = Stack(state, cfg)
        self.tk = Tracks(state, cfg)
        self.tk._stack = self.stk
        self.by_branch, self.by_agent = {}, {}
        for o in self.stk.objects():
            if o.get("branch"):
                self.by_branch.setdefault(base_name(o["id"], o["branch"]), self._obj(o))
        for tr in self.tk.tracks():
            for c in tr["cars"]:
                if c.get("branch"):  # a car beats its stack object
                    self.by_branch[base_name("%s:%s" % (tr["id"], c["n"]), c["branch"])] = self._car(tr, c)
        for ag in (read_json(state.path("agents.json")) or {}).get("agents") or []:
            if isinstance(ag, dict) and isinstance(ag.get("files"), str) and ag.get("car"):
                self.by_agent[ag["files"]] = ag["car"]  # the folder an agent's start made for its car

    def _car(self, tr, c):
        s = self.tk.stage_of(c)
        return {"ref": "%s:%s" % (tr["id"], c["n"]), "title": c.get("title") or "", "stage": s,
                "stage_ru": stages.ru(s)}

    def _obj(self, o):
        s = self.stk.stage_of(o)
        return {"ref": o["id"], "title": o.get("title") or "", "stage": s, "stage_ru": stages.ru(s)}

    def of(self, name):
        if name in self.by_agent:
            got = self._ref(self.by_agent[name])
            if got:
                return got
        for word in ("",) + tuple(sorted(PREFIX.values())):
            if name.startswith(word) and name[len(word):] in self.by_branch:
                return self.by_branch[name[len(word):]]
        m = RE_NAME.match(name)
        if not m:
            return None
        parts = m.group(2).split("-")
        for k in range(len(parts), 0, -1):
            try:
                tr, c = self.tk.car("%s:%s" % (m.group(1), "-".join(parts[:k])))
            except ValueError:
                continue
            return self._car(tr, c)
        return None

    def _ref(self, ref):
        try:
            tr, c = self.tk.car(ref)
        except ValueError:
            return None
        return self._car(tr, c)

    def canon(self, ref):
        try:
            tr, c = self.tk.car(ref)
            return "%s:%s" % (tr["id"], c["n"])
        except ValueError:
            return ref


def _head(cfg):
    value, top = root(cfg)
    return {"on": top is not None, "dir": value, "path": top, "exists": bool(top) and os.path.isdir(top)}


def _folder(name, car, files, cut):
    n = sum(f[1] for f in files)
    newest = max((f[2] for f in files), default=None)
    out = {"folder": name, "ref": None, "title": None, "stage": None, "stage_ru": None, "files": len(files),
           "bytes": n, "size": size_text(n), "newest": iso(newest) if newest is not None else None, "cut": cut}
    out.update(car or {})
    return out


def listing(state, cfg):
    """`artifacts --json` (the web's /api/artifacts): every folder, the newest first (by its newest file; an empty one
    by its own date: a run just started is on top), with its car and counts."""
    out = _head(cfg)
    out.update(folders=[], files=0, bytes=0, size=size_text(0), cut=False)
    if not out["exists"]:
        return out
    cars, budget, seen = Cars(state, cfg), [MAX_WALK], []
    for name, made in _names(out["path"]):  # the order: by the newest file; an empty folder by its own date
        found, cut = _walk(os.path.join(out["path"], name), budget)
        seen.append((max((f[2] for f in found), default=made), name, _folder(name, cars.of(name), found, cut)))
    out["folders"] = [f for _, _, f in sorted(seen, key=lambda x: (x[0], x[1]), reverse=True)]
    out["files"] = sum(f["files"] for f in out["folders"])
    out["bytes"] = sum(f["bytes"] for f in out["folders"])
    out["size"] = size_text(out["bytes"])
    out["cut"] = any(f["cut"] for f in out["folders"])
    return out


def files(state, cfg, ask, folder_only=False):
    """`artifacts --car REF --json` (the web's /api/artifacts/<folder>): the files of the folder named REF, or of every
    folder whose car is REF (folder_only: a folder's name alone), the newest first."""
    out = _head(cfg)
    if not out["on"]:
        raise ArtifactsError(t("artifacts.off"))
    if not out["exists"]:
        raise ArtifactsError(t("artifacts.missing", dir=out["dir"]))
    if not isinstance(ask, str) or not ask or "/" in ask or "\\" in ask or "\0" in ask or ask in (".", ".."):
        raise ArtifactsError(t("artifacts.bad_name", got=repr(ask)))
    names = [n for n, _ in _names(out["path"])]
    cars = Cars(state, cfg)
    if ask in names:
        picked = [ask]
    elif folder_only or not RE_REF.match(ask):
        raise ArtifactsError(t("artifacts.no_folder", name=ask, dir=out["dir"]))
    else:
        ref = cars.canon(ask)
        picked = [n for n in names if (cars.of(n) or {}).get("ref") == ref]
        if not picked:
            raise ArtifactsError(t("artifacts.no_car_folder", ref=ask, dir=out["dir"]))
    out.update(car=ask, folders=[])
    budget = [MAX_WALK]
    for name in picked:
        found, cut = _walk(os.path.join(out["path"], name), budget)
        f = _folder(name, cars.of(name), found, cut)
        found.sort(key=lambda x: (x[2], x[0]), reverse=True)
        f["entries"] = [{"name": n, "bytes": b, "size": size_text(b), "mtime": iso(m)} for n, b, m in found[:MAX_FILES]]
        f["more"] = max(0, len(found) - MAX_FILES)
        out["folders"].append(f)
    return out


# --- what it prints ----------------------------------------------------------------------------------------------

def _state_line(j):
    if not j["on"]:
        return t("artifacts.off")
    if not j["exists"]:
        return t("artifacts.missing", dir=j["dir"])
    return None


def _car_cell(f):
    if not f["ref"]:
        return t("artifacts.no_car")
    return "%s %s" % (f["ref"], _cell(f["title"]))


def listing_md(j):
    L = [t("artifacts.heading", ts=when(now_iso())), ""]
    bad = _state_line(j)
    if bad:
        return "\n".join(L + [bad]) + "\n"
    L.append(t("artifacts.summary", dir=j["dir"], n=len(j["folders"]), files=j["files"], size=j["size"]))
    L.append("")
    if not j["folders"]:
        L.append(t("artifacts.empty"))
        return "\n".join(L) + "\n"
    L.append(t("artifacts.header"))
    L.append("|---|---|---|---|---|---|")
    for f in j["folders"]:
        L.append("| %s | %s | %s | %s%d | %s | %s |" % (
            _cell(f["folder"]), _car_cell(f), f["stage_ru"] or "—", "≥" if f["cut"] else "", f["files"], f["size"],
            when(f["newest"])))
    if j["cut"]:
        L += ["", t("artifacts.cut", n=MAX_WALK)]
    return "\n".join(L) + "\n"


def files_md(j):
    L = []
    for f in j["folders"]:
        car = t("artifacts.of_car", ref=f["ref"], title=_cell(f["title"]), stage=f["stage_ru"]) if f["ref"] else \
            t("artifacts.no_car")
        L += [t("artifacts.files_heading", folder=f["folder"], car=car), "",
              t("artifacts.files_summary", files=f["files"], size=f["size"], newest=when(f["newest"])), ""]
        if f["entries"]:
            L += [t("artifacts.files_header"), "|---|---|---|"]
            L += ["| %s | %s | %s |" % (_cell(e["name"]), e["size"], when(e["mtime"])) for e in f["entries"]]
        if f["more"]:
            L += ["", t("artifacts.more", n=f["more"], shown=MAX_FILES)]
        if f["cut"]:
            L += ["", t("artifacts.cut", n=MAX_WALK)]
        L.append("")
    return "\n".join(L)


def overview_row(cfg):
    """`queues`'s row: the folders and the three changed last (by the folder's own date: no walk here)."""
    j = _head(cfg)
    bad = None if j["exists"] else t("queues.ov.artifacts_off" if not j["on"] else "queues.ov.artifacts_missing")
    if bad:
        return (t("queues.ov.artifacts"), bad, [], "artifacts", None)
    names = sorted(_names(j["path"]), key=lambda x: (x[1], x[0]), reverse=True)
    return (t("queues.ov.artifacts"), t("queues.ov.artifacts_count", n=len(names)), [n for n, _ in names[:3]],
            "artifacts", None)
