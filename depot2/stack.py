"""The stack and the ballot queue, as files in the depot.

The stack: the executors' slots (`stack.slots`) plus a queue of tasks waiting for one (`stack.queue_max`) and the
post-stack: PRs that wait only for the owner's merge and hold no slot (`stack.post_max`). A queued object starts in a
freed slot only with `autostart`. Any object can carry a lock (its price in tokens, placed in a team round's lock pass).

stack.json          {"next": n, "next_lock": n, "objects": [...]}: slots first, then the post-stack, then the queue
stack-history.jsonl every change
ballots.json        the ballot queue: the rounds' points waiting for the owner, in the reports' order
"""

import datetime
import os

from . import characters, effort, i18n, stages
from . import priority as pri
from .i18n import is_word, t
from .store import append_jsonl, now_iso, read_json, read_jsonl, write_json


# What a lock means; the README and the report say the same. The orchestrator honours it.
def lock_means():
    """What a lock does, in one line (the stack's legend and the round report's lock pass)."""
    return t("stack.lock_means")


def autostart_means():
    """What the autostart flag does, in one line (the stack's legend)."""
    return t("stack.autostart_means")


def post_means():
    """What the post-stack is, in one line (the stack's legend)."""
    return t("stack.post_means")


def stage_legend():
    """The stages' route, in one line (the stack's legend)."""
    return t("stack.stage_legend")


CAR_DONE = ("merged", "dropped")  # tracks.py's: a car's object is done only when its car is
OUTCOMES = ("merged", "dropped", "back")  # `stack done --car`: what the object's car becomes (back: to its track)


def parse_yes_no(text, flag="--autostart"):
    """yes or no in any language (in.stack.yes, in.stack.no)."""
    word = str(text).strip()
    if is_word(word, "in.stack.yes"):
        return True
    if is_word(word, "in.stack.no"):
        return False
    raise StackError(f"{flag} is yes or no, got {text!r}")


class StackError(ValueError):
    pass


class Stack:
    # The team stack (tstack.py) reuses this class with its own file, ids and config key.
    FILE = "stack.json"
    HISTORY = "stack-history.jsonl"
    PREFIX = "st"
    CFG = "stack"
    RUNNING_KEY = "stack.running"  # the status of an object that starts in a slot (stored in the run's language)
    POST = True  # the agents' stack has a post-stack; the team stack (tstack.py) has none
    STAGES = True  # the agents' objects are cars with stages (stages.py); a team item is not

    def __init__(self, state, cfg) -> None:
        self.state = state
        self.cfg = cfg
        self.path = state.path(self.FILE)
        self.history_path = state.path(self.HISTORY)
        self.data = read_json(self.path) or {"next": 1, "next_lock": 1, "objects": []}
        self._links = None  # (the tracks files' stamps, {st: car})
        self._mq = None  # the merge queue's open entries (stages.merge_index), read once
        self.notes: list[str] = []  # the lines a command prints: a stage moved off the route
        self.last_forced = None  # what the last start or post forced past its gate (gates.py)

    # --- reading ----------------------------------------------------------------------------

    @property
    def slots_n(self):
        return int(self.cfg[self.CFG]["slots"])

    @property
    def queue_max(self):
        return int(self.cfg[self.CFG]["queue_max"])

    @property
    def post_max(self):
        return int(self.cfg[self.CFG].get("post_max", 0)) if self.POST else 0

    def objects(self):
        return list(self.data["objects"])

    def in_slots(self):
        return [o for o in self.data["objects"] if o["where"] == "slot"]

    def queue(self):
        return [o for o in self.data["objects"] if o["where"] == "queue"]

    def posted(self):
        """The post-stack: PRs that wait only for the owner's merge; no slot."""
        return [o for o in self.data["objects"] if o["where"] == "post"]

    def _ordered(self, objs):
        return (
            [o for o in objs if o["where"] == "slot"]
            + [o for o in objs if o["where"] == "post"]
            + [o for o in objs if o["where"] == "queue"]
        )

    def get(self, oid):
        for o in self.data["objects"]:
            if o["id"] == oid:
                return o
        raise StackError(f"no {oid} in the stack")

    def history(self):
        return read_jsonl(self.history_path)

    def known_ids(self):
        # a migration row names no object (queues.py: `object: None`); None is no id
        return ({o["id"] for o in self.data["objects"]} | {h.get("object") for h in self.history()}) - {None}

    def empty(self):
        return not self.data["objects"]

    def _known(self, oid):
        """A dependency may name an object that is in the stack or has left it."""
        return oid in self.known_ids()

    def _dep_open(self, oid):
        """In the stack: a dependency still here is not done yet."""
        return any(x["id"] == oid for x in self.data["objects"])

    def dep_waiting(self, oid):
        """A dependency is not done while it is in the stack, or while it is or was the object of a track's
        car that is neither merged nor dropped (a parked car's object left the stack, its PR did not land;
        a car sent back to its track with `stack done --car back` did no work that landed).
        """
        return self._dep_open(oid) or oid in self.open_car_objects()

    def _cars(self):
        data = read_json(self.state.path("tracks.json")) or {}
        return [c for t in data.get("tracks") or [] for c in t.get("cars") or []]

    def car_recs(self):
        """{st: (car ref, car record)} for every live object that is a track's car."""
        data = read_json(self.state.path("tracks.json")) or {}
        return {
            c["st"]: ("{}:{}".format(t["id"], c["n"]), c)
            for t in data.get("tracks") or []
            for c in t.get("cars") or []
            if c.get("st")
        }

    def docs_of(self, o, recs=None):
        from . import gates

        if not self.STAGES:
            return None
        ref, car = (self.car_recs() if recs is None else recs).get(o["id"]) or (None, None)
        return gates.docs_state([r for r in (car, o) if r is not None], self.stage_of(o), ref=ref or o["id"])

    def stage_cell(self, o, recs=None):
        s = self.stage_of(o)
        d = self.docs_of(o, recs) if s == "build" else None
        return t("stack.stage_needs_docs", stage=stages.ru(s)) if d and d["need"] else stages.ru(s)

    def car_objects(self):
        """The ids of the objects that are a track's car (tracks.json `st`): they follow their car."""
        return {c["st"] for c in self._cars() if c.get("st")}

    def car_places(self):
        tracks = read_json(self.state.path("tracks.json")) or {}
        trains = read_json(self.state.path("trains.json")) or {}
        live = {
            r: tn
            for tn in trains.get("trains") or []
            if tn.get("state") in ("design", "impl", "merge")
            for r in tn.get("cars") or []
        }
        out = {}
        for track in tracks.get("tracks") or []:
            for c in track.get("cars") or []:
                if c.get("st"):
                    ref = "{}:{}".format(track["id"], c["n"])
                    tn = live.get(ref)
                    out[c["st"]] = {
                        "car": ref,
                        "track": track["id"],
                        "track_title": track["title"],
                        "train": tn["name"] if tn else None,
                        "train_title": tn["title"] if tn else None,
                    }
        return out

    def car_links(self):
        """{st: car} for every object that is or was a track's car: from the tracks' history (`--car back`
        clears a car's live link, a car stacked again gets a new object), the live link winning.
        """
        paths = [self.state.path("tracks.json"), self.state.path("tracks-history.jsonl")]
        stamp = tuple((os.stat(p).st_mtime_ns, os.stat(p).st_size) if os.path.exists(p) else None for p in paths)
        if self._links and self._links[0] == stamp:
            return self._links[1]
        data = read_json(paths[0]) or {}
        cars = {(t["id"], c["n"]): c for t in data.get("tracks") or [] for c in t.get("cars") or []}
        links = {}
        for h in read_jsonl(paths[1]):
            key = (h.get("track"), h.get("car"))
            if h.get("st") and key in cars:
                links[h["st"]] = cars[key]
        for c in cars.values():
            if c.get("st"):
                links[c["st"]] = c
        self._links = (stamp, links)
        return links

    def open_car_objects(self):
        return {st for st, c in self.car_links().items() if c.get("status") not in CAR_DONE}

    def left_obj(self, oid):
        """The object as it left the stack (its last `done` or `park` row, or a later `stage` row), or None."""
        for h in reversed(self.history()):
            if h.get("object") == oid and h.get("obj"):
                return h["obj"]
        return None

    # --- stages (stages.py) -------------------------------------------------------------------

    def mq_index(self):
        if self._mq is None:
            self._mq = stages.merge_index(self.state)
        return self._mq

    def stage_default_why(self, o):
        """The stage an object's place says (an object from before the field; `queues migrate` writes it)."""
        e = self.mq_index()["st"].get(o["id"])
        if e is not None and stages.entry_stage(e):
            return stages.entry_default(e)
        w = o.get("where")
        if w == "slot":
            return "build", t("stack.why_slot")
        if w == "post":
            return "acceptance", t("stack.why_post")
        if o.get("parked"):
            return "build", t("stack.why_parked_pr", pr=o.get("pr")) if o.get("pr") else t("stack.why_parked")
        if o.get("pr") or o.get("started"):
            return "build", t("stack.why_built_pr", pr=o["pr"]) if o.get("pr") else t("stack.why_built")
        if o.get("brief"):
            return "sketch", t("stack.why_brief")
        return "bogie", t("stack.why_no_brief")

    def stage_of(self, o):
        if not self.STAGES:
            return None
        return o.get("stage") or self.stage_default_why(o)[0]

    def left_stage(self, oid):
        """The stage of an object that left the stack: its own, else what its leaving row says."""
        row = None
        for h in reversed(self.history()):
            if h.get("object") == oid and h.get("obj"):
                row = h
                break
        if row is None:
            return None
        if row["obj"].get("stage"):
            return row["obj"]["stage"]
        return self._leaving_stage(
            row["obj"], row.get("outcome"), row.get("reason"), parked=row["event"] == "park", fresh=False
        )

    def _leaving_stage(self, o, outcome, reason, parked=False, fresh=True):
        from .tracks import NOT_MERGED_RE, reads_dropped

        if parked:
            return "build"
        if outcome == "back":
            return stages.prev_planning(o)
        if outcome == "dropped" or (outcome is None and (reads_dropped(reason) or NOT_MERGED_RE.search(reason or ""))):
            return "scrapped"
        e = self.mq_index()["st"].get(o["id"])
        if e is not None and e.get("merged") and e["status"] != "merged":
            return "rolling"
        return "rolling" if o.get("pr") and fresh else "arrived"

    def _stage(self, o, stage, by, why=None, forced=False, gates=None):
        """Move an object's stage in memory (the caller saves); a move off the route is said in `notes`."""
        if not self.STAGES:
            return None
        row = stages.record(
            o,
            stage,
            by,
            why,
            default=None if o.get("stage") else self.stage_default_why(o)[0],
            forced=forced,
            gates=gates,
        )
        line = stages.say(o["id"], row)
        if line:
            self.notes.append(line)
        return row

    def set_stage(self, oid, stage, by, why=None, forced=False):
        """`car stage` (and the merge queue): one object's stage, live or left (a `stage` row with the object)."""
        if self._dep_open(oid):
            row = self._stage(self.get(oid), stage, by, why, forced=forced)
            if row:
                self._save("stage", oid, stage=stages.hist(row), why=why, by=by)
            return row
        obj = self.left_obj(oid)
        if obj is None:
            raise StackError(f"no {oid} in the stack")
        obj = dict(obj)
        row = stages.record(
            obj, stage, by, why, default=None if obj.get("stage") else self.left_stage(oid), forced=forced
        )
        if row:
            self._save("stage", oid, stage=stages.hist(row), why=why, by=by, obj=obj, left=True)
        return row

    # --- writing ----------------------------------------------------------------------------

    def _save(self, event, oid, **fields):
        # slots first, the post-stack, then the queue, each in its own order
        self.data["objects"] = self._ordered(self.data["objects"])
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": now_iso(), "event": event, "object": oid}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def add(
        self,
        title,
        brief=None,
        branch=None,
        depends=(),
        at=None,
        pr=None,
        status=None,
        check_queue=True,
        size=None,
        est_mtok=None,
        autostart=False,
        stage=None,
        by="stack add",
        why=None,
        extra=None,
        forced=None,
        priority=None,
    ):
        if not (title or "").strip():
            raise StackError("a stack object needs a title")
        if stage is not None and stage not in stages.STAGES:
            raise StackError(t("stack.no_stage", stage=repr(stage)))
        if check_queue and len(self.queue()) >= self.queue_max:
            raise StackError(t("stack.queue_full", n=self.queue_max))
        bad = [d for d in depends or () if not self._known(d)]
        if bad:
            raise StackError("unknown dependency: {}".format(", ".join(bad)))
        effort.check_size(size, self.cfg)
        oid = self._new_id()
        obj = {
            "id": oid,
            "title": title.strip(),
            "where": "queue",
            "branch": branch,
            "pr": pr,
            "brief": os.path.abspath(os.path.expanduser(brief)) if brief else None,
            "depends_on": list(depends or ()),
            "status": status or t("stack.waiting"),
            "added": now_iso(),
            "started": None,
            "lock": None,
            "size": size,
            "est_mtok": est_mtok,
            "spent_mtok": None,
            "autostart": bool(autostart),
            "priority": pri.DEFAULT if priority is None else pri.parse(priority),
        }
        obj.update(extra or {})
        if self.STAGES:
            stages.start(
                obj, stage or ("sketch" if brief else "bogie"), by, why, forced=bool(forced), gates=forced or None
            )
        return self._insert(obj, at)

    def _new_id(self):
        oid = "%s-%04d" % (self.PREFIX, self.data["next"])  # noqa: UP031
        self.data["next"] += 1
        return oid

    def _insert(self, obj, at):
        q = self.queue()
        pos = len(q) if at is None else max(0, min(len(q), int(at) - 1))
        others = [o for o in self.data["objects"] if o["where"] != "queue"]
        self.data["objects"] = others + q[:pos] + [obj] + q[pos:]
        repair = (
            {"kind": "repair", "repairs": obj.get("repairs"), "bug": obj.get("bug")}
            if obj.get("kind") == "repair"
            else {}
        )  # a team stack item's own `kind` is no repair
        self._save("add", obj["id"], title=obj["title"], at=pos + 1, stage=obj.get("stage"), **repair)
        return obj

    def start(self, oid, branch=None, reason=None, by="stack start", force=False, why=None, passed=None):
        """queue → slot. From the post-stack (the owner sends it back for more work): → a free slot, or the
        head of the queue when none is free; its PR, branch and brief stay, and a lock does not hold it
        (a lock holds the merge; its agent may work to meet the condition). The way into the build is a soft
        gate (gates.py): the DoR and the slots; `force` with `why` passes it, recorded. passed: the caller
        checked the gate already (`tracks car stack --start`): what it forced, or {}.
        """
        o = self.get(oid)
        if o["where"] == "post":
            return self._unpost(o, branch, reason, by)
        if o["where"] != "queue":
            raise StackError(f"{oid} is in a slot already")
        if o.get("lock"):
            raise StackError(
                "{} is locked ({}, {}: {}): a locked object does not start".format(
                    oid, o["lock"]["id"], characters.name(o["lock"]["by"]), o["lock"]["condition"]
                )
            )
        forced = self.start_gate(o, force, why) if passed is None else (passed or None)
        self.last_forced = forced
        o.update(
            where="slot",
            started=now_iso(),
            status=t(self.RUNNING_KEY) if i18n.matches(o["status"], "stack.waiting") else o["status"],
        )
        parked = o.pop("parked", None)  # `stack start` by hand brings a parked object back
        if branch:
            o["branch"] = branch
        row = self._stage(o, "build", by, why if forced else reason, forced=bool(forced), gates=forced)
        self._save(
            "start",
            oid,
            branch=branch,
            unparked=True if parked else None,
            reason=reason,
            stage=stages.hist(row),
            forced=True if forced else None,
            why=why if forced else None,
            gates=forced,
        )
        return o

    def start_gate(self, o, force=False, why=None, cmd=None):
        """The way into the build for an object in the queue: the DoR (from a planning stage; the `after` from
        any, as before the gates) and the slots. None, or what --force passed ({gate: [items]}).
        """
        from . import gates

        if not self.STAGES:  # a team item has no stage and no gate: the old refusals
            waiting = [d for d in o.get("depends_on") or [] if self.dep_waiting(d)]
            if waiting:
                raise StackError("{} depends on {}, not done yet".format(o["id"], ", ".join(waiting)))
            if len(self.in_slots()) >= self.slots_n:
                raise StackError("no free slot (%d/%d)" % (len(self.in_slots()), self.slots_n))  # noqa: UP031
            return None
        from .tracks import Tracks

        tr = Tracks(self.state, self.cfg)
        tr._stack = self
        t, c = tr.car_by_stack(o["id"])
        if self.stage_of(o) in stages.PLANNING:
            dor = gates.dor_unmet(self, tr, obj=o, car=c, track=t)
        else:  # it was in the build before (parked, back for work): its own `depends_on`, as before the gates (a
            # car's `after` gates its merge, not its work)
            dor = gates.dor_unmet(self, tr, obj=o, items=[i for i in gates.dor_items(self.cfg) if i == "after"])
        unmet = {"DoR": dor, "limit": gates.slots_unmet(self)}
        return gates.check(
            unmet,
            force,
            why,
            o["id"],
            stages.ru("build"),
            cmd or "depot2 stack start {}".format(o["id"]),
            error=StackError,
        )

    def _unpost(self, o, branch, reason, by="stack start"):
        oid = o["id"]
        free = len(self.in_slots()) < self.slots_n
        if not free and len(self.queue()) >= self.queue_max:
            raise StackError(
                "no free slot (%d/%d) and the queue is full (%d): %s stays in the post-stack"  # noqa: UP031
                % (len(self.in_slots()), self.slots_n, self.queue_max, oid)
            )
        o.pop("posted", None)
        if branch:
            o["branch"] = branch
        row = self._stage(o, "build", by, reason or t("stack.why_unpost"))
        if free:
            o.update(where="slot", started=now_iso(), status=t(self.RUNNING_KEY))
            self._save("start", oid, branch=branch, unposted=True, reason=reason, stage=stages.hist(row))
        else:
            o.update(where="queue", status=t("stack.waiting"))
            rest = [x for x in self.data["objects"] if x is not o]
            self.data["objects"] = (
                [x for x in rest if x["where"] != "queue"] + [o] + [x for x in rest if x["where"] == "queue"]
            )
            self._save("start", oid, branch=branch, unposted=True, reason=reason, at=1, stage=stages.hist(row))
        return o

    def set(self, oid, clear_est=False, dod=None, note=None, **fields):
        o = self.get(oid)
        changed = {k: v for k, v in fields.items() if v is not None}
        if note is not None:
            if not note.strip():
                raise StackError("an empty --note adds nothing")
            changed["notes"] = [*list(o.get("notes") or []), f"{now_iso()[:10]}: {note.strip()}"]
        if "ci" in changed:
            changed["ci"] = ci_record(changed["ci"])
        handed = self._handed(dod)
        if handed:
            changed["dod"] = dict(o.get("dod") or {}, **handed)
        if "brief" in changed:
            changed["brief"] = os.path.abspath(os.path.expanduser(changed["brief"]))
        if "autostart" in changed:
            changed["autostart"] = bool(changed["autostart"])
        if "priority" in changed:
            changed["priority"] = pri.parse(changed["priority"])
        effort.check_size(changed.get("size"), self.cfg)
        if clear_est:
            if "est_mtok" in changed:
                raise StackError("an estimate, or none: not both")
            changed["est_mtok"] = None
        if not changed:
            raise StackError("nothing to set")
        o.update(changed)
        if note is not None:  # the history row keeps the line added, not the whole list again
            changed.pop("notes")
        self._save(
            "set", oid, est_cleared=True if clear_est else None, note_added=note.strip() if note else None, **changed
        )
        return o

    @staticmethod
    def _handed(dod):
        from .gates import handed

        return {k: handed(v) for k, v in (dod or {}).items() if v is not None}

    def move(self, oid, at):
        o = self.get(oid)
        if o["where"] != "queue":
            raise StackError(f"{oid} is in a slot: only the queue has an order")
        q = [x for x in self.queue() if x["id"] != oid]
        pos = max(0, min(len(q), int(at) - 1))
        self.data["objects"] = [x for x in self.data["objects"] if x["where"] != "queue"] + q[:pos] + [o] + q[pos:]
        self._save("move", oid, at=pos + 1)
        return o

    def done(self, oid, reason, outcome=None, by="stack done"):
        o = self.get(oid)
        if o.get("lock"):
            raise StackError(
                "{} is locked ({}): lift it first (stack unlock {} --met|--owner)".format(oid, o["lock"]["id"], oid)
            )
        if outcome is not None and outcome not in OUTCOMES:
            raise StackError("--car is one of {}".format(", ".join(OUTCOMES)))
        self.data["objects"] = [x for x in self.data["objects"] if x["id"] != oid]
        row = self._stage(o, self._leaving_stage(o, outcome, reason), by, reason) if self.STAGES else None
        self._save("done", oid, reason=reason, obj=o, outcome=outcome, stage=stages.hist(row))
        return o

    def park(self, oid, reason, leave=False, by="stack park"):
        """A slot whose PR waits (blocked, e.g. by a train) and no agent works: it frees the slot.
        leave=True (a track's car): the object leaves the stack; the car goes back to its track with
        its PR (tracks.py on_stack "park"). Else: slot → the head of the queue, with its branch, PR
        and status; a freed slot never takes it by itself, `stack start` brings it back.
        """
        o = self.get(oid)
        if o["where"] != "slot":
            raise StackError(f"{oid} is not in a slot: only an object in a slot is parked")
        if not (reason or "").strip():
            raise StackError("park needs --reason")
        if leave:
            if o.get("lock"):
                raise StackError(
                    "{} is locked ({}): lift it first (stack unlock {} --met|--owner); a car's object "
                    "leaves the stack when it is parked".format(oid, o["lock"]["id"], oid)
                )
            self.data["objects"] = [x for x in self.data["objects"] if x["id"] != oid]
            row = self._stage(o, "build", by, reason)  # a parked PR is still in build: it waits, blocked
            self._save("park", oid, reason=reason, obj=o, left=True, stage=stages.hist(row))
            return o
        if len(self.queue()) >= self.queue_max:
            raise StackError(t("stack.park_full_post" if self.POST else "stack.park_full", n=self.queue_max, oid=oid))
        o.update(where="queue", parked={"ts": now_iso(), "reason": reason.strip()})
        row = self._stage(o, "build", by, reason)
        rest = [x for x in self.data["objects"] if x is not o]
        self.data["objects"] = (
            [x for x in rest if x["where"] != "queue"] + [o] + [x for x in rest if x["where"] == "queue"]
        )
        self._save("park", oid, reason=reason, at=1, stage=stages.hist(row))
        return o

    def post(
        self, oid, reason=None, ci=None, pr=None, by="stack post", force=False, why=None, dod=None, principles=None
    ):
        o = self.get(oid)
        if not self.POST:
            raise StackError("this stack has no post-stack")
        if o["where"] == "post":
            raise StackError(f"{oid} is in the post-stack already")
        if not (o.get("pr") or pr):
            raise StackError(
                f"{oid} has no PR: only a PR that waits for the owner's merge goes to the post-stack "
                f"(stack set {oid} --pr N first)"
            )
        if o.get("lock"):
            raise StackError(
                "{} is locked ({}, {}: {}): a locked object does not merge; lift it first "
                "(stack unlock {} --met|--owner)".format(
                    oid, o["lock"]["id"], characters.name(o["lock"]["by"]), o["lock"]["condition"], oid
                )
            )
        from . import gates

        handed = self._handed(dod)
        was_ci = o.get("ci")
        # --ci, else the one recorded (a green one stamped now, a red or pending one kept: the DoD sees it)
        recorded = (
            ci_record(ci)
            if ci
            else ci_record("green")
            if i18n.matches((was_ci or {}).get("state"), "stack.ci.green")
            else was_ci
        )
        probe = dict(o, ci=recorded, dod=dict(o.get("dod") or {}, **handed))
        to_accept = oid not in self.mq_index()["st"]
        unmet = {
            "DoD": gates.dod_unmet(self.cfg, probe, principles=principles, docs=self.docs_of(o))
            if to_accept and self.STAGES
            else [],
            "limit": gates.post_unmet(self, oid, o["where"]),
        }
        forced = gates.check(
            unmet, force, why, oid, stages.ru("acceptance"), f"depot2 stack post {oid}", error=StackError
        )
        self.last_forced = forced
        was = o["where"]
        if not o.get("pr"):
            o["pr"] = int(pr)
        o.pop("parked", None)
        o.update(where="post", posted={"ts": now_iso(), "reason": (reason or "").strip() or None, "from": was})
        self.data["objects"] = [x for x in self.data["objects"] if x is not o] + [o]  # the post-stack in posting order
        # with no CI recorded, a post means green, as before the gates, when the DoD does not check the CI
        o["ci"] = recorded or (ci_record("green") if "ci" not in gates.dod_items(self.cfg) else None)
        if handed:
            o["dod"] = probe["dod"]
        row = (
            self._stage(o, "acceptance", by, why if forced else reason, forced=bool(forced), gates=forced)
            if to_accept
            else None
        )
        self._save(
            "post",
            oid,
            reason=reason,
            was=was,
            pr=o["pr"],
            ci=o["ci"],
            dod=handed or None,
            stage=stages.hist(row),
            forced=True if forced else None,
            why=why if forced else None,
            gates=forced,
        )
        return o

    def restore(self, obj, check_queue=True, **fields):
        """A parked car's object comes back (`tracks car stack`): the same id, at the end of the queue,
        so whatever waits for it keeps waiting for it.
        """
        oid = obj["id"]
        if self._dep_open(oid):
            raise StackError(f"{oid} is in the stack already")
        if check_queue and len(self.queue()) >= self.queue_max:
            raise StackError(t("stack.queue_full", n=self.queue_max))
        o = dict(obj)
        o.pop("parked", None)
        o.update(where="queue", status=t("stack.waiting"), started=None, lock=None)
        o.update({k: v for k, v in fields.items() if v is not None})
        row = self._stage(o, "build", "tracks car stack", t("stack.why_restore"))
        self.data["objects"].append(o)
        self._save("restore", oid, at=len(self.queue()), stage=stages.hist(row))
        return o

    def lock(self, oid, by, condition, why, round_id, tx, price):
        o = self.get(oid)
        if o.get("lock"):
            raise StackError("{} is locked already ({})".format(oid, o["lock"]["id"]))
        lid = "lk-%04d" % self.data["next_lock"]  # noqa: UP031
        self.data["next_lock"] += 1
        o["lock"] = {
            "id": lid,
            "by": by,
            "condition": condition,
            "why": why,
            "round": round_id,
            "ts": now_iso(),
            "tx": tx,
            "price": price,
        }
        self._save("lock", oid, lock=o["lock"])
        return o["lock"]

    def unlock(self, oid, how, reason):
        o = self.get(oid)
        if not o.get("lock"):
            raise StackError(f"{oid} has no lock")
        lock = o["lock"]
        o["lock"] = None
        self._save("unlock", oid, lock=lock, how=how, reason=reason)
        return lock

    # --- the table --------------------------------------------------------------------------

    def rows(self):
        """(number, where, object or None): where is slot, post or queue (its word: WHERE)."""
        out = []
        slots = self.in_slots()
        for i in range(self.slots_n):
            out.append((t("stack.num_slot", n=i + 1), "slot", slots[i] if i < len(slots) else None))
        for o in slots[self.slots_n :]:  # more in slots than the config allows now: still shown
            out.append((t("stack.num_slot_over"), "slot", o))
        for i, o in enumerate(self.posted(), 1):
            out.append((t("stack.num_post", n=i), "post", o))
        for i, o in enumerate(self.queue(), 1):
            out.append((str(i), "queue", o))
        return out

    def md(self, heading=True, now=None):
        slots, q, post = self.in_slots(), self.queue(), self.posted()
        cars = self.car_objects()
        places = self.car_places() if self.STAGES else None
        blank = " | |" if places is not None else ""
        L = [t("stack.heading")] if heading else []
        if heading:
            L.append("")
        L.append(t("stack.header") + (t("stack.header_places") if places is not None else ""))
        L.append("|---|---|---|---|---|---|---|---|---|---|---|%s" % ("---|---|" if places is not None else ""))
        rows = self.rows()
        recs = self.car_recs() if self.STAGES else {}
        agents = {}
        if self.STAGES:
            from .agents import live_by_object

            agents = live_by_object(self.state, self.cfg, places)
        # the agents' stack is the board's view (board.py): its parts are the board's columns, with their limits
        board = self.STAGES
        parts = [(t("stack.part_build_board" if board else "stack.part_slots"), "slot", len(slots), self.slots_n)]
        if self.POST:
            parts.append((t("stack.part_post_board" if board else "stack.part_post"), "post", len(post), self.post_max))
        parts.append((t("stack.part_queue_board" if board else "stack.part_queue"), "queue", len(q), self.queue_max))
        for part, where, n, cap in parts:
            over = t("stack.over_limit") if n > cap else ""
            L.append("| **%s (%d/%d)%s** | | | | | | | | | | |%s" % (part, n, cap, over, blank))  # noqa: UP031
            if where == "post" and not post:
                L.append("| — | {} | {} | | | | | | | | |{}".format(WHERE["post"], t("stack.empty"), blank))
            for num, w, o in rows:
                if w != where:
                    continue
                if o is None:
                    L.append("| {} | {} | {} | | | | | | | | |{}".format(num, WHERE[w], t("stack.free"), blank))
                    continue
                pl = (places or {}).get(o["id"]) or {}
                cells = (
                    " {} | {} |".format(_cell(place_cell(pl, "train")), _cell(place_cell(pl, "track")))
                    if places is not None
                    else ""
                )
                L.append(
                    "| {} | {} | {} {} | {} | {} | {} | {} | {} | {} | {} | {} |{}".format(
                        num,
                        WHERE[w],
                        o["id"],
                        _cell(o["title"]),
                        pri.cell(o),
                        _cell(_branch_pr(o)),
                        _cell(
                            post_cell(o, now)
                            if w == "post"
                            else status_cell(o, agents.get(o["id"]) if w == "slot" else None)
                        ),
                        autostart_cell(o, cars),
                        effort_cell(o, self.cfg),
                        ", ".join(o.get("depends_on") or []) or "—",
                        _cell(lock_cell(o.get("lock"))),
                        self.stage_cell(o, recs),
                        cells,
                    )
                )
            if where == "post":  # the merge queue (merge.py), under the post-stack: what rides, its step, what waits
                from . import merge

                L.extend(merge.stack_part(self.state, self.cfg, now, places=places))
        L.append("")
        if self.STAGES:
            L += self.marks_lines(rows, places or {})
        if self.STAGES:
            from .board import summary_line

            L.append(summary_line(self.state, self.cfg))
        if self.POST:
            from . import merge

            L.append(t("stack.legend_post", means=post_means()))
            L.append(merge.legend(self.state))
        L.append(t("stack.legend_lock", means=lock_means()))
        L.append(t("stack.legend_autostart", means=autostart_means()))
        L.append(t("stack.legend_priority", means=pri.means()))
        L.append(stage_legend())
        L.append(effort.legend(self.cfg))
        return "\n".join(L) + "\n"

    def marks_lines(self, rows, places):
        """Under the table: the marks by the principles (marks.py) of the objects in the slots and the post-stack, each
        its car's (a stack object that is no car: its own); nothing when the third check is empty.
        """
        from . import marks

        pl = marks.plist(self.state, self.cfg)
        busy = [(num, o) for num, w, o in rows if o is not None and w in ("slot", "post")]
        if not pl or not busy:
            return []
        cars = {
            c["st"]: c
            for t in (read_json(self.state.path("tracks.json")) or {}).get("tracks") or []
            for c in t.get("cars") or []
            if c.get("st")
        }
        L = [t("stack.marks_head")]
        for num, o in busy:
            ref = (places.get(o["id"]) or {}).get("car")
            L.append(
                "- {} {}{}: {}".format(
                    num, o["id"], f" ({ref})" if ref else "", marks.of(cars.get(o["id"]) or o, pl)["cell"]
                )
            )
        L.append("")
        return L

    def to_json(self):
        places = self.car_places() if self.STAGES else {}
        if self.STAGES:
            from .agents import live_by_object

            agents = live_by_object(self.state, self.cfg, places)
        else:
            agents = {}

        def norm(o):  # an object from before the flag reads as no; one from before the stages, its place's stage
            out = dict({"autostart": False}, **o)
            if self.STAGES:
                pl = places.get(o["id"]) or {}
                out.update(
                    stage=self.stage_of(o),
                    stages=o.get("stages") or [],
                    car=pl.get("car"),
                    track=pl.get("track"),
                    track_title=pl.get("track_title"),
                    train=pl.get("train"),
                    train_title=pl.get("train_title"),
                    agent=agents.get(o["id"]),
                )
            return out

        out = {
            "slots": self.slots_n,
            "queue_max": self.queue_max,
            "in_slots": [norm(o) for o in self.in_slots()],
            "queue": [norm(o) for o in self.queue()],
        }
        if self.POST:
            from . import merge

            out.update(post_max=self.post_max, post=[norm(o) for o in self.posted()], merge=merge.to_json(self.state))
        return out


# The words --ci knows, as a CI state (its word: CI_RU, stored in the run's language; read in every one).
CI_CODE = {
    "green": "green",
    "success": "green",
    "passed": "green",
    "ok": "green",
    "red": "red",
    "failure": "red",
    "failed": "red",
    "pending": "running",
    "running": "running",
    "queued": "running",
}
CI_RU = i18n.Labels("stack.ci", ("green", "red", "running"))
CI_KEYS = tuple("stack.ci." + k for k in CI_RU)  # a stored CI word in any language is shown in the run's
WHERE = i18n.Labels("stack.where", ("slot", "post", "queue"))  # a row's place (Stack.rows), as the table shows it


def ci_record(text):
    """--ci green|red|pending or any words -> {"state": its word in the run's language, or the words, "ts": now}."""
    words = " ".join(str(text or "").split())
    if not words:
        raise StackError("--ci needs a word: green, red, pending, or what the CI said")
    code = CI_CODE.get(words.lower())
    return {"state": CI_RU[code] if code else words, "ts": now_iso()}


def age_ru(iso, now=None):
    try:
        then = datetime.datetime.fromisoformat(iso)
    except (TypeError, ValueError):
        return "?"
    now = now or datetime.datetime.now().astimezone()
    mins = max(0, int((now - then).total_seconds() // 60))
    if mins < 60:
        return t("stack.age_m", m=mins)
    h, m = divmod(mins, 60)
    if h < 24:
        return t("stack.age_hm", h=h, mm="%02d" % m)  # noqa: UP031
    d, h = divmod(h, 24)
    return t("stack.age_dh", d=d, h=h)


def post_cell(o, now=None):
    p = o.get("posted") or {}
    ci = o.get("ci") or {}
    parts = [
        "CI {} ({})".format(
            i18n.retell(ci.get("state"), *CI_KEYS) or "?", (ci.get("ts") or "?")[:16].replace("T", " ")
        ),
        t("stack.post_waits", age=age_ru(p.get("ts"), now)),
    ]
    if o.get("status"):
        parts.append(o["status"])
    if p.get("reason"):
        parts.append(p["reason"])
    return " · ".join(parts)


def effort_cell(o, cfg):
    return effort.cell(effort.item_expected(o, cfg), o.get("spent_mtok"), cfg)


def place_cell(pl, what):
    key = pl.get(what)
    return "{} {}".format(key, pl.get(what + "_title") or "") if key else "—"


def autostart_cell(o, cars=()):
    if o["id"] in cars:
        return t("stack.autostart_car")
    return t("stack.yes") if o.get("autostart") else t("stack.no")


def status_cell(o, agent=None):
    p = o.get("parked")
    s = i18n.retell(o.get("status"), "stack.waiting", Stack.RUNNING_KEY) or "—"
    if agent and i18n.matches(s, Stack.RUNNING_KEY):
        s = "—"
    s += t("stack.parked_note", day=p["ts"][:10], reason=p["reason"]) if p else ""
    if agent:
        from .agents import agent_cell

        return agent_cell(agent) + ("" if s == "—" else " · " + s)
    return s


def lock_cell(lock):
    if not lock:
        return "—"
    return "{} {}: {} (-{})".format(lock["id"], characters.name(lock["by"]), lock["condition"], lock.get("price", "?"))


def _branch_pr(o):
    parts = [x for x in (o.get("branch"), ("PR #{}".format(o["pr"])) if o.get("pr") else None) if x]
    return " / ".join(parts) or "—"


def _cell(text):
    return " ".join(str(text or "—").split()).replace("|", "/")


# --- the ballot queue ----------------------------------------------------------------------------

BALLOT_STATUSES = ("waiting", "presenting", "answered", "dropped")
BALLOT_RU = i18n.Labels("stack.ballot", BALLOT_STATUSES)


class Ballots:
    """The owner's ballot queue: every finished round, in order."""

    def __init__(self, state) -> None:
        self.state = state
        self.path = state.path("ballots.json")
        self.data = read_json(self.path) or {"entries": []}

    def entries(self, open_only=False):
        return [e for e in self.data["entries"] if not open_only or e["status"] in ("waiting", "presenting")]

    def get(self, rid):
        for e in self.data["entries"]:
            if e["round"] == rid:
                return e
        raise StackError(f"{rid} is not in the ballot queue")

    def has(self, rid):
        return any(e["round"] == rid for e in self.data["entries"])

    def _save(self):
        write_json(self.path, self.data)
        self.state.touch(self.path)

    def add(self, rec, points_order, title):
        if self.has(rec["id"]):
            raise StackError("{} is in the ballot queue already".format(rec["id"]))
        e = {
            "round": rec["id"],
            "title": title,
            "kind": rec["kind"],
            "report": os.path.join(self.state.rounds_dir(), rec["id"], "report.md"),
            "created": rec.get("finished") or rec.get("created"),
            "status": "waiting",
            "at_point": None,
            "points_left": list(points_order),
            "answered": [],
            "notes": [],
        }
        self.data["entries"].append(e)
        self._save()
        return e

    def point(self, rid, pid, quiet=False):
        """Mark a point answered. quiet: `decide` calls it; nothing to do is not an error then."""
        e = self.get(rid)
        if pid not in e["points_left"]:
            if quiet:
                return None
            raise StackError(f"{rid} has no open point {pid}")
        e["points_left"].remove(pid)
        e["answered"].append(pid)
        if e["status"] == "waiting":
            e["status"] = "presenting"
        if not e["points_left"]:
            e["status"], e["at_point"] = "answered", None
        self._save()
        return e

    def at(self, rid, pid):
        e = self.get(rid)
        e.update(at_point=pid, status="presenting" if e["status"] == "waiting" else e["status"])
        self._save()
        return e

    def move(self, rid, at):
        e = self.get(rid)
        rest = [x for x in self.data["entries"] if x is not e]
        pos = max(0, min(len(rest), int(at) - 1))
        self.data["entries"] = [*rest[:pos], e, *rest[pos:]]
        self._save()
        return e

    def close(self, rid, status, reason=None):
        e = self.get(rid)
        e["status"] = status
        if reason:
            e["notes"].append(f"{now_iso()[:10]}: {reason}")
        self._save()
        return e

    def md(self, open_only=True, heading=True):
        L = [t("stack.ballots_heading"), ""] if heading else []
        L.append(t("stack.ballots_header"))
        L.append("|---|---|---|---|---|---|")
        n = 0
        for e in self.data["entries"]:
            if open_only and e["status"] not in ("waiting", "presenting"):
                continue
            n += 1
            left = e["points_left"]
            L.append(
                "| %d | %s | %s | %s | %s | %s |"  # noqa: UP031
                % (
                    n,
                    e["round"],
                    _cell(e["title"]),
                    BALLOT_RU.get(e["status"], e["status"]),
                    e.get("at_point") or "—",
                    ("%d: %s" % (len(left), ", ".join(left))) if left else "0",  # noqa: UP031
                )
            )
        if not n:
            L.append("| — | | {} | | | |".format(t("stack.empty")))
        return "\n".join(L) + "\n"


def round_title(rec):
    """A ballot's title: the input's first heading, the PR, or the kind."""
    from .prompts import KIND_RU

    if rec.get("only"):
        rest = dict(rec)
        rest.pop("only")
        return t("stack.title_solo", title=round_title(rest), who=characters.name(rec["only"]))
    if rec.get("brainstorm_of"):
        return t("stack.title_brainstorm", id=rec["brainstorm_of"]["id"])
    if rec.get("pr"):
        return "PR #{} {}".format(rec["pr"], rec.get("pr_title") or "")
    path = rec.get("input")
    if path and os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for line in f:
                if line.startswith("# "):
                    return line[2:].strip()
    return KIND_RU.get(rec["kind"], rec["kind"])
