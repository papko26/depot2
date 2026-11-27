"""The depot's data, brought to today's form once: the schema migrations (1 → 6), run before any command.

Each step reads the data of one schema and writes the next, with a history row that keeps what it moved; a step that
has nothing to move says so. The web view never migrates: it answers 503 until the next command does.
"""

import os

from . import i18n
from . import priority as pri
from . import stages
from .i18n import t
from .store import append_jsonl, atomic_write, now_iso, read_json, read_jsonl, read_text, write_json, write_new
from .tracks import CAR_BUSY, CAR_DONE, SCHEMA

ARCHIVE_HISTORY = "trains-history-v1.jsonl"
ARCHIVE_TRAINS = "trains-v1.json"
TRACK_FIELDS = ("id", "title", "notes", "cars", "added", "est_mtok")
PLANNING_FIELDS = ("status", "start", "slots", "started", "finished")
# Cyrillic → latin for the names (a plain table: the names are handles, not spelling). The keys: the Russian lower-case
# alphabet in its order (a..ye, yo, zh..ya), written as code points.
CYRILLIC_ABC = "".join(chr(c) for c in range(0x430, 0x436)) + chr(0x451) + "".join(chr(c) for c in range(0x436, 0x450))
TRANSLIT = dict(zip(CYRILLIC_ABC,
                    ["a", "b", "v", "g", "d", "e", "yo", "zh", "z", "i", "y", "k", "l", "m", "n", "o", "p", "r", "s",
                     "t", "u", "f", "kh", "ts", "ch", "sh", "shch", "", "y", "", "e", "yu", "ya"]))
NAME_WORDS = 3
OLD_STATUS = i18n.Labels("migrate.old_status", ("paused", "done"))  # a schema-1 train's status, in the step's lines
NAME_CHARS = 32


class MigrateError(ValueError):
    pass


def version(state):
    """The depot's data format: 1 for a trains.json from before the tracks, SCHEMA for today's; None: no data yet. A
    depot with no trains.json that kept the queues of before is schema 5: their step is due."""
    d = read_json(state.path("trains.json"))
    if d is None:
        from .queues import OLD_QUEUES
        return 5 if any(os.path.exists(state.path(q[0] + ".json")) for q in OLD_QUEUES) else None
    return d.get("schema", 1)


def pending(state):
    v = version(state)
    return v is not None and v < SCHEMA


def slug(title):
    head = (title or "").split(":", 1)[0].split("(", 1)[0].split(" — ", 1)[0].lower()
    out = "".join(TRANSLIT.get(ch, ch if ("a" <= ch <= "z" or "0" <= ch <= "9") else " ") for ch in head)
    words = out.split()[:NAME_WORDS]
    name = ""
    for w in words:
        if len(name) + len(w) + (1 if name else 0) > NAME_CHARS:
            break
        name = name + "-" + w if name else w
    return name or "train"


def unique(name, taken):
    n, i = name, 2
    while n in taken:
        n = "%s-%d" % (name, i)
        i += 1
    taken.add(n)
    return n


def has_build(c):
    """A car with a build or a PR: in work or a PR, a PR number, or a stage past the sketch."""
    s = c.get("stage")
    return c["status"] in CAR_BUSY or bool(c.get("pr")) or (bool(s) and s not in stages.PLANNING and
                                                              s not in stages.TERMINAL)


def plan(old, names=None):
    """(tracks.json, trains.json, the new trains' history rows, the migration row, lines) from an old trains.json.
    names: {tr-NNNN: name} to use instead of the slugs."""
    names = names or {}
    tracks, trains, rows, skipped = [], [], [], []
    taken = set()
    planning = {}
    now = now_iso()
    for old_t in old["trains"]:
        tracks.append({k: old_t.get(k) for k in TRACK_FIELDS})
        tracks[-1]["notes"] = list(old_t.get("notes") or [])
        planning[old_t["id"]] = {k: old_t.get(k) for k in PLANNING_FIELDS}
        open_ = [c for c in old_t["cars"] if c["status"] not in CAR_DONE]
        if old_t.get("status") not in ("running", "waiting"):
            skipped.append(t("migrate.skip_status", id=old_t["id"], title=old_t["title"],
                             status=OLD_STATUS.get(old_t.get("status"), old_t.get("status"))))
            continue
        if not open_:
            skipped.append(t("migrate.skip_no_open", id=old_t["id"], title=old_t["title"]))
            continue
        name = unique(names.get(old_t["id"]) or slug(old_t["title"]), taken)
        state = "impl" if any(has_build(c) for c in open_) else "design"
        st = old_t.get("start") or {}
        tn = {"name": name, "title": old_t["title"], "state": state,
              "cars": ["%s:%s" % (old_t["id"], c["n"]) for c in open_],
              "start": {"after": list(st.get("after") or []), "not_before": st.get("not_before"),
                        "owner": bool(st.get("owner")), "note": st.get("note")},
              "slots": old_t.get("slots") or {"policy": "max", "n": 1}, "est_mtok": None, "notes": [],
              "added": now, "started": old_t.get("started") if state == "impl" else None, "finished": None,
              "states": [{"state": state, "at": now, "by": "migrate",
                          "why": t("migrate.why_from_train", id=old_t["id"], status=old_t.get("status"))}]}
        trains.append(tn)
        rows.append({"ts": now, "event": "migrate", "train": name, "from": old_t["id"], "state": state,
                     "cars": tn["cars"]})
    track_data = {"schema": 2, "next": old.get("next", len(tracks) + 1), "tracks": tracks}
    train_data = {"schema": 2, "trains": trains}
    mig = {"ts": now, "event": "migrate", "track": None, "schema": 2, "from": "trains.json (schema 1)",
           "archive": [ARCHIVE_TRAINS, ARCHIVE_HISTORY], "tracks": len(tracks),
           "cars": sum(len(trk["cars"]) for trk in tracks), "trains": {tn["name"]: tn["cars"][0].split(":")[0]
                                                                   for tn in trains}, "planning": planning}
    from .trains import STATE_RU
    L = [t("migrate.head_2"),
         t("migrate.tracks_count", tracks=len(tracks), trains=len(old["trains"]), cars=mig["cars"]),
         t("migrate.trains_count", n=len(trains))]
    L += ["  " + t("migrate.train_line", name=tn["name"], track=tn["cars"][0].split(":")[0], title=tn["title"],
                   state=STATE_RU[tn["state"]], cars=len(tn["cars"])) for tn in trains]
    if skipped:
        L.append(t("migrate.no_train", n=len(skipped)))
        L += ["  " + s for s in skipped]
    return track_data, train_data, rows, mig, L


def cars_of(tracks_or_trains):
    """{ref: (status, stage, pr)} over the tracks (or an old trains.json's trains)."""
    return {"%s:%s" % (t["id"], c["n"]): (c["status"], c.get("stage"), c.get("pr"))
            for t in tracks_or_trains for c in t["cars"]}


def verify(old, track_data, train_data):
    """The tracks hold every car as it was (status, stage, PR); every train's car is an open car of a track, in one
    train at most."""
    before, after = cars_of(old["trains"]), cars_of(track_data["tracks"])
    if before != after:
        lost = sorted(set(before) - set(after))
        odd = sorted(r for r in set(before) & set(after) if before[r] != after[r])
        raise MigrateError("cars differ after the migration: lost %s, changed %s" % (lost[:5], odd[:5]))
    seen = set()
    for tn in train_data["trains"]:
        for r in tn["cars"]:
            if r not in after or after[r][0] in CAR_DONE or r in seen:
                raise MigrateError("train %s: car %s is not an open car of a track, or rides twice" % (tn["name"], r))
            seen.add(r)


def history_rows(state):
    """The old history (the archive when an earlier run made it), each row with `train` read as `track`."""
    arch = state.path(ARCHIVE_HISTORY)
    src = arch if os.path.exists(arch) else state.path("trains-history.jsonl")
    out = []
    for h in read_jsonl(src):
        h = dict(h)
        if "train" in h:
            h["track"] = h.pop("train")
        if "trains" in h:
            h["tracks"] = h.pop("trains")
        out.append(h)
    return out


def run(state, cfg, dry_run=False, names=None):
    v = version(state)
    if v is None or v >= SCHEMA:
        return [t("migrate.nothing", v=v) if v is not None else t("migrate.nothing_no_data")], 0
    from .merge import lock_held
    pid = lock_held(state)
    if pid:
        raise MigrateError("the merge queue is riding (pid %s): its old code would write the old trains.json; "
                           "stop it (or let it finish), then migrate" % pid)
    L = []
    if v < 2:
        L += step_tracks_trains(state, dry_run, names)
    if v < 3:
        L += step_marks(state, dry_run, planned=v < 2)
    if v < 4:
        L += step_priority(state, dry_run, planned=dry_run and v < 2)
    if v < 5:
        L += step_arrivals(state, cfg, dry_run, planned=dry_run and v < 4)
    L += step_queues(state, cfg, dry_run, planned=dry_run and v < 2)
    return ([t("migrate.dry_run")] + L if dry_run else L), 1


def step_tracks_trains(state, dry_run=False, names=None):
    """Schema 1 → 2: the old trains become the tracks and the trains (the module's head)."""
    old = read_json(state.path("trains.json"))
    arch = state.path(ARCHIVE_TRAINS)
    if os.path.exists(arch):  # a run cut short: the trains.json of before is the archive's
        old = read_json(arch)
    track_data, train_data, rows, mig, L = plan(old, names)
    verify(old, track_data, train_data)
    hist = history_rows(state)
    L.append(t("migrate.history", n=len(hist), hist=ARCHIVE_HISTORY, trains=ARCHIVE_TRAINS))
    if dry_run:
        return L
    if not os.path.exists(arch):
        write_new(arch, read_text(state.path("trains.json")))
        state.touch(arch)
    harch = state.path(ARCHIVE_HISTORY)
    if not os.path.exists(harch) and os.path.exists(state.path("trains-history.jsonl")):
        write_new(harch, read_text(state.path("trains-history.jsonl")))
        state.touch(harch)
    from .store import dumps_line
    atomic_write(state.path("tracks-history.jsonl"), "".join(dumps_line(h) + "\n" for h in hist))
    append_jsonl(state.path("tracks-history.jsonl"), mig)
    write_json(state.path("tracks.json"), track_data)
    atomic_write(state.path("trains-history.jsonl"), "".join(dumps_line(r) + "\n" for r in rows))
    write_json(state.path("trains.json"), train_data)  # the gate flips last
    for f in ("tracks-history.jsonl", "tracks.json", "trains-history.jsonl", "trains.json"):
        state.touch(state.path(f))
    return L


def legacy_words(stack_data):
    return {o["id"]: ((o.get("dod") or {}).get("legacy_check") or {}).get("state")
            for o in (stack_data or {}).get("objects") or [] if "legacy_check" in (o.get("dod") or {})}


def step_marks(state, dry_run=False, planned=False):
    stack_data = read_json(state.path("stack.json"))
    words = legacy_words(stack_data)
    L = [t("migrate.head_3"),
         t("migrate.legacy_words", words=", ".join("%s (%s)" % (k, v) for k, v in sorted(words.items())))
         if words else t("migrate.legacy_none"),
         t("migrate.marks_empty")]
    if dry_run:
        return L
    if words:
        for o in stack_data["objects"]:
            if "legacy_check" in (o.get("dod") or {}):
                o["dod"].pop("legacy_check")
        write_json(state.path("stack.json"), stack_data)
        state.touch(state.path("stack.json"))
    append_jsonl(state.path("tracks-history.jsonl"), {
        "ts": now_iso(), "event": "migrate", "track": None, "schema": 3, "from": "schema 2",
        "legacy_check": words or None})
    for f in ("tracks.json", "trains.json"):  # the gate flips last
        d = read_json(state.path(f))
        if d is not None:
            d["schema"] = 3
            write_json(state.path(f), d)
    for f in ("tracks-history.jsonl", "tracks.json", "trains.json"):
        state.touch(state.path(f))
    return L


QUEUE_FILES = ("wishes", "think", "todo")
QUEUE_WORDS = i18n.Labels("migrate.queue", QUEUE_FILES)  # a queue's name in the step's lines
STACK_FILES = ("stack.json", "team-stack.json")


def queue_priority(entries):
    from .queues import note_prio
    out = {}
    for e in entries:
        if "priority" in e and "prio" not in e:
            continue
        word = e.get("prio") if e.get("prio") in pri.WORDS else None
        note = None if word else note_prio(e)
        p = e["priority"] if "priority" in e else pri.WORDS[word or note] if (word or note) else pri.DEFAULT
        out[e["id"]] = (word or note, bool(note), p)
    return out


def step_priority(state, dry_run=False, planned=False):
    """Schema 3 → 4: the priority 1–10 (the module's head). planned: a dry run of the steps before came first (the
    tracks and the trains on the disk are not today's yet)."""
    L = [t("migrate.head_4")]
    rows, counts = [], {}
    for name in QUEUE_FILES:
        path = state.path(name + ".json")
        d = read_json(path)
        got = queue_priority((d or {}).get("entries") or [])
        counts[name] = len(got)
        notes = sorted(k for k, v in got.items() if v[1])
        by = pri.counts([{"priority": v[2]} for v in got.values()])
        L.append(t("migrate.queue_prio", queue=QUEUE_WORDS[name], n=len(got)) +
                 (": " + pri.counts_cell(by) if got else "") +
                 ("; " + t("migrate.prio_notes", ids=", ".join(notes)) if notes else ""))
        if got and not dry_run:
            for e in d["entries"]:
                if e["id"] in got:
                    e["priority"] = got[e["id"]][2]
                    e.pop("prio", None)
            rows.append((path, d, state.path(name + "-history.jsonl"), {
                "ts": now_iso(), "event": "migrate_priority", "entry": None, "schema": 4,
                "prio": {k: v[0] for k, v in got.items()}, "from_notes": notes or None}))
    for path, d, hpath, row in rows:  # the queues first: a run cut short finds them done and goes on
        write_json(path, d)
        append_jsonl(hpath, row)
        state.touch(path)
        state.touch(hpath)
    tracks = read_json(state.path("tracks.json")) if not planned else None
    cars = {"%s:%s" % (trk["id"], c["n"]): c
            for trk in (tracks or {}).get("tracks") or [] for c in trk.get("cars") or []}
    by_st = {c["st"]: c for c in cars.values() if c.get("st")}
    for f in STACK_FILES:
        d = read_json(state.path(f))
        objs = [o for o in (d or {}).get("objects") or [] if "priority" not in o]
        counts[f.split(".")[0]] = len(objs)
        if objs and not dry_run:
            for o in objs:
                o["priority"] = pri.of(by_st.get(o["id"])) if o["id"] in by_st else pri.DEFAULT
            write_json(state.path(f), d)
            state.touch(state.path(f))
    if planned:
        L.append(t("migrate.prio_planned"))
    else:
        trains = read_json(state.path("trains.json")) or {}
        miss_c = [c for c in cars.values() if "priority" not in c]
        miss_t = [tn for tn in trains.get("trains") or [] if "priority" not in tn]
        counts.update(cars=len(miss_c), trains=len(miss_t))
        L.append(t("migrate.prio_default", trains=len(miss_t), cars=len(miss_c), stack=counts["stack"],
                   tstack=counts["team-stack"]))
        if not dry_run:
            for c in miss_c:
                c["priority"] = pri.DEFAULT
            for tn in miss_t:
                tn["priority"] = pri.DEFAULT
            append_jsonl(state.path("tracks-history.jsonl"), {
                "ts": now_iso(), "event": "migrate", "track": None, "schema": 4, "from": "schema 3",
                "priority": counts})
            for f, d in (("tracks.json", tracks), ("trains.json", trains)):  # the gate flips last
                if d:
                    d["schema"] = 4
                    write_json(state.path(f), d)
            for f in ("tracks-history.jsonl", "tracks.json", "trains.json"):
                state.touch(state.path(f))
    return L


def arrivals_by():
    """Who moved the cars of schema 5 (a car's history row), in the run's language."""
    return t("migrate.arrivals_by")


def step_arrivals(state, cfg, dry_run=False, planned=False):
    from .merge import epoch, hhmm
    L = [t("migrate.head_5")]
    if planned:
        L.append(t("migrate.arr_planned"))
        return L
    rides = stages.rides(state)
    rolling = [it["ref"] for it in _rolling(state, cfg)]
    cars = stages.rolled_before(state, cfg, rides[-1][0]) if rides else []
    if rides:
        last = rides[-1][1]
        L.append(t("migrate.arr_last", pr=last["pr"], id=last["id"], at=hhmm(rides[-1][0]), n=len(cars)))
    else:
        L.append(t("migrate.arr_none"))
    moved = {}
    for car, at in cars:
        ts, ride = next((r for r in rides if epoch(r[0]) > epoch(at)), rides[-1])
        why = t("migrate.arr_why", pr=ride["pr"], id=ride["id"], at=hhmm(ts))
        L.append(t("migrate.arr_line", car=car.label, title=clip_title(car.title()), at=hhmm(at), why=why))
        if not dry_run:
            car.move("arrived", why=why, by=arrivals_by())
        moved[car.car_ref or car.ref] = ride["id"]
    left = [r for r in rolling if r not in moved]
    if left:
        L.append(t("migrate.arr_left", n=len(left), refs=", ".join(left)))
    if dry_run:
        return L
    append_jsonl(state.path("tracks-history.jsonl"), {
        "ts": now_iso(), "event": "migrate", "track": None, "schema": 5, "from": "schema 4",
        "arrived": moved or None, "last_ride": rides[-1][1]["id"] if rides else None})
    for f in ("tracks.json", "trains.json"):  # the gate flips last
        d = read_json(state.path(f))
        if d is not None:
            d["schema"] = 5
            write_json(state.path(f), d)
    for f in ("tracks-history.jsonl", "tracks.json", "trains.json"):
        state.touch(state.path(f))
    return L


# --- schema 5 → 6: the queues of before become tracks ------------------------------------------------------------

def title_head(title):
    """A track's name as its title gives it: the part before the first colon, folded («todo: leftovers» is todo)."""
    return i18n.fold((title or "").split(":", 1)[0])


def _neg_date(d):
    """Newest first: a sort key that reverses YYYY-MM-DD; no date sorts last."""
    if not d:
        return "~"
    return "".join(chr(ord("9") - ord(ch) + ord("0")) if ch.isdigit() else ch for ch in d)


def queue_order(name, entries):
    """The open entries in the order their queue's table showed them: the most important first; the wishes then the
    newest first, the others in the file's order."""
    if name == "wishes":
        return sorted(entries, key=lambda e: (-pri.of(e), _neg_date(e.get("date")), e["id"]))
    return sorted(entries, key=lambda e: -pri.of(e))


TODO_KIND = i18n.Labels("queues.kind", ("bug", "perf", "text", "cleanup", "reminder"))  # a todo's kind, in words


def entry_text(name, e):
    """What an entry said, on one line: its words, questions, version, place, source (the note of its car)."""
    def one(x):
        return " ".join(str(x or "").split())
    if name == "wishes":
        words, text = one(e.get("words")), one(e.get("text"))
        out = one(e.get("title")) + (t("queues.wishes.take_words", words=words) if words else "") + (
            "; %s" % text if text else "")
    elif name == "think":
        qs = [one(q) for q in e.get("questions") or [] if one(q)]
        text = one(e.get("text"))
        out = one(e.get("topic")) + (t("queues.think.take_questions", list="; ".join(
            "%d) %s" % (i, q) for i, q in enumerate(qs, 1))) if qs else "") + ("; %s" % text if text else "")
        bits = [t("migrate.bit_by", v=one(e["by"])) if e.get("by") else None,
                t("migrate.bit_related", v=", ".join(e["related"])) if e.get("related") else None,
                t("migrate.bit_round", v=e["round"]) if e.get("status") == "in_round" and e.get("round") else None]
        out += "".join(" (%s)" % b for b in bits if b)
    else:
        title, text = one(e.get("title")), one(e.get("text"))
        out = "%s: %s" % (title, text) if title and text and not text.startswith(title) else (text or title)
        bits = [x for x in (t("queues.todo.bit_kind", v=TODO_KIND.get(e["kind"], e["kind"])) if e.get("kind") else None,
                            t("queues.todo.bit_version", v=e["version"]) if e.get("version") else None,
                            t("queues.todo.bit_where", v=", ".join(e["where"])) if e.get("where") else None,
                            t("queues.todo.bit_source", v=e["source"]) if e.get("source") else None,
                            t("queues.todo.bit_when", v=e["trigger"]) if e.get("trigger") else None,
                            t("migrate.bit_in_pr", v=e["pr"]) if e.get("status") == "in_pr" and e.get("pr") else None)
                if x]
        out += " (%s)" % "; ".join(bits) if bits else ""
    if e.get("links"):
        out += " " + t("migrate.bit_links", v=", ".join(str(x) for x in e["links"]))
    return out


def entry_car(tr, trk, name, e, n, by):
    """The car an open entry becomes on its track: its title, priority, date, notes, and — a wish's and a to-think
    entry's — its stage, its history of stages and its open questions; `from` names the entry."""
    from .queues import SOURCE_KIND, SOURCE_RU, _clip
    head = e.get("title") or e.get("topic") or e.get("text") or e["id"]
    note = "%s: %s" % (now_iso()[:10], t("queues.take_note", id=e["id"], kind=SOURCE_RU[SOURCE_KIND[e["id"][:2]]],
                                          date=e.get("date") or (e.get("added") or "")[:10], text=entry_text(name, e)))
    c = tr._new_car(trk, _clip(head, 100), n=n, by=by, src=e["id"], priority=pri.of(e), note=note)
    c["notes"] += [x for x in e.get("notes") or [] if isinstance(x, str) and x.strip()]
    c["added"] = e.get("added") or c["added"]
    if e.get("stage") in stages.PLANNING and e.get("stages"):
        c["stage"], c["stages"] = e["stage"], list(e["stages"])
    else:
        stages.start(c, "bogie", by, t("migrate.queue_why", id=e["id"]))
    for k in ("asks", "needs_project"):
        if e.get(k):
            c[k] = e[k]
    return c


def queue_plan(state, cfg, by=None):
    """What the step does, read only: [{name, path (the file it reads), data, track (or None: a new one), others
    (more tracks of the name), cars [(entry, the car's name)], found {id: the car's ref of a run cut short}}]."""
    from .queues import ARCHIVE, OLD_QUEUES
    from .tracks import Tracks
    tr = Tracks(state, cfg)
    out = []
    for name, prefix, track_name, _, open_ in OLD_QUEUES:
        path = state.path(name + ".json")
        if not os.path.exists(path):
            path = state.path(ARCHIVE % name)
        d = read_json(path)
        if d is None:
            continue
        named = [trk for trk in tr.tracks() if title_head(trk["title"]) == i18n.fold(track_name)]
        live = [trk for trk in named if not tr.done(trk)]
        trk = (live or named or [None])[0]
        opened = [e for e in d.get("entries") or [] if e.get("status") in open_]
        ids, found = {e["id"] for e in opened}, {}
        if trk is not None:  # an open entry a car names already: a run cut short wrote it
            for c in trk["cars"]:
                for src in c.get("from") or ():
                    if src in ids:
                        found.setdefault(src, "%s:%s" % (trk["id"], c["n"]))
        rows = queue_order(name, [e for e in opened if e["id"] not in found])
        taken = set(c["n"] for c in trk["cars"]) | set(p["n"] for p in trk.get("moved_out") or ()) if trk else set()
        own = {}
        for e in rows:  # an entry keeps its number as the car's name where the track has it free
            n = str(int(e["id"].split("-", 1)[1]))
            if n not in taken:
                own[e["id"]] = n
                taken.add(n)
        nxt = max([int(x) for x in taken if x.isdigit()] or [0]) + 1
        cars = []
        for e in rows:
            if e["id"] not in own:
                own[e["id"]], nxt = str(nxt), nxt + 1
            cars.append((e, own[e["id"]]))
        out.append({"name": name, "prefix": prefix, "track_name": track_name, "path": path, "data": d,
                    "track": trk, "others": [x for x in named if x is not trk], "cars": cars, "found": found,
                    "entries": d.get("entries") or [], "open": open_})
    return tr, out


def step_queues(state, cfg, dry_run=False, planned=False):
    """Schema 5 → 6: the queues of before (wishes, to-think, todo) become tracks of the same name (queues.py's head).
    Each open entry is a car on its track — named by the entry's number where that is free, `from` the entry, its
    text in the car's note; a closed or a taken one stays in the archive, which keeps every entry as it was and, on
    a moved one, `moved_to`: its car. A track whose title (before a colon) is the name is used, else one is added.
    planned: the steps before are not on the disk yet (a dry run): the tracks are read as they are."""
    from .queues import ARCHIVE, ARCHIVE_HISTORY
    by = t("migrate.queue_by")
    L = [t("migrate.head_6")]
    tr, plan_ = queue_plan(state, cfg)
    if not plan_:
        L.append(t("migrate.queues_none"))
    moved_all, track_of, added = {}, {}, []
    for p in plan_:
        name, trk = p["name"], p["track"]
        n_open = len(p["cars"]) + len(p["found"])
        closed = [e for e in p["entries"] if e.get("status") not in p["open"]]
        new = trk is None and bool(p["cars"])
        if new and not dry_run:
            trk = tr._new_track(p["track_name"])
            trk["notes"].append("%s: %s" % (now_iso()[:10], t("migrate.queue_track_note", queue=QUEUE_WORDS[name],
                                                                prefix=p["prefix"])))
            tr._place(trk, None)
            added.append(trk)
        track_of[name] = trk["id"] if trk is not None else None
        moved = dict(p["found"])
        for e, n in p["cars"]:
            if trk is None:  # a dry run's new track
                moved[e["id"]] = "%s:%s" % (t("migrate.new_track"), n)
                continue
            c = entry_car(tr, trk, name, e, n, by)
            if not dry_run:
                tr._place_car(trk, c, None)
            moved[e["id"]] = "%s:%s" % (trk["id"], n)
        moved_all[name] = moved
        where = (t("migrate.queue_new_track", name=p["track_name"]) if trk is None or new else
                 t("migrate.queue_old_track", id=trk["id"], title=clip_title(trk["title"], 50)))
        prio = pri.counts_cell(pri.counts([e for e, _ in p["cars"]]))
        L.append(t("migrate.queue_line", queue=QUEUE_WORDS[name], total=len(p["entries"]), n=n_open, where=where,
                   prio=prio if p["cars"] else "—", closed=len(closed), archive=ARCHIVE % name))
        if p["found"]:
            L.append("  " + t("migrate.queue_found", n=len(p["found"])))
        if p["others"]:
            L.append("  " + t("migrate.queue_others", ids=", ".join(x["id"] for x in p["others"])))
        if dry_run and p["cars"]:
            L.append("  " + t("migrate.queue_first", list=", ".join("%s → %s" % (e["id"], moved[e["id"]])
                                                                    for e, _ in p["cars"][:3])))
    if planned:
        L.append(t("migrate.queues_planned"))
    if dry_run:
        return L
    write_json(tr.path, tr.data)  # the cars first: a run cut short finds them by their `from`, and goes on
    state.touch(tr.path)
    for trk in added:
        tr._hist("add", trk["id"], title=trk["title"], by=by, at=tr.data["tracks"].index(trk) + 1)
    tr._hist("migrate", None, schema=6, **{"from": "schema 5", "queues": {
        p["name"]: {"track": track_of[p["name"]], "moved": moved_all[p["name"]] or None,
                    "archive": ARCHIVE % p["name"]} for p in plan_}})
    for p in plan_:  # the archive: every entry as it was, a moved one with its car
        name, d = p["name"], p["data"]
        for e in d.get("entries") or []:
            if e["id"] in moved_all[name]:
                e["moved_to"] = moved_all[name][e["id"]]
        arch, harch = state.path(ARCHIVE % name), state.path(ARCHIVE_HISTORY % name)
        write_json(arch, d)
        state.touch(arch)
        hist = state.path(name + "-history.jsonl")
        if not os.path.exists(harch):
            write_new(harch, read_text(hist, ""))
        append_jsonl(harch, {"ts": now_iso(), "event": "migrate", "entry": None, "schema": 6,
                             "moved": moved_all[name] or None})
        state.touch(harch)
        for f in (state.path(name + ".json"), hist):  # the files of before go: the archive is theirs now
            if os.path.exists(f):
                os.remove(f)
                state.touch(f)
    for f in ("tracks.json", "trains.json"):  # the gate flips last
        d = read_json(state.path(f))
        if d is not None:
            d["schema"] = 6
            write_json(state.path(f), d)
    for f in ("tracks-history.jsonl", "tracks.json", "trains.json"):
        state.touch(state.path(f))
    return L


def clip_title(text, n=60):
    t = " ".join((text or "").split())
    return t if len(t) <= n else t[:n - 1] + "…"


def _rolling(state, cfg):
    from .board import Board
    return [it for it in Board(state, cfg).items(with_gates=False) if it["stage"] == "rolling"]

