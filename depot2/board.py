"""The board: every live car in the column of its stage, as a Kanban board (`depot2 board`, the web's first tab).

A column holds the cars on that stage: the stack's objects (a track's car's object is its car), the tracks' cars, the
objects that left the stack still on their way, the merge queue's PRs with no car, the open wishes and to-think
entries. Each row: the car, what it is, its train, its size, where it is, how long it has stood on its stage, and the
next gate as it stands (gates.py). The build and the acceptance have soft limits (`stack.slots`, `stack.post_max`).
"""

from . import gates, i18n, stages
from .i18n import t
from .stack import Stack, _cell, age_ru
from .store import now_iso
from .tracks import Tracks

V = 1
VIEWS = ("all", "stack")
STACK_VIEW = ("build", "docs", "acceptance", "queued")
KIND_RU = i18n.Labels("board.kind", ("car", "stack", "left", "merge", "wish", "think"))
WHERE_RU = i18n.Labels("board.where", ("slot", "post", "queue", "parked", "track", "merge"))


def legend():
    """The board's legend under its summary table."""
    return t("board.legend")


def _since(rec, fallback=None):
    rows = rec.get("stages") or []
    return rows[-1].get("at") if rows else fallback


class Board:
    def __init__(self, state, cfg) -> None:
        from .merge import MergeQueue

        self.state, self.cfg = state, cfg
        self.stk = Stack(state, cfg)
        self.tr = Tracks(state, cfg)
        self.tr._stack = self.stk
        self.trains = self.tr.trains
        self.mq = MergeQueue(state)
        self._mq_open = self.mq.entries(open_only=True)
        self._plist = None  # the third check (marks.py), read once

    # --- the items ---------------------------------------------------------------------------------------

    def _entry_of(self, st=None, car=None, pr=None):
        for e in self._mq_open:
            if (st and e.get("st") == st) or (car and e.get("car") == car) or (pr and e.get("pr") == pr):
                return e
        return None

    def _item(
        self,
        kind,
        ref,
        rec,
        stage,
        title,
        track=None,
        car=None,
        obj=None,
        where=None,
        since=None,
        with_gates=True,
        merge=None,
    ):
        rows = rec.get("stages") or []
        recs = [r for r in (car, obj) if r is not None] or [rec]
        cref = "{}:{}".format(track["id"], car["n"]) if car is not None else None
        tn = self.tr.train_of(cref) if cref else None
        it = {
            "ref": ref,
            "kind": kind,
            "object": (obj or {}).get("id") if obj is not None else None,
            "car": cref,
            "track": track["id"] if track else None,
            "track_title": track["title"] if track else None,
            "train": tn["name"] if tn else None,
            "train_title": tn["title"] if tn else None,
            "train_state": tn["state"] if tn else None,
            "title": title,
            "size": next((r.get("size") for r in recs if r.get("size")), None),
            "stage": stage,
            "stage_ru": stages.ru(stage),
            "since": _since(rec, since),
            "forced": bool(rows and rows[-1].get("forced")),
            "where": where,
            "pr": next((r.get("pr") for r in recs if r.get("pr")), None),
            "branch": next((r.get("branch") for r in recs if r.get("branch")), None),
            "brief": next((r.get("brief") for r in recs if r.get("brief")), None),
            "moves": list(stages.MOVES.get(stage, ())),
            "merge": merge["id"] if merge else None,
            "gate": None,
            "docs": None,
            "priority": recs[0].get("priority") if kind != "merge" else None,
        }
        own = recs[0]
        from .marks import compact

        it["principles"] = compact(self.verdict(car, obj)) if kind in ("car", "stack") else None
        it.update(
            returns=int(own.get("returns") or 0),
            claims=len([x for x in own.get("claim_links") or [] if not x.get("moved")]),
            repair={"of": own.get("repairs"), "bug": own.get("bug")} if own.get("kind") == "repair" else None,
        )
        if kind in ("car", "stack"):
            d = gates.docs_state(recs, stage, ref=ref)
            it["docs"] = d if d["need"] else None
        if with_gates and kind in ("car", "stack"):
            if stage in ("sketch", "project"):
                it["gate"] = {
                    "name": "DoR",
                    "to": "build",
                    "unmet": gates.dor_unmet(self.stk, self.tr, obj=obj, car=car, track=track),
                    "notes": gates.dor_notes(self.stk, obj=obj, car=car),
                }
            elif stage in stages.WORK and obj is not None:
                it["gate"] = {
                    "name": "DoD",
                    "to": "acceptance",
                    "unmet": gates.dod_unmet(self.cfg, obj, principles=self.verdict(car, obj), docs=it["docs"]),
                }
        return it

    def verdict(self, car, obj):
        """The verdict on a car's marks by the principles (marks.py): the track's car's, else the object's."""
        from . import marks

        if self._plist is None:
            self._plist = marks.plist(self.state, self.cfg)
        return marks.of(car if car is not None else obj, self._plist)

    def items(self, all_=False, with_gates=True):
        """Every car on the board, in order: the stack (slots, post-stack, queue), the tracks' cars in the tracks'
        order, the objects that left still on their way, the merge queue's PRs with no car, the wishes, the
        to-think entries. all_: the terminal stages too.
        """
        from .queues import Think, Wishes

        out = []
        objs = self.stk.objects()
        live = {o["id"] for o in objs}
        for o in objs:
            t, c = self.tr.car_by_stack(o["id"])
            where = "parked" if o.get("parked") else o["where"]
            fall = o.get("started") if o["where"] == "slot" else (o.get("posted") or {}).get("ts") or o.get("added")
            e = self._entry_of(st=o["id"])
            if c is not None:
                s = self.tr.stage_of(c)
                out.append(
                    self._item(
                        "car",
                        "{}:{}".format(t["id"], c["n"]),
                        c,
                        s,
                        c["title"],
                        track=t,
                        car=c,
                        obj=o,
                        where=where,
                        since=_since(o, fall),
                        with_gates=with_gates,
                        merge=e,
                    )
                )
            else:
                out.append(
                    self._item(
                        "stack",
                        o["id"],
                        o,
                        self.stk.stage_of(o),
                        o["title"],
                        obj=o,
                        where=where,
                        since=fall,
                        with_gates=with_gates,
                        merge=e,
                    )
                )
        for t in self.tr.tracks():
            for c in t["cars"]:
                if c.get("st") in live:
                    continue
                s = self.tr.stage_of(c)
                if s in stages.TERMINAL and not all_:
                    continue
                ref = "{}:{}".format(t["id"], c["n"])
                e = self._entry_of(car=ref, pr=c.get("pr") if c["status"] in ("pr", "merged") else None)
                where = "merge" if e else "parked" if self.tr.parked(c) else "track"
                out.append(
                    self._item(
                        "car",
                        ref,
                        c,
                        s,
                        c["title"],
                        track=t,
                        car=c,
                        where=where,
                        since=c.get("started") or c.get("added"),
                        with_gates=with_gates,
                        merge=e,
                    )
                )
        links = self.stk.car_links()
        last = {}
        for h in self.stk.history():
            if h.get("object") and h.get("obj"):
                last[h["object"]] = h
        for oid, h in last.items():
            if oid in live or oid in links:
                continue
            o = h["obj"]
            s = o.get("stage") or self.stk._leaving_stage(
                o, h.get("outcome"), h.get("reason"), parked=h.get("event") == "park", fresh=False
            )
            if s in stages.TERMINAL and not all_:
                continue
            e = self._entry_of(st=oid)
            out.append(
                self._item(
                    "left",
                    oid,
                    o,
                    s,
                    o.get("title") or "",
                    where="merge" if e else None,
                    since=h.get("ts"),
                    with_gates=False,
                    merge=e,
                )
            )
        known_st = live | set(last)
        cars = {"{}:{}".format(t["id"], c["n"]) for t in self.tr.tracks() for c in t["cars"]}
        cars_pr = {c.get("pr") for t in self.tr.tracks() for c in t["cars"] if c.get("pr")}
        from .merge import OPEN

        for e in self.mq.entries():
            s = stages.entry_stage(e)
            if s is None or not (e["status"] in OPEN or s == "rolling" or all_):  # rolling: a handoff's, still
                continue
            if (e.get("st") and e["st"] in known_st) or (e.get("car") and e["car"] in cars) or e["pr"] in cars_pr:
                continue
            if s in stages.TERMINAL and not all_:
                continue
            out.append(
                self._item(
                    "merge",
                    "#%d" % e["pr"],  # noqa: UP031
                    e,
                    s,
                    e.get("title") or "",
                    where="merge",
                    since=(e.get("merged") or {}).get("ts") if s == "rolling" else e.get("added"),
                    with_gates=False,
                    merge=e,
                )
            )
        for q in (Wishes(self.state), Think(self.state)):
            kind = "wish" if q.NAME == "wishes" else "think"
            for e in q.entries() if all_ else q.entries(open_only=True):
                s = q.stage_of(e)
                if s in stages.TERMINAL and not all_:
                    continue
                out.append(
                    self._item(
                        kind,
                        e["id"],
                        e,
                        s,
                        e.get("title") or e.get("topic") or e.get("text") or "",
                        since=e.get("added"),
                        with_gates=False,
                    )
                )
        return out

    def _next_entry(self, state, rec, track=None, car=None, why=None, command=None):
        if car is not None:
            ref, obj = "{}:{}".format(track["id"], car["n"]), car.get("st")
        else:
            trk, c = self.tr.car_by_stack(rec["id"])
            ref, obj, track = ("{}:{}".format(trk["id"], c["n"]) if c is not None else rec["id"]), rec["id"], trk
        tn = self.tr.train_of(ref) if track is not None else None
        return {
            "state": state,
            "state_ru": NEXT_RU[state],
            "ref": ref,
            "object": obj,
            "track": track["id"] if track else None,
            "train": tn["name"] if tn else None,
            "title": (car or rec).get("title") or "",
            "why": why,
            "command": command,
            "priority": (car or rec).get("priority"),
        }

    def slots(self, items=None):
        items = self.items(with_gates=True) if items is None else items
        by_obj = {it["object"]: it for it in items if it.get("object")}
        cells = []
        for i, o in enumerate(self.stk.in_slots()):
            it = dict(by_obj.get(o["id"]) or {"ref": o["id"], "object": o["id"], "title": o["title"]})
            it.update(n=i + 1, in_slot=o.get("started"))
            cells.append(it)
        cap = self.stk.slots_n
        cells += [{"n": n + 1, "ref": None} for n in range(len(cells), cap)]
        nx = self.trains.next()
        reps = [o["id"] for o in nx["repairs"]]
        nxt = [
            self._next_entry("now", o, why=t("board.why_repair"), command="depot2 stack start {}".format(o["id"]))
            for o in nx["repairs"]
        ]
        nxt += [
            self._next_entry(
                "now", None, p["track"], p["car"], t("board.why_train", name=p["train"]["name"]), p["command"]
            )
            for p in nx["picks"]
        ]
        nxt += [
            self._next_entry("now", o, why=t("board.why_autostart"), command="depot2 stack start {}".format(o["id"]))
            for o in nx["stack"]
            if o["id"] not in reps
        ]
        for u in nx["upcoming"]:
            nxt.append(
                self._next_entry(
                    "next", None, u["track"], u["car"], t("board.why_train", name=u["train"]["name"]), u["command"]
                )
                if u.get("car")
                else self._next_entry(
                    "next",
                    u["stack"],
                    command=u["command"],
                    why=t("board.why_repair") if u["stack"].get("kind") == "repair" else t("board.why_autostart"),
                )
            )
        for g in nx["gated"]:
            why = "DoR: " + "; ".join(g["dor"])
            nxt.append(
                self._next_entry("gated", None, g["track"], g["car"], why, g["command"])
                if g.get("car")
                else self._next_entry("gated", g["stack"], why=why, command=g["command"])
            )
        nxt += [self._next_entry("owner", w["stack"], why=w["why"], command=w["command"]) for w in nx["owner_word"]]
        used = len(self.stk.in_slots())
        return {"used": used, "max": cap, "free": max(0, cap - used), "cells": cells, "next": nxt}

    def slots_md(self, now=None, per=6):
        sl = self.slots()
        head = (
            "board.slots_free" if sl["free"] else "board.slots_over" if sl["used"] > sl["max"] else "board.slots_full"
        )
        L = [
            t(head, used=sl["used"], max=sl["max"], free=sl["free"]),
            "",
            t("board.slots_table"),
            "|---|---|---|---|---|---|---|",
        ]
        for c in sl["cells"]:
            if not c.get("ref"):
                L.append(t("board.slot_free_row", n=c["n"]))
                continue
            L.append(
                "| %d | %s | %s | %s | %s | %s | %s |"  # noqa: UP031
                % (
                    c["n"],
                    ref_cell(c) if "kind" in c else c["ref"],
                    _cell(clip(c.get("title"), 70)),
                    _cell(clip(train_cell(c), 40)),
                    _cell(clip(track_cell(c), 40)),
                    age_ru(c["in_slot"], now) if c.get("in_slot") else "—",
                    _cell(gate_cell(c)) if "kind" in c else "—",
                )
            )
        L.append("")
        groups = [(s, [e for e in sl["next"] if e["state"] == s]) for s in NEXT_ORDER]
        parts = []
        for s, es in groups:
            if not es:
                continue
            shown = [
                "{}{}".format(e["ref"], (" ({})".format(clip(e["why"], 60))) if e.get("why") else "") for e in es[:per]
            ]
            parts.append(
                "{} — {}{}".format(
                    NEXT_RU[s], "; ".join(shown), (" " + t("board.more", n=len(es) - per)) if len(es) > per else ""
                )
            )
        L.append(t("board.next", parts=". ".join(parts)) if parts else t("board.next_none"))
        return L

    def counts(self, all_=False):
        return stages.counts(it["stage"] for it in self.items(all_=all_, with_gates=False))

    # --- the columns ---------------------------------------------------------------------------------------

    def columns(self, all_=False, view="all"):
        if view not in VIEWS:
            raise ValueError("--view: {}".format(", ".join(VIEWS)))
        items = self.items(all_=all_)
        lim = gates.limits(self.stk)
        keys = STACK_VIEW if view == "stack" else [s for s in stages.STAGES if all_ or s not in stages.TERMINAL]
        cols = []
        for s in keys:
            its = [it for it in items if it["stage"] == s]
            if view == "stack" and s != "queued":  # the stack's own: the slots and the post-stack (a parked PR is
                its = [it for it in its if it["object"]]  # its track's)
            cols.append(
                {
                    "stage": s,
                    "ru": stages.ru(s),
                    "means": stages.STAGE_MEANS[s],
                    "count": len(its),
                    "limit": lim.get(s),
                    "items": its,
                }
            )
        return cols

    def to_json(self, all_=False, view="all"):
        cols = self.columns(all_=all_, view=view)
        out = {
            "v": V,
            "view": view,
            "generated": now_iso(),
            "all": bool(all_),
            "columns": cols,
            "summary": {c["stage"]: c["count"] for c in cols},
            "slots": self.slots([it for c in cols for it in c["items"]]),
        }
        if view == "stack":
            q = self.stk.queue()
            by_obj = {it["object"]: it for it in self.items(all_=False) if it.get("object")}
            out["queue"] = {
                "count": len(q),
                "max": self.stk.queue_max,
                "items": [by_obj.get(o["id"]) for o in q if by_obj.get(o["id"])],
            }
        return out

    def md(self, all_=False, view="all", limit=15, now=None, heading=True):
        if view == "stack":
            return self.stk.md(heading=heading, now=now)
        cols = self.columns(all_=all_, view=view)
        L = [t("board.heading"), ""] if heading else []
        L += self.slots_md(now=now)
        L.append("")
        L.append(t("board.stages_table"))
        L.append("|---|---|---|---|")
        for c in cols:
            longest = min((it for it in c["items"] if it.get("since")), key=lambda it: it["since"], default=None)
            L.append(
                "| %s | %d | %s | %s |"  # noqa: UP031
                % (
                    c["ru"],
                    c["count"],
                    limit_cell(c["limit"]),
                    ("{} ({})".format(longest["ref"], age_ru(longest["since"], now))) if longest else "—",
                )
            )
        L.append("")
        L.append(legend())
        for c in cols:
            L.append("")
            L.append(
                "### %s — %d%s"  # noqa: UP031
                % (
                    c["ru"][0].upper() + c["ru"][1:],
                    c["count"],
                    (" · " + limit_cell(c["limit"])) if c["limit"] else "",
                )
            )
            L.append("")
            if not c["items"]:
                L.append(t("board.empty"))
                continue
            L.append(t("board.cars_table"))
            L.append("|---|---|---|---|---|---|---|---|---|")
            shown = c["items"] if not limit else c["items"][:limit]
            for it in shown:
                L.append(
                    "| {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                        ref_cell(it),
                        "P%d" % it["priority"] if it.get("priority") else "—",  # noqa: UP031
                        _cell(clip(it["title"], 90)),
                        _cell(clip(train_cell(it), 40)),
                        _cell(clip(track_cell(it), 40)),
                        it["size"] or "—",
                        where_cell(it),
                        age_ru(it["since"], now) if it.get("since") else "—",
                        _cell(gate_cell(it)),
                    )
                )
            if len(shown) < len(c["items"]):
                L.append("")
                L.append(t("board.more_limit", n=len(c["items"]) - len(shown)))
        return "\n".join(L) + "\n"


NEXT_ORDER = ("now", "next", "gated", "owner")
NEXT_RU = i18n.Labels("board.next_state", NEXT_ORDER)


def clip(text, n):
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[: n - 1] + "…"


def limit_cell(lim):
    if not lim:
        return "—"
    if lim["over"]:
        return t("board.limit_over", what=lim["what"], used=lim["used"], max=lim["max"])
    if lim["used"] >= lim["max"]:
        return t("board.limit_full", what=lim["what"], used=lim["used"], max=lim["max"])
    return "%s %d/%d" % (lim["what"], lim["used"], lim["max"])  # noqa: UP031


def ref_cell(it):
    if it["kind"] == "car" and it.get("object"):
        out = "{} ({})".format(it["ref"], it["object"])
    elif it["kind"] == "merge":
        out = "{} ({})".format(it["ref"], it["merge"])
    else:
        out = it["ref"]
    rep = it.get("repair")
    marks = [
        m
        for m, on in (
            ((t("board.mark_repair_of", of=rep["of"]) if rep["of"] else t("board.mark_repair")) if rep else "", rep),
            (t("board.mark_returns", n=it.get("returns", 0)), it.get("returns")),
            (t("board.mark_claims", n=it.get("claims", 0)), it.get("claims")),
        )
        if on
    ]
    return out + ((" · " + " · ".join(marks)) if marks else "")


def train_cell(it):
    return "{} «{}»".format(it["train"], it.get("train_title") or "") if it.get("train") else "—"


def track_cell(it):
    return "{} {}".format(it["track"], it.get("track_title") or "") if it.get("track") else "—"


def where_cell(it):
    w = WHERE_RU.get(it.get("where"), "—") if it.get("where") else "—"
    if it.get("merge") and it.get("where") != "merge":
        w += ", " + t("board.where_in_merge", id=it["merge"])
    if it.get("pr"):
        w += ", PR #{}".format(it["pr"])
    return w


def gate_cell(it):
    g = it.get("gate")
    out = ""
    if g:
        out = gates.gate_text(g, t("board.gate_passed", name=g["name"]), "{}: ".format(g["name"]))
    if it.get("forced"):
        out = (out + " · " if out else "") + "⚑ --force"
    return out or "—"


def summary_line(state, cfg):
    """One line for `stack --md`: the whole board by its columns."""
    c = Board(state, cfg).counts()
    return t("board.summary", counts=stages.counts_cell(c))
