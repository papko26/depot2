"""The team stack: every interaction with the team of the rounds, beside the executors' stack.

A command of its own, `depot2 tstack`, not `stack --team`: its items have other fields (the kind, the input, who
answers, the round and its spend). Its slots run beside the stack's: the rounds are light.

team-stack.json          the same shape as stack.json; ids tm-NNNN
team-stack-history.jsonl every change
"""

import os

from . import characters, effort, i18n
from . import priority as pri
from .i18n import is_word, t
from .stack import WHERE, Stack, StackError, _cell

KINDS = ("review", "review-10", "discuss", "ballot", "brainstorm", "solo", "question", "reflect", "export",
         "other")
KIND_RU = i18n.Labels("tstack.kind", KINDS)


class TeamStack(Stack):
    FILE = "team-stack.json"
    HISTORY = "team-stack-history.jsonl"
    PREFIX = "tm"
    CFG = "team_stack"
    RUNNING_KEY = "tstack.running"
    POST = False  # the post-stack is the agents' stack's: a team item has no PR
    STAGES = False  # a team item is no car: it has no stage (stages.py)

    def agents(self):
        return Stack(self.state, self.cfg)

    # A team item may wait for an agents' stack object (a review after a PR lands).
    def _known(self, oid):
        if oid.startswith("st-"):
            return oid in self.agents().known_ids()
        return oid in self.known_ids()

    def _dep_open(self, oid):
        if oid.startswith("st-"):
            return any(x["id"] == oid for x in self.agents().objects())
        return super(TeamStack, self)._dep_open(oid)

    def add(self, kind, title=None, input_path=None, who="all", depends=(), at=None, status=None, size=None,
            est_mtok=None, priority=None):
        if kind not in KINDS:
            raise StackError("kind must be one of %s" % ", ".join(KINDS))
        if len(self.queue()) >= self.queue_max:
            raise StackError(t("tstack.queue_full", n=self.queue_max))
        bad = [d for d in depends or () if not self._known(d)]
        if bad:
            raise StackError("unknown dependency: %s" % ", ".join(bad))
        who = _who(who)
        effort.check_size(size, self.cfg)
        path = os.path.abspath(os.path.expanduser(input_path)) if input_path else None
        if not (title or "").strip():
            title = KIND_RU[kind] + (": " + os.path.basename(path) if path else "")
        obj = {"id": self._new_id(), "kind": kind, "title": title.strip(), "input": path, "who": who,
               "round": None, "cost_usd": None, "where": "queue", "depends_on": list(depends or ()),
               "status": status or t("stack.waiting"), "added": _now(), "started": None, "lock": None,
               "branch": None, "pr": None, "brief": None, "size": size, "est_mtok": est_mtok, "spent_mtok": None,
               "priority": pri.DEFAULT if priority is None else pri.parse(priority)}
        return self._insert(obj, at)

    def set(self, oid, **fields):
        if fields.get("kind") is not None and fields["kind"] not in KINDS:
            raise StackError("kind must be one of %s" % ", ".join(KINDS))
        if fields.get("who") is not None:
            fields["who"] = _who(fields["who"])
        if fields.get("input"):
            fields["input"] = os.path.abspath(os.path.expanduser(fields["input"]))
        return super(TeamStack, self).set(oid, **fields)

    def record_round(self, oid, rid, costs):
        return super(TeamStack, self).set(oid, round=rid, cost_usd=round(float(costs.get("total_usd") or 0.0), 4),
                                          spent_mtok=round(effort.costs_mtok(costs, self.cfg), 6),
                                          status=t("tstack.round_done", rid=rid))

    def command(self, o):
        """The command that runs the item (printed on `tstack start`), or None for a question."""
        solo = "" if o.get("who") in (None, "all") else " --only %s" % o["who"]
        inp = " --input %s" % o["input"] if o.get("input") else ""
        k = o["kind"]
        if k in ("review", "review-10", "discuss", "ballot"):
            kind = "review" if k == "review-10" else k
            return "depot2 round --kind %s%s%s --item %s --dry-run" % (kind, inp, solo, o["id"])
        if k == "solo":
            return "depot2 round --kind discuss%s%s --item %s --dry-run" % (inp, solo or " --only <character>", o["id"])
        if k == "reflect":
            return "depot2 reflect%s" % solo
        if k == "export":
            return "depot2 export"
        return None

    def md(self, heading=True):
        slots, q = self.in_slots(), self.queue()
        L = [t("tstack.heading"), ""] if heading else []
        L.append(t("tstack.header"))
        L.append("|---|---|---|---|---|---|---|---|---|---|---|")
        rows = self.rows()
        for part, where, n, cap in ((t("stack.part_slots"), "slot", len(slots), self.slots_n),
                                    (t("stack.part_queue"), "queue", len(q), self.queue_max)):
            L.append("| **%s (%d/%d)** | | | | | | | | | | |" % (part, n, cap))
            for num, w, o in rows:
                if w != where:
                    continue
                if o is None:
                    L.append("| %s | %s | %s | | | | | | | | |" % (num, WHERE[w], t("stack.free")))
                    continue
                L.append("| %s | %s | %s %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    num, WHERE[w], o["id"], _cell(o["title"]), pri.cell(o), KIND_RU.get(o["kind"], o["kind"]),
                    who_ru(o.get("who")),
                    _cell(os.path.basename(o["input"]) if o.get("input") else None), _cell(o.get("round") or "—"),
                    _cell(o.get("status")), effort.cell(effort.item_expected(o, self.cfg), spent_of(o, self.cfg), self.cfg),
                    ", ".join(o.get("depends_on") or []) or "—"))
        L.append("")
        L.append(t("tstack.footer"))
        L.append(effort.legend(self.cfg))
        return "\n".join(L) + "\n"

    def to_json(self):
        out = super(TeamStack, self).to_json()
        out["kind"] = "team"
        return out


def _who(who):
    if who in (None, "") or is_word(who, "in.tstack.all"):
        return "all"
    return characters.resolve(who)


def who_ru(who):
    return t("tstack.all") if who in (None, "all") else characters.name(who)


def spent_of(o, cfg):
    """An item's spend, MTok: spent_mtok; an item recorded before the MTok has only cost_usd (≈$)."""
    if o.get("spent_mtok") is not None:
        return o["spent_mtok"]
    if o.get("cost_usd"):
        return round(effort.from_usd(o["cost_usd"], cfg), 4)
    return None


def _now():
    from .store import now_iso
    return now_iso()
