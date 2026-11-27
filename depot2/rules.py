"""The project's rules for its builders (`depot2 rules`, the web view's Rules tab): the project's own Markdown files,
named in the config's `rules.files` (each a path from / or ~), shown read only.

depot2 keeps no rules of its own: a project says which of its files are the rules its builders follow, and the depot
shows them. A file is shown only as a brief is (webui.safe_md): a real .md file whose real path is inside
web.docs.dirs, at most webui.MAX_DOC of it (`cut`). Any other is listed with the reason, and nothing of it is read.
No request names a path: the list is the config's alone.
"""

import os
import re

from .artifacts import iso, size_text, when
from .i18n import t
from .stack import _cell
from .store import now_iso

RE_TITLE = re.compile(r"^#\s+(.+?)(?:\s+#+)?\s*$")


def docs_of(cfg):
    """(web.docs.dirs, web.docs.base): where a rule may be (the web view's own, read at its start, are passed in)."""
    d = (cfg.get("web") or {}).get("docs") or {}
    return tuple(d.get("dirs") or ()), d.get("base")


def why_not(path, dirs):
    """Why a rule's file is not shown (a catalog key), looked at before anything of it is read; None: it is."""
    from .webui import inside
    p = os.path.expanduser(path)
    if not p.endswith(".md"):
        return "rules.not_md"
    if not dirs:
        return "rules.no_dirs"
    real = os.path.realpath(p)
    if not real.endswith(".md"):
        return "rules.not_md"
    if not inside(real, dirs):
        return "rules.outside"
    if not os.path.isfile(real):
        return "rules.no_file"
    return None


def title_of(text, path):
    """The first line when it is a `# heading`, else the file's name."""
    for line in text.split("\n"):
        if line.strip():
            m = RE_TITLE.match(line.strip())
            return m.group(1) if m else os.path.basename(path)
    return os.path.basename(path)


def entry(path, dirs, base=None):
    from .webui import read_md, safe_md
    out = {"path": path, "title": os.path.basename(path), "shown": False, "why": None, "bytes": None, "size": None,
           "mtime": None, "text": None, "cut": False}
    key = why_not(path, dirs)
    real = safe_md(path, dirs, base) if key is None else None  # the briefs' own check, last
    if real is None:
        out["why"] = t(key or "rules.no_file")
        return out
    try:
        st = os.stat(real)
        text, cut = read_md(real)
    except OSError:
        out["why"] = t("rules.no_file")
        return out
    out.update(title=title_of(text, path), shown=True, bytes=st.st_size, size=size_text(st.st_size),
               mtime=iso(st.st_mtime), text=text, cut=cut)
    return out


def listing(cfg, dirs=None, base=None):
    """`rules --json` (the web's /api/rules): every file of rules.files in its order, with its text or why not."""
    if dirs is None:
        dirs, base = docs_of(cfg)
    files = [entry(p, tuple(dirs), base) for p in (cfg.get("rules") or {}).get("files") or []]
    return {"v": 1, "files": files, "n": len(files), "shown": sum(1 for f in files if f["shown"])}


def listing_md(j):
    L = [t("rules.heading", ts=when(now_iso())), ""]
    if not j["files"]:
        return "\n".join(L + [t("rules.none")]) + "\n"
    L += [t("rules.summary", n=j["n"], shown=j["shown"]), "", t("rules.header"), "|---|---|---|---|"]
    for f in j["files"]:
        if f["shown"]:
            L.append("| %s | `%s` | %s%s | %s |" % (_cell(f["title"]), _cell(f["path"]), f["size"],
                                                   t("rules.cut_mark") if f["cut"] else "", when(f["mtime"])))
        else:
            L.append("| — | `%s` | ⚠ %s | — |" % (_cell(f["path"]), _cell(f["why"])))
    return "\n".join(L) + "\n"
