"""The merge queue's worktrees: the merged branch's worktree goes after its merge; `merge prune-worktrees` takes the
old ones.

After `gh pr merge` (merge.py step_merge) the runner removes the PR's worktree (merge.worktrees/<branch>, the
entry's `worktree`) with `git worktree remove` — never --force — when nothing in it can be lost:
  - it is a worktree of the main clone (merge.clone; `git worktree list`), not the main one, on that branch, not locked;
  - `git status --porcelain` is empty (ignored files and a node_modules symlink do not count);
  - its HEAD is in the remote's base branch (fetched first);
  - no other open merge queue entry and no stack object in a slot has that branch.
Else the worktree is left as it is, and one line says why: the runner's output and the entry's log, its `wt`,
`merge report`, `merge --json` (`last_merged`) and the web view. Only the checkout goes: the branch, its commits and
the PR stay. A cleanup never stops the queue.

`merge prune-worktrees [--dry-run]` applies the same rules to every worktree under merge.worktrees whose branch is
merged (a merged PR on GitHub or a merged queue entry, and no open PR), whose directory is named after its branch:
what went, what stayed and why, the space freed (the disk its files took, symlinks not followed); a real run writes
one line into merge-history.jsonl.
"""

import contextlib
import os
from pathlib import Path

from .i18n import t

NODE_MODULES = "node_modules"  # a worktree's symlink to the main clone's: never followed, never in the way
PR_LIMIT = 5000


def tree_bytes(path):
    total = 0
    for root, dirs, files in os.walk(path, followlinks=False):
        for name in dirs + files:
            with contextlib.suppress(OSError):
                total += os.lstat(os.path.join(root, name)).st_blocks * 512
    return total


def size_text(n):
    n = int(n or 0)
    if n >= 1024**3:
        return "%.2f GB" % (n / 1024.0**3)
    return "%.1f MB" % (n / 1024.0**2)


def worktree_list(text):
    """`git worktree list --porcelain` → [{path, head, branch (None: a detached HEAD), bare}], the main one first.
    (git 2.25 says nothing of a lock there: locked() reads it.)
    """
    out, cur = [], None
    for ln in [*text.splitlines(), ""]:
        if not ln.strip():
            if cur:
                out.append(cur)
            cur = None
            continue
        key, _, val = ln.partition(" ")
        if key == "worktree":
            cur = {"path": val, "head": None, "branch": None, "bare": False}
        elif cur is None:
            continue
        elif key == "HEAD":
            cur["head"] = val
        elif key == "branch":
            cur["branch"] = val.removeprefix("refs/heads/")
        elif key == "bare":
            cur["bare"] = True
    return out


def locked(path):
    """`git worktree lock`: the file `locked` in the worktree's gitdir (its .git file names it) → its reason, or None."""
    try:
        with open(os.path.join(path, ".git")) as f:
            line = f.read().strip()
    except OSError:
        return None
    if not line.startswith("gitdir:"):
        return None
    gd = line[len("gitdir:") :].strip()
    p = os.path.join(gd if os.path.isabs(gd) else os.path.join(path, gd), "locked")
    if not os.path.exists(p):
        return None
    try:
        with open(p) as f:
            return f.read().strip() or t("mergewt.no_reason")
    except OSError:
        return t("mergewt.no_reason")


def wt_text(res):
    if not res:
        return ""
    if res.get("removed"):
        return t("mergewt.text_removed", size=size_text(res.get("freed")))
    from .merge import clip

    return t("mergewt.text_kept", reason=clip(res.get("reason"), 160))


def wt_line(path, branch, res):
    """The runner's line (its output and the entry's log)."""
    if res.get("removed"):
        return t("mergewt.line_removed", path=path, size=size_text(res.get("freed")), branch=branch)
    return t("mergewt.line_kept", path=path, reason=res.get("reason"))


class WorktreesMixin:
    # --- the rules ---
    def wt_known(self):
        """The main clone's worktrees by their real path, the main one left out."""
        from .merge import MergeError, clip

        code, out, err = self.git(self.clone, "worktree", "list", "--porcelain")
        if code != 0:
            raise MergeError(f"git worktree list in {self.clone}: {clip(err or out, 160)}")
        return {os.path.realpath(w["path"]): w for w in worktree_list(out)[1:]}

    def wt_busy(self, branch, but=None):
        """Why a branch's worktree is in use: an open merge queue entry (but `but`), a stack object in a slot; or None."""
        from .merge import STATUS_RU, MergeQueue
        from .stack import Stack

        for e in MergeQueue(self.state).entries(open_only=True):
            if e.get("branch") == branch and e["id"] != but:
                return t("mergewt.busy_queue", id=e["id"], pr=e["pr"], status=STATUS_RU[e["status"]])
        for o in Stack(self.state, self.cfg).in_slots():
            if o.get("branch") == branch:
                return t("mergewt.busy_slot", id=o["id"])
        return None

    def wt_link_line(self, path, line):
        return line.strip() == "?? " + NODE_MODULES and os.path.islink(os.path.join(path, NODE_MODULES))

    def wt_verdict(self, path, branch, known):
        """None: the worktree at path may go; else why it stays, in one line."""
        from .merge import clip

        if not os.path.isdir(path):
            return t("mergewt.no_dir")
        w = known.get(os.path.realpath(path))
        if w is None:
            return t("mergewt.not_a_worktree", clone=self.clone)
        if w["branch"] != branch:
            return (
                t("mergewt.on_branch", cur=w["branch"], branch=branch)
                if w["branch"]
                else t("mergewt.on_detached", branch=branch)
            )
        lk = locked(path)
        if lk:
            return t("mergewt.locked", reason=clip(lk, 80))
        # --no-optional-locks: the index is only read, never refreshed in place (an agent may be at work in it)
        code, out, err = self.git(path, "--no-optional-locks", "status", "--porcelain")
        if code != 0:
            return t("mergewt.status_failed", error=clip(err or out, 120))
        dirty = [ln for ln in out.splitlines() if ln.strip() and not self.wt_link_line(path, ln)]
        if dirty:
            more = t("mergewt.dirty_more", n=len(dirty) - 3) if len(dirty) > 3 else ""
            return t("mergewt.dirty", lines=clip("; ".join(ln.strip() for ln in dirty[:3]), 140), more=more)
        main = f"{self.remote}/{self.base}"
        head = self.rev(path, "HEAD")
        if not head:
            return t("mergewt.no_head")
        if not self.is_ancestor(path, head, main):
            n = self.git(path, "rev-list", "--count", f"{main}..{head}")[1].strip()
            return t("mergewt.head_not_in", head=head[:7], main=main, n=n or "?")
        return None

    def wt_remove(self, path):
        from .merge import clip

        size = tree_bytes(path)
        link = os.path.join(path, NODE_MODULES)
        target = None
        if os.path.islink(link) and self.git(path, "check-ignore", "-q", NODE_MODULES)[0] != 0:
            target = os.readlink(link)
            Path(link).unlink()  # the link itself: what it points at is never touched
        code, out, err = self.git(self.clone, "worktree", "remove", path)
        if code != 0:
            if target is not None and os.path.isdir(path) and not os.path.lexists(link):
                os.symlink(target, link)
            return {"removed": False, "reason": f"git worktree remove: {clip(err or out, 160)}"}
        return {"removed": True, "freed": size}

    # --- after the merge (step_merge) ---
    def after_merge_worktree(self, eid):
        """The merged PR's worktree goes when nothing in it can be lost; else it stays, and the line says why. Never
        raises (but the entry dropped meanwhile: update's Dropped).
        """
        from .merge import clip, expand, stamp

        e = self.entry(eid)
        path = e.get("worktree") or os.path.join(expand(self.m["worktrees"]), e["branch"])
        try:
            self.git(self.clone, "fetch", "-q", self.remote, self.base, timeout=300)  # origin/main with the merge in it
            why = self.wt_busy(e["branch"], but=eid) or self.wt_verdict(path, e["branch"], self.wt_known())
            res = self.wt_remove(path) if why is None else {"removed": False, "reason": why}
        except Exception as ex:
            res = {"removed": False, "reason": f"{type(ex).__name__}: {clip(ex, 160)}"}
        res.update(path=path, ts=stamp(self.clock()))
        self.update(eid, "worktree", wt=res, say=wt_line(path, e["branch"], res))
        return res

    # --- `merge prune-worktrees [--dry-run]` ---
    def pr_heads(self):
        """GitHub's PRs by their branch: {branch: [(number, state)]}, the newest first."""
        data = (
            self.gh_json(
                "pr",
                "list",
                "-R",
                self.repo,
                "--state",
                "all",
                "--limit",
                str(PR_LIMIT),
                "--json",
                "number,headRefName,state",
            )
            or []
        )
        out: dict[str | None, list[tuple[int, str]]] = {}
        for p in data:
            out.setdefault(p.get("headRefName"), []).append((int(p["number"]), str(p.get("state") or "").upper()))
        return out

    def prune_worktrees(self, dry_run=False):
        """Every worktree under merge.worktrees whose branch is merged, by the rules above → lines."""
        from .merge import HISTORY, MergeQueue, clip, expand, stamp
        from .store import append_jsonl

        root = os.path.realpath(expand(self.m["worktrees"]))
        main = f"{self.remote}/{self.base}"
        L = []
        code, out, err = self.git(self.clone, "fetch", "-q", self.remote, timeout=300)
        if code != 0:  # an old origin/main only keeps more of them
            L.append(t("mergewt.fetch_failed", clone=self.clone, error=clip(err or out, 120), main=main))
        known = self.wt_known()
        prs = self.pr_heads()
        merged_q = {}
        for e in MergeQueue(self.state).entries():
            if e["status"] == "merged":
                merged_q[e["branch"]] = e["pr"]
        under = sorted(p for p in known if p.startswith(root + os.sep) and os.path.isdir(p))  # (a gone one: nothing)
        rows = [(os.path.relpath(p, root), p, known[p]) for p in under]
        removed, kept, freed = [], [], 0
        for name, p, w in rows:
            br, why, pr_n = w["branch"], None, None
            if not br:
                why = t("mergewt.detached")
            elif name != br:
                why = t("mergewt.dir_not_branch", branch=br, name=name)
            else:
                mine = prs.get(br, [])
                opened = [n for n, s in mine if s == "OPEN"]
                merged = [n for n, s in mine if s == "MERGED"] or ([merged_q[br]] if br in merged_q else [])
                if opened:
                    why = t("mergewt.pr_open", n=opened[0])
                elif not merged:
                    why = t(
                        "mergewt.not_merged",
                        why=t("mergewt.closed_unmerged", n=mine[0][0]) if mine else t("mergewt.no_pr"),
                    )
                else:
                    pr_n = merged[0]
                    why = self.wt_busy(br) or self.wt_verdict(p, br, known)
            if why:
                kept.append((name, why))
                continue
            if dry_run:
                size = tree_bytes(p)
                freed += size
                removed.append((name, t("mergewt.pr_merged", n=pr_n, size=size_text(size)), size))
                continue
            res = self.wt_remove(p)
            if res["removed"]:
                freed += res["freed"]
                removed.append((name, t("mergewt.pr_merged", n=pr_n, size=size_text(res["freed"])), res["freed"]))
            else:
                kept.append((name, res["reason"]))
        others = 0
        if os.path.isdir(root):
            for d in sorted(os.listdir(root)):
                p = os.path.realpath(os.path.join(root, d))
                if p in known or not os.path.isdir(p) or any(k.startswith(p + os.sep) for k in under):
                    continue
                kept.append((d, t("mergewt.not_a_worktree", clone=self.clone)))
                others += 1
        how = "dry" if dry_run else "real"
        L.insert(
            0,
            t(
                "mergewt.prune_head",
                dry=t("mergewt.prune_dry") if dry_run else "",
                root=root,
                n=len(rows) + others,
                clone=self.clone,
                wts=len(rows),
                main=main,
                sha=(self.rev(self.clone, main) or "?")[:7],
            ),
        )
        L.append(t("mergewt.prune_removed_" + how, n=len(removed), size=size_text(freed)))
        L += [f"  - {n}: {x}" for n, x, _ in removed] or ["  —"]
        L.append(t("mergewt.prune_kept", n=len(kept)))
        L += [f"  - {n}: {x}" for n, x in sorted(kept)] or ["  —"]
        L.append(t("mergewt.prune_total_" + how, n=len(removed), kept=len(kept), size=size_text(freed)))
        if not dry_run:
            with self.state.lock():
                path = self.state.path(HISTORY)
                append_jsonl(
                    path,
                    {
                        "ts": stamp(self.clock()),
                        "event": "prune_worktrees",
                        "entry": None,
                        "removed": [{"name": n, "bytes": b} for n, _, b in removed],
                        "kept": len(kept),
                        "freed": freed,
                    },
                )
                self.state.touch(path)
                self.state.commit(
                    "team: merge prune-worktrees: %d removed, %s freed" % (len(removed), size_text(freed))  # noqa: UP031
                )
        return L
