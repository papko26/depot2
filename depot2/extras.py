"""What a round adds besides the points of its input:

- the standing questions of every round (config `standing_questions`: X1, X2, ...);
- the team's own brainstorm: proposed in round 1, voted on in the debate, approved only by the quorum
  (`brainstorm.quorum`: unanimous, or more than half of the team), paid by everyone (a fee that is not returned),
  run by the orchestrator as `round --kind brainstorm --brainstorm bs-NNNN`;
- measurement and test-run orders (`orders.price`), placed in a round, run by the orchestrator, settled by the owner.

The checks here are code, never a model's judgement. brainstorms.jsonl and orders.jsonl are append-only event logs.
"""

import os
import re
import shlex

from . import characters, i18n
from .i18n import t
from .store import append_jsonl, now_iso, read_jsonl, today, write_new

YES, NO = "yes", "no"


# --- the standing questions ------------------------------------------------------------------


def standing_points(cfg):
    return [
        {"id": q["id"], "title": q["title"], "question": q["question"], "recommendation": "", "source": "standing"}
        for q in cfg["standing_questions"]
    ]


# --- the event logs --------------------------------------------------------------------------


class EventLog:
    """Append-only records {id, status, ts, …}; an item is the merge of its records in order."""

    prefix: str | None = None
    filename: str | None = None

    def __init__(self, state) -> None:
        self.state = state
        self.path = state.path(self.filename)

    def records(self):
        return read_jsonl(self.path)

    def items(self):
        out = {}
        for r in self.records():
            item = out.setdefault(r["id"], {"history": []})
            item.update({k: v for k, v in r.items() if k != "ts"})
            item["history"].append({"status": r["status"], "ts": r["ts"]})
        return list(out.values())

    def get(self, item_id):
        for it in self.items():
            if it["id"] == item_id:
                return it
        raise ValueError(f"no {item_id} in {self.filename}")

    def next_id(self):
        return "%s-%04d" % (self.prefix, len({r["id"] for r in self.records()}) + 1)  # noqa: UP031

    def add(self, item_id, status, **fields):
        rec = {"id": item_id, "status": status, "ts": now_iso()}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.path, rec)
        self.state.touch(self.path)
        return rec

    OPEN: tuple[str, ...] = ()
    STATUS_RU: dict[str, str] | i18n.Labels[str] = {}  # noqa: RUF012

    def listing(self, all_=False):
        return [it for it in self.items() if all_ or it["status"] in self.OPEN]


class Brainstorms(EventLog):
    """approved → done | failed (a fatal error, the fee back) | cancelled (never ran, the fee back)."""

    prefix, filename = "bs", "brainstorms.jsonl"
    OPEN = ("approved",)
    STATUS_RU = i18n.Labels("extras.brainstorm", ("approved", "done", "failed", "cancelled"))


class Orders(EventLog):
    """queued → done → settled; `shown` marks the round whose bundle listed the results."""

    prefix, filename = "ord", "orders.jsonl"
    OPEN = ("queued", "done")
    STATUS_RU = i18n.Labels("extras.order", ("queued", "done", "settled"))

    def unshown_done(self):
        return [
            o for o in self.items() if o["status"] in ("done", "settled") and not o.get("shown_in") and o.get("results")
        ]


# --- the brainstorm --------------------------------------------------------------------------


def check_proposal(p, cfg):
    """A round-1 brainstorm_proposal → (normalized or None, why not). None, None: no proposal."""
    if not p:
        return None, None
    topic = " ".join((p.get("topic") or "").split())
    why = (p.get("why") or "").strip()
    qs = [" ".join((q or "").split()) for q in p.get("questions") or []]
    qs = [q for q in qs if q]
    if not topic and not qs and not why:
        return None, None  # left empty: no proposal
    bc = cfg["brainstorm"]
    if not topic:
        return None, t("extras.no_topic")
    if not (bc["questions_min"] <= len(qs) <= bc["questions_max"]):
        return None, t("extras.questions_count", n=len(qs), lo=bc["questions_min"], hi=bc["questions_max"])
    return {"topic": topic, "why": why, "questions": qs}, None


def fees(cfg, initiator):
    bc = cfg["brainstorm"]
    return {cid: int(bc["fee_initiator"] if cid == initiator else bc["fee_other"]) for cid in characters.IDS}


def quorum_text(cfg):
    if cfg["brainstorm"]["quorum"] == "majority":
        return t("extras.quorum_majority", need=majority(), n=len(characters.IDS))
    return t("extras.quorum_unanimous")


def evaluate(proposals, votes, took_part, balances, cfg, history, genders):
    """Decide every proposal. proposals: [{"id", "initiator", …}]; votes: {cid: {pid: (vote, line)}};
    took_part: {cid: bool}, whether its debate call succeeded; balances: after the round's stakes
    came back; history: {cid: brainstorms it called before}; genders: for the report's words.

    Returns the proposals with "votes", "fees", "status" ("approved", "passed", "failed") and
    initiator who has called the fewest brainstorms so far, then the team's fixed order.
    """
    quorum = cfg["brainstorm"]["quorum"]
    out = []
    for p in proposals:
        r = dict(p)
        r["votes"] = {cid: (votes.get(cid) or {}).get(p["id"]) for cid in characters.IDS}
        r["fees"] = fees(cfg, p["initiator"])
        reasons = []
        yes = [cid for cid in characters.IDS if took_part.get(cid) and (r["votes"][cid] or [None])[0] == YES]
        if quorum == "unanimous":
            for cid in characters.IDS:
                v = r["votes"][cid]
                g = genders.get(cid)
                if not took_part.get(cid):
                    reasons.append(t("extras.no_part", gender=g, who=characters.name(cid)))
                elif not v:
                    reasons.append(t("extras.no_vote", gender=g, who=characters.name(cid)))
                elif v[0] != YES:
                    reasons.append(t("extras.voted_no", who=characters.name(cid)))
        elif len(yes) < majority():
            reasons.append(t("extras.yes_count", yes=len(yes), n=len(characters.IDS), need=majority()))
        for cid in characters.IDS:
            if balances[cid] < r["fees"][cid]:
                reasons.append(
                    t("extras.cannot_pay", who=characters.name(cid), balance=balances[cid], fee=r["fees"][cid])
                )
        r["yes"] = len(yes)
        r["status"] = "passed" if not reasons else "failed"
        r["reasons"] = reasons
        out.append(r)
    passed = [r for r in out if r["status"] == "passed"]
    passed.sort(key=lambda r: (-r["yes"], history.get(r["initiator"], 0), characters.IDS.index(r["initiator"])))
    if passed:
        passed[0]["status"] = "approved"
        for r in passed[1:]:
            r["reasons"] = [t("extras.one_per_round", id=passed[0]["id"])]
    return out


# The Russian alphabet (U+0430..U+044F, with the yo U+0451 after the ye) in Latin letters, for a topic's file name.
RU_ALPHABET = "".join(chr(c) for c in range(0x430, 0x436)) + chr(0x451) + "".join(chr(c) for c in range(0x436, 0x450))
TRANSLIT = dict(
    zip(
        RU_ALPHABET,
        [
            "a",
            "b",
            "v",
            "g",
            "d",
            "e",
            "e",
            "zh",
            "z",
            "i",
            "y",
            "k",
            "l",
            "m",
            "n",
            "o",
            "p",
            "r",
            "s",
            "t",
            "u",
            "f",
            "kh",
            "ts",
            "ch",
            "sh",
            "shch",
            "",
            "y",
            "",
            "e",
            "yu",
            "ya",
        ],
        strict=False,
    )
)


def slugify(text, n=40):
    s = "".join(TRANSLIT.get(c, c) for c in (text or "").lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")
    return s[:n].rstrip("-") or "topic"


def topic_text(entry, gender):
    """The topic file, in the discuss format, from the initiator's proposal verbatim."""
    who = characters.name(entry["initiator"])
    lines = [
        t("extras.topic_heading", topic=entry["topic"]),
        "",
        t("extras.topic_called", gender=gender, who=who, id=entry["id"], round=entry["round"]),
        "",
        t("extras.topic_why"),
        "",
        entry.get("why") or "—",
        "",
        t("extras.topic_questions"),
        "",
    ]
    lines += ["%d. %s" % (i, q) for i, q in enumerate(entry["questions"], 1)]  # noqa: UP031
    lines += ["", f"— {who}", ""]
    return "\n".join(lines)


def write_topic(cfg, entry, gender):
    d = os.path.expanduser(cfg["brainstorm"]["topics_dir"])
    base = os.path.join(d, "{}-brainstorm-{}".format(today(), slugify(entry["topic"])))
    path, n = base + ".md", 2
    while os.path.exists(path):
        path, n = "%s-%d.md" % (base, n), n + 1  # noqa: UP031
    write_new(path, topic_text(entry, gender))
    return path


# --- orders ----------------------------------------------------------------------------------

ORDER_FIELDS = ("command", "seeds", "hardware", "threshold", "forecast", "why")
SHELL_META = re.compile(r"[;|&<>`$\n]")


def check_order(o, snap_dir, cfg):
    """An order → (normalized or None, why not). None, None: no order. Code checks only: every
    field filled, one call of a script that exists under tools/ in the round's snapshot, headless
    hardware.
    """
    if not o:
        return None, None
    o = {k: " ".join(str(o.get(k) or "").split()) for k in ORDER_FIELDS}
    if not any(o.values()):
        return None, None
    missing = [k for k in ORDER_FIELDS if not o[k]]
    if missing:
        return None, t("extras.order_missing", fields=", ".join(missing))
    if SHELL_META.search(o["command"]):
        return None, t("extras.order_meta")
    try:
        tokens = shlex.split(o["command"])
    except ValueError as e:
        return None, t("extras.order_unparsed", error=e)
    scripts = [tok for tok in tokens if tok.startswith(("tools/", "./tools/"))]
    if not scripts:
        return None, t("extras.order_no_script")
    rel = os.path.normpath(scripts[0].removeprefix("./"))
    if not rel.startswith("tools" + os.sep) or ".." in rel.split(os.sep):
        return None, t("extras.order_outside", script=scripts[0])
    if not snap_dir:
        return None, t("extras.order_no_snapshot")
    if not os.path.isfile(os.path.join(snap_dir, rel)):
        return None, t("extras.order_not_in_snapshot", script=rel)
    hw = o["hardware"].lower()
    allowed = [h.lower() for h in cfg["orders"]["hardware"]]
    if hw not in allowed:
        return None, t("extras.order_hardware", allowed=t("extras.or").join(allowed))
    o["hardware"] = hw
    o["script"] = rel
    return o, None


def majority():
    """More than half of the team."""
    return len(characters.IDS) // 2 + 1
