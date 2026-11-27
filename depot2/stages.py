"""Every car has a stage, `stage`, and the history of its moves, `stages: [{stage, from, at, by, why}]`: an agents'
stack object (st-) and a track's car (tr-NNNN:N). A team stack item is not a car.

A car and its stack object are one car: a move of one is a move of the other. The commands that move a car move its
stage (the stack, the tracks, the merge queue); `car stage` makes only the moves no other
command makes (the planning stages, the build ⇄ the docs in a slot, and in-transit → arrived by hand), and it names
the command for the rest. A move a command makes off the route (MOVES) is recorded with `off_route` and said, never
refused: the old commands keep working.
"""

import json

from . import i18n
from .store import now_iso

STAGES = ("bogie", "sketch", "project", "build", "docs", "acceptance", "queued", "rolling", "arrived", "scrapped")
# The stage's name and what it means, in the run's language (catalog keys stage.<code>, stage_means.<code>); the
# English name is the code itself.
STAGE_RU = i18n.Labels("stage", STAGES)
STAGE_MEANS = i18n.Labels("stage_means", STAGES)
PLANNING = ("bogie", "sketch", "project")
TERMINAL = ("arrived", "scrapped")
WORK = ("build", "docs")
ORDER = {s: i for i, s in enumerate(STAGES)}

MOVES = {
    "bogie": ("sketch", "scrapped"),
    "sketch": ("project", "build", "bogie", "scrapped"),
    "project": ("build", "sketch", "scrapped"),
    "build": ("acceptance", "docs", "queued", "arrived", "project", "sketch", "scrapped"),
    "docs": ("acceptance", "queued", "build", "scrapped"),
    "acceptance": ("queued", "rolling", "build", "scrapped"),
    "queued": ("rolling", "acceptance", "build", "docs", "scrapped"),
    "rolling": ("arrived",),
    "arrived": (),
    "scrapped": ("bogie", "sketch"),
}
RETURN_FROM = ("acceptance", "queued")
# The moves worth a word: (from, to) -> the catalog key of the note (move_note() says it in the run's language).
MOVE_NOTES = {("build", "queued"): "stages.move_note.build_queued",
              ("build", "arrived"): "stages.move_note.build_arrived",
              ("build", "docs"): "stages.move_note.build_docs",
              ("docs", "queued"): "stages.move_note.docs_queued",
              ("queued", "docs"): "stages.move_note.queued_docs",
              ("acceptance", "rolling"): "stages.move_note.acceptance_rolling"}


def move_note(cur, stage):
    key = MOVE_NOTES.get((cur, stage))
    return i18n.t(key) if key else None


class StageError(ValueError):
    pass


def ru(stage):
    return STAGE_RU.get(stage, stage or "—")


def parse(text):
    """A typed stage: its code, or its name in any language (any case, the Russian yo read as ye)."""
    typed = i18n.fold(text)
    for s in STAGES:
        if typed == s or any(typed == i18n.fold(f) for f in i18n.forms("stage." + s)):
            return s
    raise StageError(i18n.t("stages.no_stage_list", text=text, stages=", ".join(
        ("%s (%s)" % (s, STAGE_RU[s])) if STAGE_RU[s] != s else s for s in STAGES)))


def is_back(cur, stage):
    return cur == "scrapped" or (stage != "scrapped" and ORDER[stage] < ORDER[cur])


def needs_why(cur, stage):
    return stage == "scrapped" or (cur is not None and is_back(cur, stage))


def allowed(cur):
    return ", ".join(ru(s) for s in MOVES.get(cur, ())) or i18n.t("stages.nowhere")


def is_return(cur, stage):
    return stage in WORK and cur in RETURN_FROM


def check_move(cur, stage, why):
    """The route's check (`car stage`): refused with what the stage allows."""
    if stage == cur:
        raise StageError(i18n.t("stages.already", stage=ru(stage)))
    if stage not in MOVES.get(cur, ()):
        raise StageError(i18n.t("stages.off_route", cur=ru(cur), to=ru(stage), allowed=allowed(cur)))
    if needs_why(cur, stage) and not (why or "").strip():
        if stage == "scrapped":
            raise StageError(i18n.t("stages.needs_why_scrap", cur=ru(cur), to=ru(stage)))
        raise StageError(i18n.t("stages.needs_why_back", cur=ru(cur), to=ru(stage)))


def record(rec, stage, by, why=None, default=None, at=None, forced=False, gates=None):
    if stage not in STAGES:
        raise StageError(i18n.t("stages.no_stage", stage=stage))
    cur = rec.get("stage") or default
    if stage == cur:
        return None
    row = {"stage": stage, "from": cur, "at": at or now_iso(), "by": by, "why": (why or "").strip() or None}
    if cur and stage not in MOVES.get(cur, ()):
        row["off_route"] = True
    if forced:
        row["forced"] = True
    if gates:
        row["gates"] = gates
    if is_return(cur, stage):
        row["return"] = True
        rec["returns"] = int(rec.get("returns") or 0) + 1
    rec["stage"] = stage
    rec["stages"] = list(rec.get("stages") or []) + [row]
    return row


def start(rec, stage, by, why=None, forced=False, gates=None):
    """A new record's first stage (its history begins with it); forced/gates: a repair added past its checks."""
    rec.pop("stage", None)
    rec["stages"] = []
    return record(rec, stage, by, why, forced=forced, gates=gates)


def hist(row):
    if not row:
        return None
    return "%s→%s" % (row.get("from") or "", row["stage"])


def say(ref, row):
    if not row or not row.get("off_route"):
        return None
    return i18n.t("stages.say_off_route", ref=ref, cur=ru(row["from"]), to=ru(row["stage"]), by=row["by"],
                  allowed=allowed(row["from"]))


def prev_planning(rec, default=None):
    """The planning stage a record had last (bogie, sketch, project), for a car sent back to its track."""
    for row in reversed(rec.get("stages") or []):
        if row["stage"] in PLANNING:
            return row["stage"]
        if row.get("from") in PLANNING:
            return row["from"]
    return default or ("sketch" if rec.get("brief") else "bogie")


def counts(stages_):
    out = {}
    for s in stages_:
        out[s] = out.get(s, 0) + 1
    return out


def counts_cell(c, sep=" · "):
    return sep.join("%s %d" % (STAGE_RU[s], c[s]) for s in STAGES if c.get(s)) or "—"


def rows_md(rec):
    """A record's history, one line each, oldest first."""
    out = []
    for r in rec.get("stages") or []:
        move = ("%s → %s" % (ru(r["from"]), ru(r["stage"]))) if r.get("from") else "→ %s" % ru(r["stage"])
        tail = [x for x in (r.get("why"), r.get("by")) if x]
        flags = [x for x, on in ((i18n.t("stages.flag_return"), r.get("return")),
                                 (i18n.t("stages.flag_off_route"), r.get("off_route")),
                                 ("--force", r.get("forced"))) if on]
        if r.get("gates"):
            from .gates import gates_text
            flags.append(i18n.t("stages.flag_gates", gates=gates_text(r["gates"])))
        out.append("- %s %s%s%s" % ((r.get("at") or "?")[:16].replace("T", " "), move,
                                   (" (%s)" % "; ".join(tail)) if tail else "", (" [%s]" % ", ".join(flags)) if flags else ""))
    return out



def merge_index(state):
    """The merge queue's open entries by their object, car and PR."""
    from .merge import MergeQueue
    idx = {"st": {}, "car": {}, "pr": {}}
    for e in MergeQueue(state).entries(open_only=True):
        if e.get("st"):
            idx["st"][e["st"]] = e
        if e.get("car"):
            idx["car"][e["car"]] = e
        idx["pr"][e["pr"]] = e
    return idx


def entry_stage(e):
    if e["status"] == "dropped":
        return None
    if e["status"] == "merged":
        return "rolling" if (e.get("handoff") and not e["handoff"].get("arrived")) else "arrived"
    return "rolling" if e.get("merged") else "queued"


def entry_default(e):
    """(stage, why) of a car in an open merge queue entry."""
    s = entry_stage(e)
    if s == "queued":
        return s, i18n.t("stages.in_merge_queue", id=e["id"])
    return s, i18n.t("stages.merged_rolling", sha=((e.get("merged") or {}).get("sha") or "?")[:7], id=e["id"])



def rides(state):
    """The merge queue's rides that ended, oldest first: [(its merge's time, entry)] — its `merged` entries but the
    handoffs (a handoff's deploy rode with another ride). What merged into main before a ride's merge is in it."""
    from .merge import MergeQueue, epoch
    out = [(e["merged"]["ts"], e) for e in MergeQueue(state).entries()
           if e["status"] == "merged" and not e.get("handoff") and (e.get("merged") or {}).get("ts")]
    return sorted(out, key=lambda x: epoch(x[0]) or 0)


def rolled_in(rec):
    for r in reversed(rec.get("stages") or []):
        if r.get("stage") == "rolling":
            return r.get("at")
    return None


def rolled_before(state, cfg, cutoff):
    from .board import Board
    from .merge import epoch
    cut = epoch(cutoff)
    if cut is None:
        return []
    b = Board(state, cfg)
    out = []
    for it in b.items(with_gates=False):
        if it["stage"] != "rolling" or it["kind"] not in ("car", "left") or it["object"] or it["merge"]:
            continue
        car = Car(state, cfg, it["ref"], stack=b.stk, tracks=b.tr)
        at = rolled_in(car.rec())
        if epoch(at) is not None and epoch(at) < cut:
            out.append((car, at))
    return sorted(out, key=lambda x: epoch(x[1]))


def hand_why(e):
    return i18n.t("stages.hand_why", pr=e["pr"], id=e["id"])


def arrive_hint(ref):
    """Who sets «arrived» on a rolling car, and the command by hand."""
    return i18n.t("stages.arrive_hint", ref=ref)


# --- one car, by its id ------------------------------------------------------------------------------

class Car(object):
    """What a ref names: a stack object (live, or one that left) or a track's car; and its twin (a car's live stack
    object, an object's car)."""

    def __init__(self, state, cfg, ref, stack=None, tracks=None):
        from .stack import Stack
        from .tracks import RE_CAR, Tracks
        self.state, self.cfg, self.ref = state, cfg, ref
        self.stack = stack or Stack(state, cfg)
        self.tracks = tracks or Tracks(state, cfg)
        self.tracks._stack = self.stack
        self.obj = self.left = self.track = self.car = None
        if ref.startswith("st-"):
            if not self.stack._known(ref):
                raise StageError(i18n.t("stages.no_object", ref=ref))
            t, c = self.tracks.car_of_stack(ref)
            if c is not None and (c.get("st") == ref or not self.stack._dep_open(ref)):
                self.track, self.car = t, c
            if self.stack._dep_open(ref):
                self.obj = self.stack.get(ref)
            elif self.car is None:
                self.left = self.stack.left_obj(ref)
                if self.left is None:
                    raise StageError(i18n.t("stages.left_no_record", ref=ref))
        elif RE_CAR.match(ref):
            self.track, self.car = self.tracks.car(ref)
            if self.car.get("st") and self.stack._dep_open(self.car["st"]):
                self.obj = self.stack.get(self.car["st"])
        else:
            raise StageError(i18n.t("stages.bad_ref", ref=ref))

    @property
    def car_ref(self):
        return "%s:%s" % (self.track["id"], self.car["n"]) if self.car is not None else None

    @property
    def label(self):
        if self.car is not None:
            return i18n.t("stages.label_object", car=self.car_ref, obj=self.obj["id"]) if self.obj else self.car_ref
        return self.ref

    def rec(self):
        for r in (self.car, self.obj, self.left):
            if r is not None:
                return r

    def stage(self):
        if self.car is not None:
            return self.tracks.stage_of(self.car)
        if self.obj is not None:
            return self.stack.stage_of(self.obj)
        return self.stack.left_stage(self.ref)

    def title(self):
        return self.rec().get("title") or ""

    def owned(self, stage):
        """The command that makes this move, when one does (`car stage` refuses it and names it)."""
        if stage == "queued":
            return i18n.t("stages.owned_queued")
        if self.obj is not None:
            oid = self.obj["id"]
            if self.obj["where"] != "queue":
                where = {"slot": i18n.t("stages.where.slot"), "post": i18n.t("stages.where.post")}[self.obj["where"]]
                if stage in PLANNING:
                    return i18n.t("stages.owned_back_to_brief", oid=oid, where=where)
            return {"build": i18n.t("stages.owned_obj_build", oid=oid),
                    "acceptance": i18n.t("stages.owned_obj_acceptance", oid=oid),
                    "rolling": i18n.t("stages.owned_obj_rolling", oid=oid),
                    "arrived": i18n.t("stages.owned_obj_arrived", oid=oid),
                    "scrapped": i18n.t("stages.owned_obj_scrapped", oid=oid)}.get(stage)
        if self.car is not None:
            ref = self.car_ref
            return {"build": i18n.t("stages.owned_car_build", ref=ref),
                    "acceptance": i18n.t("stages.owned_car_acceptance", ref=ref),
                    "rolling": i18n.t("stages.owned_car_rolling", ref=ref),
                    "scrapped": i18n.t("stages.owned_car_scrapped", ref=ref)}.get(stage)
        if stage != "arrived":  # an object that left the stack
            return i18n.t("stages.owned_left", ref=self.ref)
        return None

    def in_slot(self):
        return self.obj is not None and self.obj.get("where") == "slot"

    def arrive_line(self):
        if self.obj is not None or self.kind not in ("car", "left") or self.stage() != "rolling":
            return None
        idx = self.stack.mq_index()
        r = self.rec()
        e = (idx["car"].get(self.car_ref) if self.car is not None else None) or idx["st"].get(r.get("st") or self.ref) \
            or (idx["pr"].get(r.get("pr")) if r.get("pr") else None)
        if e is not None:
            return i18n.t("stages.next_arrive_mq", id=e["id"], pr=e["pr"])
        return i18n.t("stages.next_line", hint=arrive_hint(self.car_ref or self.ref))

    def move(self, stage, why=None, by="car stage", force=False):
        cur = self.stage()
        if force:
            if not (why or "").strip():
                raise StageError(i18n.t("stages.force_needs_why"))
            if stage == cur:
                raise StageError(i18n.t("stages.already", stage=ru(stage)))
        else:
            check_move(cur, stage, why)
            if stage == "docs" and not self.in_slot():
                raise StageError(i18n.t(
                    "stages.docs_needs_slot", ref=self.ref, obj=self.ref if self.obj is None else self.obj["id"],
                    cmd="depot2 tracks car stack %s --start" % self.car_ref if self.car is not None
                    else "depot2 stack start %s" % self.ref))
            cmd = None if (cur, stage) == ("docs", "build") and self.in_slot() else self.owned(stage)
            if cmd:
                raise StageError(i18n.t("stages.owned_by_command", ref=self.ref, cur=ru(cur), to=ru(stage), cmd=cmd))
        rows = []
        if self.car is not None:
            rows.append(self.tracks.car_stage(self.car_ref, stage, by, why, forced=force))
        if self.obj is not None:
            rows.append(self.stack.set_stage(self.obj["id"], stage, by, why, forced=force))
        if self.left is not None:
            rows.append(self.stack.set_stage(self.ref, stage, by, why, forced=force))
        return cur, [r for r in rows if r]


    @property
    def kind(self):
        if self.car is not None:
            return "car"
        if self.obj is not None:
            return "stack"
        return "left"

    def recs(self):
        """The records the gates read: the track's car and its live stack object (one car), or the one record."""
        return [r for r in (self.car, self.obj) if r is not None] or [self.rec()]

    def primary(self):
        """Where a car's own records live (its questions, `needs_project`): the track's car, else the object; an
        object that left the stack has none."""
        for r in (self.car, self.obj):
            if r is not None:
                return r
        raise StageError(i18n.t("stages.left_no_asks", ref=self.ref))

    def _save_rec(self, event, **fields):
        if self.car is not None:
            self.tracks._save(event, self.track["id"], car=self.car["n"], **fields)
        else:
            self.stack._save(event, self.obj["id"], **fields)

    def ask(self, text, by="car ask"):
        """An open question: the DoR waits for its answer."""
        if not (text or "").strip():
            raise StageError(i18n.t("stages.empty_question"))
        r = self.primary()
        qs = list(r.get("asks") or [])
        q = {"n": max([x["n"] for x in qs] or [0]) + 1, "q": " ".join(text.split()), "at": now_iso(), "by": by,
             "answer": None, "answered": None}
        r["asks"] = qs + [q]
        self._save_rec("ask", question=q["n"], text=q["q"], by=by)
        return q

    def answer(self, n, text, by="car answer"):
        if not (text or "").strip():
            raise StageError(i18n.t("stages.empty_answer"))
        r = self.primary()
        for q in r.get("asks") or []:
            if q["n"] == int(n):
                if q.get("answered"):
                    raise StageError(i18n.t("stages.question_closed", n=q["n"], at=q["answered"][:10],
                                            answer=q["answer"]))
                q.update(answer=" ".join(text.split()), answered=now_iso(), answered_by=by)
                self._save_rec("answer", question=q["n"], text=q["answer"], by=by)
                return q
        raise StageError(i18n.t("stages.no_question", ref=self.ref, n=n, qs=", ".join(
            str(q["n"]) for q in r.get("asks") or []) or i18n.t("stages.none")))

    def needs_project(self, why=None, off=False, by="car needs-project"):
        r = self.primary()
        if off:
            if not r.get("needs_project"):
                raise StageError(i18n.t("stages.no_project_mark", ref=self.ref))
            r.pop("needs_project")
            self._save_rec("needs_project", off=True, by=by)
            return None
        if not (why or "").strip():
            raise StageError(i18n.t("stages.project_why"))
        r["needs_project"] = {"why": " ".join(why.split()), "at": now_iso(), "by": by}
        self._save_rec("needs_project", why=r["needs_project"]["why"], by=by)
        return r["needs_project"]

    def gate(self):
        """The next gate, as it stands: the DoR for a planning stage, the DoD for the build (by what the agent
        handed in to its stack object); None where no gate is next (an object that left the stack, past the build)."""
        from . import gates
        s = self.stage()
        if self.left is not None:
            return None
        if s in ("sketch", "project"):
            return {"name": "DoR", "to": "build", "items": list(gates.dor_items(self.cfg)),
                    "unmet": gates.dor_unmet(self.stack, self.tracks, obj=self.obj, car=self.car, track=self.track),
                    "notes": gates.dor_notes(self.stack, obj=self.obj, car=self.car)}
        if s in WORK and self.obj is not None:
            v, d = self.verdict(), self.docs()
            return {"name": "DoD", "to": "acceptance", "items": list(gates.dod_items(self.cfg)),
                    "unmet": gates.dod_unmet(self.cfg, self.obj, principles=v, docs=d),
                    "handed": gates.dod_cells(self.cfg, self.obj, principles=v, docs=d)}
        return None

    def docs(self):
        from . import gates
        if self.left is not None:
            return None
        return gates.docs_state(self.recs(), self.stage(), ref=self.car_ref or self.ref)

    def verdict(self):
        """The verdict on its marks by the principles (marks.py): a track's car's, else its live stack object's; None
        for an object that left the stack."""
        from . import marks
        rec = self.car if self.car is not None else self.obj
        if rec is None:
            return None
        if not hasattr(self, "_plist"):
            self._plist = marks.plist(self.state, self.cfg)
        return marks.of(rec, self._plist)

    def gate_lines(self):
        from . import gates
        L = []
        g = self.gate()
        if g:
            L.append(i18n.t("stages.gate_next", gate=gates.GATE_RU[g["name"]], text=gates.gate_text(
                g, i18n.t("stages.gate_passed"), i18n.t("stages.gate_failed"))))
            if g.get("handed"):
                L.append(i18n.t("stages.handed", handed=g["handed"]))
        need = gates.project_need(self.recs())
        if need:
            L.append(i18n.t("stages.project_required_passed", need=need) if gates.project_passed(self.recs())
                     else i18n.t("stages.project_required_missing", need=need))
        d = self.docs()
        if d and d["need"]:
            L.append(i18n.t("stages.docs_needed", need=d["need"], word=gates.docs_word(d)))
        v = self.verdict()
        if v is not None and v["listed"]:
            from . import marks
            L += marks.lines(v)
        qs = [q for r in self.recs() for q in r.get("asks") or []]
        if qs:
            L.append(i18n.t("stages.questions"))
            for q in qs:
                L.append(i18n.t("stages.question_answered", n=q["n"], q=q["q"], at=q["answered"][:10],
                                answer=q["answer"]) if q.get("answered")
                         else i18n.t("stages.question_open", n=q["n"], q=q["q"]))
        return L

    def _verdict_json(self):
        v = self.verdict()
        return v if v is not None and v["listed"] else None

    def train(self):
        """The live train the car rides (trains.py), or None."""
        return self.tracks.train_of(self.car_ref) if self.car is not None else None

    def show(self):
        from . import priority as pri
        r = self.rec()
        L = ["# %s — %s" % (self.label, ru(self.stage())), "", self.title(), ""]
        L.append(i18n.t("stages.show_priority", p=pri.cell(r), ref=self.ref))
        if self.car is not None:
            tn = self.train()
            L.append(i18n.t("stages.show_track", track=self.track["id"], title=self.track["title"],
                            train="%s «%s»" % (tn["name"], tn["title"]) if tn else i18n.t("stages.none")))
            if ":" in self.ref and self.ref != self.car_ref:
                L.append(i18n.t("stages.show_old_ref", ref=self.ref, now=self.car_ref))
            for m in self.car.get("moved_from") or ():
                L.append(i18n.t("stages.show_moved", src=m.get("from"), to=m.get("to"), at=(m.get("at") or "")[:10],
                                why="; " + m["why"] if m.get("why") else ""))
            if self.car.get("from"):
                from .queues import source_line
                L.append(i18n.t("stages.show_came_from", sources="; ".join(source_line(x) for x in self.sources())))
        if self.left is not None:
            L.append(i18n.t("stages.show_left"))
        L.append(i18n.t("stages.show_route", allowed=allowed(self.stage())))
        nxt = self.arrive_line()
        if nxt:
            L.append(nxt)
        L += self.gate_lines()
        from .claims import car_lines
        L += car_lines(self)
        notes = self.notes()
        if notes:
            L += ["", i18n.t("stages.show_notes")] + ["- %s" % n for n in notes]
        L.append("")
        L.append(i18n.t("stages.show_history"))
        L += rows_md(r) or [i18n.t("stages.show_no_history_left") if self.left is not None
                            else i18n.t("stages.show_no_history")]
        if self.obj is not None and self.car is not None and (self.obj.get("stages") or []) != (r.get("stages") or []):
            L.append("")
            L.append(i18n.t("stages.show_obj_history", id=self.obj["id"]))
            L += rows_md(self.obj) or [i18n.t("stages.show_none_row")]
        return "\n".join(L) + "\n"

    def status_ru(self, r):
        s = (r or {}).get("status")
        if r is self.car and r is not None:
            from .tracks import CAR_RU
            return CAR_RU.get(s, s)
        return s

    def notes(self):
        out = []
        for key, r in (("stages.note_car", self.car), ("stages.note_obj", self.obj)):
            if not r:
                continue
            if r.get("note"):
                out.append(i18n.t(key, note=r["note"]))
            ns = r.get("notes") or ()
            for n in [ns] if isinstance(ns, str) else ns:
                out.append(n if isinstance(n, str) else json.dumps(n, ensure_ascii=False))
        return out

    def sources(self):
        from .queues import source
        return [source(self.state, self.cfg, r) for r in (self.car or {}).get("from") or ()]

    def to_json(self):
        from . import gates
        r = self.rec()
        o = self.obj or self.left or {}
        s = self.stage()
        tn = self.train()
        out = {"v": 1, "ref": self.ref, "kind": self.kind, "car": self.car_ref, "object": o.get("id") if o else None,
               "track": {"id": self.track["id"], "title": self.track["title"]} if self.track else None,
               "train": ({"name": tn["name"], "title": tn["title"], "state": tn["state"]} if tn else None),
               "title": self.title(), "stage": s, "stage_ru": ru(s), "moves": list(MOVES.get(s, ())),
               "stages": r.get("stages") or [],
               "object_stages": (self.obj.get("stages") or []) if self.obj is not None and self.car is not None else None,
               "brief": next((x.get("brief") for x in self.recs() if x.get("brief")), None),
               "size": next((x.get("size") for x in self.recs() if x.get("size")), None),
               "branch": next((x.get("branch") for x in self.recs() if x.get("branch")), None),
               "pr": next((x.get("pr") for x in self.recs() if x.get("pr")), None),
               "where": o.get("where") if self.obj is not None else None,
               "status": r.get("status"), "status_ru": self.status_ru(r), "priority": r.get("priority"),
               "asks": [q for x in self.recs() for q in x.get("asks") or []],
               "project": gates.project_need(self.recs()), "project_passed": gates.project_passed(self.recs()),
               "docs": self.docs(),
               "gate": self.gate(), "ci": o.get("ci"), "dod": o.get("dod") or {},
               "principles": self._verdict_json(),
               "left": self.left is not None, "from": self.sources(),
               "records": {"car": self.car, "object": self.obj, "left": self.left}}
        from .claims import car_json
        out.update(car_json(self))
        return out


# --- the one-shot move of the stack and the tracks onto the stages (`queues migrate`) ----------------------------

def migrate(state, cfg, dry_run=False):
    from .stack import Stack
    from .tracks import Tracks
    by = "queues migrate"
    L, changed = [], 0
    stk = Stack(state, cfg)
    moved = {}
    for o in stk.objects():
        if o.get("stage"):
            continue
        s, why = stk.stage_default_why(o)
        start(o, s, by, i18n.t("stages.migrated_why", why=why))
        moved[o["id"]] = s
    if moved and not dry_run:
        stk._save("migrate_stage", None, objects=moved)
    L.append(i18n.t("stages.migrate_stack", n=len(moved), counts=counts_cell(counts(moved.values()))))
    changed += len(moved)
    tr = Tracks(state, cfg)
    tr._stack = stk  # the objects' stages as just written (a dry run writes nothing)
    moved = {}
    for t in tr.data["tracks"]:
        for c in t["cars"]:
            if c.get("stage"):
                continue
            s, why = tr.stage_default_why(t, c)
            start(c, s, by, i18n.t("stages.migrated_why", why=why))
            moved["%s:%s" % (t["id"], c["n"])] = s
    if moved and not dry_run:
        tr._save("migrate_stage", None, cars=moved)
    L.append(i18n.t("stages.migrate_tracks", n=len(moved), counts=counts_cell(counts(moved.values()))))
    changed += len(moved)
    from .claims import migrate as returns_migrate
    lines, n = returns_migrate(state, cfg, stk, tr, dry_run=dry_run)
    L += lines
    changed += n
    return L, changed


def summary(state, cfg):
    """{stage: n} over the live cars: the stack's objects and the tracks' cars (a car with a live object counts
    once); the terminal stages left out."""
    from .stack import Stack
    from .tracks import Tracks
    stk = Stack(state, cfg)
    tr = Tracks(state, cfg)
    tr._stack = stk
    live = {o["id"] for o in stk.objects()}
    out = [stk.stage_of(o) for o in stk.objects()]
    for t in tr.tracks():
        for c in t["cars"]:
            if c.get("st") in live:
                continue
            out.append(tr.stage_of(c))
    return counts(s for s in out if s not in TERMINAL)
