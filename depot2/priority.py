"""The priority 1-10 of every train and every car in any of its forms (10 — the most important, 5 by default).

On: a train, a track's car, a stack object and a team stack item, a wish, a to-think entry, a todo. A car and its live
stack object are one car: one priority, written on both. An entry taken into a new car gives it its priority.
`depot2 priority ID [N]` prints or sets it; `--priority N` on the add and set commands does the same.
"""

import re

from . import i18n
from .i18n import t

MIN, MAX, DEFAULT = 1, 10, 5
WORDS = {"high": 8, "normal": 5, "later": 2}  # the English words; every language's are the `in.priority.<word>` keys
RE_P = re.compile(r"^[Pp]?(\d{1,2})$")


def means():
    """What a priority is and how a free slot reads it, in one line (the stack's legend, the web view)."""
    return t("priority.means")


class PriorityError(ValueError):
    pass


def parse(value):
    if isinstance(value, bool):
        raise PriorityError("priority is a whole number %d-%d, got %r" % (MIN, MAX, value))  # noqa: UP031
    if isinstance(value, int):
        n = value
    else:
        s = str(value).strip().lower()
        for w, p in WORDS.items():
            if i18n.is_word(s, "in.priority." + w):
                return p
        m = RE_P.match(s)
        if not m:
            raise PriorityError("priority is a whole number %d-%d (10 — the most important; or high 8, normal 5, "  # noqa: UP031
                                "later 2), got %r" % (MIN, MAX, value))
        n = int(m.group(1))
    if not MIN <= n <= MAX:
        raise PriorityError("priority is %d-%d (10 — the most important), got %d" % (MIN, MAX, n))  # noqa: UP031
    return n


def arg(text):
    """argparse's `type` for --priority (and --prio): a bad value is the parser's error, with the range."""
    import argparse
    try:
        return parse(text)
    except PriorityError as e:
        raise argparse.ArgumentTypeError(str(e))  # noqa: B904


def add_arg(parser, synonym=False):
    """--priority N on an add or a set (and --prio, its synonym, where the queues had it)."""
    flags = ("--priority", "--prio") if synonym else ("--priority",)
    parser.add_argument(*flags, dest="priority", type=arg, metavar="N",
                        help="priority %d-%d, 10 — the most important (default %d)%s" % (  # noqa: UP031
                            MIN, MAX, DEFAULT, "; --prio high|normal|later = 8 / 5 / 2" if synonym else ""))


def of(rec):
    """A record's priority (every record has it since the data's migration; a record built by hand in a test reads as
    the default)."""
    return (rec or {}).get("priority", DEFAULT)


def cell(rec):
    return "P%d" % of(rec)  # noqa: UP031


def counts(recs):
    """{priority: n} over the records."""
    out = {}
    for r in recs:
        out[of(r)] = out.get(of(r), 0) + 1
    return out


def counts_cell(c, sep=" · "):
    return sep.join("P%d %d" % (p, c[p]) for p in sorted(c, reverse=True)) or "—"  # noqa: UP031


def ordered(rows, rec=lambda x: x):
    """The rows by priority, the most important first; ties keep their order (a stable sort)."""
    return sorted(rows, key=lambda x: -of(rec(x)))


# --- `depot2 priority ID [N]`: any train or car --------------------------------------------------------------

def kind_of(ref):
    """What a ref names: train, car, stack, tstack, wishes, think, todo; a track is refused (a theme has none)."""
    from .tracks import RE_CAR, RE_TRACK
    from .trains import RE_NAME
    ref = (ref or "").strip()
    if RE_CAR.match(ref):
        return "car"
    if RE_TRACK.match(ref):
        raise PriorityError(t("priority.is_track", ref=ref))
    head = ref[:3]
    kind = {"st-": "stack", "tm-": "tstack", "wl-": "wishes", "th-": "think", "td-": "todo"}.get(head)
    if kind and re.match(r"^[a-z]{2}-\d{4}$", ref):
        return kind
    if RE_NAME.match(ref):
        return "train"
    raise PriorityError(t("priority.bad_ref", ref=repr(ref)))


def _queue(state, cfg, kind):
    from . import queues
    return {"wishes": queues.Wishes, "think": queues.Think, "todo": queues.Todo}[kind](state, cfg)


def get_any(state, cfg, ref):
    """(kind, the ref as it stands now, the record) for `depot2 priority ID`."""
    from .stack import Stack
    from .trains import Trains
    from .tstack import TeamStack
    kind = kind_of(ref)
    if kind == "train":
        return kind, ref, Trains(state, cfg).get(ref)
    if kind == "car":
        tr = Trains(state, cfg).tracks
        trk, c = tr.car(ref)
        return kind, "{}:{}".format(trk["id"], c["n"]), c
    if kind in ("stack", "tstack"):
        stk = (Stack if kind == "stack" else TeamStack)(state, cfg)
        if not stk._dep_open(ref):
            raise PriorityError(t("priority.not_in_stack" if kind == "stack" else "priority.not_in_tstack", ref=ref))
        return kind, ref, stk.get(ref)
    q = _queue(state, cfg, kind)
    e = q.get(ref)
    if e is None:
        raise PriorityError(t("priority.not_in_queue", ref=ref, queue=q.NAME))
    return kind, ref, e


def set_any(state, cfg, ref, n, by="priority"):
    """`depot2 priority ID N`: the one setter for every kind; a car's live stack object and the object's car follow
    (one car, one priority). Returns the lines to print."""
    from .trains import Trains
    n = parse(n)
    kind, now_ref, rec = get_any(state, cfg, ref)
    before = of(rec)
    trains = Trains(state, cfg)
    tr = trains.tracks
    car_ref = now_ref if kind == "car" else tr.car_ref(ref) if kind == "stack" else None
    if kind == "train":
        trains.set(ref, priority=n)
        label = t("priority.label_train", ref=ref)
    elif car_ref:  # a car, or the object of one: both written
        _, c = tr.car_set(car_ref, priority=n, by=by)
        twin = c.get("st") if c.get("st") and tr.stack._dep_open(c["st"]) else None
        label = t("priority.label_twin", car=car_ref, st=twin) if twin else car_ref
        if ref not in (car_ref, twin):
            label = f"{ref} → {label}"  # a moved car's old ref
    elif kind in ("stack", "tstack"):
        from .stack import Stack
        from .tstack import TeamStack
        (Stack if kind == "stack" else TeamStack)(state, cfg).set(ref, priority=n)
        label = ref
    else:
        _queue(state, cfg, kind).set(ref, priority=n)
        label = ref
    return [t("priority.set", label=label, before=before, n=n) if before != n else
            t("priority.same", label=label, n=n)]
