"""The owner's decisions, read: `depot2 decisions` lists decisions.jsonl newest first, with a filter.

`depot2 decide` writes the file (ledger.Decisions, append-only); this module only reads it, so the web view's column
and `decisions --json` are the same data. A decision's links are the ids its text names: the cars (tr-NNNN:N), the
tracks, the stack's and the queues' ids (st-, wl-, th-, td-, tm-, pc-, bs-, ord-), other decisions (dec-NNNNNN) and the
PRs (#NNN).
"""

import re

from . import characters
from .i18n import t
from .ledger import Decisions

V = 1
RE_LINK = re.compile(r"(?<![\w-])(?:(tr-\d{4}:[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*)|(tr-\d{4})"
                     r"|((?:st|wl|th|td|tm|pc|bs|ord)-\d{4})|(dec-\d{6})|#(\d{1,5}))(?![\w])")
LINK_KINDS = ("cars", "tracks", "items", "decisions", "prs")


def who_ru(who):
    if who in (None, "", "all"):
        return t("decisions.all")
    return characters.name(who) if who in characters.IDS else who


def links(text):
    """The ids the text names, each kind in the order they first appear, once."""
    out = {k: [] for k in LINK_KINDS}
    for m in RE_LINK.finditer(text or ""):
        car, track, item, dec, pr = m.groups()
        kind, val = (("cars", car) if car else ("tracks", track) if track else ("items", item) if item else
                     ("decisions", dec) if dec else ("prs", int(pr)))
        if val not in out[kind]:
            out[kind].append(val)
    return out


def row(rec):
    """A record as it is stored, with `date`, `who` (in the run's language) and `links`."""
    return dict(rec, round=rec.get("round"), point=rec.get("point"), date=(rec.get("ts") or "")[:10] or None,
                who=who_ru(rec.get("character")), links=links(rec.get("text")))


def words(text):
    return [w for w in (text or "").casefold().split() if w]


def matches(r, ws):
    """Every word of the filter in the id, the text, who (the id or the name), the round or the point; any case."""
    hay = " ".join(str(r.get(k) or "") for k in ("id", "text", "character", "who", "round", "point")).casefold()
    return all(w in hay for w in ws)


def listing(state, grep=None, limit=None):
    """(the decisions newest first, filtered and cut, how many there are in all)."""
    if limit is not None and limit < 0:
        raise ValueError("--limit is a number from 0")
    recs = Decisions(state).records()
    rows = [row(r) for r in sorted(recs, key=lambda r: r.get("seq") or 0, reverse=True)]
    ws = words(grep)
    if ws:
        rows = [r for r in rows if matches(r, ws)]
    found = len(rows)
    return (rows[:limit] if limit is not None else rows), found, len(recs)


def to_json(state, grep=None, limit=None):
    """`decisions --json`: {v, total, found, count, grep, limit, decisions}; `found` — those the filter keeps."""
    rows, found, total = listing(state, grep, limit)
    return {"v": V, "total": total, "found": found, "count": len(rows), "grep": grep or None, "limit": limit,
            "decisions": rows}


def md(state, grep=None, limit=None):
    """`decisions --md`: a list, newest first, every text whole on one line."""
    rows, found, total = listing(state, grep, limit)
    L = [t("decisions.head_grep", n=len(rows), total=total, grep=grep, found=found) if grep else
         t("decisions.head", n=len(rows), total=total), ""]
    for r in rows:
        where = (" · " + (t("decisions.round_point", round=r["round"], point=r["point"]) if r.get("point") else
                          t("decisions.round", round=r["round"])) if r.get("round") else "")
        ln = [x for k in LINK_KINDS for x in (["#%d" % p for p in r["links"][k]] if k == "prs" else r["links"][k])
              if x != r["id"]]
        L.append("- **%s** · %s · %s%s — %s%s" % (r["id"], r.get("date") or "—", r["who"], where,
                                                 " ".join((r.get("text") or "").split()),
                                                 (" (%s)" % t("decisions.links", links=", ".join(ln))) if ln else ""))
    if not rows:
        L.append(t("decisions.empty"))
    L.append("")
    L.append(t("decisions.footer"))
    return "\n".join(L) + "\n"
