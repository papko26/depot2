"""The trains: named runs of cars from any tracks, in three states — design, impl, merge — then arrived or scrapped.

A train: its name (a slug, unique: the handle in the CLI), a title, an ordered list of cars (refs tr-NNNN:N, from any
tracks; a car is in at most one live train), its state, its start rules (after another train, not before a time, the
owner's word, a note), its slots (`max N` or `all-free`), notes and a priority. `trains next` answers what a free slot
takes now.

trains.json          {"schema": 5, "next": n, "trains": [...]}
trains-history.jsonl every change
"""

import datetime
import re

from . import effort as eff
from . import gates, i18n, stages
from . import priority as pri
from .stack import _cell
from .store import append_jsonl, now_iso, read_json, write_json
from .tracks import (
    CAR_DONE,
    CAR_RU,
    RE_CAR,
    SCHEMA,
    Tracks,
    counts_text,
    effort_of,
    effort_text,
    post_line,
    stage_legend,
)

STATES = ("design", "impl", "merge", "arrived", "scrapped")
LIVE = ("design", "impl", "merge")
# A train's state and what it means, in the run's language (trains.state.<code>, trains.state_means.<code>).
STATE_RU = i18n.Labels("trains.state", STATES)
STATE_MEANS = i18n.Labels("trains.state_means", STATES)
# The moves a train makes; the forward ones pass their gate (`unmet`), the way back has none.
MOVES = {"design": ("impl", "arrived", "scrapped"), "impl": ("merge", "design", "arrived", "scrapped"),
         "merge": ("impl", "design", "arrived", "scrapped"), "arrived": (), "scrapped": ()}
GATED = ("impl", "merge", "arrived")
# The trains' effort totals: (key, its states, the catalog key of its line under the table).
GROUPS = (("live", LIVE, "trains.effort_live"), ("done", ("arrived", "scrapped"), "trains.effort_done"))


def effort_note():
    """How the trains' effort is counted, in a few words (under the table, the web view's totals)."""
    return i18n.t("trains.effort_note")


GREEN = ("acceptance", "queued", "rolling", "arrived")
RE_NAME = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
RE_ID = re.compile(r"^[a-z]{2}-\d{4}$")
NAME_MAX = 40
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
WEEKDAY_RU = i18n.Labels("trains.weekday", WEEKDAYS)  # by name: WEEKDAY_RU[WEEKDAYS[d.weekday()]]


class TrainError(ValueError):
    pass


def check_name(name):
    n = (name or "").strip()
    if not RE_NAME.match(n) or RE_ID.match(n) or len(n) > NAME_MAX or not re.search(r"[a-z]", n):
        raise TrainError("a train's name is a slug: lower-case latin letters, digits and hyphens, with a letter, up to "  # noqa: UP031
                         "%d (e.g. north-wing), and not like an id (tr-0001); got %r" % (NAME_MAX, name))
    return n


# --- small parsers and formatters ------------------------------------------------------------------

def parse_slots(text):
    if isinstance(text, dict):
        text = "all-free" if text.get("policy") == "all-free" else "max {}".format(text.get("n"))
    t = " ".join(str(text or "").lower().replace("≤", "max ").split())
    if i18n.is_word(t, "in.trains.all_free"):
        return {"policy": "all-free", "n": None}
    m = re.match(r"^(?:max\s*)?(\d+)$", t)
    if not m or int(m.group(1)) < 1:
        raise TrainError(f"a slot policy is `max N` (N ≥ 1) or `all-free`, got {text!r}")
    return {"policy": "max", "n": int(m.group(1))}


def slots_ru(s):
    return i18n.t("trains.slots_all_free") if s["policy"] == "all-free" else "≤ %d" % s["n"]  # noqa: UP031


def parse_state(text):
    """A typed train state: its code, or its name in any language (any case); None when it names no state."""
    typed = i18n.fold(text)
    for s in STATES:
        if typed == s or any(typed == i18n.fold(f) for f in i18n.forms("trains.state." + s)):
            return s
    return None


def parse_when(text):
    t = str(text).strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        raise TrainError(f"not a date-time: {text!r} (want 2031-04-08 or 2031-04-08T15:00)")
    if d.tzinfo is None:
        d = d.astimezone()  # local time
    return d


def when_iso(text):
    return None if text in (None, "", "none", "-") else parse_when(text).isoformat(timespec="minutes")


def fmt_when(iso):
    d = parse_when(iso)
    s = "{} {}".format(WEEKDAY_RU[WEEKDAYS[d.weekday()]], d.strftime("%Y-%m-%d"))
    return s + (d.strftime(" %H:%M") if (d.hour, d.minute) != (0, 0) else "")


def _now(now):
    return now or datetime.datetime.now().astimezone()


class Trains:
    def __init__(self, state, cfg, tracks=None):
        self.state = state
        self.cfg = cfg
        self.path = state.path("trains.json")
        self.history_path = state.path("trains-history.jsonl")
        self.data = read_json(self.path) or {"schema": SCHEMA, "trains": []}
        self.tracks = tracks if tracks is not None else Tracks(state, cfg, trains=self)
        self._of = None  # {car ref: its live train}; `_save` drops it

    @property
    def stack(self):
        return self.tracks.stack

    # --- reading ---------------------------------------------------------------------------------

    def trains(self, include_done=True):
        return [t for t in self.data["trains"] if include_done or t["state"] in LIVE]

    def find(self, name):
        for t in self.data["trains"]:
            if t["name"] == name:
                return t
        return None

    def get(self, name):
        t = self.find(name)
        if t is None:
            raise TrainError(f"no train {name}")
        return t

    def of_car(self, ref):
        """The live train a car (tr-NNNN:N) is in, or None."""
        if self._of is None:
            self._of = {r: t for t in self.trains(include_done=False) for r in t["cars"]}
        return self._of.get(ref)

    def cars_of(self, tn):
        """[(ref, track, car)] in the train's order."""
        out = []
        for ref in tn["cars"]:
            t, c = self.tracks.car(ref)
            out.append((ref, t, c))
        return out

    def open_cars(self, tn):
        return [(r, t, c) for r, t, c in self.cars_of(tn) if c["status"] not in CAR_DONE]

    def history(self):
        from .store import read_jsonl
        return read_jsonl(self.history_path)

    # --- the gates and the slots -----------------------------------------------------------------

    def start_blockers(self, tn, now=None):
        out = []
        st = tn["start"]
        if st.get("not_before") and _now(now) < parse_when(st["not_before"]):
            out.append(i18n.t("trains.not_before", when=fmt_when(st["not_before"])))
        for r in st.get("after") or []:
            met, text = self.tracks.ref_state(r)
            if not met:
                out.append(i18n.t("trains.after", text=text))
        return out

    def unmet(self, tn, to, now=None):
        """{gate: [what is missing]} for the move to `to` (an empty list: passed)."""
        open_ = self.open_cars(tn)
        if to == "impl":
            dor = []
            for ref, t, c in open_:
                miss = self.tracks.dor_of(t, c)
                if miss:
                    dor.append("{}: {}".format(ref, "; ".join(miss)))
            return {"start": self.start_blockers(tn, now), "DoR": dor}
        if to == "merge":
            out = []
            for ref, t, c in open_:  # noqa: B007
                s = self.tracks.stage_of(c)
                if s not in GREEN:
                    out.append(f"{ref}: {stages.ru(s)}")
                elif not c.get("pr"):
                    out.append(i18n.t("trains.no_pr", ref=ref))
            return {"PR": out}
        if to == "arrived":
            return {"cars": ["{} ({})".format(ref, CAR_RU[c["status"]]) for ref, t, c in open_]}
        return {}

    def next_state(self, tn):
        """The way forward from where the train is, or None."""
        return {"design": "impl", "impl": "merge", "merge": "arrived"}.get(tn["state"])

    def gate_line(self, tn, now=None):
        """The next way forward and what holds it, in words."""
        to = self.next_state(tn)
        if to is None:
            return "—"
        miss = ["{}: {}".format(g, "; ".join(v)) for g, v in self.unmet(tn, to, now).items() if v]
        state = STATE_RU[to]
        if not miss:
            if to == "arrived":
                return i18n.t("trains.gate_all_closed", state=state)
            if to == "impl" and tn["start"].get("owner"):
                return i18n.t("trains.gate_passed_owner", state=state, name=tn["name"], to=to)
            if to == "merge":
                return i18n.t("trains.gate_passed_word", state=state, name=tn["name"], to=to)
            return i18n.t("trains.gate_passed", state=state, name=tn["name"], to=to)
        return i18n.t("trains.gate_waits", state=state, miss="; ".join(miss))

    def busy(self, tn):
        """The train's cars that hold a slot."""
        return [c for t, c in self.tracks.busy_cars([(t, c) for r, t, c in self.cars_of(tn)])]

    def room(self, tn, free):
        """How many slots the train may take now, by its policy, from `free`."""
        if tn["slots"]["policy"] == "all-free":
            return max(0, free)
        return max(0, min(free, tn["slots"]["n"] - len(self.busy(tn))))

    def may_take_slot(self, tn):
        if tn["state"] != "impl":
            raise TrainError(i18n.t("trains.slot_only_impl", name=tn["name"], state=STATE_RU[tn["state"]]))
        if self.room(tn, 10 ** 6) < 1:
            raise TrainError(i18n.t("trains.slots_full", name=tn["name"], busy=len(self.busy(tn)), n=tn["slots"]["n"],
                                    cars=", ".join(c["n"] for c in self.busy(tn))))

    # --- what a free slot takes --------------------------------------------------------------------

    def next(self, now=None):
        """Which cars a free slot takes now, then which stack items; and what waits, and why.

        A repair (claims.py, `kind: repair`) goes out of turn: a free slot takes it first. Then the trains in
        room of its startable cars by their priority (ties: its order; a car its DoR holds waits in `gated`). The free
        slots left go to the stack's queue by priority (ties: its order): not a track's car, not locked, its
        dependencies done, and autostart = yes. `owner_word` lists the other
        startable ones (no autostart, or parked), each with its `stack start`. `upcoming` is what the next freed
        slot would take; `parked` the parked cars, ready to merge when nothing blocks them; `waiting` the live
        """
        stk = self.stack
        tracks = self.tracks
        free = max(0, stk.slots_n - len(stk.in_slots()))
        left = free
        picks, upcoming, waiting, gated, ready = [], [], [], [], []
        cars_in_stack = {c.get("st") for t in tracks.tracks() for c in t["cars"] if c.get("st")}

        def stack_dor(o):
            return gates.dor_unmet(stk, tracks, obj=o, items=[i for i in gates.dor_items(self.cfg) if i != "after"]) \
                if stk.stage_of(o) in stages.PLANNING and not o.get("parked") else []

        repairs, repairs_next = [], []
        queue = pri.ordered(stk.queue())
        for o in queue:
            if o.get("kind") != "repair" or o["id"] in cars_in_stack or o.get("lock") or o.get("parked") or \
                    not o.get("autostart") or any(stk.dep_waiting(d) for d in o.get("depends_on") or []) or stack_dor(o):
                continue  # the loop below says why it waits
            if left > 0:
                repairs.append(o)
                left -= 1
            else:
                repairs_next.append({"stack": o, "command": "depot2 stack start {}".format(o["id"])})
        upcoming += repairs_next
        for tn in pri.ordered(self.trains(include_done=False)):
            if tn["state"] != "impl":
                waiting.append((tn, [STATE_RU[tn["state"]] + ": " + self.gate_line(tn, now)]))
                if tn["state"] == "design" and not any(self.unmet(tn, "impl", now).values()):
                    ready.append({"train": tn, "owner": bool(tn["start"].get("owner")),
                                  "command": "depot2 trains state {} impl".format(tn["name"])})
                continue
            cars = []
            for _ref, t, c in pri.ordered(self.cars_of(tn), lambda x: x[2]):
                if not tracks.startable(t, c):
                    continue
                dor = tracks.dor_of(t, c)  # a car its DoR holds is not taken: it waits for its brief, its answers
                if dor:
                    gated.append({"train": tn, "track": t, "car": c, "dor": dor, "command": tracks.car_command(t, c)})
                else:
                    cars.append((t, c))
            room_now = self.room(tn, left)
            for t, c in cars[:room_now]:
                picks.append({"train": tn, "track": t, "car": c, "command": tracks.car_command(t, c)})
            left -= min(room_now, len(cars))
            policy_room = self.room(tn, 10 ** 6)
            for t, c in cars[room_now:policy_room][:1]:
                upcoming.append({"train": tn, "track": t, "car": c, "command": tracks.car_command(t, c)})
        stack_picks, owner_word = list(repairs), []
        taken = {o["id"] for o in repairs} | {u["stack"]["id"] for u in repairs_next}
        for o in queue:
            if o["id"] in cars_in_stack or o.get("lock") or o["id"] in taken:
                continue
            if any(stk.dep_waiting(d) for d in o.get("depends_on") or []):
                continue
            cmd = "depot2 stack start {}".format(o["id"])
            dor = stack_dor(o)
            if dor and o.get("autostart"):
                gated.append({"stack": o, "dor": dor, "command": cmd})
                continue
            if o.get("parked"):  # its PR waits; only `stack start` by hand brings it back
                owner_word.append({"stack": o, "why": i18n.t("trains.why_parked", reason=o["parked"]["reason"]),
                                   "command": cmd})
            elif not o.get("autostart"):
                owner_word.append({"stack": o, "why": i18n.t("trains.why_owner_word"), "command": cmd})
            elif left > 0:
                stack_picks.append(o)
                left -= 1
            elif len(upcoming) < 3:
                upcoming.append({"stack": o, "command": cmd})
        return {"free": free, "slots": stk.slots_n, "picks": picks, "stack": stack_picks, "repairs": repairs,
                "upcoming": upcoming, "waiting": waiting, "ready": ready, "owner_word": owner_word,
                "parked": tracks.parked_cars(), "post": stk.posted(), "post_max": stk.post_max, "gated": gated}

    # --- writing ---------------------------------------------------------------------------------

    def _save(self, event, name, **fields):
        self._of = None
        self.data["schema"] = SCHEMA
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": now_iso(), "event": event, "train": name}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def check_start_refs(self, refs, tn):
        """A train's start waits for tracks, cars, other trains, stack objects or PRs: never for itself or a car
        of its own."""
        for r in refs or ():
            if r == tn["name"] or r in tn["cars"]:
                raise TrainError(f"a train cannot wait for itself or its own car ({r})")
            self.tracks.ref_state(r)

    def _check_car(self, tn, ref):
        """A car may join a train: it exists, it is open, it is in no other live train, not in this one yet."""
        if not RE_CAR.match(ref or ""):
            raise TrainError(f"a car is named tr-NNNN:<car>, e.g. tr-0001:2; got {ref!r}")
        t, c = self.tracks.car(ref)
        ref = "{}:{}".format(t["id"], c["n"])  # a moved car's old ref: the train keeps its ref now
        if c["status"] in CAR_DONE:
            raise TrainError("{} is {}: a closed car joins no train".format(ref, CAR_RU[c["status"]]))
        if ref in tn["cars"]:
            raise TrainError("{} is in train {} already".format(ref, tn["name"]))
        other = self.of_car(ref)
        if other is not None and other is not tn:
            raise TrainError("{} is in train {} already: a car rides one train (`depot2 trains car drop {} {}`)".format(ref, other["name"], other["name"], ref))
        return ref

    def add(self, name, title, cars=(), at=None, after=(), not_before=None, owner=False, note=None, slots=None,
            est_mtok=None, by="trains add", priority=None):
        name = check_name(name)
        if self.find(name) is not None:
            raise TrainError(f"the name {name} is taken")
        if not (title or "").strip():
            raise TrainError("a train needs a title")
        now = now_iso()
        tn = {"name": name, "title": title.strip(), "state": "design", "cars": [],
              "start": {"after": list(after or ()), "not_before": when_iso(not_before), "owner": bool(owner),
                        "note": (note or "").strip() or None},
              "slots": parse_slots(slots or self.cfg["trains"]["default_slots"]), "est_mtok": eff.parse_mtok(est_mtok),
              "notes": [], "added": now, "started": None, "finished": None,
              "states": [{"state": "design", "at": now, "by": by}],
              "priority": pri.DEFAULT if priority is None else pri.parse(priority)}
        for ref in cars or ():
            tn["cars"].append(self._check_car(tn, ref))
        self.check_start_refs(tn["start"]["after"], tn)
        self._place(tn, at)
        self._save("add", name, title=tn["title"], cars=tn["cars"] or None, at=self.data["trains"].index(tn) + 1)
        return tn

    def _place(self, tn, at):
        rest = [x for x in self.data["trains"] if x is not tn]
        pos = len(rest) if at is None else max(0, min(len(rest), int(at) - 1))
        self.data["trains"] = rest[:pos] + [tn] + rest[pos:]

    def set(self, name, new_name=None, title=None, after=None, no_after=False, not_before=None, owner=None, note=None,
            slots=None, est_mtok=None, clear_est=False, at=None, add_note=None, priority=None):
        """new_name: the train's new name; the other trains' start rules that name it follow. priority: 1–10 — a free
        slot asks the trains by it (`next`)."""
        tn = self.get(name)
        changed = {}
        if new_name is not None and new_name != name:
            new_name = check_name(new_name)
            if self.find(new_name) is not None:
                raise TrainError(f"the name {new_name} is taken")
            tn["name"] = changed["new_name"] = new_name
            for o in self.data["trains"]:
                o["start"]["after"] = [new_name if r == name else r for r in o["start"].get("after") or []]
        if title is not None:
            tn["title"] = changed["title"] = title.strip()
        if no_after:
            tn["start"]["after"] = []
            changed["after"] = []
        if after:
            self.check_start_refs(after, tn)
            tn["start"]["after"] = changed["after"] = list(dict.fromkeys(tn["start"]["after"] + list(after)))
        if not_before is not None:
            tn["start"]["not_before"] = changed["not_before"] = when_iso(not_before)
        if owner is not None:
            tn["start"]["owner"] = changed["owner"] = bool(owner)
        if note is not None:
            tn["start"]["note"] = changed["note"] = note.strip() or None
        if slots is not None:
            tn["slots"] = changed["slots"] = parse_slots(slots)
        if clear_est:
            tn["est_mtok"] = changed["est_mtok"] = None
        elif est_mtok is not None:
            tn["est_mtok"] = changed["est_mtok"] = eff.parse_mtok(est_mtok)
        if add_note:
            tn["notes"].append(f"{now_iso()[:10]}: {add_note}")
            changed["note_added"] = add_note
        if priority is not None:
            tn["priority"] = changed["priority"] = pri.parse(priority)
        if at is not None:
            self._place(tn, at)
            changed["at"] = self.data["trains"].index(tn) + 1
        if not changed:
            raise TrainError("nothing to set")
        self._save("set", name, **changed)
        return tn

    def _state_row(self, tn, to, by, why=None, forced=None):
        now = now_iso()
        tn["state"] = to
        if to == "impl" and not tn.get("started"):
            tn["started"] = now
        if to in ("arrived", "scrapped"):
            tn["finished"] = now
        row = {"state": to, "at": now, "by": by}
        if why:
            row["why"] = why
        if forced:
            row.update(forced=True, gates=forced)
        tn["states"].append(row)
        return row

    def set_state(self, name, to, by="trains state", why=None, force=False, word=None, sh=None, now=None):
        tn = self.get(name)
        was = tn["state"]
        if to not in STATES:
            raise TrainError("a train's state is one of {}".format(", ".join(STATES)))
        if to == was:
            raise TrainError(f"{name} is in «{STATE_RU[to]}» already")
        if to not in MOVES[was]:
            raise TrainError("{}: «{}» → «{}» is no move (from «{}»: {})".format(
                name, STATE_RU[was], STATE_RU[to], STATE_RU[was], ", ".join(STATE_RU[m] for m in MOVES[was]) or "—"))
        if to == "scrapped" and not (why or "").strip():
            raise TrainError(i18n.t("trains.scrap_needs_why"))
        forced = None
        if to in GATED:
            forced = gates.check(self.unmet(tn, to, now), force, why, name, STATE_RU[to],
                                 f"depot2 trains state {name} {to}", error=TrainError)
        lines = []
        if to == "merge":
            if not (word or "").strip():
                raise TrainError(i18n.t("trains.merge_needs_word"))
            lines += self._enqueue(tn, word, sh)
        self._state_row(tn, to, by, why, forced)
        self._save("state", tn["name"], was=was, state=to, why=why, forced=forced or None, word=word)
        if forced:
            lines.append(i18n.t("trains.state_moved_forced", name=tn["name"], was=STATE_RU[was], to=STATE_RU[to],
                                gates=gates.gates_text(forced)))
        else:
            lines.append(i18n.t("trains.state_moved", name=tn["name"], was=STATE_RU[was], to=STATE_RU[to]))
        return lines

    def _enqueue(self, tn, word, sh=None):
        from . import merge
        plan = self._merge_plan(tn, word, sh)
        out, added = [], []
        try:
            for ref, n, kept in plan:
                if kept:
                    out.append(i18n.t("trains.pr_queued_already", ref=ref, n=n))
                    continue
                try:
                    e, lines = merge.add(self.state, self.cfg, n, word, sh=sh)
                except Exception as x:
                    back = self._take_back(tn, added, n)
                    if not isinstance(x, ValueError):
                        raise
                    raise TrainError(i18n.t("trains.enqueue_failed", name=tn["name"], ref=ref, said=_pr_said(n, x),
                                            back="\n".join(back), state=STATE_RU[tn["state"]]))
                added.append((ref, e))
                out += [f"{ref}: {x}" for x in lines[:1]]
        finally:
            self.tracks = Tracks(self.state, self.cfg, trains=self)  # merge.add moved the cars' stages: read them again
        return out

    def _merge_plan(self, tn, word, sh=None):
        """[(ref, PR, kept: in the queue already)] in the train's order, once every open car passed: it has a PR, no
        other car of the train has it, `merge.check` passes it (gh read as `merge add` reads it). Any refusal: one
        TrainError, each car with what to do; nothing is written."""
        from . import merge
        queued = {e["pr"] for e in merge.MergeQueue(self.state).entries(open_only=True)}
        plan, bad, seen = [], [], {}
        for ref, _t, c in self.open_cars(tn):
            n = c.get("pr")
            off = i18n.t("trains.off_hint", name=tn["name"], ref=ref)
            if not n:
                bad.append(i18n.t("trains.plan_no_pr", ref=ref, off=off))
            elif n in seen:
                bad.append(i18n.t("trains.plan_same_pr", ref=ref, n=n, other=seen[n], off=off))
            elif n in queued:
                plan.append((ref, n, True))
            else:
                try:
                    merge.check(self.state, self.cfg, n, word, sh=sh)
                    plan.append((ref, n, False))
                except ValueError as x:  # MergeError, and a car's or an object's own (TrackError, StackError)
                    bad.append(i18n.t("trains.plan_refused", ref=ref, said=_pr_said(n, x), off=off))
            if n:
                seen.setdefault(n, ref)
        if bad:
            raise TrainError(i18n.t("trains.plan_not_ready", name=tn["name"], bad=len(bad), all=len(bad) + len(plan),
                                    list="\n".join("- " + b for b in bad)))
        return plan

    def _take_back(self, tn, added, n):
        """The PRs this call queued leave the queue again (`merge drop`, recorded): → the lines saying so."""
        from . import merge
        why = i18n.t("trains.take_back_why", name=tn["name"], n=n)
        if not added:
            return [i18n.t("trains.take_back_nothing")]
        out = [i18n.t("trains.take_back_head", why=why)]
        for ref, e in reversed(added):
            try:
                merge.drop(self.state, self.cfg, e["id"], why)
                out.append(i18n.t("trains.take_back_dropped", ref=ref, id=e["id"], pr=e["pr"]))
            except ValueError as x:
                out.append(i18n.t("trains.take_back_failed", ref=ref, id=e["id"], pr=e["pr"], err=x, why=why))
        return out

    def done(self, name, why=None, force=False):
        return self.set_state(name, "arrived", by="trains done", why=why, force=force)

    def drop(self, name, why):
        return self.set_state(name, "scrapped", by="trains drop", why=why)

    def _live(self, name, what="it takes no cars"):
        tn = self.get(name)
        if tn["state"] not in LIVE:
            raise TrainError("{} is {}: {}".format(name, STATE_RU[tn["state"]], what))
        return tn

    def car_add(self, name, ref, at=None):
        tn = self._live(name)
        ref = self._check_car(tn, ref)
        tn["cars"].append(ref)
        if at is not None:
            self._place_car(tn, ref, at)
        self._save("car_add", name, car=ref, at=tn["cars"].index(ref) + 1)
        return tn

    def _place_car(self, tn, ref, at):
        rest = [x for x in tn["cars"] if x != ref]
        pos = len(rest) if at is None else max(0, min(len(rest), int(at) - 1))
        tn["cars"] = rest[:pos] + [ref] + rest[pos:]

    def _of_train(self, tn, ref):
        """A car's ref in a train's list: as given, or a moved car's old ref read as its new one."""
        if ref not in tn["cars"] and RE_CAR.match(ref or ""):
            try:
                ref = self.tracks.canon(ref)
            except ValueError:
                pass
        if ref not in tn["cars"]:
            raise TrainError("{} has no car {}".format(tn["name"], ref))
        return ref

    def car_move(self, name, ref, at):
        tn = self._live(name, "its cars stay as they were")
        ref = self._of_train(tn, ref)
        self._place_car(tn, ref, at)
        self._save("car_move", name, car=ref, at=tn["cars"].index(ref) + 1)
        return tn

    def car_drop(self, name, ref, why=None):
        """The car leaves the train (it stays on its track as it is)."""
        tn = self._live(name, "its cars stay as they were")
        ref = self._of_train(tn, ref)
        tn["cars"] = [x for x in tn["cars"] if x != ref]
        self._save("car_drop", name, car=ref, why=why)
        return tn

    # --- what a car's move means for its train (tracks.py calls these) -------------------------------

    def car_renamed(self, old, new, by):
        out = []
        for tn in self.data["trains"]:
            start = tn.get("start") or {}
            cars = [new if r == old else r for r in tn["cars"]]
            after = [new if r == old else r for r in start.get("after") or []]
            if cars == tn["cars"] and after == (start.get("after") or []):
                continue
            if new in cars:
                out.append(i18n.t("trains.renamed_car", name=tn["name"], old=old, new=new, pos=cars.index(new) + 1))
            if after != (start.get("after") or []):
                out.append(i18n.t("trains.renamed_after", name=tn["name"], old=old, new=new))
            tn["cars"] = cars
            if "after" in start:
                start["after"] = after
            self._save("car_renamed", tn["name"], car=old, to=new, by=by)
        return out

    def car_started(self, ref):
        tn = self.of_car(ref)
        if tn is None:
            return []
        out = []
        if tn["state"] != "impl":
            out.append(i18n.t("trains.taken_by_hand", name=tn["name"], state=STATE_RU[tn["state"]], ref=ref))
        if tn["slots"]["policy"] == "max" and len(self.busy(tn)) > tn["slots"]["n"]:
            out.append(i18n.t("trains.over_slots", name=tn["name"], busy=len(self.busy(tn)), n=tn["slots"]["n"]))
        return out

    def car_closed(self, ref, by):
        """A car was merged or dropped: when it was its live train's last open car, the train arrives (or, with no car
        merged, it is scrapped) by itself."""
        tn = self.of_car(ref)
        if tn is None or self.open_cars(tn):
            return []
        merged = any(c["status"] == "merged" for r, t, c in self.cars_of(tn))
        to = "arrived" if merged else "scrapped"
        was = tn["state"]
        why = i18n.t("trains.last_car_closed", ref=ref)
        self._state_row(tn, to, by, why)
        self._save("state", tn["name"], was=was, state=to, why=why)
        return [i18n.t("trains.closed_line", name=tn["name"], state=STATE_RU[to], why=why)]

    # --- the tables ------------------------------------------------------------------------------

    def effort_parts(self, tn):
        """A train's (expected, spent, unpriced): `effort_of` over its cars (its own estimate first), and its cars with
        no estimate and no spend, which neither sum holds (none when the train has its own estimate)."""
        cars = [c for r, t, c in self.cars_of(tn)]
        exp, spent, _ = effort_of(self.tracks, cars, tn.get("est_mtok"))
        unpriced = 0 if tn.get("est_mtok") is not None else sum(
            1 for c in cars if c["status"] != "dropped" and self.tracks.car_est(c) is None and c.get("spent_mtok") is None)
        return exp, spent, unpriced

    def effort_cell(self, tn):
        return effort_text(self.tracks, [c for r, t, c in self.cars_of(tn)], tn.get("est_mtok")) + \
            _unpriced(self.effort_parts(tn)[2])

    def effort_total(self, trains):
        """A group of trains' price: the expected summed over the trains that have one (a train's own range adds as a
        range), the spent over those that have it, their cars with neither; `text` as a train's cell prints it."""
        exp = spent = None
        unpriced = 0
        for tn in trains:
            e, s, u = self.effort_parts(tn)
            exp, spent, unpriced = _add(exp, e), _add(spent, s), unpriced + u
        return {"trains": len(trains), "expected": exp, "spent": spent, "unpriced": unpriced,
                "text": eff.cell(exp, spent, self.cfg) + _unpriced(unpriced)}

    def effort_groups(self):
        return {key: self.effort_total([t for t in self.data["trains"] if t["state"] in states])
                for key, states, _ in GROUPS}

    def effort_lines(self, include_done=False):
        """The tab's totals, as `trains` prints them under its table: the live trains, and the done with --all."""
        g = self.effort_groups()
        return [i18n.t(line, n=g[key]["trains"], note=effort_note(), text=g[key]["text"])
                for key, states, line in GROUPS if key == "live" or (include_done and g[key]["trains"])]

    def start_cell(self, tn, now=None):
        st = tn["start"]
        parts = []
        if tn["state"] in ("impl", "merge"):
            parts.append(i18n.t("trains.since", date=(tn.get("started") or "?")[:10]))
        elif tn["state"] in ("arrived", "scrapped"):
            parts.append("{} {}".format(STATE_RU[tn["state"]], (tn.get("finished") or "?")[:10]))
        else:
            if st.get("not_before"):
                parts.append(i18n.t("trains.not_before", when=fmt_when(st["not_before"])))
            for r in st.get("after") or []:
                parts.append(i18n.t("trains.after", text=self.tracks.ref_state(r)[1]))
            if st.get("owner"):
                parts.append(i18n.t("trains.owner_word"))
        s = "; ".join(parts) or i18n.t("trains.at_once")
        if st.get("note"):
            s += " ({})".format(st["note"])
        return s

    def status_cell(self, tn, now=None):
        s = STATE_RU[tn["state"]]
        if tn["state"] == "impl":
            busy = self.busy(tn)
            if busy:
                s = i18n.t("trains.status_busy", state=s, cars=", ".join(c["n"] for c in busy))
        return s

    def md(self, include_done=False, now=None, heading=True):
        L = [i18n.t("trains.md_heading"), ""] if heading else []
        L.append(i18n.t("trains.md_header"))
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        rows = self.trains(include_done)
        for i, tn in enumerate(rows, 1):
            cars = [c for r, t, c in self.cars_of(tn)]
            L.append("| %d | %s «%s» | %s | %s | %s | %s | %s | %s | %s | %s |" % (  # noqa: UP031
                i, tn["name"], _cell(tn["title"]), pri.cell(tn), _cell(self.status_cell(tn, now)),
                _cell(self.start_cell(tn, now)),
                counts_text(cars), slots_ru(tn["slots"]), self.effort_cell(tn), _cell(self.gate_line(tn, now)),
                stages.counts_cell(stages.counts(self.tracks.stage_of(c) for c in cars))))
        if not rows:
            L.append(i18n.t("trains.md_empty"))
        L.append("")
        L += self.effort_lines(include_done)
        L.append("")
        L += self.next_lines(now)
        L.append("")
        L.append(states_legend())
        L.append(eff.legend(self.cfg))
        return "\n".join(L) + "\n"

    def next_lines(self, now=None, train=None):
        nx = self.next(now)
        tracks = self.tracks
        L = [i18n.t("trains.free_slots", free=nx["free"], slots=nx["slots"])]
        reps = [] if train is not None else nx["repairs"]
        if reps:  # out of turn: before the trains (claims.py)
            L.append(i18n.t("trains.repairs", list="; ".join("{} «{}» — `depot2 stack start {}`".format(
                o["id"], o["title"], o["id"]) for o in reps)))
        picks = [p for p in nx["picks"] if train is None or p["train"] is train]
        if picks:
            L.append(i18n.t("trains.can_now", list="; ".join(
                i18n.t("trains.pick_item", track=p["track"]["id"], n=p["car"]["n"], title=p["car"]["title"],
                       train=p["train"]["name"], tp=pri.cell(p["train"]), cp=pri.cell(p["car"]), cmd=p["command"])
                for p in picks)))
        stack_picks = [] if train is not None else [o for o in nx["stack"] if o not in reps]
        if stack_picks:
            L.append(i18n.t("trains.then_stack", list="; ".join("{} «{}» ({}) — `depot2 stack start {}`".format(
                o["id"], o["title"], pri.cell(o), o["id"]) for o in stack_picks)))
        if not picks and not stack_picks and not reps:
            L.append(i18n.t("trains.can_now_nothing") if nx["free"] else i18n.t("trains.can_now_nothing_full"))
        up = [u for u in nx["upcoming"] if train is None or u.get("train") is train]
        if up:
            L.append(i18n.t("trains.when_free", list="; ".join(
                ("{}:{} «{}»".format(u["track"]["id"], u["car"]["n"], u["car"]["title"])) if u.get("car") else
                ("{} «{}»".format(u["stack"]["id"], u["stack"]["title"])) for u in up)))
        ready = [r for r in nx["ready"] if train is None or r["train"] is train]
        if ready:
            L.append(i18n.t("trains.ready_impl", list="; ".join(
                i18n.t("trains.ready_item_owner", name=r["train"]["name"], cmd=r["command"]) if r["owner"]
                else "{} — `{}`".format(r["train"]["name"], r["command"]) for r in ready)))
        mine = {r for r in (train["cars"] if train is not None else ())}
        parked = [p for p in nx["parked"] if train is None or "{}:{}".format(p[0]["id"], p[1]["n"]) in mine]
        ok = [tracks.parked_line(*p) for p in parked if not p[2]]
        if ok:
            L.append(i18n.t("trains.ready_merge", list="; ".join(ok)))
        held = [tracks.parked_line(*p) for p in parked if p[2]]
        if held:
            L.append(i18n.t("tracks.parked_list", list="; ".join(held)))
        post = nx["post"] if train is None else [o for o in nx["post"] if tracks.car_ref(o["id"]) in mine]
        if post:
            L.append(post_line(post, nx["post_max"], tracks.car_by_stack, now))
        gated = [g for g in nx["gated"] if train is None or g.get("train") is train]
        if gated:
            L.append(i18n.t("trains.gated", list="; ".join(
                i18n.t("trains.gated_item",
                       ref=("{}:{}".format(g["track"]["id"], g["car"]["n"])) if g.get("car") else g["stack"]["id"],
                       title=(g.get("car") or g.get("stack"))["title"], dor=", ".join(g["dor"]), cmd=g["command"])
                for g in gated)))
        if train is None and nx["owner_word"]:
            L.append(i18n.t("trains.owner_word_head"))
            L += ["- {} «{}» — {}: `{}`".format(w["stack"]["id"], w["stack"]["title"], w["why"], w["command"])
                  for w in nx["owner_word"]]
        L.append(i18n.t("trains.order"))
        return L

    def car_md(self, name, now=None):
        """One train: its cars in order, each with its track, its status and its stage."""
        tn = self.get(name)
        L = [i18n.t("trains.car_md_heading", name=tn["name"], title=tn["title"]), ""]
        L.append(i18n.t("trains.car_md_state", state=self.status_cell(tn, now), p=pri.cell(tn),
                        start=self.start_cell(tn, now), slots=slots_ru(tn["slots"]), effort=self.effort_cell(tn),
                        next=self.gate_line(tn, now)))
        L.append("")
        L.append(i18n.t("trains.car_md_header"))
        L.append("|---|---|---|---|---|---|---|---|---|")
        for i, (ref, t, c) in enumerate(self.cars_of(tn), 1):
            bp = " / ".join(x for x in (c.get("branch"), ("PR #{}".format(c["pr"])) if c.get("pr") else None) if x) or "—"
            L.append("| %d | %s | %s | %s %s | %s | %s | %s | %s | %s |" % (  # noqa: UP031
                i, ref, pri.cell(c), t["id"], _cell(t["title"]), _cell(c["title"]), _cell(bp), c.get("st") or "—",
                _cell(self.tracks.car_status_cell(t, c)), stages.ru(self.tracks.stage_of(c))))
        if not tn["cars"]:
            L.append(i18n.t("trains.car_md_empty"))
        L.append("")
        L += self.tracks.marks_lines([(ref, c) for ref, t, c in self.cars_of(tn)])
        if tn["state"] in LIVE:
            L += self.next_lines(now, train=tn)
        L.append(stage_legend())
        L.append(states_legend())
        for n in tn.get("notes") or []:
            L.append(f"- {n}")
        return "\n".join(L) + "\n"

    def train_json(self, tn, now=None):
        """A train as data: its cars in order (each as the tracks' `car_json`), its state and what the way on waits
        for, as `--md` prints them."""
        cars = [self.tracks.car_json(t, c) for r, t, c in self.cars_of(tn)]
        exp, spent, unpriced = self.effort_parts(tn)
        to = self.next_state(tn)
        return dict(tn, state_ru=STATE_RU[tn["state"]], status_ru=self.status_cell(tn, now),
                    start_ru=self.start_cell(tn, now), next_state=to, gate_ru=self.gate_line(tn, now),
                    gate={"to": to, "unmet": self.unmet(tn, to, now)} if to else None,
                    effort={"expected": exp, "spent": spent, "unpriced": unpriced, "text": self.effort_cell(tn)},
                    cars=cars)

    def to_json(self, now=None):
        nx = self.next(now)

        def car(p):
            return "{}:{}".format(p["track"]["id"], p["car"]["n"])
        return {"v": 1, "states": [{"state": s, "ru": STATE_RU[s], "means": STATE_MEANS[s]} for s in STATES],
                "trains": [self.train_json(t, now) for t in self.data["trains"]],
                "effort": dict(self.effort_groups(), note=effort_note()),
                "next": {"free": nx["free"], "slots": nx["slots"],
                         "picks": [{"car": car(p), "train": p["train"]["name"], "command": p["command"]}
                                   for p in nx["picks"]],
                         "stack": [o["id"] for o in nx["stack"]], "repairs": [o["id"] for o in nx["repairs"]],
                         "owner_word": [{"id": w["stack"]["id"], "why": w["why"], "command": w["command"]}
                                        for w in nx["owner_word"]],
                         "parked": [{"car": "{}:{}".format(t["id"], c["n"]), "pr": c.get("pr"), "ready": not b,
                                     "waits": b} for t, c, b in nx["parked"]],
                         "post": [{"id": o["id"], "pr": o.get("pr"), "ci": o.get("ci"),
                                   "since": (o.get("posted") or {}).get("ts"), "car": self.tracks.car_ref(o["id"])}
                                  for o in nx["post"]],
                         "post_max": nx["post_max"],
                         "gated": [{"car": car(g) if g.get("car") else None, "train": g["train"]["name"] if g.get("train")
                                    else None, "id": g["stack"]["id"] if g.get("stack") else None, "dor": g["dor"],
                                    "command": g["command"]} for g in nx["gated"]],
                         "ready": [{"train": r["train"]["name"], "owner": r["owner"], "command": r["command"]}
                                   for r in nx["ready"]],
                         "waiting": [{"train": t["name"], "why": why} for t, why in nx["waiting"]]}}


def _pr_said(n, x):
    text = str(x).replace("\n", "\n  ")
    return text if f"#{n}" in text else f"PR #{n}: {text}"


def _add(a, b):
    """Two efforts in MTok summed; None is no figure; a range [lo, hi] (a train's own estimate) adds as a range."""
    if a is None or b is None:
        return b if a is None else a
    if isinstance(a, (list, tuple)) or isinstance(b, (list, tuple)):
        lo, hi = ((x[0], x[1]) if isinstance(x, (list, tuple)) else (x, x) for x in (a, b))
        return [round(lo[0] + hi[0], 4), round(lo[1] + hi[1], 4)]
    return round(a + b, 4)


def _unpriced(n):
    return "; " + i18n.t("trains.unpriced", n=n) if n else ""


def states_legend():
    """A train's route of states and its gates, one line (the trains' tables)."""
    return i18n.t("trains.states_legend")


def freed_slot_lines(state, cfg):
    nx = Trains(state, cfg).next()
    picks = [i18n.t("trains.freed_repair", id=o["id"]) for o in nx["repairs"]] + \
        [i18n.t("trains.freed_pick", track=p["track"]["id"], n=p["car"]["n"], train=p["train"]["name"],
                tp=pri.cell(p["train"]), cp=pri.cell(p["car"]), cmd=p["command"])
         for p in nx["picks"]] + \
        ["{} ({}) — `depot2 stack start {}`".format(o["id"], pri.cell(o), o["id"]) for o in nx["stack"]
         if o not in nx["repairs"]]
    if picks:
        return [i18n.t("trains.freed_takes", list="; ".join(picks))]
    word = [w["stack"]["id"] for w in nx["owner_word"]]
    if word:
        return [i18n.t("trains.freed_nothing_word", ids=", ".join(word))]
    return [i18n.t("trains.freed_nothing")]
