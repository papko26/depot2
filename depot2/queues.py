"""Three queues as files in the depot, and one overview of every queue (`depot2 queues`).

wishes.json  + wishes-history.jsonl   wishes not scheduled yet (wl-NNNN), in the owner's words
think.json   + think-history.jsonl    open questions for a brainstorm, a team round or the owner's thought (th-NNNN)
todo.json    + todo-history.jsonl     concrete leftovers and reminders (td-NNNN), with the date, version and place

Every entry can be taken into a car (`take`: a new car on a track, an existing car, or a stack object).
"""

import os
import re
import subprocess
from typing import TYPE_CHECKING

from . import i18n, stages
from . import priority as pri
from .i18n import t
from .stack import _cell
from .store import append_jsonl, now_iso, read_json, read_jsonl, today, write_json

if TYPE_CHECKING:
    from .stack import Stack
    from .tracks import Tracks


class QueueError(ValueError):
    pass


class Text:
    """A class attribute that reads as its message in the language of the moment: Text(key) is a string;
    Text(k1, k2, …, many=True) a tuple of them (a table's columns).
    """

    def __init__(self, *keys, **kw) -> None:
        self.keys = keys
        self.many = kw.get("many", len(keys) > 1)

    def __get__(self, obj, cls=None) -> str | tuple[str, ...]:
        words = tuple(t(k) for k in self.keys)
        return words if self.many else words[0]


class Queue:
    NAME: str | None = None  # the file's name: <NAME>.json, <NAME>-history.jsonl
    PREFIX: str | None = None  # the id's prefix
    TITLE: Text | None = None  # the table's heading (a Text)
    STATUS_RU: dict[str, str] | i18n.Labels[str] = {}  # status -> its word (an i18n.Labels)  # noqa: RUF012
    OPEN: tuple[str, ...] = ()  # the statuses of an open entry
    ALSO_FROM: dict[str, tuple[str, ...]] = {}  # target status -> closed statuses allowed to move there  # noqa: RUF012
    FIELDS: tuple[str, ...] = ()  # the fields `set` may change (lists: LIST_FIELDS append)
    LIST_FIELDS: tuple[str, ...] = ("links", "notes")
    STAGES = False  # a wish and a to-think entry are cars with stages (stages.py); a todo is not
    STATUS_STAGE: dict[str, str] = {}  # a status move -> the stage it means (to_status)  # noqa: RUF012
    KIND_RU: Text | str | None = None  # entry kind for the car's source note

    def __init__(self, state, cfg=None) -> None:
        self.state = state
        self.cfg = cfg
        if self.NAME is None:
            raise TypeError("Queue subclasses must define NAME")
        self.path = state.path(self.NAME + ".json")
        self.history_path = state.path(self.NAME + "-history.jsonl")
        self.data = read_json(self.path) or {"next": 1, "entries": []}
        self.notes: list[str] = []  # the lines a command prints: a stage moved off the route
        self._ctx: dict[
            str, Stack | Tracks
        ] = {}  # the stack and the tracks, read once for a table's rows (a taken wish's target)

    # --- reading ---------------------------------------------------------------------------------

    def entries(self, open_only=False):
        return [e for e in self.data["entries"] if not open_only or e["status"] in self.OPEN]

    def get(self, eid):
        for e in self.data["entries"]:
            if e["id"] == eid:
                return e
        raise QueueError(f"no {eid} in {self.NAME}")

    def history(self):
        return read_jsonl(self.history_path)

    def imported(self, path):
        path = os.path.abspath(os.path.expanduser(path))
        return [h for h in self.history() if h.get("event") == "import" and h.get("file") == path]

    def ordered(self, open_only=True):
        return self.entries(open_only)

    # --- stages (stages.py) ----------------------------------------------------------------------

    def stage_default_why(self, e):
        return "bogie", t("queues.why_entry")

    def stage_of(self, e):
        if not self.STAGES:
            return None
        return e.get("stage") or self.stage_default_why(e)[0]

    def _stage(self, e, stage, by, why=None, forced=False):
        if not self.STAGES:
            return None
        row = stages.record(
            e, stage, by, why, default=None if e.get("stage") else self.stage_default_why(e)[0], forced=forced
        )
        line = stages.say(e["id"], row)
        if line:
            self.notes.append(line)
        return row

    def set_stage(self, eid, stage, by, why=None, forced=False):
        e = self.get(eid)
        row = self._stage(e, stage, by, why, forced=forced)
        if row:
            self._save("stage", eid, stage=stages.hist(row), why=why, by=by)
        return row

    def stage_cell(self, e):
        own = stages.ru(self.stage_of(e))
        if e["status"] == "taken" and e.get("taken_to"):
            there = stages.stage_of_ref(self.state, self.cfg or {}, e["taken_to"], self._ctx)
            if there:
                return f"{own} → {stages.ru(there)}"
        return own

    def prio_counts(self):
        """The open entries by priority: {8: n, 5: n, …} (priority.py)."""
        return pri.counts(self.entries(True))

    # --- writing ---------------------------------------------------------------------------------

    def _save(self, event, eid, **fields):
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": now_iso(), "event": event, "entry": eid}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def _new(self, fields, by=None):
        e = {"id": "%s-%04d" % (self.PREFIX, self.data["next"]), "added": now_iso(), "links": [], "notes": []}  # noqa: UP031
        self.data["next"] += 1
        e.update({k: v for k, v in fields.items() if v is not None or k not in e})
        e["links"] = list(e.get("links") or [])
        e["notes"] = list(e.get("notes") or [])
        if e.get("priority") is None:  # an import's entry (mdimport) names none: the default
            e["priority"] = pri.DEFAULT
        self.check(e)
        if self.STAGES:
            stages.start(e, self.stage_default_why(e)[0], by or f"{self.NAME} add")
        self.data["entries"].append(e)
        return e

    def check(self, e):
        if e["status"] not in self.STATUS_RU:
            raise QueueError("{}: status must be one of {}".format(e["id"], ", ".join(self.STATUS_RU)))

    def add(self, **fields):
        e = self._new(self.normalize(fields))
        self._save("add", e["id"], entry_data=e)
        return e

    def normalize(self, fields):
        return fields

    def set(self, eid, **fields):
        e = self.get(eid)
        changed = {}
        for k, v in self.normalize({k: v for k, v in fields.items() if v is not None}).items():
            if k in self.LIST_FIELDS:
                e[k] = e.get(k) or []
                add = [x for x in (v if isinstance(v, (list, tuple)) else [v]) if x not in e[k]]
                if k == "notes":
                    add = [f"{today()}: {x}" for x in add]
                e[k] += add
                changed[k] = add
            elif k in self.FIELDS:
                e[k] = changed[k] = v
            else:
                raise QueueError(f"{self.NAME} has no field {k}")
        if not changed:
            raise QueueError("nothing to set")
        self.check(e)
        self._save("set", eid, **changed)
        return e

    def closable(self, eid, status):
        e = self.get(eid)
        if self.OPEN and e["status"] not in self.OPEN and e["status"] not in self.ALSO_FROM.get(status, ()):
            raise QueueError(
                "{} is «{}»: it is closed and stays so (only an open one — {} — changes); the history keeps "
                "it, new work is a new entry: depot2 {} add …".format(
                    eid,
                    self.STATUS_RU.get(e["status"], e["status"]),
                    ", ".join(self.STATUS_RU.get(s, s) for s in self.OPEN),
                    self.NAME,
                )
            )
        return e

    def to_status(self, eid, status, reason=None, **fields):
        e = self.closable(eid, status)
        row = None
        if status in self.STATUS_STAGE:
            row = self._stage(e, self.STATUS_STAGE[status], f"{self.NAME} {status}", reason)
        e["status"] = status
        e.update({k: v for k, v in fields.items() if v is not None})
        if reason:
            e["notes"].append(f"{today()}: {reason}")
        self.check(e)
        self._save(status, eid, reason=reason, stage=stages.hist(row), **fields)
        return e

    def car_title(self, e):
        """The title of the car an entry becomes (`take` without --title)."""
        return _clip(self.headline(e), 100)

    def take_note(self, e):
        return t(
            "queues.take_note",
            id=e["id"],
            kind=self.KIND_RU,
            date=e.get("date") or (e.get("added") or "")[:10],
            text=self.take_text(e),
        )

    def take_text(self, e):
        return " ".join((self.headline(e) or "").split())

    def take(self, eid, to, tracks, stack=None, n=None, size=None, title=None, by=None):
        from .tracks import RE_CAR, RE_STACK, RE_TRACK

        e = self.get(eid)
        if e["status"] not in self.OPEN:
            raise QueueError(
                "{} is {}{}: only an open entry is taken".format(
                    eid, self.STATUS_RU[e["status"]], " → {}".format(e["taken_to"]) if e.get("taken_to") else ""
                )
            )
        to = (to or "").strip()
        new = bool(RE_TRACK.match(to))
        if not new and (n is not None or size is not None or title is not None):
            raise QueueError(t("queues.take_new_only_car" if RE_CAR.match(to) else "queues.take_new_only_other", to=to))
        by = by or f"{self.NAME} take"
        lines = []
        if new:
            trk, c = tracks.car_add(
                to,
                title or self.car_title(e),
                n=n,
                size=size,
                src=eid,
                by=by,
                note=f"{today()}: {self.take_note(e)}",
                priority=pri.of(e),
            )
            ref = "{}:{}".format(trk["id"], c["n"])
            lines.append(
                t("queues.took_new", id=eid, ref=ref, title=_clip(c["title"], 60), stage=stages.ru(c["stage"]))
            )
        elif RE_CAR.match(to):
            trk, c = tracks.car(to)
            lines += [x for x in (tracks.moved_note(to), tracks.closed_note(trk, c)) if x]
            ref = "{}:{}".format(trk["id"], c["n"])
            tracks.car_took(ref, eid, self.take_note(e), by=by)
            if pri.of(c) != pri.of(e):
                lines.append(
                    t(
                        "queues.took_car_prio",
                        id=eid,
                        ref=ref,
                        title=_clip(c["title"], 60),
                        cprio=pri.cell(c),
                        eprio=pri.cell(e),
                    )
                )
            else:
                lines.append(t("queues.took_car", id=eid, ref=ref, title=_clip(c["title"], 60)))
        elif RE_STACK.match(to):
            if stack is None or not stack(to):
                raise QueueError(f"unknown stack object {to}")
            ref = to
            lines.append(f"{eid} → {ref}")
        else:
            raise QueueError(f"--to is tr-NNNN (a new car on it), tr-NNNN:<car>, st-NNNN or tm-NNNN; got {to!r}")
        self.to_status(eid, "taken", taken_to=ref)
        return e, ref, lines

    # --- the table -------------------------------------------------------------------------------

    COLUMNS: Text | tuple[str, ...] = ()

    def row(self, e):
        raise NotImplementedError

    def md(self, open_only=True, heading=True, limit=None):
        cols = self.COLUMNS + ((t("queues.col.stage"),) if self.STAGES else ())
        L = ["## " + self.TITLE, ""] if heading else []
        L.append("| {} | ".format(t("queues.col.n")) + " | ".join(cols) + " |")
        L.append("|---|" + "---|" * len(cols))
        rows = self.ordered(open_only)
        for i, e in enumerate(rows[:limit] if limit else rows, 1):
            cells = tuple(self.row(e)) + ((self.stage_cell(e),) if self.STAGES else ())
            L.append("| %d | %s |" % (i, " | ".join(_cell(x) for x in cells)))  # noqa: UP031
        if not rows:
            L.append("| — | {} |".format(t("stack.empty")) + " |" * (len(cols) - 1))
        L.append("")
        n_open = len(self.entries(True))
        if limit and len(rows) > limit:
            L.append(t("queues.open_of_first", n=n_open, total=len(self.data["entries"]), limit=limit))
        else:
            L.append(t("queues.open_of", n=n_open, total=len(self.data["entries"])))
        if self.STAGES:
            L.append(stage_note())
        return "\n".join(L) + "\n"

    def to_json(self, open_only=True):
        """The entries as data; with stages, each with its stage (an entry from before the field: the default)."""
        rows = self.entries(open_only=open_only)
        if not self.STAGES:
            return rows
        return [dict(e, stage=self.stage_of(e), stages=e.get("stages") or []) for e in rows]

    def head(self, n=3):
        return ["{} {}".format(e["id"], _clip(self.headline(e), 70)) for e in self.ordered(True)[:n]]

    def headline(self, e):
        return e.get("title") or e.get("text") or ""

    def show(self, eid):
        e = self.get(eid)
        L = ["# {} ({})".format(e["id"], self.STATUS_RU.get(e["status"], e["status"]))]
        for k in sorted(e):
            if k in ("id", "status"):
                continue
            v = e[k]
            if v in (None, [], ""):
                continue
            if k == "stages":
                L.append("stages:")
                L += ["  " + x for x in stages.rows_md(e)]
                continue
            if isinstance(v, list):
                L.append(f"{k}:")
                L += [f"  - {x}" for x in v]
            elif isinstance(v, str) and "\n" in v:
                L.append(f"{k}:")
                L += ["  " + x for x in v.splitlines()]
            else:
                L.append(f"{k}: {v}")
        return "\n".join(L) + "\n"


def stage_note():
    """Under a queue with stages: what its entries' stage is."""
    return t("queues.stage_note")


def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- wishes ----------------------------------------------------------------------------------------

AREAS = ("product", "tools", "infra", "release", "site")
AREA_RU = i18n.Labels("queues.area", AREAS)
PRIOS = tuple(pri.WORDS)  # the words of before (`--prio high|normal|later`): 8 / 5 / 2, synonyms on input


def check_priority(e):
    """Every entry has its priority, a whole number 1-10 (priority.py)."""
    p = e.get("priority")
    if isinstance(p, bool) or not isinstance(p, int) or not pri.MIN <= p <= pri.MAX:
        raise QueueError("%s: priority is a whole number %d-%d, got %r" % (e.get("id"), pri.MIN, pri.MAX, p))  # noqa: UP031


def by_prio(q, rows):
    """The open ones first, then by priority (10 first); within a priority, the queue's own order."""
    return sorted(rows, key=lambda e: (e["status"] not in q.OPEN, -pri.of(e)))


def prio_cell(counts):
    return pri.counts_cell(counts)


class Wishes(Queue):
    NAME = "wishes"
    PREFIX = "wl"
    TITLE = Text("queues.wishes.title")
    STATUS_RU = i18n.Labels("queues.wishes.status", ("waiting", "taken", "declined", "done"))
    ALSO_FROM = {"done": ("taken",)}  # noqa: RUF012
    OPEN = ("waiting",)
    FIELDS = ("date", "words", "title", "area", "priority", "text", "source_ref")
    COLUMNS = Text(
        "queues.col.wish",
        "queues.col.date",
        "queues.col.words",
        "queues.col.area",
        "queues.col.priority",
        "queues.col.status",
    )
    STAGES = True
    STATUS_STAGE = {"declined": "scrapped", "done": "arrived"}  # noqa: RUF012
    KIND_RU = Text("queues.wishes.kind")

    def stage_default_why(self, e):
        s = e["status"]
        if s == "declined":
            return "scrapped", self.STATUS_RU["declined"]
        if s == "done":
            return "arrived", self.STATUS_RU["done"]
        if s == "taken":
            return "bogie", t("queues.wishes.why_taken", to=e.get("taken_to"))
        return "bogie", self.KIND_RU

    def check(self, e):
        super().check(e)
        if not (e.get("title") or "").strip():
            raise QueueError("a wish needs a title")
        if e.get("area") not in AREAS:
            raise QueueError("area must be one of {}".format(", ".join(AREAS)))
        check_priority(e)

    def add(self, title, words=None, date=None, area="product", priority=None, links=(), text=None):
        return super().add(
            title=title,
            words=words,
            date=date or today(),
            area=area,
            priority=pri.DEFAULT if priority is None else pri.parse(priority),
            status="waiting",
            taken_to=None,
            links=list(links or ()),
            text=text,
        )

    def take_text(self, e):
        words = " ".join((e.get("words") or "").split())
        text = " ".join((e.get("text") or "").split())
        return (
            e["title"] + (t("queues.wishes.take_words", words=words) if words else "") + (f"; {text}" if text else "")
        )

    def done(self, eid, reason):
        """Not `drop`."""
        e = self.get(eid)
        if e["status"] == "done":
            raise QueueError(f"{eid} is done already")
        if not (reason or "").strip():
            raise QueueError("done needs --reason")
        return self.to_status(eid, "done", reason=reason)

    def ordered(self, open_only=True):
        rows = self.entries(open_only)
        return sorted(rows, key=lambda e: (e["status"] not in self.OPEN, -pri.of(e), _neg_date(e.get("date")), e["id"]))

    def row(self, e):
        st = self.STATUS_RU[e["status"]] + (" → {}".format(e["taken_to"]) if e.get("taken_to") else "")
        return (
            "{} {}".format(e["id"], e["title"]),
            e.get("date") or "—",
            _clip(e.get("words"), 160) or "—",
            AREA_RU.get(e["area"], e["area"]),
            pri.cell(e),
            st,
        )


def _neg_date(d):
    """Newest first: a sort key that reverses YYYY-MM-DD; no date sorts last."""
    if not d:
        return "~"
    return "".join(chr(ord("9") - ord(ch) + ord("0")) if ch.isdigit() else ch for ch in d)


# --- to-think --------------------------------------------------------------------------------------


class Think(Queue):
    NAME = "think"
    PREFIX = "th"
    TITLE = Text("queues.think.title")
    STATUS_RU = i18n.Labels("queues.think.status", ("waiting", "in_round", "decided", "dropped", "taken"))
    OPEN = ("waiting", "in_round")
    FIELDS = ("date", "topic", "questions", "by", "related", "text", "source_ref", "priority")
    LIST_FIELDS = ("links", "notes", "related", "questions")
    COLUMNS = Text(
        "queues.col.topic",
        "queues.col.date",
        "queues.col.questions",
        "queues.col.by",
        "queues.col.related",
        "queues.col.priority",
        "queues.col.status",
    )
    STAGES = True
    STATUS_STAGE = {"dropped": "scrapped"}  # noqa: RUF012
    KIND_RU = Text("queues.think.kind")

    def stage_default_why(self, e):
        """A to-think entry is a bogie with a question (`question`); answered, it stays one, closed (any work
        it led to is a car of its own); taken into a car (`take`), it lives on there; dropped, scrapped.
        """
        s = e["status"]
        if s == "dropped":
            return "scrapped", self.STATUS_RU["dropped"]
        if s == "decided":
            return "bogie", t("queues.think.why_decided")
        if s == "taken":
            return "bogie", t("queues.think.why_taken", to=e.get("taken_to"))
        return "bogie", t("queues.think.why_open")

    def take_text(self, e):
        qs = [" ".join(q.split()) for q in e.get("questions") or [] if (q or "").strip()]
        text = " ".join((e.get("text") or "").split())
        listed = "; ".join("%d) %s" % (i, q) for i, q in enumerate(qs, 1))  # noqa: UP031
        return (
            e["topic"] + (t("queues.think.take_questions", list=listed) if qs else "") + (f"; {text}" if text else "")
        )

    def normalize(self, fields):
        if "question" not in fields and fields.get("topic") is not None and fields.get("status"):
            from .stages import question_of

            fields = dict(fields, question=question_of(fields))
        return fields

    def check(self, e):
        super().check(e)
        if not (e.get("topic") or "").strip():
            raise QueueError("a to-think entry needs a topic")
        check_priority(e)

    def add(self, topic, questions=(), by=None, related=(), date=None, links=(), text=None, priority=None):
        return super().add(
            topic=topic,
            questions=list(questions or ()),
            by=by,
            related=list(related or ()),
            date=date or today(),
            status="waiting",
            round=None,
            decision=None,
            links=list(links or ()),
            text=text,
            priority=pri.DEFAULT if priority is None else pri.parse(priority),
        )

    def ordered(self, open_only=True):
        return by_prio(self, self.entries(open_only))

    def to_round(self, eid, rid, cfg):
        """Link the entry to a team round that ran (rounds/<id>) or is queued (tm-NNNN)."""
        state = self.state
        if re.match(r"^tm-\d{4}$", rid):
            from .tstack import TeamStack

            if rid not in TeamStack(state, cfg).known_ids():
                raise QueueError(f"no {rid} in the team stack")
        elif not os.path.isfile(os.path.join(state.rounds_dir(), rid, "round.json")):
            raise QueueError(f"no round {rid}")
        return self.to_status(eid, "in_round", round=rid)

    def decide(self, eid, dec=None, reason=None, decisions=None):
        if dec and decisions is not None and dec not in decisions:
            raise QueueError(f"no decision {dec} in decisions.jsonl")
        if not dec and not reason:
            raise QueueError("decide needs --dec dec-NNNNNN or --reason")
        return self.to_status(eid, "decided", reason=reason, decision=dec)

    def headline(self, e):
        return e["topic"]

    def row(self, e):
        st = self.STATUS_RU[e["status"]]
        if e["status"] == "in_round":
            st += " " + e["round"]
        elif e["status"] == "decided" and e.get("decision"):
            st += " " + e["decision"]
        elif e["status"] == "taken" and e.get("taken_to"):
            st += " → " + e["taken_to"]
        qs = e.get("questions") or []
        q = ("%d: %s" % (len(qs), _clip(qs[0], 120))) if qs else "—"  # noqa: UP031
        return (
            "{} {}".format(e["id"], _clip(e["topic"], 120)),
            e.get("date") or "—",
            q,
            e.get("by") or "—",
            ", ".join(e.get("related") or []) or "—",
            pri.cell(e),
            st,
        )


# --- todo ------------------------------------------------------------------------------------------

KINDS = ("bug", "perf", "text", "cleanup", "reminder")
KIND_RU = i18n.Labels("queues.kind", KINDS)


class Todo(Queue):
    NAME = "todo"
    PREFIX = "td"
    TITLE = Text("queues.todo.title")
    STATUS_RU = i18n.Labels("queues.todo.status", ("open", "in_pr", "done", "wont", "taken"))
    OPEN = ("open", "in_pr")
    FIELDS = ("date", "version", "where", "text", "title", "source", "kind", "trigger", "source_ref", "priority")
    LIST_FIELDS = ("links", "notes", "where")
    COLUMNS = Text(
        "queues.col.what",
        "queues.col.date",
        "queues.col.version",
        "queues.col.where",
        "queues.col.source",
        "queues.col.kind",
        "queues.col.priority",
        "queues.col.status",
    )
    KIND_RU = "todo"

    def take_text(self, e):
        title = " ".join((e.get("title") or "").split())
        text = " ".join((e.get("text") or "").split())
        what = f"{title}: {text}" if title and text and not text.startswith(title) else (text or title)
        bits = [
            x
            for x in (
                t("queues.todo.bit_kind", v=KIND_RU.get(e.get("kind"), e.get("kind"))),
                t("queues.todo.bit_version", v=e["version"]) if e.get("version") else None,
                t("queues.todo.bit_where", v=", ".join(e["where"])) if e.get("where") else None,
                t("queues.todo.bit_source", v=e["source"]) if e.get("source") else None,
                t("queues.todo.bit_when", v=e["trigger"]) if e.get("trigger") else None,
            )
            if x
        ]
        return what + " ({})".format("; ".join(bits))

    def check(self, e):
        super().check(e)
        if not (e.get("text") or e.get("title") or "").strip():
            raise QueueError("a todo needs a text")
        if e.get("kind") not in KINDS:
            raise QueueError("kind must be one of {}".format(", ".join(KINDS)))
        check_priority(e)

    def add(
        self,
        text,
        title=None,
        where=(),
        version=None,
        date=None,
        source=None,
        kind="bug",
        trigger=None,
        links=(),
        priority=None,
    ):
        return super().add(
            text=text,
            title=title,
            where=list(where or ()),
            version=version,
            date=date or today(),
            source=source,
            kind=kind,
            trigger=trigger,
            status="open",
            pr=None,
            links=list(links or ()),
            priority=pri.DEFAULT if priority is None else pri.parse(priority),
        )

    def ordered(self, open_only=True):
        return by_prio(self, self.entries(open_only))

    def in_pr(self, eid, pr):
        return self.to_status(eid, "in_pr", pr=int(pr))

    def headline(self, e):
        return e.get("title") or e.get("text") or ""

    def row(self, e):
        st = (
            self.STATUS_RU[e["status"]]
            + (" #{}".format(e["pr"]) if e["status"] == "in_pr" else "")
            + (" → {}".format(e["taken_to"]) if e["status"] == "taken" and e.get("taken_to") else "")
        )
        what = e.get("title") or ""
        text = e.get("text") or ""
        if what and text and not text.startswith(what):
            what = f"{what}: {text}"
        what = _clip(what or text, 180)
        if e.get("trigger"):
            what += " ({})".format(t("queues.todo.bit_when", v=e["trigger"]))
        return (
            "{} {}".format(e["id"], what),
            e.get("date") or "—",
            e.get("version") or "—",
            ", ".join(e.get("where") or []) or "—",
            e.get("source") or "—",
            KIND_RU.get(e["kind"], e["kind"]),
            pri.cell(e),
            st,
        )


SOURCE_KIND = {"wl": "wish", "th": "think", "td": "todo", "st": "stack", "tm": "tstack"}
SOURCE_RU = i18n.Labels("queues.source", ("wish", "think", "todo", "stack", "tstack"))


def source(state, cfg, ref):
    """A ref of a car's `from` as data: {ref, kind, kind_ru, title, status_ru} (title and status None when it names
    nothing the depot keeps).
    """
    kind = SOURCE_KIND.get((ref or "")[:2])
    kind_ru = SOURCE_RU.get(kind)
    out = {"ref": ref, "kind": kind, "kind_ru": kind_ru, "title": None, "status_ru": None}
    try:
        if kind in ("wish", "think", "todo"):
            q = {"wish": Wishes, "think": Think, "todo": Todo}[kind](state, cfg)
            e = q.get(ref)
            out.update(title=q.headline(e), status_ru=q.STATUS_RU.get(e["status"], e["status"]))
        elif kind in ("stack", "tstack"):
            from .stack import Stack
            from .tstack import TeamStack

            stk = (Stack if kind == "stack" else TeamStack)(state, cfg)
            live = stk._dep_open(ref)
            o = stk.get(ref) if live else stk.left_obj(ref)
            out.update(
                title=(o or {}).get("title"), status_ru=t("queues.source_live" if live else "queues.source_left")
            )
    except ValueError:
        pass
    return out


def source_line(s):
    return "{} — {}{}{}".format(
        s["ref"],
        s["kind_ru"] or "?",
        " «{}»".format(_clip(s["title"], 80)) if s.get("title") else "",
        " ({})".format(s["status_ru"]) if s.get("status_ru") else "",
    )


def repo_version(cfg):
    clone = os.path.expanduser((cfg.get("deep_repo") or {}).get("clone") or "")
    if not clone or not os.path.isdir(clone):
        return None
    ver = None
    pkg = read_json(os.path.join(clone, "package.json"))
    if pkg:
        ver = pkg.get("version")
    r = subprocess.run(
        ["git", "-C", clone, "rev-parse", "--short=7", "origin/main"], capture_output=True, text=True, timeout=30
    )
    commit = r.stdout.strip() if r.returncode == 0 else None
    parts = [x for x in (ver, ("main " + commit) if commit else None) if x]
    return ", ".join(parts) or None


# --- the one-shot migration ------------------------------------------------------------------------

PRIO_NOTE = re.compile(r"^\s*(?:\d{4}-\d{2}-\d{2}:\s*)?prio:\s*(high|normal|later)\b", re.IGNORECASE)


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
    without `stage` gets its stage (stages.migrate); every entry taken into a car is named in the car's `from`
    (migrate_from). An entry or object that has the field is never touched, so a second run changes nothing. The
    priority of a todo or to-think entry from its newest `prio: …` note, that this command wrote once, is the depot's
    data migration's now (migrate.py, schema 3 → 4: `priority` 1-10 for every entry). Returns (lines, changed).
    """
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
    L.append(
        t(
            "queues.migrate_autostart",
            n=len(missing),
            list=(": " + ", ".join(o["id"] for o in missing)) if missing else "",
        )
    )
    lines, n = stages.migrate(state, cfg, dry_run=dry_run)
    L += lines
    changed += n
    lines, n = migrate_from(state, cfg, dry_run=dry_run)
    L += lines
    changed += n
    if not changed:
        L.append(t("queues.migrate_nothing"))
    return L, changed


def migrate_from(state, cfg, dry_run=False):
    from .tracks import RE_CAR, RE_TRACK, TrackError, Tracks, car_notes

    tr = Tracks(state, cfg)
    added, moved, left, queues = {}, {}, [], []
    for q in (Wishes(state), Think(state), Todo(state)):
        retarget = {}
        for e in q.entries():
            to = e.get("taken_to") or ""
            if e["status"] != "taken":
                continue
            if RE_TRACK.match(to):
                try:
                    trk = tr.get(to)
                except TrackError:
                    continue
                name = re.compile(r"(?<![\w-]){}(?![\w-])".format(re.escape(e["id"])))
                hits = [c for c in trk["cars"] if name.search(" ".join([c.get("title") or "", *car_notes(c)]))]
                if len(hits) != 1:
                    left.append(
                        t("queues.migrate_many_cars", id=e["id"], to=to, n=len(hits))
                        if hits
                        else t("queues.migrate_no_car", id=e["id"], to=to)
                    )
                    continue
                c = hits[0]
                retarget[e["id"]] = {"was": to, "now": "{}:{}".format(trk["id"], c["n"])}
                moved[e["id"]] = retarget[e["id"]]["now"]
                e["taken_to"] = retarget[e["id"]]["now"]
            elif RE_CAR.match(to):
                try:
                    trk, c = tr.car(to)
                except TrackError:
                    continue  # a ref that names no car: the entry keeps it as it is
            else:
                continue
            if e["id"] in (c.get("from") or []):
                continue
            c["from"] = [*list(c.get("from") or []), e["id"]]
            added.setdefault("{}:{}".format(trk["id"], c["n"]), []).append(e["id"])
        if retarget:
            queues.append((q, retarget))
    if not dry_run:
        if added:  # the cars first: a run cut short after them finds each car naming its entry, and retargets again
            tr._save("migrate_from", None, cars=added)
        for q, retarget in queues:
            q._save("migrate_from", None, taken_to=retarget)
    L = [
        t(
            "queues.migrate_from",
            n=len(added),
            list=(": " + ", ".join("{} ← {}".format(k, ", ".join(v)) for k, v in added.items())) if added else "",
        )
    ]
    if moved or left:
        L.append(
            t(
                "queues.migrate_moved",
                n=len(moved),
                moved=(": " + ", ".join("{} → {}".format(*kv) for kv in moved.items())) if moved else "",
                left=t("queues.migrate_left", list="; ".join(left)) if left else "",
            )
        )
    return L, sum(len(v) for v in added.values()) + len(moved)


# --- the overview ----------------------------------------------------------------------------------


def overview(state, cfg, now=None):
    from .stack import Ballots, Stack
    from .trains import STATE_RU, Trains
    from .tstack import TeamStack

    stk, tst, tr = Stack(state, cfg), TeamStack(state, cfg), Trains(state, cfg)
    tk = tr.tracks
    bq = Ballots(state)
    wl, th, td = Wishes(state), Think(state), Todo(state)
    rows = []

    def stack_head(s):
        objs = s.in_slots() + s.posted() + s.queue()
        tag = {"slot": t("queues.ov.tag_slot"), "post": t("queues.ov.tag_post")}
        return ["{} {}{}".format(o["id"], _clip(o["title"], 50), tag.get(o["where"], "")) for o in objs[:3]]

    rows.append(
        (
            t("queues.ov.stack"),
            t(
                "queues.ov.stack_count",
                s=len(stk.in_slots()),
                sn=stk.slots_n,
                p=len(stk.posted()),
                pn=stk.post_max,
                q=len(stk.queue()),
                qn=stk.queue_max,
            ),
            stack_head(stk),
            "stack",
            None,
        )
    )
    from .merge import overview_row

    rows.append(overview_row(state))
    rows.append(
        (
            t("queues.ov.tstack"),
            t("queues.ov.tstack_count", s=len(tst.in_slots()), sn=tst.slots_n, q=len(tst.queue()), qn=tst.queue_max),
            stack_head(tst),
            "tstack",
            None,
        )
    )
    live = tr.trains(include_done=False)
    nx = tr.next(now)
    by = {}
    for tn in live:
        by[tn["state"]] = by.get(tn["state"], 0) + 1
    head = (
        [t("queues.ov.can_now", list=", ".join("{}:{}".format(p["track"]["id"], p["car"]["n"]) for p in nx["picks"]))]
        if nx["picks"]
        else []
    )
    ready = ["{}:{}".format(tn["id"], c["n"]) for tn, c, b in nx["parked"] if not b]
    head += [t("queues.ov.to_merge", list=", ".join(ready))] if ready else []
    head += ["{} ({})".format(tn["name"], STATE_RU[tn["state"]]) for tn in live[:3]]
    states = ", ".join("%s %d" % (STATE_RU[k], by[k]) for k in ("design", "impl", "merge") if k in by)  # noqa: UP031
    rows.append((t("queues.ov.trains"), "%d (%s)" % (len(live), states) if live else "0", head, "trains", None))  # noqa: UP031
    open_tracks = [tk_ for tk_ in tk.tracks() if not tk.done(tk_)]
    rows.append(
        (
            t("queues.ov.tracks"),
            t("queues.ov.tracks_count", n=len(open_tracks), total=len(tk.tracks())),
            ["{} {}".format(tk_["id"], _clip(tk_["title"], 40)) for tk_ in open_tracks[:3]],
            "tracks",
            None,
        )
    )
    open_b = bq.entries(open_only=True)
    rows.append(
        (
            t("queues.ov.ballots"),
            str(len(open_b)),
            [
                "{}{}".format(e["round"], t("queues.ov.at_point", point=e["at_point"]) if e.get("at_point") else "")
                for e in open_b[:3]
            ],
            "ballots",
            None,
        )
    )
    rows.append((t("queues.ov.wishes"), str(len(wl.entries(True))), wl.head(), "wishes", wl.prio_counts()))
    rows.append((t("queues.ov.think"), str(len(th.entries(True))), th.head(), "think", th.prio_counts()))
    kinds = {}
    for e in td.entries(True):
        kinds[e["kind"]] = kinds.get(e["kind"], 0) + 1
    rows.append(
        (
            "Todo",
            "%d%s"  # noqa: UP031
            % (
                len(td.entries(True)),
                (" ({})".format(", ".join("%s %d" % (KIND_RU[k], kinds[k]) for k in KINDS if k in kinds)))  # noqa: UP031
                if kinds
                else "",
            ),
            td.head(),
            "todo",
            td.prio_counts(),
        )
    )
    from .principles import Principles

    pc = Principles(state, cfg)
    rows.append(
        (
            t("queues.ov.principles"),
            t("queues.ov.principles_count", n=len(pc.entries(True))),
            ["③ {} {}".format(e["id"], _clip(pc.headline(e), 60)) for e in pc.checked()[:3]],
            "principles",
            None,
        )
    )

    L = [t("queues.ov.heading", ts=now_iso()[:16].replace("T", " ")), ""]
    L.append(t("queues.ov.header"))
    L.append("|---|---|---|---|---|")
    for name, count, head, cmd, prio in rows:
        L.append(
            "| {} | {} | {} | {} | `depot2 {} --md` |".format(
                name, count, prio_cell(prio) if prio is not None else "—", _cell("; ".join(head) or "—"), cmd
            )
        )
    L.append("")
    L.append(t("queues.ov.priority_legend"))
    by_stage = stages.summary(state, cfg)
    L.append(
        t(
            "queues.ov.stages",
            counts="%d — %s" % (sum(by_stage.values()), stages.counts_cell(by_stage))  # noqa: UP031
            if by_stage
            else "0",
        )
    )
    return "\n".join(L) + "\n", rows
