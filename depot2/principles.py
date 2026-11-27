"""The design principles as a queue of the depot: `depot2 principles`.

principles.json + principles-history.jsonl   one entry per principle, pc-NNNN (its number is the principle's own;
                                             pc-0005a is principle 5a); numbers are never reused
the rendered file                            `principles render` writes it (read-only between renders), from the
                                             depot; `principles import FILE` reads an existing file once, without loss

A principle: its section (core — priority 1, the gates; mechanics — priority 2, the score; work, checks, voice),
title, text, sub-points, the owner's words with dates, links, and `check` (it is in the third check, the PR's lines).
A section is stored by its code; an older depot's codes (Russian words) are read as these codes (section_key, through
the aliases in.principles.section.<code>) and written as them from then on.
"""


import os
import re
import stat

from . import i18n
from .i18n import rx, t
from .queues import Queue, QueueError, _clip
from .stack import _cell
from .store import atomic_write, now_iso, read_text, sha256_text, today

SECTION_CODES = ("core", "mechanics", "work", "checks", "voice")
SECTION_TITLE = i18n.Labels("principles.section", SECTION_CODES)


class _Sections:
    """SECTIONS: the (code, title) pairs in their order, each title in the language of the moment."""

    def __iter__(self):
        return iter([(k, SECTION_TITLE[k]) for k in SECTION_CODES])

    def __len__(self):
        return len(SECTION_CODES)

    def __getitem__(self, i):
        return list(self)[i]


SECTIONS = _Sections()
SECTION_RANK = {k: i for i, k in enumerate(SECTION_CODES)}
# The third check's priority of a principle with `check: true`, by its section: 1 the gates, any other section 2.
PRIORITY = {"core": 1}
PRIORITY_MEANS = i18n.Labels("principles.means", (1, 2))


def banner_line():
    """The render's line under the file's title (read back in every language: RE_BANNER)."""
    return t("principles.banner")


def default_head():
    return [t("principles.default_head"), ""]


RE_BANNER = re.compile(r"^> {}\b".format(rx("in.principles.banner")))

RE_ITEM = re.compile(r"^(\d+)([a-z]?)\. (.*)$")
RE_SUB = re.compile(r"^( +)- (.*)$")
RE_ID = re.compile(r"^pc-(\d{4})([a-z]?)$")
DATE = re.compile(r"\b(20\d\d-\d\d-\d\d)\b")
QUOTE = re.compile(r"«([^«»]*(?:«[^«»]*»[^«»]*)*)»")
OWNER = re.compile(r"(?i){}".format(rx("in.principles.owner")))
LINKS = re.compile(r"\b(?:dec-\d{6}|(?:wl|th|td|st|tm)-\d{4}|tr-\d{4}(?::\w+)?)\b")
TIGHT = ",.:;!?)"  # a text that starts with one of these follows the bold title with no space



def checks_frame():
    """The first two checks: (title, text)."""
    return ((t("principles.check1_title"), t("principles.check1_text")),
            (t("principles.check2_title"), t("principles.check2_text")))


FRAME = re.compile(r"(?i)^\*\*[^*]*{}".format(rx("in.principles.frame")))
RE_THIRD = re.compile(rx("in.principles.third_check"), re.IGNORECASE)

STATUS_RU = i18n.Labels("principles.status", ("active", "replaced", "dropped"))


class Principles(Queue):
    NAME = "principles"
    PREFIX = "pc"
    STATUS_RU = STATUS_RU
    OPEN = ("active",)
    FIELDS = ("date", "section", "title", "text", "order", "gap", "check")
    LIST_FIELDS = ("links", "notes")

    def __init__(self, state, cfg=None):
        super().__init__(state, cfg)
        self.data.setdefault("next", 1)
        for e in self.data.get("entries") or []:  # an older depot's section codes: read as today's
            if e.get("section") not in SECTION_RANK:
                e["section"] = norm_section(e.get("section"))

    @property
    def TITLE(self):
        return t("principles.title")

    # --- reading ---------------------------------------------------------------------------------

    def ordered(self, open_only=True):
        return sorted(self.entries(open_only), key=lambda e: (SECTION_RANK.get(e.get("section"), 9),
                                                             e.get("order") or 0, e.get("n") or 0))

    def target(self):
        """The PRINCIPLES.md the depot renders: the file `principles import` read (None before the import)."""
        return self.data.get("file")

    def checked(self):
        return [e for e in self.ordered(True) if e.get("check")]

    def check_list(self):
        return [{"id": e["id"], "label": label(e), "title": e.get("title") or _clip(e.get("text"), 70),
                 "section": e["section"], "priority": priority(e)} for e in self.checked()]

    def headline(self, e):
        return "{}. {}".format(label(e), e.get("title") or _clip(e.get("text"), 70))

    # --- checking --------------------------------------------------------------------------------

    def check(self, e):
        super().check(e)
        if e.get("section") not in SECTION_TITLE:
            raise QueueError("{}: section must be one of {}".format(e["id"], ", ".join(SECTION_TITLE)))
        if not ((e.get("title") or "").strip() or (e.get("text") or "").strip()):
            raise QueueError("{}: a principle needs a title or a text".format(e["id"]))
        for k in ("title", "text"):
            if "\n" in (e.get(k) or ""):
                raise QueueError("{}: {} is one line (its subs are --sub)".format(e["id"], k))
        for s in e.get("subs") or []:
            if not isinstance(s, str) or not s.strip() or "\n" in s:
                raise QueueError("{}: a sub is one line of text".format(e["id"]))
        for w in e.get("words") or []:
            if not isinstance(w, dict) or not (w.get("text") or "").strip():
                raise QueueError("{}: a word is {{date, text}}".format(e["id"]))
        if not isinstance(e.get("check"), bool):
            raise QueueError("{}: check is yes or no".format(e["id"]))
        if not isinstance(e.get("gap"), int) or e["gap"] < 0:
            raise QueueError("{}: gap is a number of blank lines >= 0".format(e["id"]))
        if e["status"] == "replaced" and not e.get("by"):
            raise QueueError("{}: replaced by whom (--by pc-…)".format(e["id"]))

    # --- writing ---------------------------------------------------------------------------------

    def _make(self, n, suffix, fields, by):
        eid = pc_id(n, suffix)
        if any(x["id"] == eid for x in self.data["entries"]):
            raise QueueError(f"{eid} is there already (a number is never reused)")
        e = {"id": eid, "n": n, "suffix": suffix or "", "added": now_iso(), "status": "active", "by": None,
             "replaces": [], "reason": None, "words": [], "subs": [], "links": [], "notes": [], "check": False,
             "gap": 0}
        e.update({k: v for k, v in fields.items() if v is not None})
        self.check(e)
        self.data["entries"].append(e)
        self.data["next"] = max(self.data.get("next") or 1, n + 1)
        return e

    def last_in(self, section):
        rows = [e for e in self.ordered(True) if e["section"] == section]
        return rows[-1] if rows else None

    def add(self, section, title=None, text=None, words=(), subs=(), date=None, links=(), check=False, order=None):
        """A new principle, numbered from `next` (one past the highest number so far): at the end of its section unless
        --order, with the blank lines that closed the section."""
        section = section_key(section)
        last = self.last_in(section)
        date = date or today()
        e = self._make(self.data.get("next") or 1, "", {
            "section": section, "title": title, "text": text or "", "date": date, "words": [word(w, date) for w in words or ()],
            "subs": list(subs or ()), "links": list(links or ()), "check": bool(check),
            "order": float(order) if order is not None else ((last["order"] if last else 0) + 1),
            "gap": last["gap"] if last else 1}, "principles add")
        e["order"] = _num(e["order"])
        self._save("add", e["id"], entry_data=e)
        return e

    def set(self, eid, words=(), subs=(), sub_set=None, sub_drop=None, **fields):
        """Change a principle: the fields replaced (the history row keeps what they were), `--words` and `--sub`
        appended, `--sub-set N TEXT` and `--sub-drop N` (from 1) on one sub."""
        e = self.get(eid)
        if e["status"] != "active":
            raise QueueError("{} is {}: only an active principle changes".format(eid, STATUS_RU[e["status"]]))
        changed, before = {}, {}
        fields = {k: v for k, v in fields.items() if v is not None}
        if "section" in fields:
            fields["section"] = section_key(fields["section"])
            if fields["section"] != e["section"] and "order" not in fields:
                last = self.last_in(fields["section"])
                fields["order"] = (last["order"] if last else 0) + 1
        if "order" in fields:
            fields["order"] = _num(float(fields["order"]))
        if "check" in fields:
            fields["check"] = bool(fields["check"])
        if "gap" in fields:
            fields["gap"] = int(fields["gap"])
        for k, v in fields.items():
            if k in self.LIST_FIELDS:
                add = [x for x in (v if isinstance(v, (list, tuple)) else [v]) if x not in (e.get(k) or [])]
                if k == "notes":
                    add = [f"{today()}: {x}" for x in add]
                if add:
                    e[k] = (e.get(k) or []) + add
                    changed[k] = add
            elif k in self.FIELDS:
                if e.get(k) != v:
                    before[k], e[k] = e.get(k), v
                    changed[k] = v
            else:
                raise QueueError(f"principles have no field {k}")
        new_words = [word(w, today()) for w in words or ()]
        new_words = [w for w in new_words if w not in e["words"]]
        if new_words:
            e["words"] = e["words"] + new_words
            changed["words"] = new_words
        subs_before = list(e["subs"])
        if sub_set is not None:
            i, text = sub_set
            e["subs"][_sub_index(e, i)] = text
        if sub_drop is not None:
            del e["subs"][_sub_index(e, sub_drop)]
        if subs:
            e["subs"] = e["subs"] + list(subs)
        if e["subs"] != subs_before:
            before["subs"], changed["subs"] = subs_before, list(e["subs"])
        if not changed:
            raise QueueError("nothing to set")
        self.check(e)
        self._save("set", eid, before=before or None, **changed)
        return e

    def replace(self, eid, by, reason=None):
        """It is replaced by another principle (an active one, not itself): out of the file, in --all."""
        e, b = self.get(eid), self.get(by)
        if eid == by:
            raise QueueError(f"{eid} cannot replace itself")
        for x in (e, b):
            if x["status"] != "active":
                raise QueueError("{} is {}".format(x["id"], STATUS_RU[x["status"]]))
        if eid not in b["replaces"]:
            b["replaces"].append(eid)
        return self.to_status(eid, "replaced", reason=reason, by=by)

    def drop(self, eid, reason):
        e = self.get(eid)
        if e["status"] != "active":
            raise QueueError("{} is {}".format(eid, STATUS_RU[e["status"]]))
        if not (reason or "").strip():
            raise QueueError("drop needs --reason")
        e["reason"] = reason
        return self.to_status(eid, "dropped", reason=reason)

    def head(self):
        """The file's head as the depot keeps it: the lines before the first section (the banner is added by the
        render)."""
        return list(default_head() if self.data.get("head") is None else self.data["head"])

    def set_head(self, text):
        lines = [ln.rstrip() for ln in (text or "").replace("\r\n", "\n").split("\n")]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        if not lines:
            raise QueueError("the head's text is empty: --text «…»")
        if any(ln.startswith("## ") for ln in lines):
            raise QueueError("the head cannot hold a section («## …»): the sections come from the principles")
        if any(RE_BANNER.match(ln) for ln in lines):
            raise QueueError("the banner «{}» is the render's own: leave it out of --text".format(t("principles.banner_words")))
        old = self.head()
        title = old[0] if old and old[0].startswith("# ") else default_head()[0]
        if lines[0].startswith("# "):
            title = lines.pop(0)
            while lines and not lines[0].strip():
                lines.pop(0)
        new = [title, ""] + lines + ([""] if lines else [])
        if new == old:
            raise QueueError("the head is that already: nothing to set")
        self.data["head"] = new
        self._save("head", None, before="\n".join(old), text="\n".join(new))
        return new

    # --- the one-shot import -----------------------------------------------------------------------

    def import_file(self, path, dry_run=False):
        """Today's PRINCIPLES.md into the depot, without loss: refused for a file imported before, for a number
        the depot has, and when the render of what was read is not the file line for line (but the banner). A real
        import writes the entries, then renders the file in place (read-only)."""
        path = os.path.abspath(os.path.expanduser(path))
        if self.imported(path):
            raise QueueError("{} was imported already ({})".format(path, self.imported(path)[0]["ts"]))
        text = read_text(path)
        if text is None:
            raise QueueError(f"no such file: {path}")
        head, items = parse(text, os.path.basename(path))
        for it in items:
            if any(x["id"] == pc_id(it["n"], it["suffix"]) for x in self.data["entries"]):
                raise QueueError("{} is in the depot already: nothing imported".format(pc_id(it["n"], it["suffix"])))
        lost = diff_lines(today_headings(strip_banner(text)), render_text(head, items, banner=False).splitlines())
        if lost:
            raise QueueError(t("principles.import_lossy", lost=lost))
        if dry_run:
            return head, items
        made = []
        for it in items:
            n, suffix = it.pop("n"), it.pop("suffix")
            made.append(self._make(n, suffix, it, "principles import"))
        self.data["head"] = head
        self.data["file"] = path
        self._save("import", None, file=path, sha256=sha256_text(text), entries=[e["id"] for e in made],
                   check=[e["id"] for e in made if e["check"]])
        return head, made

    # --- the render --------------------------------------------------------------------------------

    def render(self, banner=True):
        return render_text(self.data.get("head"), self.ordered(True), banner=banner)

    def drift(self):
        """Where the file on disk parts from the depot's render (a hand edit), else None (no file, or the same)."""
        path = self.target()
        disk = read_text(path) if path else None
        return None if disk is None else diff_lines(today_headings(disk.splitlines()), self.render().splitlines())

    def live_target(self):
        from .config import DEFAULT_STATE
        live = os.path.realpath(os.path.expanduser(DEFAULT_STATE))
        t = self.target()
        return bool(t) and os.path.realpath(self.state.root) != live and \
            os.path.realpath(t).startswith(os.path.dirname(live) + os.sep)

    def write(self, out=None):
        """Render into `out`, else into the target; the target is read-only between renders (the render takes the
        write bit off again). Returns (path, changed); changed is None when the target is the live depot's and this
        depot another (live_target): nothing written."""
        path = os.path.abspath(os.path.expanduser(out)) if out else self.target()
        if not path:
            raise QueueError("no PRINCIPLES.md yet: `depot2 principles import FILE` first, or `render --out PATH`")
        if path == self.target() and self.live_target():
            return path, None
        text = self.render()
        old = read_text(path)
        locked = path == self.target()
        if old != text:
            atomic_write(path, text)
        if locked:
            mode = os.stat(path).st_mode
            os.chmod(path, mode & ~(stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH))
        return path, old != text

    # --- the views ---------------------------------------------------------------------------------

    def md(self, open_only=True, heading=True, limit=None):
        L = ["## " + self.TITLE, ""] if heading else []
        L.append(t("principles.md_header"))
        L.append("|---|---|---|---|---|---|")
        rows = self.ordered(open_only)
        for e in rows[:limit] if limit else rows:
            words = e.get("words") or []
            w = ("«{}» ({})".format(_clip(words[0]["text"], 60), words[0].get("date") or "—")) if words else "—"
            if len(words) > 1:
                w += " (+%d)" % (len(words) - 1)  # noqa: UP031
            st = STATUS_RU[e["status"]] + (" → {}".format(e["by"]) if e.get("by") else "")
            L.append("| {} | {} | {} | {} | {} | {} |".format(*tuple(_cell(x) for x in (
                "{} · {}{}".format(e["id"], self.headline(e), f" · ③ {CHECK_MARK[priority(e)]}" if e.get("check") else ""),
                SECTION_TITLE.get(e["section"], e["section"]), e.get("date") or "—", w,
                str(len(e.get("subs") or [])) if e.get("subs") else "—", st))))
        if not rows:
            L.append(t("principles.md_empty"))
        L.append("")
        L.append(t("principles.md_footer", n=len(self.entries(True)), of=len(self.data["entries"]),
                   first=t("principles.md_first", n=limit) if limit and len(rows) > limit else "",
                   file=self.target() or t("principles.md_no_file")))
        return "\n".join(L) + "\n"

    def to_json(self, open_only=True):
        rows = self.ordered(open_only)
        sections = []
        for key, title in SECTIONS:
            items = [dict(e, label=label(e), status_ru=STATUS_RU[e["status"]], lines=render_item(e), priority=priority(e))
                     for e in rows if e["section"] == key]
            sections.append({"key": key, "title": title, "count": len(items), "items": items})
        counts = dict.fromkeys(STATUS_RU, 0)
        for e in self.data["entries"]:
            counts[e["status"]] = counts.get(e["status"], 0) + 1
        return {"v": 1, "generated": now_iso(), "all": not open_only, "file": self.target(),
                "sections": sections, "counts": counts, "checks": [e["id"] for e in self.checked()]}

    def show(self, eid):
        e = self.get(eid)
        L = [t("principles.show_heading", id=e["id"], label=label(e), status=STATUS_RU[e["status"]]), ""]
        L += render_item(e)
        L.append("")
        L.append(t("principles.show_fields", section=SECTION_TITLE.get(e["section"], e["section"]),
                   order=e.get("order"), date=e.get("date") or "—",
                   check=t("principles.show_check", mark=CHECK_MARK[priority(e)]) if e.get("check") else ""))
        if e.get("words"):
            L.append(t("principles.show_words"))
            L += ["  - {}: «{}»".format(w.get("date") or "—", w["text"]) for w in e["words"]]
        for k in ("by", "replaces", "reason", "links", "source_ref", "notes"):
            v = e.get(k)
            if v:
                L.append("- {}: {}".format(t("principles.field." + k), ", ".join(v) if isinstance(v, list) else v))
        return "\n".join(L) + "\n"

    def checks(self):
        """The three checks as data: [{n, title, text}], the third with its principles (`check: true`), each {id,
        label, title, section, priority}, and its priorities [{priority, title, means, labels}]."""
        out = [{"n": i, "title": title, "text": x} for i, (title, x) in enumerate(checks_frame(), 1)]
        third = self.check_list()
        pri = [{"priority": n, "title": SECTION_TITLE["core" if n == 1 else "mechanics"], "means": PRIORITY_MEANS[n],
                "labels": [p["label"] for p in third if p["priority"] == n]} for n in (1, 2)]
        from .marks import format_line, section
        out.append({"n": 3, "title": section(), "text": t("principles.check3_text", format=format_line()),
                    "principles": third, "priorities": pri})
        return out

    def checks_md(self):
        from .marks import format_line, section
        L = [t("principles.checks_heading"), "", t("principles.checks_intro"), ""]
        cs = self.checks()
        for c in cs[:2]:
            L.append("%d. **%s** — %s" % (c["n"], c["title"], c["text"]))  # noqa: UP031
        third = cs[2]
        L.append(t("principles.checks_third", title=third["title"], format=format_line()))
        for pr in third["priorities"]:
            ps = [p for p in third["principles"] if p["priority"] == pr["priority"]]
            L.append("   - **{}** — {}:".format(pr["title"], pr["means"]))
            L += ["     - {}. {} ({})".format(p["label"], p["title"], p["id"]) for p in ps] or \
                [t("principles.checks_none")]
        L.append("")
        L.append(t("principles.checks_where", section=section()))
        return "\n".join(L) + "\n"

    def round_line(self):
        """The bundle's line about the three checks."""
        pri = self.checks()[2]["priorities"]
        third = "; ".join("{}: {}".format(p["title"].lower(), ", ".join(p["labels"]) or t("principles.round_none"))
                          for p in pri) if any(p["labels"] for p in pri) else t("principles.round_no_checks")
        return t("principles.round_line", third=third)


# --- the file: parse and render ----------------------------------------------------------------------

def pc_id(n, suffix=""):
    return "pc-%04d%s" % (int(n), suffix or "")  # noqa: UP031


def label(e):
    return "%d%s" % (e["n"], e.get("suffix") or "")  # noqa: UP031


def priority(e):
    return PRIORITY.get(e.get("section"), 2)


CHECK_MARK = i18n.Labels("principles.check_mark", (1, 2))


def section_key(name):
    """A typed section (its code, its title or another name of it, in any language; an older depot's code) → its
    code."""
    k = " ".join(str(name or "").strip().lower().split())
    for key in SECTION_CODES:
        if k == key or k in [f.lower() for f in i18n.forms("principles.section." + key)]:
            return key
    for key in SECTION_CODES:
        if i18n.is_word(k, "in.principles.section." + key):
            return key
    raise QueueError("section must be one of {}".format(", ".join(SECTION_CODES)))


def norm_section(code):
    """A stored section as today's code: an older depot's (a Russian word) through section_key; else as it is."""
    if code in SECTION_RANK:
        return code
    try:
        return section_key(code)
    except QueueError:
        return code


def word(w, date):
    """`--words TEXT` or `--words "YYYY-MM-DD: TEXT"` -> {date, text}."""
    if isinstance(w, dict):
        return w
    m = re.match(r"^\s*(20\d\d-\d\d-\d\d)\s*:\s*(.+)$", w or "")
    text = (m.group(2) if m else w or "").strip()
    if text.startswith("«") and text.endswith("»"):
        text = text[1:-1].strip()
    return {"date": m.group(1) if m else date, "text": text}


def _num(x):
    return int(x) if float(x).is_integer() else x


def _sub_index(e, n):
    n = int(n)
    if not 1 <= n <= len(e["subs"]):
        raise QueueError("%s has subs 1..%d, not %d" % (e["id"], len(e["subs"]), n))  # noqa: UP031
    return n - 1


def banner_lines(lines):
    """The indexes of the render's banner and the blank line after it (none in a file never rendered)."""
    for i, line in enumerate(lines):
        if RE_BANNER.match(line):
            return {i, i + 1} if i + 1 < len(lines) and not lines[i + 1].strip() else {i}
    return set()


def strip_banner(text):
    """The lines without the render's banner (and the blank line after it)."""
    lines = text.splitlines()
    skip = banner_lines(lines)
    return [line for i, line in enumerate(lines) if i not in skip]


def today_headings(lines):
    out = []
    for line in lines:
        if line.startswith("## "):
            try:
                line = "## " + SECTION_TITLE[section_key(line[3:])]
            except QueueError:
                pass
        out.append(line)
    return out


def diff_lines(want, got):
    for i in range(max(len(want), len(got))):
        a = want[i] if i < len(want) else None
        b = got[i] if i < len(got) else None
        if a != b:
            return t("principles.diff_line", n=i + 1, was=f"«{_clip(a, 90)}»" if a is not None else
                     t("principles.diff_eof"), got=f"«{_clip(b, 90)}»" if b is not None else t("principles.diff_eof"))
    return None


def _mask(text):
    return QUOTE.sub(lambda m: "«" + "x" * (len(m.group(0)) - 2) + "»", text)


def _stretch_start(masked, end):
    depth = 0
    i = end - 1
    while i >= 0:
        c = masked[i]
        if c == ")":
            depth += 1
        elif c == "(":
            if depth == 0:
                return i + 1
            depth -= 1
        elif depth == 0 and (c == ";" or (c == "." and i + 1 < len(masked) and masked[i + 1] == " ")):
            return i + 1
        i -= 1
    return 0


def owner_words(line, default_date):
    out = []
    masked = _mask(line)
    for m in QUOTE.finditer(line):
        start = _stretch_start(masked, m.start())
        clause = line[start:m.start()]
        if not clause.rstrip(" *").endswith(":"):
            continue
        if not (OWNER.search(clause) or DATE.search(clause)):
            continue
        d = DATE.search(clause)
        if not d:
            before = DATE.findall(line[:start])
            date = before[-1] if before else default_date
        else:
            date = d.group(1)
        out.append({"date": date, "text": m.group(1).strip()})
    return out


def parse(text, name="PRINCIPLES.md"):
    lines = text.splitlines()
    skip = banner_lines(lines)
    head, items, section, cur = [], [], None, None
    for no, line in enumerate(lines, 1):
        if no - 1 in skip:
            continue
        if section is None and not line.startswith("## "):
            head.append(line)
            continue
        if line.startswith("## "):
            section = section_key(line[3:])
            cur = None
            continue
        m = RE_ITEM.match(line)
        if m:
            cur = _item(m, section, len([x for x in items if x["section"] == section]) + 1, "%s:%d" % (name, no))  # noqa: UP031
            items.append(cur)
            continue
        if not line.strip():
            if cur is None:
                raise QueueError(t("principles.parse_blank", name=name, no=no))
            cur["gap"] += 1
            continue
        s = RE_SUB.match(line)
        if s and cur is not None and not cur["gap"] and len(s.group(1)) >= 3:
            cur["subs"].append(" " * (len(s.group(1)) - 3) + s.group(2))
            continue
        raise QueueError(t("principles.parse_junk", name=name, no=no, line=_clip(line, 80)))
    for it in items:
        date = it["date"]
        words = []
        for x in [it["title"] + " " + it["text"]] + it["subs"]:
            for w in owner_words(x, date):
                if w not in words:
                    words.append(w)
        it["words"] = words
        it["check"] = any(FRAME.match(s) for s in it["subs"]) or bool(RE_THIRD.search(it["title"]))
        it["links"] = []
        for x in [it["text"]] + it["subs"]:
            for ln in LINKS.findall(x):
                if ln not in it["links"]:
                    it["links"].append(ln)
    return head, items


def _item(m, section, order, ref):
    n, suffix, rest = int(m.group(1)), m.group(2), m.group(3)
    title, text = "", rest
    if rest.startswith("**"):
        close = rest.find("**", 2)
        if close > 2:
            title, text = rest[2:close], rest[close + 2:]
            text = text.removeprefix(" ")
    d = DATE.search(title + " " + text)
    return {"n": n, "suffix": suffix, "section": section, "order": order, "title": title, "text": text,
            "date": d.group(1) if d else None, "subs": [], "gap": 0, "source_ref": ref}


def render_item(e):
    words = [w for w in e.get("words") or [] if "«{}»".format(w["text"]) not in
             " ".join([e.get("title") or "", e.get("text") or "", *list(e.get("subs") or [])])]
    text = e.get("text") or ""
    if words:  # the owner's words the text does not quote: in a bracket after the title
        said = t("principles.owner_said",
                 words="; ".join("{}: «{}»".format(w.get("date") or "—", w["text"]) for w in words))
        text = said + ("." if text else "") + (" " + text if text else "")
    line = f"{label(e)}. "
    if e.get("title"):
        line += "**{}**".format(e["title"])
        if text:
            line += ("" if text[0] in TIGHT else " ") + text
    else:
        line += text
    out = [line]
    for s in e.get("subs") or []:
        lead = len(s) - len(s.lstrip(" "))
        out.append("   " + " " * lead + "- " + s.lstrip(" "))
    return out


def render_text(head, items, banner=True):
    """The whole file: the head (with the banner after its first line), then the sections in their order, each
    principle in its `order`, with its blank lines after it."""
    head = list(default_head() if head is None else head)
    if banner:
        at = 2 if len(head) > 1 and not head[1].strip() else 1
        head = [*head[:at], banner_line(), "", *head[at:]]
    L = list(head)
    rows = sorted(items, key=lambda e: (SECTION_RANK.get(e["section"], 9), e.get("order") or 0, e.get("n") or 0))
    seen = None
    for e in rows:
        if e["section"] != seen:
            seen = e["section"]
            L.append("## " + SECTION_TITLE[seen])
        L += render_item(e)
        L += [""] * int(e.get("gap") or 0)
    return "\n".join(L) + "\n"
