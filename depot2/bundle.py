"""`prep`: the code gathers the round's context ONCE into one bundle that every character reads.

ballot:  the questions file (its points are the questions)
review:  the PRs merged since the last review (gh), the open PRs, and any --context files
discuss: a topic file
brainstorm: the topic file the team's approved brainstorm wrote
"""

import glob
import json
import os
import re
import subprocess

from . import i18n
from .i18n import t
from .store import read_text

# A point heading: up to three letters (latin or Cyrillic, U+0410-U+044F) and a number, as in "### B12." or "## Q3)".
HEADING = re.compile(r"^(#{2,4})\s+([A-Za-z\u0410-\u044f]{0,3}\d+[a-z]?)[.:)]\s*(.*)$")
FIELD = re.compile(r"^\*\*([^*]+?):?\*\*:?\s*(.*)$")
# A point's fields, in every language: the recommendation, the question in full (preferred), the question.
REC_FIELD = re.compile(i18n.rx("in.bundle.rec"), re.I)
QUESTION_FIELDS = (re.compile(i18n.rx("in.bundle.question_full"), re.I),
                   re.compile(i18n.rx("in.bundle.question"), re.I))


class Bundle:
    def __init__(self):
        self.parts = []      # (title, text)
        self.cuts = []       # human-readable notes of what was cut
        self.points = []     # [{"id", "title", "question", "recommendation", "source"}]
        self.sources = []    # paths and commands read
        self.pr = None       # `--pr`: the PR's gh view

    def add(self, title, text, cap, what):
        text = text.strip()
        if len(text) > cap:
            self.cuts.append(t("bundle.cut_note", what=what, cap=int(cap), n=len(text)))
            text = text[:cap].rstrip() + "\n" + t("bundle.cut_mark")
        self.parts.append((title, text))

    def text(self, max_chars):
        out = []
        for title, body in self.parts:
            out.append(f"## {title}\n\n{body}\n")
        body = "\n".join(out)
        if len(body) > max_chars:
            self.cuts.append(t("bundle.cut_all", cap=int(max_chars), n=len(body)))
            body = body[:max_chars].rstrip() + "\n" + t("bundle.cut_mark")
        head = t("bundle.head") + "\n\n"
        if self.cuts:
            head += t("bundle.cuts_head") + "\n" + "\n".join("- " + c for c in self.cuts) + "\n\n"
        return head + body


TITLE = i18n.Labels("bundle.title", ("ballot", "discuss", "brainstorm", "review"))  # the input's section title
NUMBERED = re.compile(r"^(\d+)[.)]\s+(.*)$")
BOLD_TITLE = re.compile(r"^\*\*(.+?)\*\*[:.]?\s*(.*)$")
QUESTION_SECTION = re.compile(i18n.rx("in.bundle.question"), re.I)


def parse_points(text):
    """The input's points.


    """
    points = _heading_points(text)
    return points if points else _numbered_points(text)


def _numbered_points(text):
    points = []
    in_section = False
    cur = None
    for line in text.splitlines():
        if line.startswith("#"):
            in_section = bool(QUESTION_SECTION.search(line))
            cur = None
            continue
        if not in_section:
            continue
        m = NUMBERED.match(line)
        if m:
            body = m.group(2).strip()
            bm = BOLD_TITLE.match(body)
            if bm:
                title, rest = bm.group(1).strip().rstrip(".:"), bm.group(2).strip()
            else:
                first = re.split(r"(?<=[.?!:])\s", body, 1)  # noqa: B034
                title = first[0].rstrip(".:")[:100]
                rest = first[1].strip() if len(first) > 1 else ""
            cur = {"id": f"Q{m.group(1)}", "title": title,
                   "question": body.replace("**", "") if rest else "",
                   "recommendation": "", "source": "input"}
            points.append(cur)
        elif cur is not None and line.strip() and (line[0] in " \t" or line.lstrip().startswith("-")):
            cur["question"] = (cur["question"] + "\n" + line.strip()).strip()
        elif cur is not None and line.strip():
            cur = None  # a paragraph after the list ends the item
    for p in points:
        if not p["question"]:
            p["question"] = p["title"]
    check_unique(points)
    return points


def check_unique(points):
    seen = set()
    for p in points:
        if p["id"] in seen:
            raise ValueError("duplicate point id {} in the input".format(p["id"]))
        seen.add(p["id"])


def _heading_points(text):
    lines = text.splitlines()
    heads = [(i, HEADING.match(l)) for i, l in enumerate(lines)]  # noqa: E741
    heads = [(i, m) for i, m in heads if m]
    points = []
    for n, (i, m) in enumerate(heads):
        end = heads[n + 1][0] if n + 1 < len(heads) else len(lines)
        # A heading of a higher level also ends the point.
        for j in range(i + 1, end):
            if lines[j].startswith("#") and len(lines[j]) - len(lines[j].lstrip("#")) <= len(m.group(1)) \
                    and not HEADING.match(lines[j]):
                end = j
                break
        body = lines[i + 1:end]
        fields = {}
        current = None
        loose = []
        for l in body:  # noqa: E741
            if l.lstrip().startswith(("|", "---")):
                current = None  # a table or a rule ends the field
                continue
            fm = FIELD.match(l.strip())
            if fm:
                current = fm.group(1).strip().lower()
                fields[current] = [fm.group(2)]
            elif current is not None:
                fields[current].append(l)
            else:
                loose.append(l)
        joined = {k: "\n".join(v).strip() for k, v in fields.items()}
        rec = next((v for k, v in joined.items() if REC_FIELD.fullmatch(k)), "")
        q = next((v for rx in QUESTION_FIELDS for k, v in joined.items() if rx.fullmatch(k)), "")
        if not q:
            q = "\n".join(loose).strip()
        pid = m.group(2)
        points.append({"id": pid, "title": m.group(3).strip(), "question": q.strip(),
                       "recommendation": rec.strip(), "source": "input"})
    check_unique(points)
    return points


def latest_decisions(cfg):
    if not cfg["bundle"].get("decisions_glob"):
        return None
    paths = sorted(glob.glob(os.path.expanduser(cfg["bundle"]["decisions_glob"])))
    return paths[-1] if paths else None


def gh_json(args):
    """A read-only gh call. The tool never writes to GitHub."""
    if args[0] not in ("pr",) or args[1] not in ("list", "view", "diff"):
        raise ValueError("gh: read-only pr list/view only")
    p = subprocess.run(["gh", *args], capture_output=True, text=True, timeout=120)
    if p.returncode != 0:
        raise RuntimeError("gh {} failed: {}".format(" ".join(args[:2]), p.stderr.strip()[:300]))
    return json.loads(p.stdout or "[]")


def _pr_block(pr, cap):
    body = (pr.get("body") or "").strip()
    cut = ""
    if len(body) > cap:
        cut = "\n" + t("bundle.pr_body_cut", cap=int(cap), n=len(body))
        body = body[:cap].rstrip()
    when = pr.get("mergedAt") or ("draft" if pr.get("isDraft") else "open")
    return "### #{} {} ({})\n\n{}{}\n".format(pr["number"], pr.get("title", ""), when, body, cut)


PR_FIELDS = ("number,title,body,state,isDraft,mergedAt,author,headRefName,headRefOid,baseRefName,"
             "additions,deletions,changedFiles,files,url")


def _one_pr(b, cfg, n, gh):
    """`--pr N`: one PR's body and the files it changes (read-only gh pr view)."""
    bc = cfg["bundle"]
    from .config import repo as repo_of
    pr = gh(["pr", "view", str(int(n)), "-R", repo_of(cfg), "--json", PR_FIELDS])
    b.sources.append("gh pr view %d -R %s" % (int(n), repo_of(cfg)))  # noqa: UP031
    b.pr = pr
    files = pr.get("files") or []
    head = ["#{} {}".format(pr["number"], pr.get("title", "")),
            "- state: {}{}; {} → {}; head {}".format(
                pr.get("state"), " (draft)" if pr.get("isDraft") else "", pr.get("headRefName"),
                pr.get("baseRefName"), pr.get("headRefOid")),
            "- {} files, +{} -{}".format(pr.get("changedFiles", len(files)), pr.get("additions"), pr.get("deletions")),
            "- {}".format(pr.get("url", ""))]
    b.add(t("bundle.pr_title", n=pr["number"]), "\n".join(head), 2000, t("bundle.pr_title_what"))
    b.add(t("bundle.pr_body", n=pr["number"]), pr.get("body") or t("bundle.empty"), bc["pr_body_max_chars"] * 4,
          t("bundle.pr_body_what", n=pr["number"]))
    lines = ["- {} (+{} -{})".format(f.get("path"), f.get("additions"), f.get("deletions")) for f in files]
    b.add(t("bundle.pr_files", n=pr["number"], count=len(files)), "\n".join(lines) or t("prompts.none"),
          bc["context_max_chars"], t("bundle.pr_files_what"))


def add_principles(b, state, cfg):
    from .principles import Principles
    p = Principles(state, cfg)
    if not p.entries(True):
        return
    b.sources.append("depot2 principles render ({})".format(state.path("principles.json")))
    b.add(t("bundle.principles"), p.round_line() + "\n\n" + p.render(banner=False),
          cfg["bundle"].get("principles_max_chars", 24000), t("bundle.principles_what"))


def build(kind, cfg, input_path=None, contexts=(), since=None, gh=gh_json, pr=None, state=None):
    """The round's bundle; with the state, the depot's principles too (add_principles)."""
    b = Bundle()
    bc = cfg["bundle"]
    if pr is not None and kind != "review":
        raise ValueError("--pr goes with --kind review")
    if kind in ("ballot", "discuss", "brainstorm"):
        if not input_path:
            raise ValueError(f"--input is required for a {kind} round")
    if input_path:
        text = read_text(os.path.expanduser(input_path))
        if text is None:
            raise ValueError(f"no such input: {input_path}")
        b.sources.append(os.path.abspath(os.path.expanduser(input_path)))
        b.points = parse_points(text)
        if kind == "ballot" and not b.points:
            raise ValueError("the ballot input has no '### B<n>. title' questions")
        b.add(TITLE[kind], text, bc["max_chars"] // 2, os.path.basename(input_path))
    if kind == "review" and pr is not None:
        _one_pr(b, cfg, pr, gh)
    elif kind == "review":
        from .config import repo as repo_of
        repo = repo_of(cfg)
        merged = gh(["pr", "list", "-R", repo, "--state", "merged", "--limit",
                     str(bc["merged_pr_limit"]), "--json", "number,title,mergedAt,body"])
        b.sources.append("gh pr list -R {} --state merged ({})".format(repo, "merged after " + since if since else "the last 10"))
        merged.sort(key=lambda p: p.get("mergedAt") or "")
        if since:
            merged = [p for p in merged if (p.get("mergedAt") or "") > since]
        else:
            merged = merged[-10:]
            b.cuts.append(t("bundle.no_last_review"))
        text = "\n".join(_pr_block(p, bc["pr_body_max_chars"]) for p in merged) or t("prompts.none")
        b.add(t("bundle.merged_since", n=len(merged), since=since) if since else t("bundle.merged", n=len(merged)),
              text, bc["max_chars"] // 2, t("bundle.merged_what"))
        opened = gh(["pr", "list", "-R", repo, "--state", "open", "--json",
                     "number,title,isDraft,body"])
        b.sources.append(f"gh pr list -R {repo} --state open")
        text = "\n".join(_pr_block(p, bc["open_pr_body_max_chars"]) for p in opened) or t("prompts.none")
        b.add(t("bundle.open", n=len(opened)), text, bc["max_chars"] // 4, t("bundle.open_what"))
    for c in contexts or ():
        text = read_text(os.path.expanduser(c))
        if text is None:
            raise ValueError(f"no such context file: {c}")
        b.sources.append(os.path.abspath(os.path.expanduser(c)))
        b.add(t("bundle.context", name=os.path.basename(c)), text, bc["context_max_chars"], os.path.basename(c))
    dec = latest_decisions(cfg)
    if dec:
        b.sources.append(dec)
        b.add(t("bundle.decisions", name=os.path.basename(dec)), read_text(dec),
              bc["decisions_max_chars"], os.path.basename(dec))
    elif bc.get("decisions_glob"):
        b.cuts.append(t("bundle.no_decisions", glob=bc["decisions_glob"]))
    if state is not None:
        add_principles(b, state, cfg)
    from . import hooks
    for parts in hooks.call_all(cfg, "round_context", cfg, kind):
        for title, text in parts:
            b.sources.append(f"plugin: {title}")
            b.add(str(title), str(text), bc["context_max_chars"], str(title))
    return b
