"""The car's marks by the principles: priority 1 principles are gates, the others give the car a score.

The source: the section «Principles check» of the car's PR body (marks.section, read in every language), one line per
principle of the third check (`depot2 principles checks`):
  - 4: ok — how it is done            (the words in.marks.ok)
  - 7: n/a                            (in.marks.na: also «na»)
  - 2: violated — what; instead …     (in.marks.bad: also «bad»)
read at `stack post`, at `merge add` (when the DoD has its item `principles`) and by `tracks car check REF --from-pr`;
set by hand with `tracks car check REF N ok|n/a|bad [--line «…»]`.
"""

import re

from . import i18n
from .config import repo as config_repo
from .i18n import rx, t
from .store import now_iso

MARKS = ("ok", "na", "bad")
MARK_RU = i18n.Labels("marks.mark", MARKS)
POINTS = {"ok": 1, "na": 0, "bad": -1}
RANK = {"bad": 0, "na": 1, "ok": 2}  # a number written twice: the worse mark counts


def hand():
    """The `by` of a mark set by hand, as it is written now (the run's language)."""
    return t("marks.hand")


def is_hand(by):
    """Whether a mark's `by` is «by hand», in any language (an old depot wrote it in Russian)."""
    return by is not None and i18n.matches(by, "marks.hand")


def section():
    """The PR body's section of the marks, as depot2 names it (any language's name is read: in.marks.section)."""
    return t("marks.section")


def format_line():
    """The lines of the section, by example."""
    return t("marks.format")


def legend():
    return t("marks.legend", section=section())


RE_SECTION = re.compile(r"(?i){}".format(rx("in.marks.section")))
RE_SUBLABEL = re.compile(r"(?i){}".format(rx("in.marks.sublabel")))  # a sub-heading inside the section, not its end
RE_HEAD = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
RE_BOLD = re.compile(r"^\s{0,3}\*\*(.+?)\*\*")
RE_FENCE = re.compile(r"^\s{0,3}(```|~~~)")
RE_ITEM = re.compile(
    r"^\s*[-*+]\s+(?:{}\s+)?(\d+)((?:[a-z]|{})?)\b\s*[:.)—\u2013-]?\s*(.*)$".format(
        rx("in.marks.principle"), rx("in.marks.suffix_a")
    ),
    re.IGNORECASE,
)
RE_MARK = re.compile(
    r"^({}|{}|{})(?![\w/])\s*[—\u2013:,.;-]*\s*(.*)$".format(rx("in.marks.na"), rx("in.marks.bad"), rx("in.marks.ok")),
    re.IGNORECASE,
)


class MarksError(ValueError):
    pass


def mark_of(word):
    w = " ".join(str(word or "").lower().split()).strip(".,;:!")
    for mark in MARKS:
        if i18n.is_word(w, "in.marks." + mark):
            return mark
    return None


def norm_label(n, suffix=""):
    s = (suffix or "").lower()
    return "%d%s" % (int(n), "a" if s and i18n.is_word(s, "in.marks.suffix_a") else s)  # noqa: UP031


def _clip(text, n=120):
    s = " ".join(str(text or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# --- the PR's section ------------------------------------------------------------------------------------------


def parse(body, labels=None):
    """The section starts at a heading (or a bold label at a line's start) that names it and ends at the next heading of
    its level or higher (a bold label's: at the next heading or bold label); a heading or a bold label that names a
    a number but has no mark word is junk; any other line is ignored, and so is a code block. labels: the numbers of
    the third check; a number not among them is `unknown`. A number written twice is a repeat: the worse mark counts.
    """
    out = {"found": False, "marks": {}, "junk": [], "repeats": [], "unknown": []}
    inside, level, fence = False, 0, False
    for no, line in enumerate((body or "").replace("\r\n", "\n").split("\n"), 1):
        if RE_FENCE.match(line):
            fence = not fence
            continue
        if fence:
            continue
        h = RE_HEAD.match(line)
        b = None if h else RE_BOLD.match(line)
        if h or b:
            title = h.group(2) if h else b.group(1)
            lv = len(h.group(1)) if h else 7
            if RE_SECTION.search(title):
                inside, level, out["found"] = True, lv, True
            elif inside and not RE_SUBLABEL.search(title) and (lv <= level or level == 7):
                inside = False
            continue
        if not inside:
            continue
        m = RE_ITEM.match(line.replace("**", "").replace("`", ""))
        if not m:
            continue
        label = norm_label(m.group(1), m.group(2))
        mk = RE_MARK.match(m.group(3).strip())
        if not mk:
            out["junk"].append([no, _clip(line.strip())])
            continue
        if labels is not None and label not in labels:
            if label not in out["unknown"]:
                out["unknown"].append(label)
            continue
        mark, text = mark_of(mk.group(1)), " ".join(mk.group(2).split()) or None
        old = out["marks"].get(label)
        if old is not None:
            if label not in out["repeats"]:
                out["repeats"].append(label)
            if RANK[mark] >= RANK[old["mark"]]:
                continue
        out["marks"][label] = {"mark": mark, "text": text, "line": no}
    return out


def from_pr(old, parsed, pr, at=None):
    """The marks after a read of PR `pr`: the hand marks stay, every other mark is the read's (a number the read has
    no line for loses the mark an earlier read gave).
    """
    at = at or now_iso()
    new = {k: dict(v) for k, v in (old or {}).items() if is_hand(v.get("by"))}
    for label, m in parsed["marks"].items():
        if label not in new:
            new[label] = {"mark": m["mark"], "text": m["text"], "by": f"PR #{pr}", "at": at}
    return new


def by_hand(old, label, mark, text=None, at=None):
    new = {k: dict(v) for k, v in (old or {}).items()}
    new[label] = {"mark": mark, "text": " ".join(str(text or "").split()) or None, "by": hand(), "at": at or now_iso()}
    return new


# --- the verdict: the gates and the score ----------------------------------------------------------------------


def signed(n):
    return "+%d" % n if n > 0 else ("-%d" % -n if n < 0 else "0")  # noqa: UP031


def evaluate(marks, plist):
    """The marks against the third check (plist: `Principles.check_list()`): every principle with its mark, the gate of
    priority 1 (passed: each has a line, none is bad), the score of priority 2 and what is not checked.
    """
    marks = marks or {}
    items = []
    for p in plist or ():
        m = marks.get(p["label"]) or {}
        mark = m.get("mark") if m.get("mark") in MARKS else None
        items.append(
            dict(
                p,
                mark=mark,
                mark_ru=MARK_RU.get(mark) or t("marks.unchecked"),
                text=m.get("text"),
                by=m.get("by"),
                at=m.get("at"),
            )
        )
    p1 = [i for i in items if i["priority"] == 1]
    p2 = [i for i in items if i["priority"] != 1]
    missing = [i["label"] for i in p1 if i["mark"] is None]
    bad = [i["label"] for i in p1 if i["mark"] == "bad"]
    counts = {k: len([i for i in p2 if i["mark"] == k]) for k in MARKS}
    counts["none"] = len([i for i in p2 if i["mark"] is None])
    v = {
        "listed": bool(items),
        "read": any(i["mark"] for i in items),
        "gate": {"passed": not missing and not bad, "missing": missing, "bad": bad, "of": [i["label"] for i in p1]},
        "score": counts["ok"] - counts["bad"],
        "max": len(p2),
        "counts": counts,
        "unchecked": [i["label"] for i in p2 if i["mark"] is None],
        "items": items,
        "hand": [i["label"] for i in items if is_hand(i["by"])],
        "sources": sorted({i["by"] for i in items if i["by"] and not is_hand(i["by"])}),
    }
    v["passed"] = v["gate"]["passed"]
    v["unmet"] = [] if v["passed"] else ["; ".join(gate_parts(v))]
    v["text"] = text(v)
    v["cell"] = cell(v)
    return v


def gate_parts(v):
    g = v["gate"]
    return ([t("marks.gate_missing", labels=", ".join(g["missing"]))] if g["missing"] else []) + (
        [t("marks.gate_bad", labels=", ".join(g["bad"]))] if g["bad"] else []
    )


def gate_ru(v):
    return t("marks.gate_passed") if v["gate"]["passed"] else t("marks.gate_failed", parts="; ".join(gate_parts(v)))


def score_ru(v, full=True, bare=False):
    """The score: «score +2 of 5 (…)»; bare: without its first word (after a label that says it)."""
    s = t("marks.score_bare" if bare else "marks.score", score=signed(v["score"]), max=v["max"])
    c = v["counts"]
    if full and v["max"]:
        s += t("marks.score_counts", ok=c["ok"], na=c["na"], bad=c["bad"])
    if v["unchecked"]:
        s += (
            t("marks.score_unchecked", labels=", ".join(v["unchecked"]))
            if full
            else t("marks.score_unchecked_n", n=len(v["unchecked"]))
        )
    return s


def text(v):
    if not v["listed"]:
        return t("marks.text_not_needed")
    if not v["read"]:
        return t("marks.text_not_read", section=section())
    return f"{gate_ru(v)} · {score_ru(v)}"


def cell(v):
    if not v["listed"]:
        return t("marks.cell_not_needed")
    if not v["read"]:
        return t("marks.unchecked")
    return f"{gate_ru(v)}, {score_ru(v, full=False)}"


def compact(v):
    """The verdict without its items, for a car's row, a slot and a board card (None: nothing to check)."""
    if v is None or not v["listed"]:
        return None
    return {
        "read": v["read"],
        "passed": v["gate"]["passed"],
        "missing": v["gate"]["missing"],
        "bad": v["gate"]["bad"],
        "score": v["score"],
        "max": v["max"],
        "counts": v["counts"],
        "unchecked": v["unchecked"],
        "cell": v["cell"],
    }


def md(ref, v, heading=True):
    """`tracks car check REF`: the verdict, then every principle with its mark, its line and where it came from."""
    L = [t("marks.md_heading", ref=ref), ""] if heading else []
    if not v["listed"]:
        L.append(t("marks.md_empty"))
        return "\n".join(L) + "\n"
    L.append(
        t("marks.md_gates_passed")
        if v["gate"]["passed"]
        else t("marks.md_gates_failed", parts="; ".join(gate_parts(v)))
    )
    L.append(t("marks.md_score", score=score_ru(v, bare=True)))
    src = list(v["sources"]) + ([t("marks.md_hand", labels=", ".join(v["hand"]))] if v["hand"] else [])
    L.append(t("marks.md_sources", sources="; ".join(src) if src else t("marks.md_no_marks")))
    L.append("")
    L.append(t("marks.md_header"))
    L.append("|---|---|---|---|---|")
    for i in v["items"]:
        L.append(
            "| {}. {} | {} | {} | {} | {} |".format(
                i["label"],
                _cell(i["title"]),
                t("marks.md_priority_1") if i["priority"] == 1 else t("marks.md_priority_2"),
                i["mark_ru"],
                _cell(i["text"] or "—"),
                _cell(i["by"] or "—"),
            )
        )
    L.append("")
    L.append(legend())
    return "\n".join(L) + "\n"


def _cell(x):
    return " ".join(str(x or "—").split()).replace("|", "/")


def lines(v, title=None):
    """`car show`: the verdict and the principles with a mark or a gate's missing line, one line each."""
    L = ["{}: {}.".format(title or t("marks.title"), v["text"])]
    if v["read"]:
        for i in v["items"]:
            if i["mark"] or i["priority"] == 1:
                L.append(
                    "- {} ({}) {} — {}{}{}".format(
                        i["label"],
                        t("marks.kind_gate") if i["priority"] == 1 else t("marks.kind_score"),
                        _clip(i["title"], 50),
                        i["mark_ru"],
                        ": " + _clip(i["text"], 100) if i["text"] else "",
                        " ({})".format(i["by"]) if i["by"] else "",
                    )
                )
    return L


# --- where the marks live, the reading and the saving ----------------------------------------------------------


def plist(state, cfg=None):
    from .principles import Principles

    return Principles(state, cfg).check_list()


def wanted(cfg):
    """The DoD checks the principles (`gates.dod` has `principles`): `stack post` and `merge add` read the PR."""
    from .gates import dod_items

    return "principles" in dod_items(cfg)


def holder(state, cfg, ref):
    """(the stages.Car, the record whose `marks` these are): the track's car, else the stack object."""
    from . import stages
    from .stack import StackError
    from .tracks import TrackError

    try:
        car = stages.Car(state, cfg, ref)
    except (stages.StageError, TrackError, StackError) as e:
        raise MarksError(str(e))  # noqa: B904
    rec = car.car if car.car is not None else car.obj
    if rec is None:
        raise MarksError(t("marks.holder_none", ref=ref))
    return car, rec


def of(rec, pl):
    """The verdict on a record's marks (a track's car, a stack object)."""
    return evaluate((rec or {}).get("marks"), pl)


def pr_body(state, cfg, n):
    """The PR's body (`gh pr view N --json body`), else MarksError."""
    from .merge import MergeError, Rider

    try:
        d = Rider(state, cfg).gh_json("pr", "view", str(n), "-R", config_repo(cfg), "--json", "body")
    except MergeError as e:
        raise MarksError(t("marks.pr_unread", n=n, error=e))  # noqa: B904
    return str((d or {}).get("body") or "")


class Reading:
    """One command's reading of a car's PR: the marks it has, the PR's section read into them (saved by `save`), and
    the verdict. ref: the car (tr-NNNN:N) or the stack object (st-NNNN), None for a PR with neither (its verdict only);
    body: the PR's body when the caller has it, else read with gh; fetch=False: no read (the marks as they are).
    """

    def __init__(self, state, cfg, ref, pr=None, body=None, fetch=True, pl=None) -> None:
        self.state, self.cfg, self.ref, self.pr = state, cfg, ref, pr
        self.plist = plist(state, cfg) if pl is None else pl
        self.old = dict(holder(state, cfg, ref)[1].get("marks") or {}) if ref else {}
        self.parsed, self.error = None, None
        if fetch and self.plist and pr:
            if body is None:
                try:
                    body = pr_body(state, cfg, pr)
                except MarksError as e:
                    self.error = str(e)
            if body is not None:
                self.parsed = parse(body, {p["label"] for p in self.plist})
        self.at = now_iso()
        self.new = from_pr(self.old, self.parsed, pr, self.at) if self.parsed is not None else self.old
        self.verdict = evaluate(self.new, self.plist)

    @property
    def changed(self):
        return self.new != self.old

    def lines(self):
        """What the reading says: what the PR's section gave, what it could not read, and the verdict."""
        who = self.ref or (f"PR #{self.pr}")
        if not self.plist:
            return []
        L = []
        if self.error:
            L.append(t("marks.read_error", error=self.error))
        elif self.parsed is not None and not self.parsed["found"]:
            L.append(t("marks.read_no_section", pr=self.pr, section=section(), format=format_line()))
        elif self.parsed is not None:
            ms = [m["mark"] for m in self.parsed["marks"].values()]
            L.append(
                t(
                    "marks.read_lines",
                    pr=self.pr,
                    section=section(),
                    n=len(ms),
                    ok=ms.count("ok"),
                    na=ms.count("na"),
                    bad=ms.count("bad"),
                )
            )
            for no, junk in self.parsed["junk"]:
                L.append(t("marks.read_junk", no=no, line=junk, format=format_line()))
            if self.parsed["repeats"]:
                L.append(t("marks.read_repeats", labels=", ".join(self.parsed["repeats"])))
            if self.parsed["unknown"]:
                L.append(t("marks.read_unknown", labels=", ".join(self.parsed["unknown"])))
            kept = [k for k in self.new if is_hand(self.new[k].get("by")) and k in self.parsed["marks"]]
            if kept:
                L.append(t("marks.read_hand_kept", labels=", ".join(sorted(kept))))
        L.append("{}: {}".format(who, self.verdict["text"]))
        return L

    def row(self):
        """The history row's fields for this reading."""
        p = self.parsed or {}
        return {
            "pr": self.pr,
            "found": p.get("found") if self.parsed is not None else None,
            "junk": p.get("junk") or None,
            "repeats": p.get("repeats") or None,
            "unknown": p.get("unknown") or None,
            "error": self.error,
            "score": self.verdict["score"],
            "passed": self.verdict["gate"]["passed"],
        }

    def save(self, by):
        """Write the new marks on the car (or the object), read fresh from the disk; nothing when they did not change
        (or there is no car or object to hold them). → whether it wrote.
        """
        if not self.ref or not self.changed:
            return False
        save(self.state, self.cfg, self.ref, self.new, by=by, **self.row())
        return True


def save(state, cfg, ref, new, by, **row):
    car, rec = holder(state, cfg, ref)
    rec["marks"] = new
    car._save_rec("marks", marks=new, by=by, **{k: v for k, v in row.items() if v is not None})
    return rec
