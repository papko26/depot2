"""The base of a list of entries kept in one depot file (`Queue`: the principles are one), and one overview of every
queue the depot keeps (`depot2 queues`): the stack, the merge queue, the team's stack, the trains, the tracks, the
ballots, the agents, the decisions, the principles and the cars' files (artifacts.py: only listed here).

A depot of schema 5 kept three more queues: wishes (wl-NNNN), things to think over (th-NNNN) and todos (td-NNNN).
Schema 6 made each one a track of the same name (`wishes`, `to-think`, `todo`; migrate.py): their open entries are
cars there, and their files stay as an archive (`<name>-v5.json`, `<name>-history-v5.jsonl`) that an old ref — a car's
`from`, a decision's link — still reads.
"""

import os
import re

from . import i18n
from . import priority as pri
from . import stages
from .i18n import t
from .stack import _cell
from .store import append_jsonl, now_iso, read_json, read_jsonl, today, write_json


class QueueError(ValueError):
    pass


class Text(object):
    """A class attribute that reads as its message in the language of the moment: Text(key) is a string;
    Text(k1, k2, …, many=True) a tuple of them (a table's columns)."""

    def __init__(self, *keys, **kw):
        self.keys = keys
        self.many = kw.get("many", len(keys) > 1)

    def __get__(self, obj, cls=None):
        words = tuple(t(k) for k in self.keys)
        return words if self.many else words[0]


class Queue(object):
    """Entries with an id, a status and a history file: <NAME>.json and <NAME>-history.jsonl."""
    NAME = None        # the file's name: <NAME>.json, <NAME>-history.jsonl
    PREFIX = None      # the id's prefix
    STATUS_RU = {}     # status -> its word (an i18n.Labels)
    OPEN = ()          # the statuses of an open entry
    ALSO_FROM = {}     # a status that a closed entry may still take, -> from which closed ones (to_status)

    def __init__(self, state, cfg=None):
        self.state = state
        self.cfg = cfg
        self.path = state.path(self.NAME + ".json")
        self.history_path = state.path(self.NAME + "-history.jsonl")
        self.data = read_json(self.path) or {"next": 1, "entries": []}

    # --- reading ---------------------------------------------------------------------------------

    def entries(self, open_only=False):
        return [e for e in self.data["entries"] if not open_only or e["status"] in self.OPEN]

    def get(self, eid):
        for e in self.data["entries"]:
            if e["id"] == eid:
                return e
        raise QueueError("no %s in %s" % (eid, self.NAME))

    def history(self):
        return read_jsonl(self.history_path)

    def imported(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        return [h for h in self.history() if h.get("event") == "import" and h.get("file") == path]

    def ordered(self, open_only=True):
        return self.entries(open_only)

    def headline(self, e):
        return e.get("title") or e.get("text") or ""

    # --- writing ---------------------------------------------------------------------------------

    def _save(self, event, eid, **fields):
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": now_iso(), "event": event, "entry": eid}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def check(self, e):
        if e["status"] not in self.STATUS_RU:
            raise QueueError("%s: status must be one of %s" % (e["id"], ", ".join(self.STATUS_RU)))

    def closable(self, eid, status):
        e = self.get(eid)
        if self.OPEN and e["status"] not in self.OPEN and e["status"] not in self.ALSO_FROM.get(status, ()):
            raise QueueError("%s is «%s»: it is closed and stays so (only an open one — %s — changes); the history keeps "
                             "it, new work is a new entry: depot2 %s add …" % (
                                 eid, self.STATUS_RU.get(e["status"], e["status"]),
                                 ", ".join(self.STATUS_RU.get(s, s) for s in self.OPEN), self.NAME))
        return e

    def to_status(self, eid, status, reason=None, **fields):
        e = self.closable(eid, status)
        e["status"] = status
        e.update({k: v for k, v in fields.items() if v is not None})
        if reason:
            e["notes"].append("%s: %s" % (today(), reason))
        self.check(e)
        self._save(status, eid, reason=reason, **fields)
        return e


def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


def prio_cell(counts):
    return pri.counts_cell(counts)


# --- the queues of before: an archive old refs read ------------------------------------------------------------------

# (the file's name, the id's prefix, the track it became, its statuses, the open ones among them)
OLD_QUEUES = (("wishes", "wl", "wishes", ("waiting", "taken", "declined", "done"), ("waiting",)),
              ("think", "th", "to-think", ("waiting", "in_round", "decided", "dropped", "taken"),
               ("waiting", "in_round")),
              ("todo", "td", "todo", ("open", "in_pr", "done", "wont", "taken"), ("open", "in_pr")))
OLD_STATUS = {q[0]: i18n.Labels("queues.%s.status" % q[0], q[3]) for q in OLD_QUEUES}
ARCHIVE = "%s-v5.json"
ARCHIVE_HISTORY = "%s-history-v5.jsonl"


def old_queue(ref):
    """The queue of before an id names (wl-/th-/td-NNNN), or None."""
    for q in OLD_QUEUES:
        if re.match(r"^%s-\d{4}$" % q[1], ref or ""):
            return q[0]
    return None


def archived(state, ref):
    """An entry of the queues of before, as the archive keeps it (`moved_to`: the car it became), or None."""
    name = old_queue(ref)
    if name is None:
        return None
    path = state.path(ARCHIVE % name)
    d = read_json(path if os.path.exists(path) else state.path(name + ".json")) or {}
    for e in d.get("entries") or []:
        if e.get("id") == ref:
            return e
    return None


SOURCE_KIND = {"wl": "wish", "th": "think", "td": "todo", "st": "stack", "tm": "tstack"}
SOURCE_RU = i18n.Labels("queues.source", ("wish", "think", "todo", "stack", "tstack"))


def source(state, cfg, ref):
    """A ref of a car's `from` as data: {ref, kind, kind_ru, title, status_ru} (title and status None when it names
    nothing the depot keeps). A ref of the queues of before reads their archive."""
    kind = SOURCE_KIND.get((ref or "")[:2])
    kind_ru = SOURCE_RU.get(kind)
    out = {"ref": ref, "kind": kind, "kind_ru": kind_ru, "title": None, "status_ru": None}
    try:
        if kind in ("wish", "think", "todo"):
            e = archived(state, ref)
            if e is not None:
                name = old_queue(ref)
                out.update(title=e.get("title") or e.get("topic") or e.get("text"),
                           status_ru=t("queues.source_moved", to=e["moved_to"]) if e.get("moved_to") else
                           OLD_STATUS[name].get(e.get("status"), e.get("status")))
        elif kind in ("stack", "tstack"):
            from .stack import Stack
            from .tstack import TeamStack
            stk = (Stack if kind == "stack" else TeamStack)(state, cfg)
            live = stk._dep_open(ref)
            o = stk.get(ref) if live else stk.left_obj(ref)
            out.update(title=(o or {}).get("title"),
                       status_ru=t("queues.source_live" if live else "queues.source_left"))
    except ValueError:
        pass
    return out


def source_line(s):
    return "%s — %s%s%s" % (s["ref"], s["kind_ru"] or "?", " «%s»" % _clip(s["title"], 80) if s.get("title") else "",
                           " (%s)" % s["status_ru"] if s.get("status_ru") else "")


# --- the one-shot migration ------------------------------------------------------------------------

PRIO_NOTE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}:\s*)?prio:\s*(high|normal|later)\b", re.I)


def note_prio(e):
    """The newest leading `prio: …` note of an entry (notes are appended, so the last), or None."""
    found = None
    for n in e.get("notes") or []:
        m = PRIO_NOTE.match(n or "")
        if m:
            found = m.group(1).lower()
    return found


def migrate(state, cfg, dry_run=False):
    """`queues migrate`: one shot, idempotent. A stack object without `autostart` gets it written: no. Every car
    without `stage` gets its stage (stages.migrate). An object or a car that has the field is never touched, so a
    second run changes nothing. The priorities and the queues of before are the depot's data migration's
    (`depot2 migrate`, migrate.py). Returns (lines, changed)."""
    from .stack import Stack
    L, changed = [], 0
    stk = Stack(state, cfg)
    missing = [o for o in stk.objects() if "autostart" not in o]
    for o in missing:
        o["autostart"] = False
    if missing:
        changed += len(missing)
        if not dry_run:
            stk._save("migrate_autostart", None, objects=[o["id"] for o in missing], autostart=False)
    L.append(t("queues.migrate_autostart", n=len(missing),
               list=(": " + ", ".join(o["id"] for o in missing)) if missing else ""))
    lines, n = stages.migrate(state, cfg, dry_run=dry_run)
    L += lines
    changed += n
    if not changed:
        L.append(t("queues.migrate_nothing"))
    return L, changed


# --- the overview ----------------------------------------------------------------------------------

# The rows of the overview, in their order: the key is the command that prints the queue (`depot2 <key> --md`) and
# names its line of what it is (`queues.means.<key>`: what it holds, who writes into it, how a thing leaves it).
ROWS = ("stack", "merge", "tstack", "trains", "tracks", "agents", "ballots", "decisions", "principles", "artifacts")


def means(key):
    return t("queues.means." + key)


def overview_rows(state, cfg, now=None):
    """The overview's rows, in ROWS's order: (name, open, head, command, priority counts or None)."""
    from .agents import Agents
    from .decisions import listing
    from .principles import Principles
    from .stack import Ballots, Stack
    from .trains import STATE_RU, Trains
    from .tstack import TeamStack
    stk, tst, tr = Stack(state, cfg), TeamStack(state, cfg), Trains(state, cfg)
    tk = tr.tracks
    rows = []

    def stack_head(s):
        objs = s.in_slots() + s.posted() + s.queue()
        tag = {"slot": t("queues.ov.tag_slot"), "post": t("queues.ov.tag_post")}
        return ["%s %s%s" % (o["id"], _clip(o["title"], 50), tag.get(o["where"], "")) for o in objs[:3]]

    rows.append((t("queues.ov.stack"), t("queues.ov.stack_count", s=len(stk.in_slots()), sn=stk.slots_n,
                                         p=len(stk.posted()), pn=stk.post_max, q=len(stk.queue()), qn=stk.queue_max),
                 stack_head(stk), "stack", None))
    from .merge import overview_row
    rows.append(overview_row(state))
    rows.append((t("queues.ov.tstack"), t("queues.ov.tstack_count", s=len(tst.in_slots()), sn=tst.slots_n,
                                          q=len(tst.queue()), qn=tst.queue_max), stack_head(tst), "tstack", None))
    live = tr.trains(include_done=False)
    nx = tr.next(now)
    by = {}
    for tn in live:
        by[tn["state"]] = by.get(tn["state"], 0) + 1
    head = [t("queues.ov.can_now", list=", ".join("%s:%s" % (p["track"]["id"], p["car"]["n"]) for p in nx["picks"]))] \
        if nx["picks"] else []
    ready = ["%s:%s" % (tn["id"], c["n"]) for tn, c, b in nx["parked"] if not b]
    head += [t("queues.ov.to_merge", list=", ".join(ready))] if ready else []
    head += ["%s (%s)" % (tn["name"], STATE_RU[tn["state"]]) for tn in live[:3]]
    states = ", ".join("%s %d" % (STATE_RU[k], by[k]) for k in ("design", "impl", "merge") if k in by)
    rows.append((t("queues.ov.trains"), "%d (%s)" % (len(live), states) if live else "0", head, "trains", None))
    open_tracks = [tk_ for tk_ in tk.tracks() if not tk.done(tk_)]
    rows.append((t("queues.ov.tracks"), t("queues.ov.tracks_count", n=len(open_tracks), total=len(tk.tracks())),
                 ["%s %s (%d)" % (tk_["id"], _clip(tk_["title"], 40), len([c for c in tk_["cars"]
                                                                          if tk.stage_of(c) not in stages.TERMINAL]))
                  for tk_ in open_tracks[:3]], "tracks", None))
    ags = [a for a in Agents(state, cfg, tracks=tk).agents() if a.get("state") not in ("finished", "stopped")]
    rows.append((t("queues.ov.agents"), t("queues.ov.agents_count", n=len(ags)),
                 ["%s %s" % (a["id"], a["car"]) for a in ags[:3]], "agents", None))
    open_b = Ballots(state).entries(open_only=True)
    rows.append((t("queues.ov.ballots"), str(len(open_b)),
                 ["%s%s" % (e["round"], t("queues.ov.at_point", point=e["at_point"]) if e.get("at_point") else "")
                  for e in open_b[:3]], "ballots", None))
    decs, _, n_decs = listing(state, limit=3)
    rows.append((t("queues.ov.decisions"), t("queues.ov.decisions_count", n=n_decs),
                 ["%s %s" % (d["id"], _clip(d.get("text"), 50)) for d in decs], "decisions", None))
    pc = Principles(state, cfg)
    rows.append((t("queues.ov.principles"), t("queues.ov.principles_count", n=len(pc.entries(True))),
                 ["③ %s %s" % (e["id"], _clip(pc.headline(e), 60)) for e in pc.checked()[:3]], "principles", None))
    from .artifacts import overview_row as artifacts_row
    rows.append(artifacts_row(cfg))
    return rows


def overview_json(state, cfg, now=None):
    """`queues --json` and the web view's: one object per row, with the line of what it is."""
    return [{"key": r[3], "queue": r[0], "open": r[1], "head": r[2], "command": r[3], "priority": r[4],
             "means": means(r[3])} for r in overview_rows(state, cfg, now)]


def overview(state, cfg, now=None):
    rows = overview_rows(state, cfg, now)
    L = [t("queues.ov.heading", ts=now_iso()[:16].replace("T", " ")), ""]
    L.append(t("queues.ov.header"))
    L.append("|---|---|---|---|---|")
    for name, count, head, cmd, prio in rows:
        L.append("| %s | %s | %s | %s | `depot2 %s --md` |" % (name, count, prio_cell(prio) if prio is not None else "—",
                                                               _cell("; ".join(head) or "—"), cmd))
    L.append("")
    L.append(t("queues.ov.priority_legend"))
    by_stage = stages.summary(state, cfg)
    L.append(t("queues.ov.stages", counts="%d — %s" % (sum(by_stage.values()), stages.counts_cell(by_stage))
               if by_stage else "0"))
    return "\n".join(L) + "\n", rows
