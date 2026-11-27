"""The tracks and their cars, as files in the depot.

A track is a theme: its id `tr-NNNN`, its title, its notes and its cars (a design doc, a PR, a measurement), each an
executor's task in the stack while it runs. The cars' refs are `tr-NNNN:N`. The planning of what runs when is the
trains' (trains.py).

tracks.json          {"schema": 5, "next": n, "tracks": [...]}: in the order they are shown
tracks-history.jsonl every change
"""

import hashlib
import json
import os
import re

from . import effort as eff
from . import gates, i18n, stages
from . import priority as pri
from .stack import Stack, _cell
from .store import append_jsonl, now_iso, read_json, write_json

SCHEMA = 5
CAR_STATUSES = ("waiting", "ready", "running", "pr", "merged", "dropped")
CAR_RU = i18n.Labels("tracks.car", CAR_STATUSES)  # a car's status in words, in the run's language
CAR_BUSY = ("running", "pr")       # holds an agent's slot
CAR_DONE = ("merged", "dropped")   # an `after` on it is met
CAR_OPEN = ("waiting", "ready")    # may still start
# A reason read as words: a drop, a merge, not a merge (in.tracks.* — every language's forms at once).
DROP_RE = re.compile(r"(?i)" + i18n.rx("in.tracks.dropped"))
CLOSED_RE = re.compile(r"(?i)\bclosed?\b")
MERGED_RE = re.compile(r"(?i)" + i18n.rx("in.tracks.merged"))
NOT_MERGED_RE = re.compile(r"(?i)" + i18n.rx("in.tracks.not_merged"))


def reads_dropped(why):
    why = why or ""
    if DROP_RE.search(why):
        return True
    return bool(CLOSED_RE.search(why)) and not (MERGED_RE.search(why) and not NOT_MERGED_RE.search(why))


RE_TRACK = re.compile(r"^tr-\d{4}$")
TITLE_IN_REF = 60
RE_CAR = re.compile(r"^(tr-\d{4}):(.+)$")
RE_STACK = re.compile(r"^(st|tm)-\d{4}$")
RE_PR = re.compile(r"^(?:#|pr:?#?|PR:?\s*#?)(\d+)$")


class TrackError(ValueError):
    pass


def clip(title, n=TITLE_IN_REF):
    """A car's title in a line about it, cut."""
    title = title or ""
    return title if len(title) <= n else title[:n - 1] + "…"


# The keys a `tracks import` JSON may carry; any other is refused, so nothing is dropped unseen.
TRACK_KEYS = {"title", "est_pct", "est_mtok", "notes", "cars"}
CAR_KEYS = {"n", "title", "after", "size", "est_pct", "est_mtok", "spent_mtok", "status", "brief", "branch", "pr",
            "st", "note", "priority"}


def car_notes(c):
    """A car's note lines, oldest first. A car from before kept one string, `note`: its first line."""
    old = c.get("note")
    return ([old] if old else []) + list(c.get("notes") or [])


def _known_keys(obj, allowed, where):
    bad = sorted(set(obj) - allowed)
    if bad:
        raise TrackError("{}: unknown key{} {} (allowed: {})".format(where, "s" if len(bad) > 1 else "", ", ".join(bad),
                                                               ", ".join(sorted(allowed))))


class Tracks:
    def __init__(self, state, cfg, trains=None):
        self.state = state
        self.cfg = cfg
        self.path = state.path("tracks.json")
        self.history_path = state.path("tracks-history.jsonl")
        self.data = read_json(self.path) or {"schema": SCHEMA, "next": 1, "tracks": []}
        self._stack = None
        self._tstack = None
        self._trains = trains
        self._links = None  # {st: (track, car)} from the history; `_save` drops it
        self._plist = None  # the third check (marks.py), read once

    # --- reading ---------------------------------------------------------------------------------

    @property
    def stack(self):
        if self._stack is None:
            self._stack = Stack(self.state, self.cfg)
        return self._stack

    @property
    def tstack(self):
        if self._tstack is None:
            from .tstack import TeamStack
            self._tstack = TeamStack(self.state, self.cfg)
        return self._tstack

    @property
    def trains(self):
        """The trains (trains.py), read once, sharing this object."""
        if self._trains is None:
            from .trains import Trains
            self._trains = Trains(self.state, self.cfg, tracks=self)
        return self._trains

    def tracks(self):
        return list(self.data["tracks"])

    def get(self, tid):
        for t in self.data["tracks"]:
            if t["id"] == tid:
                return t
        raise TrackError(f"no track {tid}")

    def car(self, ref):
        m = RE_CAR.match(ref or "")
        if not m:
            raise TrackError(f"a car is named tr-NNNN:<car>, e.g. tr-0001:2; got {ref!r}")
        seen = set()
        while True:
            t = self.get(m.group(1))
            for c in t["cars"]:
                if c["n"] == m.group(2):
                    return t, c
            p = self.pointer(t, m.group(2))
            if p is None or p["to"] in seen:
                raise TrackError("{} has no car {}".format(t["id"], m.group(2)))
            seen.add(p["to"])
            m = RE_CAR.match(p["to"])

    def pointer(self, t, n):
        """The record a car moved away left on its old place (`moved_out`: {n, to, title, at, by, why}), or None."""
        for p in t.get("moved_out") or ():
            if p["n"] == n:
                return p
        return None

    def canon(self, ref):
        """A car's ref as it stands now (an old ref of a moved car: its new one)."""
        t, c = self.car(ref)
        return "{}:{}".format(t["id"], c["n"])

    def moved_note(self, ref):
        if not RE_CAR.match(ref or ""):
            return None
        now = self.canon(ref)
        return None if now == ref else i18n.t("tracks.moved_note", ref=ref, now=now)

    def taken(self, t, n):
        for c in t["cars"]:
            if c["n"] == n:
                return i18n.t("tracks.taken_car", track=t["id"], n=n, title=clip(c["title"]),
                              status=CAR_RU[c["status"]])
        p = self.pointer(t, n)
        if p is not None:
            return i18n.t("tracks.taken_pointer", track=t["id"], n=n, title=clip(p.get("title")), to=p["to"])
        return None

    def next_n(self, t):
        """The name a new car gets by default: the next number on its track (a moved car's old name counts)."""
        names = [c["n"] for c in t["cars"]] + [p["n"] for p in t.get("moved_out") or ()]
        nums = [int(x) for x in names if x.isdigit()]
        return str(max(nums) + 1 if nums else (0 if not names else len(names)))

    def closed_note(self, t, c):
        if self.stage_of(c) not in stages.TERMINAL:
            return None
        return i18n.t("tracks.closed_note", track=t["id"], n=c["n"], title=clip(c["title"]), status=CAR_RU[c["status"]],
                      stage=stages.ru(self.stage_of(c)))

    def history(self):
        from .store import read_jsonl
        return read_jsonl(self.history_path)

    def car_by_stack(self, oid):
        for t in self.data["tracks"]:
            for c in t["cars"]:
                if c.get("st") == oid:
                    return t, c
        return None, None

    def car_ref(self, oid):
        t, c = self.car_by_stack(oid)
        return "{}:{}".format(t["id"], c["n"]) if c is not None else None

    def car_of_stack(self, oid):
        """The car whose object this is, or was: the live link, else the history's last row that names it
        (a car sent back with `stack done --car back` has no link; stacked again, a new object)."""
        t, c = self.car_by_stack(oid)
        if c is not None:
            return t, c
        if self._links is None:
            cars = {(t["id"], c["n"]): (t, c) for t in self.data["tracks"] for c in t["cars"]}
            self._links = {}
            for h in self.history():
                key = (h.get("track"), h.get("car"))
                if h.get("st") and key in cars:
                    self._links[h["st"]] = cars[key]
        return self._links.get(oid, (None, None))

    @property
    def plist(self):
        """The third check (`Principles.check_list()`, marks.py), read once."""
        if self._plist is None:
            from . import marks
            self._plist = marks.plist(self.state, self.cfg)
        return self._plist

    def verdict(self, c):
        """The verdict on a car's marks by the principles (marks.py)."""
        from . import marks
        return marks.of(c, self.plist)

    def marks_lines(self, cars):
        """Under a track's or a train's table: the cars' marks by the principles — those with marks, and those whose
        PR is open (`pr`: the gates wait for its lines); nothing when the third check is empty. cars: [(label, c)]."""
        if not self.plist:
            return []
        rows = [(n, c) for n, c in cars if c.get("marks") or c["status"] == "pr"]
        if not rows:
            return []
        return [i18n.t("tracks.marks_head")] + \
            ["- {}: {}".format(n, self.verdict(c)["cell"]) for n, c in rows] + [""]

    def train_of(self, ref):
        """The live train a car is in (trains.py), or None."""
        return self.trains.of_car(ref)

    def done(self, t):
        """A track is done when it has cars and every one is merged or dropped (a theme with a new car is open again)."""
        return bool(t["cars"]) and all(c["status"] in CAR_DONE for c in t["cars"])

    # --- stages (stages.py) ----------------------------------------------------------------------

    def track_of(self, c):
        for t in self.data["tracks"]:
            if any(x is c for x in t["cars"]):
                return t
        return None

    def status_stage(self, c):
        """The stage a car's own status says (its object aside)."""
        s = c["status"]
        if s == "dropped":
            return "scrapped", i18n.t("tracks.why_dropped")
        if s == "merged":
            return "arrived", i18n.t("tracks.why_merged_before")
        if s == "running":
            return "build", i18n.t("tracks.why_running")
        if s == "pr":
            return "build", i18n.t("tracks.why_pr_parked" if c.get("parked") else "tracks.why_pr_open", pr=c.get("pr"))
        if c.get("brief"):
            return "sketch", i18n.t("tracks.why_brief")
        return "bogie", i18n.t("tracks.why_no_brief")

    def stage_default_why(self, t, c):
        """The stage a car from before the field has: its live object's, the merge queue's, else its status's."""
        st = c.get("st")
        if st and self.stack._dep_open(st):
            return self.stack.stage_of(self.stack.get(st)), i18n.t("tracks.why_as_object", st=st)
        t = t or self.track_of(c)
        mq = self.stack.mq_index()
        e = mq["car"].get("{}:{}".format(t["id"], c["n"])) if t else None
        if e is None and c.get("pr") and c["status"] in ("pr", "merged"):
            e = mq["pr"].get(c["pr"])
        if e is not None and stages.entry_stage(e):
            return stages.entry_default(e)
        return self.status_stage(c)

    def stage_of(self, c):
        return c.get("stage") or self.stage_default_why(None, c)[0]

    def _car_stage(self, t, c, stage, by, why=None, default=None, forced=False, gates=None):
        """Move a car's stage in memory (the caller saves). Returns (row, the line to print or None)."""
        if not c.get("stage") and default is None:
            default = self.status_stage(c)[0]
        row = stages.record(c, stage, by, why, default=None if c.get("stage") else default, forced=forced, gates=gates)
        return row, stages.say("{}:{}".format(t["id"], c["n"]), row)

    def car_stage(self, ref, stage, by, why=None, forced=False):
        """`car stage` and the merge queue: one car's stage, saved."""
        t, c = self.car(ref)
        row, _ = self._car_stage(t, c, stage, by, why, default=self.stage_default_why(t, c)[0], forced=forced)
        if row:
            self._save("car_stage", t["id"], car=c["n"], stage=stages.hist(row), why=why, by=by)
        return row

    def object_stage(self, oid):
        """A car follows its object: the object's stage as the stack has it now (read fresh), live or left."""
        stk = Stack(self.state, self.cfg)
        if stk._dep_open(oid):
            return stk.stage_of(stk.get(oid))
        return stk.left_stage(oid)

    # --- refs: what `after` names, and whether it is met ----------------------------------------

    def ref_state(self, ref, track=None):
        """(met, text) for a ref; raises TrackError for one that names nothing. track: the track whose car's
        `after` this is (a bare name is a car of it); none: a train's start rule (a bare name is a train)."""
        ref = str(ref).strip()
        if RE_TRACK.match(ref):
            t = self.get(ref)
            open_ = [c for c in t["cars"] if c["status"] not in CAR_DONE]
            title = t["title"] if len(t["title"]) <= TITLE_IN_REF else t["title"][:TITLE_IN_REF - 1] + "…"
            return self.done(t), "{} {} ({})".format(t["id"], title, i18n.t("tracks.done_word") if self.done(t) else
                                                  i18n.t("tracks.open_cars", n=len(open_)) if t["cars"]
                                                  else i18n.t("tracks.no_cars"))
        m = RE_CAR.match(ref)
        if m:
            t, c = self.car(ref)
            now = "{}:{}".format(t["id"], c["n"])
            return c["status"] in CAR_DONE, "{} ({})".format(ref if now == ref else f"{ref} → {now}",
                                                         CAR_RU[c["status"]])
        if RE_STACK.match(ref):
            stk = self.stack if ref.startswith("st-") else self.tstack
            if not stk._known(ref):
                raise TrackError(f"unknown stack object {ref}")
            open_ = stk._dep_open(ref)
            if not open_ and ref.startswith("st-"):  # a car's object: the car is the truth (a parked one is not done)
                t, c = self.car_of_stack(ref)
                if c is not None and c["status"] not in CAR_DONE:
                    return False, i18n.t("tracks.ref_out_of_stack", ref=ref, track=t["id"], n=c["n"],
                                         status=CAR_RU[c["status"]])
            if open_ and ref.startswith("st-") and stk.get(ref)["where"] == "post":
                return False, i18n.t("tracks.ref_in_post", ref=ref)
            return not open_, i18n.t("tracks.ref_in_stack" if open_ else "tracks.ref_left_stack", ref=ref)
        m = RE_PR.match(ref)
        if m:
            n = int(m.group(1))
            merged = self.pr_merged(n)
            post = [o["id"] for o in self.stack.posted() if o.get("pr") == n]
            if merged:
                return merged, i18n.t("tracks.ref_pr_merged", n=n)
            if post:
                return merged, i18n.t("tracks.ref_pr_post", n=n, post=", ".join(post))
            return merged, i18n.t("tracks.ref_pr_not_merged", n=n)
        if track is not None:
            for c in track["cars"]:
                if c["n"] == ref:
                    return c["status"] in CAR_DONE, "{} ({})".format(ref, CAR_RU[c["status"]])
            if self.pointer(track, ref) is not None:  # a car moved away: where it stands now
                t, c = self.car("{}:{}".format(track["id"], ref))
                return c["status"] in CAR_DONE, "{} → {}:{} ({})".format(ref, t["id"], c["n"], CAR_RU[c["status"]])
            raise TrackError("{} has no car {}".format(track["id"], ref))
        tn = self.trains.find(ref)  # a train's start rules only: a car's bare name is a car of its track
        if tn is not None:
            from .trains import STATE_RU
            return tn["state"] == "arrived", i18n.t("tracks.ref_train", name=tn["name"], state=STATE_RU[tn["state"]])
        raise TrackError(f"{ref!r} is not a track, a car (tr-NNNN:N), a train, a stack object or a PR (#N)")

    def car_stack_ids(self):
        """Every stack object that is or was a track's car: the cars' `st` and the history's."""
        ids = {c["st"] for t in self.data["tracks"] for c in t["cars"] if c.get("st")}
        return ids | {h["st"] for h in self.history() if h.get("st")}

    def pr_merged(self, n):
        if any(c.get("pr") == n and c["status"] == "merged" for t in self.data["tracks"] for c in t["cars"]):
            return True
        if any(o.get("pr") == n for o in self.stack.posted()):
            return False
        cars = self.car_stack_ids()
        for h in self.stack.history():
            if h.get("event") != "done" or (h.get("obj") or {}).get("pr") != n or h.get("object") in cars:
                continue
            if h.get("outcome"):
                if h["outcome"] == "merged":
                    return True
                continue
            why = h.get("reason") or ""
            if not reads_dropped(why) and not NOT_MERGED_RE.search(why):
                return True
        return False

    def check_refs(self, refs, track=None, own=None):
        """Every ref must name something. own: the car itself (it may not wait for itself)."""
        me = (own, "{}:{}".format(track["id"], own)) if own is not None and track is not None else ()
        for r in refs or ():
            if r in me:
                raise TrackError(f"a car cannot wait for itself ({r})")
            self.ref_state(r, track)
            if me and self.after_ref(r, track) in me:  # its own old ref, from before a move
                raise TrackError(f"a car cannot wait for itself ({r} is its old ref)")

    # --- a car: what holds it, where its PR waits ------------------------------------------------

    def car_est(self, c):
        return eff.item_expected(c, self.cfg)

    def car_blockers(self, t, c, after=True, lock=True):
        """What holds a car (t: its track). after=False: only the lock (a parked car brought back for work: its
        `after` gates the merge); lock=False: only the `after`."""
        out = []
        for r in (c.get("after") or []) if after else []:
            met, text = self.ref_state(r, t)
            if not met:
                out.append(text)
        if c.get("st") and lock:
            for o in self.stack.queue():
                if o["id"] == c["st"] and o.get("lock"):
                    out.append(i18n.t("tracks.lock_blocker", id=o["lock"]["id"], condition=o["lock"]["condition"]))
        return out

    def parked(self, c):
        """Status `pr` and its stack object in no slot and not in the post-stack: the PR waits on its track,
        it holds no slot."""
        return c["status"] == "pr" and bool(c.get("st")) and not any(
            o["id"] == c["st"] for o in self.stack.in_slots() + self.stack.posted())

    def posted(self, c):
        """Its stack object is in the post-stack: its PR waits for the owner's merge; no slot."""
        return bool(c.get("st")) and any(o["id"] == c["st"] for o in self.stack.posted())

    def busy_cars(self, cars):
        """The cars of [(track, car)] that hold a slot: running, or a PR not parked and not in the post-stack (a car
        with no stack link is counted)."""
        return [(t, c) for t, c in cars if c["status"] in CAR_BUSY and not self.parked(c) and not self.posted(c)]

    def parked_cars(self, track=None):
        """[(track, car, blockers)] for every parked car; no blockers: its PR is ready to merge."""
        out = []
        for t in self.tracks() if track is None else [track]:
            for c in t["cars"]:
                if self.parked(c):
                    out.append((t, c, self.car_blockers(t, c)))
        return out

    def parked_line(self, t, c, blockers):
        pr = ("PR #{}".format(c["pr"])) if c.get("pr") else "PR"
        if blockers:
            return i18n.t("tracks.parked_waits", track=t["id"], n=c["n"], pr=pr, blockers=", ".join(blockers))
        return i18n.t("tracks.parked_ready", track=t["id"], n=c["n"], pr=pr)

    def startable(self, t, c):
        """A car that may go into a slot now: waiting or ready, nothing holds it (its `after`, a lock)."""
        return c["status"] in CAR_OPEN and not self.car_blockers(t, c)

    def car_command(self, t, c):
        if c.get("st") and any(o["id"] == c["st"] for o in self.stack.queue()):
            return "depot2 stack start {}".format(c["st"])
        return "depot2 tracks car stack {}:{} --start".format(t["id"], c["n"])

    # --- writing ---------------------------------------------------------------------------------

    def _save(self, event, tid, **fields):
        self._links = None
        write_json(self.path, self.data)
        self.state.touch(self.path)
        return self._hist(event, tid, **fields)

    def _hist(self, event, tid, **fields):
        """One row of tracks-history.jsonl (`_save` writes tracks.json first)."""
        rec = {"ts": now_iso(), "event": event, "track": tid}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    def _new_track(self, title, est_mtok=None):
        if not (title or "").strip():
            raise TrackError("a track needs a title")
        tid = "tr-%04d" % self.data["next"]  # noqa: UP031
        self.data["next"] += 1
        return {"id": tid, "title": title.strip(), "est_mtok": eff.parse_mtok(est_mtok), "cars": [], "added": now_iso(),
                "notes": []}

    def add(self, title, at=None, est_mtok=None, note=None):
        t = self._new_track(title, est_mtok=est_mtok)
        if note:
            t["notes"].append(f"{now_iso()[:10]}: {note}")
        self._place(t, at)
        self._save("add", t["id"], title=t["title"], at=self.data["tracks"].index(t) + 1)
        return t

    def _place(self, t, at):
        rest = [x for x in self.data["tracks"] if x is not t]
        pos = len(rest) if at is None else max(0, min(len(rest), int(at) - 1))
        self.data["tracks"] = rest[:pos] + [t] + rest[pos:]

    def set(self, tid, title=None, est_mtok=None, clear_est=False, at=None, add_note=None):
        t = self.get(tid)
        changed = {}
        if title is not None:
            t["title"] = changed["title"] = title.strip()
        if clear_est:
            t["est_mtok"] = changed["est_mtok"] = None
        elif est_mtok is not None:
            t["est_mtok"] = changed["est_mtok"] = eff.parse_mtok(est_mtok)
        if add_note:
            t["notes"].append(f"{now_iso()[:10]}: {add_note}")
            changed["note_added"] = add_note
        if at is not None:
            self._place(t, at)
            changed["at"] = self.data["tracks"].index(t) + 1
        if not changed:
            raise TrackError("nothing to set")
        self._save("set", tid, **changed)
        return t

    # --- cars ------------------------------------------------------------------------------------

    def _new_car(self, t, title, n=None, after=(), size=None, est_mtok=None, brief=None, branch=None, pr=None,
                 status="waiting", spent_mtok=None, st=None, note=None, stage=None, by="tracks car add", src=None,
                 priority=None):
        if not (title or "").strip():
            raise TrackError("a car needs a title")
        if status not in CAR_STATUSES:
            raise TrackError("a car's status is one of {}".format(", ".join(CAR_STATUSES)))
        eff.check_size(size, self.cfg)
        n = self.check_n(t, self.next_n(t) if n is None else n)
        e = eff.parse_mtok(est_mtok)
        if isinstance(e, list):
            raise TrackError(f"a car's estimate is one number, got the range {est_mtok!r}")
        if stage is not None and stage not in stages.STAGES:
            raise TrackError(i18n.t("stages.no_stage", stage=stage))
        c = {"n": n, "title": title.strip(), "after": [str(a) for a in after or ()], "size": size, "est_mtok": e,
             "spent_mtok": eff.parse_mtok(spent_mtok),
             "brief": os.path.abspath(os.path.expanduser(brief)) if brief else None,
             "branch": branch, "pr": int(pr) if pr is not None else None, "status": status, "st": st,
             "notes": [(note or "").strip()] if (note or "").strip() else [], "added": now_iso(), "started": None,
             "finished": None, "priority": pri.DEFAULT if priority is None else pri.parse(priority)}
        if src:
            c["from"] = [src]
        stages.start(c, stage or self.status_stage(c)[0], by)
        return c

    def check_n(self, t, n, car=None):
        n = str(n).strip()
        if not n or ":" in n:
            raise TrackError("{}: {!r} is not a car's name (a number, or a name like B2a)".format(t["id"], n))
        busy = self.taken(t, n)
        p = self.pointer(t, n)
        if busy and not (car is not None and p is not None and self._leads_to(p, car)):
            raise TrackError(i18n.t("tracks.name_busy", busy=busy, free=self.next_n(t)))
        return n

    def _leads_to(self, p, car):
        try:
            return self.car(p["to"])[1] is car
        except TrackError:
            return False

    def after_ref(self, r, t):
        """An `after` ref as it is written (t: the track of the car that waits): a moved car's old ref, bare or full,
        becomes its ref now — bare when it stands on t, else full; any other ref as given."""
        r = str(r).strip()
        m = RE_CAR.match(r)
        src, n = (self.get(m.group(1)), m.group(2)) if m else (t, r)
        if src is None or self.pointer(src, n) is None or any(c["n"] == n for c in src["cars"]):
            return r
        nt, nc = self.car("{}:{}".format(src["id"], n))
        return nc["n"] if not m and nt is t else "{}:{}".format(nt["id"], nc["n"])

    def car_add(self, tid, title, at=None, **kw):
        t = self.get(tid)
        c = self._new_car(t, title, **kw)
        self.check_refs(c["after"], t, own=c["n"])
        c["after"] = list(dict.fromkeys(self.after_ref(r, t) for r in c["after"]))
        self._place_car(t, c, at)
        self._save("car_add", tid, car=c["n"], title=c["title"], stage=c["stage"], st=c.get("st"),
                   **{"from": (c.get("from") or [None])[0]})
        return t, c

    def car_took(self, ref, src, note, by="take"):
        t, c = self.car(ref)
        if src not in (c.get("from") or []):
            c["from"] = list(c.get("from") or []) + [src]
        c["notes"] = car_notes(c) + [f"{now_iso()[:10]}: {note.strip()}"]
        c.pop("note", None)
        self._save("car_from", t["id"], car=c["n"], by=by, note_added=note.strip(), **{"from": src})
        return t, c

    def take_object(self, oid, tid, n=None, size=None, title=None, by=None):
        if not RE_TRACK.match(tid or ""):
            raise TrackError(i18n.t("tracks.to_not_track", tid=tid))
        self.get(tid)
        team = oid.startswith("tm-")
        stk = self.tstack if team else self.stack
        by = by or ("tstack take" if team else "stack take")
        if not RE_STACK.match(oid or "") or not stk._known(oid):
            raise TrackError(i18n.t("tracks.no_object_team" if team else "tracks.no_object", oid=oid))
        if not stk._dep_open(oid):
            raise TrackError(i18n.t("tracks.object_left_team" if team else "tracks.object_left", oid=oid))
        o = stk.get(oid)
        est = o.get("est_mtok") if not isinstance(o.get("est_mtok"), list) else None
        kw = {"n": n, "size": size or o.get("size"), "est_mtok": est, "after": list(o.get("depends_on") or ()), "src": oid,
                  "by": by, "note": f"{now_iso()[:10]}: {object_note(o, team)}", "priority": pri.of(o)}
        if team:
            if o["where"] == "slot":
                raise TrackError(i18n.t("tracks.team_in_slot", oid=oid))
            nt, c = self.car_add(tid, title or o["title"], **kw)
            ref = "{}:{}".format(nt["id"], c["n"])
            stk.done(oid, i18n.t("tracks.taken_reason", ref=ref), by=by)
            return nt, c, [i18n.t("tracks.taken_team", oid=oid, ref=ref, title=clip(c["title"]),
                                  stage=stages.ru(c["stage"]))]
        ot, oc = self.car_of_stack(oid)
        if oc is not None:
            raise TrackError(i18n.t("tracks.already_object", oid=oid, track=ot["id"], n=oc["n"]))
        status = {"slot": "running", "post": "pr"}.get(o["where"], "waiting")
        kw.update(brief=o.get("brief"), branch=o.get("branch"), pr=o.get("pr"), spent_mtok=o.get("spent_mtok"),
                  status=status, st=oid, stage=stk.stage_of(o))
        nt, c = self.car_add(tid, title or o["title"], **kw)
        ref = "{}:{}".format(nt["id"], c["n"])
        name = "{} {} — {}".format(ref, nt["title"], c["title"])
        stk.set(oid, title=name)
        return nt, c, [i18n.t("tracks.taken_obj", oid=oid, ref=ref, title=clip(c["title"]), status=CAR_RU[c["status"]],
                              stage=stages.ru(c["stage"]), name=name)]

    def _place_car(self, t, c, at):
        rest = [x for x in t["cars"] if x is not c]
        pos = len(rest) if at is None else max(0, min(len(rest), int(at) - 1))
        t["cars"] = rest[:pos] + [c] + rest[pos:]

    def car_set(self, ref, title=None, after=None, no_after=False, size=None, est_mtok=None, clear_est=False,
                spent_mtok=None, brief=None, branch=None, pr=None, status=None, st=None, note=None, note_replace=None,
                docs=None, priority=None, by="tracks car set", by_hand=False):
        t, c = self.car(ref)
        if by_hand:
            self.hand_checks(t, c, status=status, st=st)
        changed = {}
        self.stage_lines = []
        if title is not None:
            c["title"] = changed["title"] = title.strip()
        if no_after:
            c["after"] = changed["after"] = []
        if after:
            self.check_refs(after, t, own=c["n"])
            c["after"] = changed["after"] = list(dict.fromkeys(c["after"] + [self.after_ref(a, t) for a in after]))
        if size is not None:
            c["size"] = changed["size"] = eff.check_size(size, self.cfg)
        if clear_est:
            c["est_mtok"] = changed["est_mtok"] = None
        elif est_mtok is not None:
            if isinstance(est_mtok, list):
                raise TrackError("a car's estimate is one number")
            c["est_mtok"] = changed["est_mtok"] = float(est_mtok)
        if spent_mtok is not None:
            c["spent_mtok"] = changed["spent_mtok"] = float(spent_mtok)
            if c.get("st") and self.stack._dep_open(c["st"]):  # its stack object shows the same spend
                self.stack.set(c["st"], spent_mtok=float(spent_mtok))
        if brief is not None:
            c["brief"] = changed["brief"] = os.path.abspath(os.path.expanduser(brief))
        if branch is not None:
            c["branch"] = changed["branch"] = branch
        if pr is not None:
            c["pr"] = changed["pr"] = int(pr)
        if (pr is not None or branch is not None) and c.get("st") and self.stack._dep_open(c["st"]):
            self.stack.set(c["st"], pr=int(pr) if pr is not None else None, branch=branch)
        if docs is not None:
            c["docs"] = changed["docs"] = bool(docs)
        if priority is not None:
            c["priority"] = changed["priority"] = pri.parse(priority)
        if st is not None:
            if not self.stack._known(st):
                raise TrackError(f"unknown stack object {st}")
            other_t, other_c = self.car_by_stack(st)
            if other_c is not None and other_c is not c:
                raise TrackError("{} is the stack object of {}:{} already".format(st, other_t["id"], other_c["n"]))
            c["st"] = changed["st"] = st
            if c["status"] in CAR_OPEN:  # what `stack done --car back` returns it to
                c["stacked_from"] = c["status"]
            if c["status"] in CAR_OPEN and any(o["id"] == st for o in self.stack.in_slots()):
                status = status or "running"
        if status is not None:
            if status not in CAR_STATUSES:
                raise TrackError("a car's status is one of {}".format(", ".join(CAR_STATUSES)))
            before = self.stage_of(c)
            self._car_status(c, status)
            changed["status"] = status
            target = status_move(before, status, c)
            if target:
                row, line = self._car_stage(t, c, target, by, note or note_replace, default=before)
                if row:
                    changed["stage"] = stages.hist(row)
                if line:
                    self.stage_lines.append(line)
                if row and target == "rolling" and by_hand:
                    cref = "{}:{}".format(t["id"], c["n"])
                    self.stage_lines.append(i18n.t("tracks.rolling_hint", ref=cref, hint=stages.arrive_hint(cref)))
        if note is not None and note_replace is not None:
            raise TrackError("give --note (appends) or --note-replace, not both")
        if note is not None:
            if not note.strip():
                raise TrackError("an empty --note adds nothing (--note-replace '' clears the notes)")
            c["notes"] = car_notes(c) + [f"{now_iso()[:10]}: {note.strip()}"]
            c.pop("note", None)
            changed["note_added"] = note.strip()
        if note_replace is not None:
            c["notes"] = [f"{now_iso()[:10]}: {note_replace.strip()}"] if note_replace.strip() else []
            c.pop("note", None)
            changed["note_replaced"] = note_replace.strip()
        if not changed:
            raise TrackError("nothing to set")
        if (priority is not None or st is not None) and c.get("st") and self.stack._dep_open(c["st"]) and \
                pri.of(self.stack.get(c["st"])) != pri.of(c):  # its object shows the car's priority (one car)
            self.stack.set(c["st"], priority=pri.of(c))
        self._save("car_set", t["id"], car=c["n"], **changed)
        if changed.get("status") in CAR_DONE:
            self.stage_lines += self.trains.car_closed("{}:{}".format(t["id"], c["n"]), by)
        return t, c

    def hand_checks(self, t, c, status=None, st=None):
        ref = "{}:{}".format(t["id"], c["n"])
        live = c.get("st") if c.get("st") and self.stack._dep_open(c["st"]) else None
        if status is not None and live and status != c["status"]:
            raise TrackError(f"{ref}: its stack object {live} is in the stack, the status goes with it: depot2 stack done {live} "
                             f"--car merged|dropped|back --reason «…», stack start {live} or stack park {live} --reason «…» "
                             "(--status is for a car with no object in the stack)")
        if st is not None and st != c.get("st"):
            if live:
                raise TrackError(f"{ref} is linked to {live}, which is in the stack: done with it first (depot2 stack done {live} "
                                 "--car back --reason «…»), then --st")
            ot, oc = self.car_of_stack(st)
            if oc is not None and oc is not c:
                raise TrackError("{} is or was the stack object of {}:{}: a car links only to an object of its own "
                                 "(a new one: depot2 tracks car stack {})".format(st, ot["id"], oc["n"], ref))

    def _car_status(self, c, status):
        was = c.get("status")
        c["status"] = status
        if status in CAR_BUSY and not c.get("started"):
            c["started"] = now_iso()
        if status not in CAR_DONE:
            c["finished"] = None
        elif was not in CAR_DONE or not c.get("finished"):
            c["finished"] = now_iso()

    def car_move(self, ref, at):
        t, c = self.car(ref)
        self._place_car(t, c, at)
        self._save("car_move", t["id"], car=c["n"], at=t["cars"].index(c) + 1)
        return t, c

    def move_blocker(self, t, c):
        """Why a car may not move to another track or name, or None: it has arrived or rolls out (its PR landed under
        its ref), or its PR is in the merge queue (an open entry names it). A car in a slot, a parked one, one in the
        post-stack or a scrapped one moves."""
        s = self.stage_of(c)
        if s == "rolling":
            return i18n.t("tracks.blocker_rolling")
        if c["status"] == "merged" or s == "arrived":
            return i18n.t("tracks.blocker_arrived")
        mq = self.stack.mq_index()
        e = (mq["car"].get("{}:{}".format(t["id"], c["n"])) or (mq["st"].get(c["st"]) if c.get("st") else None)
             or (mq["pr"].get(c["pr"]) if c.get("pr") else None))
        if e is not None:
            return i18n.t("tracks.blocker_queued", pr=e["pr"], id=e["id"])
        return None

    def car_relocate(self, ref, to=None, n=None, at=None, why=None, by="tracks car move"):
        t, c = self.car(ref)
        old, old_n = "{}:{}".format(t["id"], c["n"]), c["n"]
        nt = self.get(to) if to else t
        new_n = c["n"] if n is None else str(n).strip()
        if nt is t and new_n == old_n:
            raise TrackError(f"{old} stays where it is: --to another track, or --n another name (--at alone: its place)")
        blocker = self.move_blocker(t, c)
        if blocker:
            raise TrackError(i18n.t("tracks.no_move", old=old, blocker=blocker))
        new_n = self.check_n(nt, new_n, car=c)
        new = "{}:{}".format(nt["id"], new_n)
        # the objects it had before its live one, linked in the history by its old place: linked to the new one too
        formerly = sorted({h["st"] for h in self.history() if h.get("st") and h.get("track") == t["id"]
                           and h.get("car") == old_n} - {c.get("st")})
        now = now_iso()
        p = self.pointer(nt, new_n)
        if p is not None:  # its own old place, moved back: the car stands there again
            nt["moved_out"] = [x for x in nt["moved_out"] if x is not p]
        if nt is not t:
            t["cars"] = [x for x in t["cars"] if x is not c]
            self._place_car(nt, c, at)
        elif at is not None:
            self._place_car(t, c, at)
        c["n"] = new_n
        row = {k: v for k, v in (("from", old), ("to", new), ("at", now), ("by", by), ("why", why)) if v}
        c["moved_from"] = list(c.get("moved_from") or []) + [row]
        t["moved_out"] = list(t.get("moved_out") or []) + [dict(n=old_n, title=c["title"], **row)]
        lines = [i18n.t("tracks.moved", old=old, new=new)]
        if c["status"] in CAR_BUSY and c.get("st"):
            lines.append(i18n.t("tracks.moved_slot_pr", new=new, st=c["st"], pr=c["pr"]) if c.get("pr")
                         else i18n.t("tracks.moved_slot", new=new, st=c["st"]))

        def follow(r, xt):
            if r == old:
                return new
            if xt is t and r == old_n:  # a bare name on its old track
                return new_n if nt is t else new
            return r
        waits = []
        for xt in self.data["tracks"]:
            for x in xt["cars"]:
                if x is c:
                    after = ["{}:{}".format(t["id"], r) if nt is not t and self._bare(r) else r for r in x["after"]]
                    after = [self.after_ref(r, nt) for r in after]
                else:
                    after = [follow(r, xt) for r in x["after"]]
                    if after != x["after"]:
                        waits.append("{}:{}".format(xt["id"], x["n"]))
                x["after"] = list(dict.fromkeys(after))
                self._rename_in(x, old, new)
        if waits:
            lines.append(i18n.t("tracks.moved_waits", waits=", ".join(waits)))
        self._save("car_moved", t["id"], car=old_n, to=new, title=c["title"], why=why, by=by,
                   waits=waits or None)
        self._hist("car_moved", nt["id"], car=new_n, **{"from": old, "st": c.get("st"), "why": why, "by": by})
        for st in formerly:
            self._hist("car_relink", nt["id"], car=new_n, st=st, **{"from": old})
        lines += self.trains.car_renamed(old, new, by)
        lines += self._stack_follows(c, t, nt, old, new)
        return nt, c, lines

    @staticmethod
    def _bare(r):
        r = str(r)
        return not (RE_TRACK.match(r) or RE_CAR.match(r) or RE_STACK.match(r) or RE_PR.match(r))

    @staticmethod
    def _rename_in(rec, old, new):
        """A moved car's ref in a record's repair and claims fields (claims.py: `repairs`, `claim_links[].repair`,
        `claim_links[].moved.to`) follows it. True when something changed."""
        changed = False
        if rec.get("repairs") == old:
            rec["repairs"], changed = new, True
        for x in rec.get("claim_links") or ():
            if x.get("repair") == old:
                x["repair"], changed = new, True
            if (x.get("moved") or {}).get("to") == old:
                x["moved"]["to"], changed = new, True
        return changed

    def _stack_follows(self, c, t, nt, old, new):
        stk, out = self.stack, []
        for o in list(stk.data["objects"]):
            fields = {}
            if o["id"] == c.get("st"):
                title = o.get("title") or ""
                if title == "{} {} — {}".format(old, t["title"], c["title"]):
                    fields["title"] = "{} {} — {}".format(new, nt["title"], c["title"])
                elif title.startswith(old + " "):
                    fields["title"] = new + title[len(old):]
            probe = json.loads(json.dumps({k: o.get(k) for k in ("repairs", "claim_links") if o.get(k)}))
            if self._rename_in(probe, old, new):
                fields.update(probe)
            if fields:
                stk.set(o["id"], **fields)
                out.append(i18n.t("tracks.obj_renamed_title", id=o["id"], title=fields["title"]) if "title" in fields
                           else i18n.t("tracks.obj_renamed_links", id=o["id"], new=new))
        return out

    def car_stack(self, ref, start=False, now=None, force=False, why=None):
        t, c = self.car(ref)
        ref = "{}:{}".format(t["id"], c["n"])  # an old ref of a moved car: its train and messages are the new one's
        stk = self.stack
        msgs = []
        back = self.parked(c)  # a parked PR that needs work again
        if self.posted(c):
            raise TrackError("{} is in the post-stack ({}, PR #{}): `depot2 stack start {}` brings it back for work".format(ref, c["st"], c.get("pr"), c["st"]))
        if c["status"] not in CAR_OPEN and not back:
            raise TrackError("{} is {}: only a waiting, a ready or a parked car goes into the stack".format(ref, CAR_RU[c["status"]]))
        if c.get("st") and stk._dep_open(c["st"]):
            if not start:
                raise TrackError("{} is in the stack already ({})".format(ref, c["st"]))
            obj = stk.get(c["st"])
        else:
            obj = None
        # a parked PR's `after` gates its merge, not the work; with --start the `after` is the DoR's (soft)
        blockers = self.car_blockers(t, c, after=not back and not start)
        if blockers:
            raise TrackError("{} waits for {}".format(ref, ", ".join(blockers)))
        if back:
            gate = self.car_blockers(t, c)
            if gate:
                msgs.append("{}: its after ({}) is not met; it gates the merge, not the work".format(ref, ", ".join(gate)))
        if start:
            tn = self.train_of(ref)
            if tn is not None:
                self.trains.may_take_slot(tn)
            forced = self.build_gate(t, c, obj, back, force, why, ref)
        if obj is None:
            old = stk.left_obj(c["st"]) if back else None
            if old is not None:  # the same object, its id kept: whatever waits for it keeps waiting
                moved = any((old.get("title") or "").startswith(m.get("from", "") + " ")
                            for m in c.get("moved_from") or ())
                obj = stk.restore(old, check_queue=not start, branch=c.get("branch"), pr=c.get("pr"),
                                  title="{} {} — {}".format(ref, t["title"], c["title"]) if moved else None,
                                  priority=pri.of(c))
                msgs.append("{}:{} → {} back in the stack queue (PR #{})".format(t["id"], c["n"], obj["id"], c.get("pr")))
            else:
                deps = [r for r in c["after"] if r.startswith("st-") and stk._dep_open(r)]
                obj = stk.add("{}:{} {} — {}".format(t["id"], c["n"], t["title"], c["title"]), brief=c.get("brief"),
                              branch=c.get("branch"), pr=c.get("pr"), depends=deps, check_queue=not start,
                              size=c.get("size"), est_mtok=c.get("est_mtok"), stage=self.stage_of(c),
                              by="tracks car stack", why=i18n.t("tracks.stack_why", ref="{}:{}".format(t["id"], c["n"])),
                              priority=pri.of(c))
                msgs.append("{}:{} → {} in the stack queue".format(t["id"], c["n"], obj["id"]))
            c["st"] = obj["id"]
            if back:
                self._car_status(c, "ready")
            else:  # what `stack done --car back` returns it to
                c["stacked_from"] = c["status"]
            c.pop("returned", None)
            self._save("car_stack", t["id"], car=c["n"], st=obj["id"], back=True if back else None)
        if start:
            stk.start(obj["id"], by="tracks car stack --start", why=why, passed=forced or {})
            msgs += self.on_stack("start", obj["id"], by="tracks car stack --start", forced=forced, why=why)
            if forced:
                msgs.append(i18n.t("tracks.forced_gates", ref=ref, why=why, gates=gates.gates_text(forced)))
        return obj, msgs

    def build_gate(self, t, c, obj, back, force=False, why=None, ref=None):
        """`tracks car stack --start`, before anything is written: the DoR (from a planning stage; a parked PR
        brought back for work was in the build: none) and the slots. None, or what --force passed."""
        unmet = self.build_unmet(t, c, obj, back)
        ref = ref or "{}:{}".format(t["id"], c["n"])
        return gates.check(unmet, force, why, ref, stages.ru("build"), f"depot2 tracks car stack {ref} --start",
                           error=TrackError)

    def build_unmet(self, t, c, obj, back):
        """What the gate into the build misses, {"DoR": [...], "limit": [...]}: `build_gate`'s, and `agents start
        --role build` shows it beside its own (agents.py)."""
        stk = self.stack
        items = gates.dor_items(self.cfg)
        if back:
            items = ()
        elif self.stage_of(c) not in stages.PLANNING:  # in the build before: only its `after`, as before the gates
            items = tuple(i for i in items if i == "after")
        return {"DoR": gates.dor_unmet(stk, self, obj=obj, car=c, track=t, items=items),
                "limit": gates.slots_unmet(stk)}

    def dor_of(self, t, c):
        """What the DoR misses for a car that may start now (its `after` aside: `startable` checks it)."""
        if self.stage_of(c) not in stages.PLANNING:
            return []
        obj = self.stack.get(c["st"]) if c.get("st") and self.stack._dep_open(c["st"]) else None
        return gates.dor_unmet(self.stack, self, obj=obj, car=c, track=t,
                               items=[i for i in gates.dor_items(self.cfg) if i != "after"])

    def on_stack(self, event, oid, reason=None, pr=None, car_status=None, spent_mtok=None, by=None, forced=None,
                 why=None, priority=None):
        """The stack link: what a stack command means for the car whose object it is. Returns lines
        to print; nothing when the object is no track's car. The car's stage follows its object's (the
        stack moved it first): one car. forced: what the object's move forced past its gate (gates.py). A car
        merged or dropped may bring its train in (trains.py `car_closed`)."""
        t, c = self.car_by_stack(oid)
        if c is None:
            return []
        ref = "{}:{}".format(t["id"], c["n"])
        out = []
        before = c.get("stage") or self.status_stage(c)[0]
        if event == "start":
            self._car_status(c, "running")
            out.append(i18n.t("tracks.on_start", ref=ref, oid=oid))
            out += self.trains.car_started(ref)
        elif event == "set":
            if pr is None and spent_mtok is None and priority is None:
                return []
            if pr is not None:
                c["pr"] = int(pr)
                if c["status"] in CAR_OPEN + ("running",):
                    self._car_status(c, "pr")
                out.append("%s → PR #%d" % (ref, int(pr)))  # noqa: UP031
            if spent_mtok is not None:
                c["spent_mtok"] = float(spent_mtok)
                out.append(i18n.t("tracks.on_spent", ref=ref, spent=eff.fmt(c["spent_mtok"], self.cfg)))
            if priority is not None:  # one car, one priority (priority.py)
                c["priority"] = pri.parse(priority)
                out.append(i18n.t("tracks.on_priority", ref=ref, p=c["priority"], oid=oid))
        elif event == "park":  # the object left the stack; the car waits in its track with its PR
            if pr is not None:
                c["pr"] = int(pr)
            self._car_status(c, "pr")
            c["parked"] = {"ts": now_iso(), "st": oid, "reason": reason}
            out.append(i18n.t("tracks.on_park", ref=ref, pr=c.get("pr"), oid=oid))
            blockers = self.car_blockers(t, c)
            out.append(self.parked_line(t, c, blockers))
        elif event == "post":  # the object waits in the post-stack, linked; the car's PR is open
            if pr is not None:
                c["pr"] = int(pr)
            self._car_status(c, "pr")
            c.pop("parked", None)
            out.append(i18n.t("tracks.on_post", ref=ref, pr=c.get("pr"), oid=oid))
        elif event == "requeue":  # back from the post-stack with no free slot: the head of the queue
            self._car_status(c, "ready")
            out.append(i18n.t("tracks.on_requeue", ref=ref, oid=oid, pr=c.get("pr")))
        elif event == "done":
            if car_status not in ("merged", "dropped", "back"):
                raise TrackError(done_needs_car(oid, t, c))
            if car_status == "back":
                was = c["status"]
                status = was if was in CAR_OPEN else (c.get("stacked_from") if c.get("stacked_from") in CAR_OPEN
                                                      else "ready")
                self._car_status(c, status)
                c["st"] = None
                c.pop("parked", None)
                c["returned"] = {"ts": now_iso(), "st": oid, "reason": reason}
                out.append(i18n.t("tracks.on_back", ref=ref, status=CAR_RU[status], oid=oid))
            else:
                self._car_status(c, car_status)
                out.append("{} → {} ({})".format(ref, CAR_RU[car_status], reason or ""))
        else:
            return []
        target = self.object_stage(oid)
        row = None
        if target:
            row, line = self._car_stage(t, c, target, by or "stack " + event, why if forced else reason, default=before,
                                        forced=bool(forced), gates=forced or None)
            if line:
                out.append(line)
        self._save("stack_" + event, t["id"], car=c["n"], st=oid, reason=reason, pr=pr, spent_mtok=spent_mtok,
                   priority=priority, car_status=car_status if event == "done" else None, stage=stages.hist(row))
        if event == "done" and car_status in CAR_DONE:
            out += self.trains.car_closed(ref, by or "stack done")
        return out

    # --- import ----------------------------------------------------------------------------------

    def import_json(self, path, dry_run=False):
        if self.data["tracks"]:
            raise TrackError("tracks.json has %d tracks already: the import is one-off" % len(self.data["tracks"]))  # noqa: UP031
        with open(os.path.expanduser(path), encoding="utf-8") as f:
            raw = f.read()
        spec = json.loads(raw)
        items = spec["tracks"] if isinstance(spec, dict) else spec
        made = []
        for it in items:
            _known_keys(it, TRACK_KEYS, "track {!r}".format(it.get("title")))
            t = self._new_track(it["title"], est_mtok=eff.est_from(self.cfg, it.get("est_pct"), it.get("est_mtok"),
                                                                  allow_range=True))
            for n in it.get("notes") or []:
                t["notes"].append(n)
            for ci in it.get("cars") or []:
                _known_keys(ci, CAR_KEYS, "track {!r} car {!r}".format(it.get("title"), ci.get("n")))
                c = self._new_car(t, ci["title"], n=ci.get("n"), after=ci.get("after") or (), size=ci.get("size"),
                                  est_mtok=eff.est_from(self.cfg, ci.get("est_pct"), ci.get("est_mtok")),
                                  brief=ci.get("brief"), branch=ci.get("branch"), pr=ci.get("pr"),
                                  status=ci.get("status", "waiting"), spent_mtok=ci.get("spent_mtok"), st=ci.get("st"),
                                  note=ci.get("note"), by="tracks import", priority=ci.get("priority"))
                t["cars"].append(c)
            self.data["tracks"].append(t)
            made.append(t)
        for t in made:  # every ref, now that all tracks and cars exist
            try:
                for c in t["cars"]:
                    self.check_refs(c["after"], t, own=c["n"])
                    if c.get("st") and not self.stack._known(c["st"]):
                        raise TrackError("car {}: unknown stack object {}".format(c["n"], c["st"]))
            except TrackError as e:
                raise TrackError("{} {}: {}".format(t["id"], t["title"], e))
        if not dry_run:
            self._save("import", None, file=os.path.abspath(os.path.expanduser(path)),
                       sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(), tracks=[t["id"] for t in made])
        return made

    # --- the tables ------------------------------------------------------------------------------

    def effort(self, t):
        """(expected, spent, cars_sum) in MTok: the cars' estimates (a car's actual when it has no
        estimate) unless the track has its own estimate; the spent is the sum of the actuals."""
        return effort_of(self, t["cars"], t.get("est_mtok"))

    def effort_cell(self, t):
        return effort_text(self, t["cars"], t.get("est_mtok"))

    def counts_cell(self, t):
        return counts_text(t["cars"])

    def status_cell(self, t):
        """A track's state in words: done, or its open cars and the trains that hold them."""
        if not t["cars"]:
            return i18n.t("tracks.no_cars")
        if self.done(t):
            return i18n.t("tracks.done_word")
        open_ = [c for c in t["cars"] if c["status"] not in CAR_DONE]
        names = []
        for c in open_:
            tn = self.train_of("{}:{}".format(t["id"], c["n"]))
            if tn is not None and tn["name"] not in names:
                names.append(tn["name"])
        free = sum(1 for c in open_ if self.train_of("{}:{}".format(t["id"], c["n"])) is None)
        parts = [i18n.t("tracks.open_n", n=len(open_))]
        if names:
            parts.append(i18n.t("tracks.in_trains", names=", ".join(names)))
        if free:
            parts.append(i18n.t("tracks.free_n", n=free))
        return "; ".join(parts)

    def md(self, include_done=False, now=None, heading=True):
        L = [i18n.t("tracks.md_heading"), ""] if heading else []
        L.append(i18n.t("tracks.md_header"))
        L.append("|---|---|---|---|---|---|")
        rows = [t for t in self.tracks() if include_done or not self.done(t)]
        for i, t in enumerate(rows, 1):
            L.append("| %d | %s %s | %s | %s | %s | %s |" % (  # noqa: UP031
                i, t["id"], _cell(t["title"]), self.counts_cell(t), self.effort_cell(t), _cell(self.status_cell(t)),
                stages.counts_cell(stages.counts(self.stage_of(c) for c in t["cars"]))))
        if not rows:
            L.append(i18n.t("tracks.md_empty"))
        L.append("")
        hidden = len(self.tracks()) - len(rows)
        if hidden:
            L.append(i18n.t("tracks.md_hidden", n=hidden))
        L.append(i18n.t("tracks.md_legend"))
        L.append(eff.legend(self.cfg))
        return "\n".join(L) + "\n"

    def car_md(self, tid, now=None):
        t = self.get(tid)
        L = [i18n.t("tracks.car_md_heading", id=t["id"], title=t["title"]), ""]
        L.append(i18n.t("tracks.car_md_state", state=self.status_cell(t), effort=self.effort_cell(t)))
        L.append("")
        L.append(i18n.t("tracks.car_md_header"))
        L.append("|---|---|---|---|---|---|---|---|---|---|")
        older = []
        for c in t["cars"]:
            size = ((c["size"] + ": ") if c.get("size") else "") + eff.cell(self.car_est(c), c.get("spent_mtok"), self.cfg)
            bp = " / ".join(x for x in (c.get("branch"), ("PR #{}".format(c["pr"])) if c.get("pr") else None) if x) or "—"
            notes = car_notes(c)
            what = c["title"] + ((f" ({notes[-1]})") if notes else "") + ((" (+%d)" % (len(notes) - 1))  # noqa: UP031
                                                                             if len(notes) > 1 else "")
            older += ["{}: {}".format(c["n"], x) for x in notes[:-1]]
            tn = self.train_of("{}:{}".format(t["id"], c["n"]))
            L.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                c["n"], pri.cell(c), _cell(what), ", ".join(c["after"]) or "—", size, _cell(bp), c.get("st") or "—",
                tn["name"] if tn else "—", _cell(self.car_status_cell(t, c)), self.stage_cell(c)))
        if not t["cars"]:
            L.append(i18n.t("tracks.car_md_empty"))
        L.append("")
        moved = self.moved_lines(t)
        if moved:
            L.append(i18n.t("tracks.car_md_moved", moved="; ".join(moved)))
            L.append("")
        L += self.marks_lines([(c["n"], c) for c in t["cars"]])
        parked = self.parked_cars(t)
        if parked:
            L.append(i18n.t("tracks.parked_list", list="; ".join(self.parked_line(*p) for p in parked)))
        L.append(stage_legend())
        for n in t.get("notes") or []:
            L.append(f"- {n}")
        if older:
            L.append("")
            L.append(i18n.t("tracks.car_md_older"))
            L += [f"- {x}" for x in older]
        return "\n".join(L) + "\n"

    def stage_cell(self, c):
        s = self.stage_of(c)
        if s == "build" and self.docs_need(c):
            return i18n.t("tracks.stage_docs_needed", stage=stages.ru(s))
        return stages.ru(s)

    def docs_need(self, c):
        """Why the car's docs are needed (gates.docs_need: its flag, else its sketch's line; its live object's
        flag after its own), or None."""
        obj = self.stack.get(c["st"]) if c.get("st") and self.stack._dep_open(c["st"]) else None
        return gates.docs_need([r for r in (c, obj) if r is not None])

    def pointer_now(self, t, p):
        """Where the car a pointer names stands now (a car moved twice: the end of the chain), or None."""
        try:
            return self.canon("{}:{}".format(t["id"], p["n"]))
        except TrackError:
            return None

    def moved_lines(self, t):
        out = []
        for p in t.get("moved_out") or ():
            now = self.pointer_now(t, p)
            out.append("{} → {} «{}» ({}{})".format(p["n"], now or p["to"], clip(p.get("title")), (p.get("at") or "")[:10],
                                               ": " + p["why"] if p.get("why") else ""))
        return out

    def car_status_cell(self, t, c):
        if c["status"] in CAR_OPEN:
            b = self.car_blockers(t, c)
            if b:
                return i18n.t("tracks.status_waits", blockers=", ".join(b))
            if c.get("st") and self.stack._dep_open(c["st"]):
                return i18n.t("tracks.status_ready_queued")
            return i18n.t("tracks.status_ready")
        if self.parked(c):
            b = self.car_blockers(t, c)
            if b:
                return i18n.t("tracks.status_parked_waits", blockers=", ".join(b))
            return i18n.t("tracks.status_parked_ready")
        if self.posted(c):
            return i18n.t("tracks.status_posted")
        return CAR_RU[c["status"]]

    def car_json(self, t, c):
        """A car as data: its stage (a car from before the field: the default) and history, its status and effort
        as `--md` prints them, the train it is in (if any), and the verdict on its marks by the principles
        (`principles`, marks.py; None when the third check is empty)."""
        from .marks import compact
        est = self.car_est(c)
        tn = self.train_of("{}:{}".format(t["id"], c["n"]))
        return dict(c, ref="{}:{}".format(t["id"], c["n"]), track=t["id"], track_title=t["title"],
                    principles=compact(self.verdict(c)) if self.plist else None,
                    train=tn["name"] if tn else None, train_title=tn["title"] if tn else None,
                    stage=self.stage_of(c), stages=c.get("stages") or [], status_ru=self.car_status_cell(t, c),
                    docs_need=self.docs_need(c),
                    effort={"expected": est, "spent": c.get("spent_mtok"),
                            "text": eff.cell(est, c.get("spent_mtok"), self.cfg)})

    def track_json(self, t):
        """A track as data, each car as `car_json`; its state and effort as `--md` prints them."""
        exp, spent, _ = self.effort(t)
        return dict(t, done=self.done(t), status_ru=self.status_cell(t),
                    effort={"expected": exp, "spent": spent, "text": self.effort_cell(t)},
                    cars=[self.car_json(t, c) for c in t["cars"]],
                    moved_out=[dict(p, now=self.pointer_now(t, p)) for p in t.get("moved_out") or ()])

    def to_json(self):
        return {"v": 1, "tracks": [self.track_json(t) for t in self.data["tracks"]]}


def object_note(o, team=False):
    """A car's note line from the stack object it came from (`take_object`): its words, its place, its date."""
    where = i18n.t("stages.where." + (o.get("where") if o.get("where") in ("slot", "post") else "queue"))
    if team:
        from .tstack import KIND_RU, who_ru
        bits = [i18n.t("tracks.note_kind", kind=KIND_RU.get(o.get("kind"), o.get("kind"))),
                i18n.t("tracks.note_who", who=who_ru(o.get("who")))]
        bits += [i18n.t("tracks.note_input", input=o["input"])] if o.get("input") else []
        return i18n.t("tracks.note_team", id=o["id"], where=where, bits="; ".join(bits), added=(o.get("added") or "")[:10],
                      title=o.get("title") or "")
    return i18n.t("tracks.note_obj", id=o["id"], where=where, added=(o.get("added") or "")[:10],
                  title=o.get("title") or "")


def effort_of(tracks, cars, own=None):
    """(expected, spent, cars_sum) in MTok over cars (a track's, a train's): the cars' estimates (a car's actual when it
    has no estimate) unless `own` is given; the spent is the sum of the actuals; a dropped car counts for neither sum
    of the estimates."""
    est = [tracks.car_est(c) if tracks.car_est(c) is not None else c.get("spent_mtok") for c in cars
           if c["status"] != "dropped"]
    known = [e for e in est if e is not None]
    cars_sum = round(sum(known), 4) if known else None
    spent = [c["spent_mtok"] for c in cars if c.get("spent_mtok") is not None]
    exp = own if own is not None else cars_sum
    return exp, (round(sum(spent), 4) if spent else None), cars_sum


def effort_text(tracks, cars, own=None):
    exp, spent, cars_sum = effort_of(tracks, cars, own)
    s = eff.cell(exp, spent, tracks.cfg)
    if own is not None and cars_sum is not None and eff.fmt(cars_sum, tracks.cfg) != eff.fmt(own, tracks.cfg):
        s += " " + i18n.t("tracks.by_cars", sum=eff.fmt(cars_sum, tracks.cfg))
    return s


def counts_text(cars):
    live = [c for c in cars if c["status"] != "dropped"]
    done = sum(1 for c in live if c["status"] == "merged")
    s = "%d / %d" % (done, len(live))  # noqa: UP031
    dropped = len(cars) - len(live)
    return s + (" " + i18n.t("tracks.dropped_n", n=dropped) if dropped else "")


def stage_legend():
    """The stages' route and where the docs and the history are, one line (the tracks' and the trains' tables)."""
    return i18n.t("tracks.stage_legend")


def status_move(before, status, c):
    """`tracks car set --status` by hand: the stage it means, or None (the stage stays)."""
    if status == "dropped":
        return "scrapped"
    if status == "merged":
        return None if before in ("rolling", "arrived") else "rolling"
    if status == "running":
        return None if before in stages.WORK else "build"
    if status == "pr":
        return None if before in stages.WORK + ("acceptance", "queued") else "build"
    if before in stages.PLANNING:  # waiting, ready
        return None
    return stages.prev_planning(c)


def done_needs_car(oid, t, c):
    return ("{} is the object of {}:{} ({}): say what the car becomes: --car merged (its PR landed), --car dropped "
            "(the car is off), or --car back (no PR: back to its track as it was, the link cleared)".format(oid, t["id"], c["n"], CAR_RU[c["status"]]))


def post_line(post, cap, car_by_stack, now=None):
    """`trains next`: the post-stack on one line, as the parked cars are."""
    from .stack import age_ru
    items = []
    for o in post:
        t, c = car_by_stack(o["id"])
        ci = (o.get("ci") or {}).get("state") or "?"
        items.append(i18n.t("tracks.post_item", id=o["id"], car=(" ({}:{})".format(t["id"], c["n"])) if c else "",
                            pr=o.get("pr"), ci=ci, age=age_ru((o.get("posted") or {}).get("ts"), now)))
    return i18n.t("tracks.post_line", n=len(post), cap=cap, items="; ".join(items))
