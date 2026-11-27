"""Returns and claims: `depot2 repair` and `depot2 claims`.

  defect  a car handed in and sent back to the build (from the acceptance, or dropped from the merge queue): the
          same car, `returns` += 1 on its record and `return: true` on its history row;
  claim   a defect found in production after the car arrived: a new repair car (`repair add --of <culprit>`),
          optionally linked to a bug's id in the bug table (`repair.bugs`, a Markdown table the tool only reads);
  repair  the repair car itself: a free slot takes it out of turn (`trains next`).
"""

import os
import re

from . import gates, i18n, stages
from .i18n import t
from .store import now_iso

V = 1
ON_PROD = ("rolling", "arrived")
RE_BUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.\-]*$")
RE_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
WHAT = i18n.Labels("claims.what", ("car", "culprit", "repair", "old_culprit"))  # what a ref names, in resolve()'s error


class ClaimError(ValueError):
    pass


def _text(s):
    return " ".join(str(s or "").split())


def clip(text, n):
    t = _text(text)
    return t if len(t) <= n else t[:n - 1] + "…"



def bugs_path(cfg):
    return os.path.expanduser((cfg.get("repair") or {}).get("bugs") or "")


def read_bugs(cfg):
    path, out = bugs_path(cfg), {}
    if not path or not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8") as f:
        for line in f:
            if not line.startswith("|"):
                continue
            cells = [c.strip() for c in line.strip().strip("|").split("|")] + [""] * 5
            if cells[0] != "id" and RE_BUG.match(cells[0]):
                out.setdefault(cells[0], dict(zip(("id", "who", "what", "where", "status"), cells, strict=False)))
    return out


# --- a car by its ref, and where its fields live -----------------------------------------------------------------

def is_car_ref(text):
    from .tracks import RE_CAR, RE_PR
    t = str(text or "").strip()
    return bool(re.match(r"^st-\d{4}$", t) or RE_CAR.match(t) or RE_PR.match(t))


def ref_of_pr(stk, tr, n):
    """The car a PR belongs to: a track's car, a live stack object, one that left, a merge queue entry's."""
    for trk in tr.data["tracks"]:
        for c in trk["cars"]:
            if str(c.get("pr")) == str(n):
                return "{}:{}".format(trk["id"], c["n"])
    for o in stk.objects():
        if str(o.get("pr")) == str(n):
            return o["id"]
    found = [h["object"] for h in stk.history() if h.get("object") and str((h.get("obj") or {}).get("pr")) == str(n)]
    if found:
        return found[-1]
    from .merge import MergeQueue
    for e in reversed(MergeQueue(stk.state).entries()):
        if str(e["pr"]) == str(n) and (e.get("car") or e.get("st")):
            return e.get("car") or e["st"]
    raise ClaimError(t("claims.no_pr", n=n))


def resolve(state, cfg, text, stk, tr, what="car"):
    """A stages.Car for st-NNNN, tr-NNNN:N or #PR (the stack and the tracks shared: one write of each file)."""
    from .tracks import RE_PR
    ref = str(text or "").strip()
    m = RE_PR.match(ref)
    if m:
        ref = ref_of_pr(stk, tr, int(m.group(1)))
    elif not is_car_ref(ref):
        raise ClaimError(t("claims.bad_ref", what=WHAT[what], text=repr(text)))
    try:
        return stages.Car(state, cfg, ref, stack=stk, tracks=tr)
    except ValueError as e:
        raise ClaimError(str(e))


def canon(car):
    """A car's one name: a track's car by its ref (its object is the car), else the object's id."""
    return car.car_ref or car.ref


def where(car):
    """(kind, record) for a car's own fields: car | stack | left; (None, None) for a wish or a to-think entry."""
    if car.car is not None:
        return "car", car.car
    if car.obj is not None:
        return "stack", car.obj
    if car.left is not None:
        return "left", dict(car.left)
    return None, None


def save(car, kind, rec, event, **fields):
    if kind == "car":
        car.tracks._save(event, car.track["id"], car=car.car["n"], **fields)
    elif kind == "stack":
        car.stack._save(event, car.obj["id"], **fields)
    else:  # the object that left: a row with the object; its stage written, as every view reads it now
        rec.setdefault("stage", car.stage())
        car.stack._save(event, car.ref, obj=rec, left=True, **fields)
        car.left = rec


def active(rec):
    return [x for x in rec.get("claim_links") or [] if not x.get("moved")]


def add_claim(car, repair, bug, why, by, forced=None):
    kind, rec = where(car)
    links = list(rec.get("claim_links") or [])
    link = {"n": max([x["n"] for x in links] or [0]) + 1, "repair": repair, "bug": bug, "at": now_iso(), "by": by,
            "why": _text(why) or None}
    if forced:
        link["forced"] = forced
    rec["claim_links"] = links + [link]
    rec["claims"] = len(active(rec))
    save(car, kind, rec, "claim", claim=link["n"], repair=repair, bug=bug, why=link["why"], by=by)
    return link


def _on_prod(car, ref):
    s = car.stage()
    if s in ON_PROD:
        return []
    return [t("claims.not_on_prod", ref=ref, stage=stages.ru(s))]


# --- repair add, repair link -------------------------------------------------------------------------------------

def add(state, cfg, of, bug=None, title=None, brief=None, why=None, size=None, by="repair add", force=False):
    """A claim: a new repair car, out of turn. of: the culprit (st-, tr-…:N, #PR) or, when it is not known, the
    bug's id in BUGS.md. Soft checks (the culprit on production, the bug in BUGS.md): --force with --why passes
    them, recorded. Returns (the repair object, the culprit's ref or None, the claim or None, lines)."""
    from .stack import Stack
    from .tracks import Tracks
    stk = Stack(state, cfg)
    tr = Tracks(state, cfg)
    tr._stack = stk
    of = str(of or "").strip()
    culprit = None
    if is_car_ref(of):
        culprit = resolve(state, cfg, of, stk, tr, what="culprit")
    elif bug:
        raise ClaimError(t("claims.of_and_bug", of=of, bug=bug))
    elif of.startswith(("wl-", "th-")):
        raise ClaimError(t("claims.not_rolled", of=of))
    else:
        bug = of
    if not (culprit or bug):
        raise ClaimError(t("claims.of_needed"))
    ref = canon(culprit) if culprit else None
    unmet = _on_prod(culprit, ref) if culprit else []
    bugs = read_bugs(cfg)
    if bug and bug not in bugs:
        unmet.append(t("claims.no_bug", bug=bug, path=bugs_path(cfg)) if os.path.isfile(bugs_path(cfg)) else
                     t("claims.no_bugs_file", path=bugs_path(cfg)))
    # the gate's name is a shown word (it is kept in the forced history row as it is shown, in the run's language)
    forced = gates.check({t("claims.gate"): unmet}, force, why, ref or bug, t("claims.target"),
                         f"depot2 repair add --of {of}", error=ClaimError)
    if not _text(title):
        what = bugs.get(bug, {}).get("what") if bug else None
        title = ((t("claims.title_bug_what", bug=bug, what=clip(what, 80)) if what else t("claims.title_bug", bug=bug))
                 if not culprit else t("claims.title_car", ref=ref, title=clip(culprit.title(), 80)))
    obj = stk.add(title, brief=brief, at=1, autostart=True, stage="sketch", check_queue=False, size=size, by=by,
                  why=why, extra={"kind": "repair", "repairs": ref, "bug": bug}, forced=forced)
    claim = add_claim(culprit, obj["id"], bug, why or title, by, forced) if culprit else None
    key = "claims.added" + ("_ref" if ref else "_noref") + ("" if brief else "_no_brief")
    L = [t(key, id=obj["id"], ref=ref, n=len(stk.queue()), max=stk.queue_max)]
    if claim:
        L.append(t("claims.claim_added", ref=ref, n=claim["n"], repair=obj["id"], claims=where(culprit)[1]["claims"]))
    else:
        L.append(t("claims.culprit_later", id=obj["id"]))
    if bug:
        L.append(t("claims.bugs_md", bug=bug, id=obj["id"]))
    if forced:
        L.append(t("claims.forced", id=obj["id"], why=why, gates=gates.gates_text(forced)))
    return obj, ref, claim, L


def link(state, cfg, repair, of, why=None, by="repair link", force=False):
    """The culprit of a repair, found later; or a defect repaired inside another car (not a repair car: --why says
    what the defect was). The culprit's claims += 1. A repair car that has a culprit already moves to another
    only with --force --why (the old culprit's claim is kept, marked `moved`). Returns (claim, lines)."""
    from .stack import Stack
    from .tracks import Tracks
    stk = Stack(state, cfg)
    tr = Tracks(state, cfg)
    tr._stack = stk
    rcar = resolve(state, cfg, repair, stk, tr, what="repair")
    xcar = resolve(state, cfg, of, stk, tr, what="culprit")
    rref, xref = canon(rcar), canon(xcar)
    if rref == xref:
        raise ClaimError(t("claims.self_repair", ref=rref))
    rkind, rrec = where(rcar)
    is_repair = rrec.get("kind") == "repair"
    old = rrec.get("repairs") if is_repair else None
    if old == xref:
        raise ClaimError(t("claims.already", repair=rref, of=xref))
    if not is_repair and not _text(why):
        raise ClaimError(t("claims.not_repair_car", ref=rref))
    what = _text(why) or rrec.get("title")
    _, xrec = where(xcar)
    if any(x.get("repair") == rref and _text(x.get("why")) == what for x in active(xrec)):
        raise ClaimError(t("claims.dup", of=xref, repair=rref))
    unmet = _on_prod(xcar, xref)
    if old:
        unmet.append(t("claims.move_unmet", repair=rref, old=old, of=xref))
    forced = gates.check({t("claims.gate"): unmet}, force, why, xref, t("claims.target"),
                         f"depot2 repair link {repair} --of {of}", error=ClaimError)
    L = []
    if old:
        ocar = resolve(state, cfg, old, stk, tr, what="old_culprit")
        okind, orec = where(ocar)
        for x in active(orec):
            if x.get("repair") == rref:
                x["moved"] = {"to": xref, "at": now_iso(), "by": by, "why": _text(why)}
        orec["claims"] = len(active(orec))
        save(ocar, okind, orec, "claim_moved", repair=rref, to=xref, why=_text(why), by=by)
        L.append(t("claims.moved", old=old, repair=rref, of=xref, claims=orec["claims"]))
    if is_repair:
        rrec["repairs"] = xref
        save(rcar, rkind, rrec, "repair_link", repairs=xref, by=by)
    claim = add_claim(xcar, rref, rrec.get("bug") if is_repair else None, what, by, forced)
    L.append(t("claims.linked" if is_repair else "claims.linked_inside", of=xref, n=claim["n"], repair=rref,
               why=claim["why"], claims=where(xcar)[1]["claims"]))
    return claim, L


# --- every car once, its returns and claims ----------------------------------------------------------------------

def universe(stk, tr):
    """Every car that can hold returns and claims, once: [{ref, kind, rec, track, objects}] — the tracks' cars (with
    every object each had), the live stack objects that are no car's, the objects that left and are no car's."""
    objs_of = {}
    for track in tr.data["tracks"]:
        for c in track["cars"]:
            if c.get("st"):
                objs_of.setdefault("{}:{}".format(track["id"], c["n"]), set()).add(c["st"])
    for h in tr.history():
        if h.get("st") and h.get("track") and h.get("car") is not None:
            objs_of.setdefault("{}:{}".format(h["track"], h["car"]), set()).add(h["st"])
    car_st = set().union(*objs_of.values()) if objs_of else set()
    out = []
    for track in tr.data["tracks"]:
        for c in track["cars"]:
            ref = "{}:{}".format(track["id"], c["n"])
            out.append({"ref": ref, "kind": "car", "rec": c, "track": track, "objects": sorted(objs_of.get(ref, ()))})
    live = set()
    for o in stk.objects():
        live.add(o["id"])
        if o["id"] not in car_st:
            out.append({"ref": o["id"], "kind": "stack", "rec": o, "track": None, "objects": [o["id"]]})
    last = {}
    for h in stk.history():
        if h.get("object") and h.get("obj"):
            last[h["object"]] = h["obj"]
    for oid, o in last.items():
        if oid not in live and oid not in car_st:
            out.append({"ref": oid, "kind": "left", "rec": o, "track": None, "objects": [oid]})
    return out


def pre_stage_returns(stk):
    """{object: [row]}: the stack's rows back from the post-stack (`start` with `unposted`) that moved no stage —
    the returns from before the stages."""
    out = {}
    for h in stk.history():
        if h.get("event") == "start" and h.get("unposted") and not h.get("stage"):
            out.setdefault(h["object"], []).append(h)
    return out


def return_events(rec, pre=()):
    """A car's returns to the build, oldest first: its history's return rows and the stack's rows before the stages."""
    ev = [{"at": r.get("at"), "from": r.get("from"), "by": r.get("by"), "why": r.get("why")}
          for r in rec.get("stages") or [] if r.get("return") or stages.is_return(r.get("from"), r.get("stage"))]
    ev += [{"at": h.get("ts"), "from": "acceptance", "by": t("claims.by_pre_stage"), "why": h.get("reason"),
            "object": h.get("object")} for h in pre]
    return sorted(ev, key=lambda e: e["at"] or "")


def events(stk, tr):
    """[(unit, returns, claims)] for every car, all time."""
    pre = pre_stage_returns(stk)
    return [(u, return_events(u["rec"], [h for st in u["objects"] for h in pre.get(st, [])]), active(u["rec"]))
            for u in universe(stk, tr)]


def migrate(state, cfg, stk, tr, dry_run=False):
    """`queues migrate`: every car's `returns` = its returns in the history (the ones before the stages too), written
    where it differs (absent reads 0); a second run changes nothing. Returns (lines, changed)."""
    fix = {"car": {}, "stack": {}, "left": {}}
    for u, ret, _ in events(stk, tr):
        if len(ret) != int(u["rec"].get("returns") or 0):
            fix[u["kind"]][u["ref"]] = (u, len(ret))
    for (u, n) in fix["car"].values():
        u["rec"]["returns"] = n
    for (u, n) in fix["stack"].values():
        u["rec"]["returns"] = n
    if not dry_run:
        if fix["car"]:
            tr._save("migrate_returns", None, cars={r: n for r, (u, n) in fix["car"].items()})
        if fix["stack"]:
            stk._save("migrate_returns", None, objects={r: n for r, (u, n) in fix["stack"].items()})
        for oid, (u, n) in sorted(fix["left"].items()):
            obj = dict(u["rec"], returns=n)
            obj.setdefault("stage", stk.left_stage(oid))
            stk._save("migrate_returns", oid, obj=obj, left=True, returns=n)
    n = sum(len(v) for v in fix.values())
    total = sum(x for v in fix.values() for (_, x) in v.values())
    return [t("claims.migrated", n=n, total=total) + (
        (": " + ", ".join("%s %d" % (r, x) for v in fix.values() for r, (_, x) in sorted(v.items()))) if n else "")], n  # noqa: UP031


# --- the views: car show, the report -----------------------------------------------------------------------------

def fixes_of(car):
    """The claims, on other cars, whose repair this car is: [{of, n, at, why, bug}]."""
    me = canon(car)
    return [{"of": u["ref"], "n": x["n"], "at": x["at"], "why": x.get("why"), "bug": x.get("bug")}
            for u in universe(car.stack, car.tracks) for x in active(u["rec"]) if x.get("repair") == me]


def objects_of(car):
    """The stack objects a car is or was (a track's car: its live one and every one its history names)."""
    if car.car is None:
        return [car.ref] if car.ref.startswith("st-") else []
    ref = canon(car)
    out = {car.car.get("st")} | {h["st"] for h in car.tracks.history()
                                 if h.get("st") and "{}:{}".format(h.get("track"), h.get("car")) == ref}
    return sorted(x for x in out if x)


def car_returns(car, rec):
    pre = pre_stage_returns(car.stack)
    return return_events(rec, [h for st in objects_of(car) for h in pre.get(st, [])])


def link_text(x, n=None):
    tail = [b for b in (t("claims.tail_repair", id=x["repair"]) if x.get("repair") else None,
                        t("claims.tail_bug", id=x["bug"]) if x.get("bug") else None,
                        "--force" if x.get("forced") else None) if b]
    why = x.get("why") or "—"
    return "№%d %s: %s%s" % (x["n"], (x.get("at") or "?")[:10], clip(why, n) if n else why,  # noqa: UP031
                             (" ({})".format(", ".join(tail))) if tail else "")


def car_lines(car):
    """`car show`: its returns, its claims, what it repairs."""
    _kind, rec = where(car)
    if rec is None:
        return []
    L = []
    ev = car_returns(car, rec)
    if ev:
        L.append(t("claims.returns_line", n=len(ev), at=(ev[-1]["at"] or "?")[:10], why=ev[-1].get("why") or "—"))
    act = active(rec)
    if act:
        L.append(t("claims.claims_head", n=len(act)))
        L += ["- " + link_text(x) for x in act]
    if rec.get("kind") == "repair":
        key = "claims.repair_fixes" if rec.get("repairs") else "claims.repair_nocul"
        L.append(t(key + ("_bug" if rec.get("bug") else ""), of=rec.get("repairs"), ref=canon(car), bug=rec.get("bug")))
    fx = [f for f in fixes_of(car) if f["of"] != rec.get("repairs")]
    if fx:
        L.append(t("claims.fixes_line", list="; ".join("%s №%d" % (f["of"], f["n"]) for f in fx)))  # noqa: UP031
    return L


def car_json(car):
    kind, rec = where(car)
    rec = rec or {}
    log = car_returns(car, rec) if kind else []
    return {"returns": len(log), "returns_log": log, "claims": len(active(rec)),
            "claim_links": rec.get("claim_links") or [],
            "repair": {"of": rec.get("repairs"), "bug": rec.get("bug")} if rec.get("kind") == "repair" else None,
            "fixes": fixes_of(car) if rec else []}


def _day(text, name):
    if text and not RE_DATE.match(text):
        raise ClaimError(t("claims.bad_day", name=name, text=repr(text)))
    return text or None


def within(at, since, until):
    d = (at or "")[:10]
    return bool(d) and (not since or d >= since) and (not until or d <= until)


def report(state, cfg, since=None, until=None):
    from .stack import Stack
    from .tracks import Tracks
    since, until = _day(since, "since"), _day(until, "until")
    stk = Stack(state, cfg)
    tr = Tracks(state, cfg)
    tr._stack = stk
    bugs = read_bugs(cfg)
    cars, tracks, repairs = [], {}, []
    for u, ret, cl in events(stk, tr):
        rec = u["rec"]
        ret = [e for e in ret if within(e["at"], since, until)]
        cl = [x for x in cl if within(x["at"], since, until)]
        if rec.get("kind") == "repair" and within(rec.get("added"), since, until):
            b = bugs.get(rec.get("bug")) or {}
            repairs.append({"ref": u["ref"], "title": rec.get("title"), "stage": rec.get("stage"),
                            "stage_ru": stages.ru(rec.get("stage")), "of": rec.get("repairs"), "bug": rec.get("bug"),
                            "bug_status": b.get("status"), "added": rec.get("added")})
        if not (ret or cl):
            continue
        trk = u["track"]
        cars.append({"ref": u["ref"], "kind": u["kind"], "track": trk["id"] if trk else None,
                     "track_title": trk["title"] if trk else None, "title": rec.get("title") or "", "pr": rec.get("pr"),
                     "returns": ret, "claims": cl})
        k = trk["id"] if trk else None
        row = tracks.setdefault(k, {"id": k, "title": trk["title"] if trk else None, "returns": 0, "claims": 0,
                                    "cars": 0})
        row["returns"] += len(ret)
        row["claims"] += len(cl)
        row["cars"] += 1
    cars.sort(key=lambda c: (-len(c["claims"]), -len(c["returns"]), c["ref"]))
    tl = sorted(tracks.values(), key=lambda r: (r["id"] is None, -(r["claims"] + r["returns"]), r["id"] or ""))
    return {"v": V, "since": since, "until": until, "generated": now_iso(),
            "total": {"returns": sum(len(c["returns"]) for c in cars), "claims": sum(len(c["claims"]) for c in cars),
                      "cars": len(cars), "repairs": len(repairs),
                      "repairs_open": len([r for r in repairs if r["stage"] not in stages.TERMINAL])},
            "tracks": tl, "cars": cars, "repairs": repairs}


def _cell(text):
    return str(text).replace("|", "\\|").replace("\n", " ")


def report_md(data):
    since, until = data["since"], data["until"]
    period = (t("claims.period_all") if not (since or until) else
              t("claims.period", since=since or t("claims.since_start"), until=until or t("claims.until_today")))
    tot = data["total"]
    L = [t("claims.report_head", period=period), "", t("claims.report_means"), "",
         t("claims.report_total", returns=tot["returns"], claims=tot["claims"], cars=tot["cars"],
           repairs=tot["repairs"], open=tot["repairs_open"])]
    if data["tracks"]:
        L += ["", t("claims.by_tracks"), "", t("claims.tracks_table"), "|---|---|---|---|"]
        for r in data["tracks"]:
            L.append("| %s | %d | %d | %d |" % (_cell(clip("{} {}".format(r["id"], r["title"]), 60)) if r["id"] else  # noqa: UP031
                                               t("claims.off_tracks"), r["cars"], r["returns"], r["claims"]))
        L += ["", t("claims.cars_head"), "", t("claims.cars_table"), "|---|---|---|---|---|---|"]
        for c in data["cars"]:
            det = [t("claims.det_return", at=(e["at"] or "?")[:10], why=clip(e.get("why") or "—", 70))
                   for e in c["returns"]]
            det += [t("claims.det_claim", link=link_text(x, 70)) for x in c["claims"]]
            L.append("| %s | %s | %s | %d | %d | %s |" % (c["ref"], _cell(clip(c["title"], 60)),  # noqa: UP031
                                                          ("#{}".format(c["pr"])) if c.get("pr") else "—", len(c["returns"]),
                                                          len(c["claims"]), _cell("; ".join(det))))
    if data["repairs"]:
        L += ["", t("claims.repairs_head"), "", t("claims.repairs_table"), "|---|---|---|---|---|"]
        for r in data["repairs"]:
            L.append("| {} | {} | {} | {} | {} |".format(r["ref"], _cell(clip(r["title"], 60)), r["stage_ru"], r["of"] or "—",
                                                    _cell(("{} ({})".format(r["bug"], clip(r["bug_status"], 40))) if
                                                          r.get("bug_status") else r["bug"] or "—")))
    if not data["cars"] and not data["repairs"]:
        L += ["", t("claims.none", period=period)]
    return "\n".join(L) + "\n"
