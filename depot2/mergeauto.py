"""The merge queue's own conflict rules and its network rule. Pure functions; merge.py runs the commands.

- rule_of: which rule a conflicting path falls under (merge.auto): a generated file and the snapshot files take the
  base's side and are rebuilt; a recorded (golden) file takes the base's side and is re-recorded after the push; the
  docs matching `union` are joined when both sides only added text. Any other path: a prepared resolution or a stop.
- snapshot_test: the test a snapshot file belongs to (the plugins' snapshot_test, else <dir>/__snapshots__/<t>.snap).
- union: `git merge-file --diff3` output → the file with every conflict joined, or None.
- is_net, net_hit: a git or gh failure that is the network's, retried with a pause.
"""

import difflib
import fnmatch
import posixpath
import re

MARK = 23
TAG = "merge-queue-"  # their labels: <TAG>ours, <TAG>base, <TAG>theirs (fixed: the same file joins the same way)
ITEM_RE = re.compile(r"^\s*(\||[-*+]\s|\d+[.)]\s)")  # a table row or a list item: rows join with no blank line


def rule_of(path, auto, cfg=None):
    """The queue's rule for a conflicting path (merge.auto) → "generated", "snapshot", "golden", "union", or None."""
    if not auto:
        return None
    c = auto.get("generated")
    if c and path == c.get("file"):
        return "generated"
    s = auto.get("snapshots")
    if s and s.get("glob") and fnmatch.fnmatchcase(path, s["glob"]) and snapshot_test(path, cfg):
        return "snapshot"
    g = auto.get("golden")
    if g and path == g.get("file"):
        return "golden"
    if any(fnmatch.fnmatchcase(path, p) for p in auto.get("union") or []):
        return "union"
    return None


def snapshot_test(path, cfg=None):
    """The test a snapshot file belongs to: the plugins' snapshot_test first, else the common layout
    <dir>/__snapshots__/<test>.snap → <dir>/<test>."""
    if cfg:
        from . import hooks
        got = hooks.call_first(cfg, "snapshot_test", path)
        if got:
            return got
    d, name = posixpath.split(path)
    if posixpath.basename(d) != "__snapshots__" or not name.endswith(".snap") or len(name) <= len(".snap"):
        return None
    return posixpath.join(posixpath.dirname(d), name[:-len(".snap")])


def markers(tag=TAG):
    return ("<" * MARK + " " + tag + "ours", "|" * MARK + " " + tag + "base", "=" * MARK,
            ">" * MARK + " " + tag + "theirs")


def union(text, tag=TAG):
    """git merge-file --diff3 --marker-size=MARK -L <tag>ours -L <tag>base -L <tag>theirs output → (the joined
    text, the number of places), or (None, why) when at some place a side changed or took away common text."""
    o, b, s, t = markers(tag)
    lines = text.split("\n")
    out, i, n = [], 0, 0
    while i < len(lines):
        if lines[i] != o:
            out.append(lines[i])
            i += 1
            continue
        try:
            bi = lines.index(b, i + 1)
            si = lines.index(s, bi + 1)
            ti = lines.index(t, si + 1)
        except ValueError:
            return None, "a conflict's markers are not whole"
        if o in lines[i + 1:ti]:
            return None, "a conflict's markers are not whole"
        got = join_hunk(lines[bi + 1:si], lines[i + 1:bi], lines[si + 1:ti])
        if got is None:
            return None, "both sides changed the common text at line %d" % (len(out) + 1)
        out += got
        n += 1
        i = ti + 1
    return "\n".join(out), n


def join_hunk(base, ours, theirs):
    """One conflict → its lines, or None. An empty (or blank) base: both sides only added; a base both sides only
    inserted into, by lines or, for one line, by characters (a running list both appended to)."""
    if not any(x.strip() for x in base):
        return join_adds(ours, theirs)
    got = both_inserts(base, ours, theirs)
    if got is None and len(base) == len(ours) == len(theirs) == 1:
        one = both_inserts(base[0], ours[0], theirs[0])
        got = None if one is None else [one]
    return got


def join_adds(ours, theirs):
    """Two additions at one place: the lines both added alike at their start and end once, the branch's lines, then
    main's; a blank line between two paragraphs (not between two rows of a table or two items of a list)."""
    p = 0
    while p < min(len(ours), len(theirs)) and ours[p] == theirs[p]:
        p += 1
    s = 0
    while s < min(len(ours), len(theirs)) - p and ours[len(ours) - 1 - s] == theirs[len(theirs) - 1 - s]:
        s += 1
    a, b = ours[p:len(ours) - s], theirs[p:len(theirs) - s]
    sep = [""] if a and b and a[-1].strip() and b[0].strip() and not (ITEM_RE.match(a[-1]) and ITEM_RE.match(b[0])) \
        else []
    return ours[:p] + a + sep + b + ours[len(ours) - s:]


def inserts(x, y):
    """y is x with only insertions → [(position in x, what is inserted)]; else None."""
    ops = difflib.SequenceMatcher(None, x, y, autojunk=False).get_opcodes()
    if any(op[0] not in ("equal", "insert") for op in ops):
        return None
    return [(i1, y[j1:j2]) for tag, i1, _, j1, j2 in ops if tag == "insert"]


def both_inserts(base, ours, theirs):
    """ours and theirs each only insert into base (a list of lines or a string) → base with both sides' insertions
    (at one place the branch's first, then main's); None when either side deleted or changed any of base."""
    a, t = inserts(base, ours), inserts(base, theirs)
    if a is None or t is None:
        return None
    res = list(base)
    for pos, _, part in sorted([(p, 1, x) for p, x in a] + [(p, 0, x) for p, x in t], key=lambda z: (-z[0], z[1])):
        res[pos:pos] = list(part)
    return "".join(res) if isinstance(base, str) else res


# --- the network -------------------------------------------------------------------------------------------

NET_RE = re.compile(
    r"timed out|time-?out|hung up|TLS handshake|SSL_ERROR|gnutls|Could not resolve host|Could not resolve hostname|"
    r"Connection reset|Connection refused|Connection closed|connection was closed|unexpected disconnect|early EOF|"
    r"RPC failed|unexpected EOF|\": EOF|error connecting to|i/o timeout|no such host|network is unreachable|"
    r"Temporary failure in name resolution|deadline exceeded|HTTP 50[0234]|50[234] (Bad Gateway|Service Unavailable|Gateway Time)|"
    r"Could not read from remote repository|remote end hung up", re.I)
NOT_NET_RE = re.compile(
    r"Permission denied|Authentication failed|Bad credentials|could not read Username|HTTP 40[0-9]|non-fast-forward|"
    r"\[rejected\]|protected branch|not found|does not exist|no such pull request", re.I)


def is_net(code, text):
    """A failed git fetch/push/ls-remote or gh call that the network failed (a timeout, a hang-up, TLS, a 50x):
    worth a pause and another try. A hang past the command's own timeout (code None) is one too. An auth failure,
    a rejected push, a 40x are not, whatever else the text says."""
    if code == 0:
        return False
    if code is None:
        return True
    t = text or ""
    return bool(NET_RE.search(t)) and not NOT_NET_RE.search(t)


def net_hit(log, patterns):
    """The first of patterns (merge.ci_net_patterns) in a run's failed log, or None."""
    for p in patterns or []:
        if p and p in (log or ""):
            return p
    return None
