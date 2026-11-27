"""The gates between a car's stages, and the columns' limits.

DoR, the definition of ready: the way into the build from a planning stage (bogie, sketch, project):
  brief      the brief is there (the record's `brief`, and the file exists)
  questions  no open question (`car ask`, `car answer`)
  after      the car's `after` is met (a stack object that is no car: its `depends_on`)
  project    the stage «project» passed where it is required: size L, or `car needs-project`
  checks     the brief has the sections of the three checks (`gates.checks_since`: only for cars added from that day)
DoD, the definition of done: the way into the acceptance:
  ci, heavy, scan, shots   what the run handed in (`stack post --ci --heavy --scan --shots`; «n/a» where not needed)
  principles               the car's marks by the principles (marks.py): priority 1 are gates
  docs                     a car whose docs are needed goes through the stage «docs» first
The columns' limits: the build's slots (`stack.slots`), the acceptance's post-stack places (`stack.post_max`).
The gates are soft: a gate not passed refuses the move and says why; `--force --why «…»` passes it, recorded.
"""

import os
import re

from . import i18n, stages
from .i18n import is_word, rx, t
from .store import now_iso, read_text

DOR = ("brief", "questions", "after", "project", "checks")
DOD = ("ci", "heavy", "scan", "shots", "principles", "docs")
ITEM_RU = i18n.Labels("gates.item", DOR + DOD)
# The sections of the three checks in a brief (their shown names: gates.section.<code>), read in every language.
CHECK_SECTIONS = (("code", re.compile(r"(?i)" + rx("in.gates.section_code"))),
                  ("principles", re.compile(r"(?i)" + rx("in.gates.section_principles"))))
RE_SECTION = re.compile(r"^\s{0,3}(?:#{1,6}\s|\*\*)")
# A sketch's line «Docs: needed» / «Docs: not needed» (group 1: the «not»), in every language.
RE_DOCS = re.compile(r"(?im)^\s{0,3}(?:[-*+]\s+|#{1,6}\s+)?(?:\*\*)?" + rx("in.gates.docs") +
                     r"(?:\*\*)?\s*:\s*(?:\*\*)?\s*(" + rx("in.gates.docs_not") + r")?" + rx("in.gates.docs_needed") +
                     r"(?!\w)")
CHECKS_SINCE = None  # the rule's day: `gates.checks_since` in the config (null: every car)
GATE_RU = i18n.Labels("gates.gate", ("DoR", "DoD", "limit", "role", "window", "agent"))


def checks_old_note():
    """The DoR's quiet note on a car older than the rule (dor_notes)."""
    return t("gates.checks_old_note")


class GateError(ValueError):
    pass


def _norm(text):
    return " ".join(str(text or "").strip().lower().split())


def verdict(text):
    """'ok', 'na' (not needed), 'bad', or 'other' (shown as it is, not passed); None: nothing handed in. What the agent
    handed in, as words of every language: in.gates.ok, in.gates.na, in.gates.bad (the whole text or its first word)."""
    w = _norm(text)
    if not w:
        return None
    if is_word(w, "in.gates.na"):
        return "na"
    first = w.split()[0].strip(",.:;()")
    if is_word(w, "in.gates.ok") or is_word(first, "in.gates.ok"):
        return "ok"
    if is_word(w, "in.gates.bad") or is_word(first, "in.gates.bad"):
        return "bad"
    return "other"


def handed(text):
    """`--heavy|--scan|--shots WORDS` -> {"state": the words, "ts": now}."""
    words = " ".join(str(text or "").split())
    if not words:
        raise GateError(t("gates.need_word"))
    return {"state": words, "ts": now_iso()}


def item_state(obj, item):
    """The words handed in for one DoD item (the CI: the stack's `ci` record), or None."""
    if item == "ci":
        return (obj.get("ci") or {}).get("state")
    return ((obj.get("dod") or {}).get(item) or {}).get("state")


def dod_items(cfg):
    items = (cfg.get("gates") or {}).get("dod")
    return tuple(DOD if items is None else items)


def dod_unmet(cfg, obj, principles=None, docs=None):
    out = []
    for item in dod_items(cfg):
        if item == "principles":
            if principles is None:
                out.append(t("gates.cell_principles", cell=t("gates.unchecked")))
            elif principles["unmet"]:
                out.append(t("gates.principles_gates", unmet="; ".join(principles["unmet"])))
            continue
        if item == "docs":
            if docs and docs["need"] and not docs["done"]:
                out.append(t("gates.docs_not_done", need=docs["need"], cmd=docs["cmd"]))
            continue
        s = item_state(obj, item)
        v = "ok" if item == "ci" and i18n.matches(s, "stack.ci.green") else verdict(s)
        if v in ("ok", "na"):
            continue
        out.append("{}: {}".format(ITEM_RU.get(item, item), t("gates.not_handed") if v is None else s))
    return out


def dod_cells(cfg, obj, principles=None, docs=None):
    out = []
    for i in dod_items(cfg):
        if i == "principles":
            out.append(t("gates.cell_principles", cell=principles["cell"] if principles else t("gates.unchecked")))
        elif i == "docs":
            if docs and docs["need"]:
                out.append(t("gates.cell_docs", word=docs_word(docs)))
        else:
            out.append("{} {}".format(ITEM_RU.get(i, i), item_state(obj, i) or t("gates.not_handed")))
    return " · ".join(out)


def brief_docs(path):
    m = RE_DOCS.search(read_text(os.path.expanduser(path)) or "") if path else None
    return None if m is None else not m.group(1)


def docs_need(recs):
    """Why the car's docs are needed, or None: its flag `docs` (`tracks car set --docs yes|no`, the car's first, then
    its object's) wins; with none set, its sketch's line, read as it is now."""
    for r in recs:
        if r.get("docs") is not None:
            return t("gates.docs_flag") if r["docs"] else None
    brief = next((r.get("brief") for r in recs if r.get("brief")), None)
    return t("gates.docs_sketch") if brief and os.path.isfile(os.path.expanduser(brief)) and \
        brief_docs(brief) else None


DOCS_WORD = i18n.Labels("gates.docs_word", ("ahead", "now", "passed", "skipped"))


def docs_state(recs, stage, ref=None):
    if stage == "docs":
        state = "now"
    elif stage in stages.PLANNING + ("build",):
        state = "ahead"
    else:
        state = "passed" if any(row.get("stage") == "docs" for r in recs for row in r.get("stages") or []) else "skipped"
    return {"need": docs_need(recs), "state": state, "word": DOCS_WORD[state], "done": state == "now",
            "cmd": "depot2 car stage %s docs" % (ref or "…")}


def docs_word(d):
    return d["word"]


def project_need(recs):
    for r in recs:
        need = r.get("needs_project")
        if need:
            return need.get("why") if isinstance(need, dict) else str(need)
    for r in recs:
        if str(r.get("size") or "").upper() == "L":
            return t("gates.size_l")
    return None


def project_passed(recs):
    return any(row.get("stage") == "project" for r in recs for row in r.get("stages") or [])


def open_questions(recs):
    return [q for r in recs for q in r.get("asks") or [] if not q.get("answered")]


def checks_missing(path):
    heads = [ln for ln in (read_text(path) or "").splitlines() if RE_SECTION.match(ln)]
    return [t("gates.section." + code) for code, rx_ in CHECK_SECTIONS if not any(rx_.search(h) for h in heads)]


def checks_since(cfg):
    """The day the DoR's `checks` starts to count from (`gates.checks_since`, YYYY-MM-DD), or None: every car."""
    day = (cfg.get("gates") or {}).get("checks_since") or CHECKS_SINCE
    return str(day)[:10] if day else None


def added_day(car=None, obj=None):
    """The day a car came in: the track's car record's `added`, else (a stack object that is no car) the
    object's own; None when the record has none."""
    rec = car if car is not None else obj
    return str((rec or {}).get("added") or "")[:10] or None


def checks_old(cfg, car=None, obj=None):
    """The car (or the stack object that is no car) came in before the rule: the `checks` item does not count.
    A record without a date counts as old (it predates the dates). No rule day (null): no car is old."""
    since = checks_since(cfg)
    if since is None:
        return False
    day = added_day(car, obj)
    return day is None or day < since


def _checks_unmet(items, brief):
    path = os.path.expanduser(brief) if brief else None
    if path and os.path.isfile(path):
        missing = checks_missing(path)
        if missing:
            return [t("gates.checks_missing", sections=", ".join(f"«{m}»" for m in missing))]
    elif "brief" not in items:
        return [t("gates.checks_no_brief")]
    return []


def dor_notes(stack, obj=None, car=None, items=None):
    """The DoR's quiet notes, not counted: a car older than the rule whose brief lacks the three checks'
    sections (or has no brief to check)."""
    items = dor_items(stack.cfg) if items is None else tuple(items)
    if "checks" not in items or not checks_old(stack.cfg, car=car, obj=obj):
        return []
    brief = next((r.get("brief") for r in (car, obj) if r is not None and r.get("brief")), None)
    return [checks_old_note()] if _checks_unmet(items, brief) else []


def dor_items(cfg):
    items = (cfg.get("gates") or {}).get("dor")
    return tuple(DOR if items is None else items)


def dor_unmet(stack, tracks, obj=None, car=None, track=None, items=None):
    items = dor_items(stack.cfg) if items is None else tuple(items)
    after = "after" in items
    recs = [r for r in (car, obj) if r is not None]
    out = []
    brief = next((r.get("brief") for r in recs if r.get("brief")), None)
    if "brief" not in items:
        pass
    elif not brief:
        out.append(t("gates.no_brief"))
    elif not os.path.isfile(os.path.expanduser(brief)):
        out.append(t("gates.brief_not_found", path=brief))
    qs = open_questions(recs) if "questions" in items else []
    if qs:
        out.append(t("gates.open_questions", n=len(qs), list="; ".join("%d. %s" % (q["n"], q["q"]) for q in qs[:3])  # noqa: UP031
                     + (" …" if len(qs) > 3 else "")))
    if after and car is not None and tracks is not None and track is not None:
        b = tracks.car_blockers(track, car, lock=False)
        if b:
            out.append(t("gates.after_unmet", blockers=", ".join(b)))
    elif after and obj is not None:
        deps = [d for d in obj.get("depends_on") or [] if stack.dep_waiting(d)]
        if deps:
            out.append("{} depends on {}, not done yet".format(obj["id"], ", ".join(deps)))
    need = project_need(recs) if "project" in items else None
    if need and not project_passed(recs):
        out.append(t("gates.project_required", need=need))
    if "checks" in items and not checks_old(stack.cfg, car=car, obj=obj):
        out += _checks_unmet(items, brief)
    return out


def slots_unmet(stack):
    used, cap = len(stack.in_slots()), stack.slots_n
    return ["no free slot (%d/%d), stack.slots" % (used, cap)] if used >= cap else []  # noqa: UP031


def post_unmet(stack, oid, where):
    used, cap = len(stack.posted()), stack.post_max
    key = "gates.post_full_slot" if where == "slot" else "gates.post_full_queue"
    return [t(key, used=used, cap=cap, oid=oid)] if used >= cap else []


def limits(stack):
    """The columns' limits as the board shows them: {stage: {what, key, used, max, over}}."""
    out = {}
    for stage, what, key, used, cap in (("build", t("gates.limit_slots"), "stack.slots", len(stack.in_slots()),
                                         stack.slots_n),
                                        ("acceptance", t("gates.limit_post"), "stack.post_max", len(stack.posted()),
                                         stack.post_max)):
        out[stage] = {"what": what, "key": key, "used": used, "max": cap, "over": used > cap}
    return out


def check(unmet, force, why, ref, target, cmd, error=GateError):
    """The soft gate: nothing missing -> None; missing and no --force -> error (what is missing, how to pass
    anyway; the stack's and the trains' own error classes); --force with a reason -> {gate: [what was missing]}
    for the history row (`gates`)."""
    unmet = dict((k, list(v)) for k, v in unmet.items() if v)
    if not unmet:
        return None
    text = "; ".join("{} — {}".format(GATE_RU.get(k, k), ", ".join(v)) for k, v in unmet.items())
    if not force:
        raise error(t("gates.refused", ref=ref, target=target, text=text, cmd=cmd))
    if not (why or "").strip():
        raise error(t("gates.force_needs_why", text=text))
    return unmet


def gate_text(g, passed, failed):
    notes = "; ".join(g.get("notes") or [])
    if not g["unmet"]:
        return passed + (f" ({notes})" if notes else "")
    return failed + "; ".join(g["unmet"]) + (" · " + notes if notes else "")


def gates_text(gates):
    return "; ".join("{} — {}".format(GATE_RU.get(k, k), ", ".join(v)) for k, v in (gates or {}).items())
