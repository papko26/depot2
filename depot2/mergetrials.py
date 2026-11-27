"""The merge queue's trials: while one PR rides (its CI, its merge, the deploy), the next ones are tried on top of it.

The trial of the PR after the one riding is `<merge.trial_prefix><PR>` = that PR's head with the riding one's head
merged in, by the same merge as every sync, in a throwaway worktree; pushed after the scan of what that push carries,
so CI runs on it early. The next trial is on top of that one, merge.presync_depth deep. A PR's own branch never gets
another PR's commits before that PR is merged.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from typing import TYPE_CHECKING

from . import i18n
from .i18n import t
from .store import expand_user_path

if TYPE_CHECKING:
    from collections.abc import Callable

    from .store import State

TRIAL_OK = ("pushed", "same")


class Hold(Exception):
    """A trial not built: why, under its status."""

    def __init__(self, status: str, reason: str) -> None:
        Exception.__init__(self, reason)
        self.status, self.reason = status, reason


class TrialsMixin:
    clone: str
    remote: str
    base: str
    state: State
    clock: Callable[[], float]
    out: Callable[[str], None]

    def depth(self):
        return int(self.m.get("presync_depth") or 0)

    def trial_branch(self, n):
        return "%s%d" % (self.m.get("trial_prefix") or "mq/", int(n))  # noqa: UP031

    def tree(self, cwd, ref):
        if not ref:
            return None
        code, out, _ = self.git(cwd, "rev-parse", "--verify", "-q", ref + "^{tree}")
        return out.strip() if code == 0 and out.strip() else None

    # --- the poll's hook ---
    def presync(self, eid):
        """Called by wait_runs before it sleeps: build (or rebuild) at most one trial. → True when it did (the poll
        needs no sleep then: the build took the time). Never raises: a trial never stops the queue.
        """
        if self.depth() <= 0:
            return False
        try:
            return self._presync(eid)
        except Exception as ex:
            from .merge import clip

            self.out(f"trials: {type(ex).__name__}: {clip(ex, 200)} (the ride goes on)")
            return False

    def trial_chain(self, q, cur):
        """The entries tried behind cur: the next waiting ones in the queue's order, presync_depth of them; the chain
        ends at one that is not plainly waiting for its sync (resumed past it, merged already).
        """
        es = q.entries(open_only=True)
        ids = [x["id"] for x in es]
        if cur["id"] not in ids:
            return []
        out = []
        for e2 in es[ids.index(cur["id"]) + 1 :]:
            if len(out) >= self.depth():
                break
            if e2["status"] != "waiting" or e2.get("merged") or e2.get("step") not in (None, "sync"):
                break
            out.append(e2)
        return out

    def _presync(self, eid):
        from .merge import MergeQueue, epoch

        q = MergeQueue(self.state)
        cur = q.get(eid)
        chain = self.trial_chain(q, cur)
        if not chain or not os.path.isdir(self.clone):  # noqa: PTH112 - unusable directories must test false, including on Python 3.11
            return False
        if self.git(self.clone, "fetch", "-q", self.remote, timeout=300)[0] != 0:
            return False
        main_ref = f"{self.remote}/{self.base}"
        main = self.rev(self.clone, main_ref)
        if cur.get("merged"):  # main has it: the trials go on top of main
            base = main
            before = self.rev(self.clone, cur["merged"]["sha"] + "^1") or main
        else:
            base = cur.get("head")
            if not base or not self.rev(self.clone, base) or not main or not self.is_ancestor(self.clone, main, base):
                return False  # not synced yet, or main moved past it: a trial on it would miss what main has
            before = main
        bases = [("main", before), ("#%d" % cur["pr"], base)]  # noqa: UP031
        label = "%s + #%d" % (main_ref, cur["pr"])  # noqa: UP031
        retry = float(self.m.get("trial_retry_min") or 10) * 60
        for e2 in chain:
            own = self.rev(self.clone, "{}/{}".format(self.remote, e2["branch"]))
            base_tree = self.tree(self.clone, base)
            t = e2.get("trial") or {}
            fresh = (
                bool(t)
                and t.get("base_tree") == base_tree
                and t.get("own") == own
                and not (t.get("status") in ("blocked", "failed") and self.clock() - (epoch(t.get("ts")) or 0) >= retry)
            )
            if not fresh:
                return self.build_trial(e2, own, base, base_tree, label, bases) is not None
            if t.get("status") not in TRIAL_OK:
                return False  # the chain stops where a trial would stop: the ones behind would be thrown away
            base = t["sha"]
            label = "%s + #%d" % (label, e2["pr"])  # noqa: UP031
            bases.append(("#%d" % e2["pr"], base))  # noqa: UP031
        return False

    # --- one trial ---
    def build_trial(self, e2, own, base, base_tree, label, bases):
        """The trial of e2 on top of base (main + the PRs ahead) → the record put on e2, or None (e2 rides already,
        or left the queue).
        """
        from .merge import Stop, clip, stamp

        n, br = e2["pr"], e2["branch"]
        name = self.trial_branch(n)
        rec = {
            "branch": name,
            "own": own,
            "base": base,
            "base_tree": base_tree,
            "on": label,
            "ts": stamp(self.clock()),
            "status": None,
        }
        try:
            if not own:
                raise Hold("blocked", f"no {self.remote}/{br}")
            pr = self.pr_view(n)
            if pr["state"] == "MERGED":
                raise Hold("blocked", "PR #%d is merged on GitHub already" % n)  # noqa: UP031
            try:
                self.check_pr(e2, pr)
                self.gates(e2)
            except Stop as s:
                raise Hold("blocked", s.reason)  # noqa: B904
            with self.scratch(self.clone, own, "trial") as tw:
                try:
                    res = self.merge_in(tw, base, br, label, "trial")
                except Stop as s:
                    rec.update(status="stop", reason=s.reason, conflicts=self.attribute(tw, s.files, bases, own))
                    return self.trial_put(e2, rec)
                rec.update(auto=res["auto"], resolved=res["resolved"])
                if res.get("golden"):
                    raise Hold(
                        "golden",
                        "{} takes main's side and is re-recorded at its turn: the trial is not pushed".format(
                            ((self.m.get("auto") or {}).get("golden") or {}).get("file")
                        ),
                    )
                if not res["push"]:  # main and the PRs ahead are in its head already: the head is the trial
                    rec.update(status="same", sha=own, tree=self.tree(tw, own))
                    return self.trial_put(e2, rec)
                head = res["head"]
                rec.update(sha=head, tree=self.tree(tw, head))
                try:
                    self.scan_trial(tw, name, head, [own, base])
                except Stop as s:
                    rec.update(status="stop", reason=s.reason)
                    return self.trial_put(e2, rec)
                ok, why = self.push_trial(tw, name, head)
                rec.update(status="pushed" if ok else "failed", reason=why)
        except Hold as h:
            rec.update(status=h.status, reason=h.reason)
        except Stop as s:  # the throwaway worktree, a git step
            rec.update(status="failed", reason=s.reason)
        except Exception as ex:
            rec.update(status="failed", reason=f"{type(ex).__name__}: {clip(ex, 200)}")
        return self.trial_put(e2, rec)

    def scan_trial(self, tw, name, head, parents):
        """The private scan of what the trial's push carries that GitHub has not: its merge commit and the files the
        merge made (push.export_range, scanned with merge.trial_scan_args, as the depot's own push does); the rest of
        its tree is on GitHub already, in the two branches it merges (each pushed after its scan). The whole tree's
        scan (merge.scan_args) is the PR's own, at its turn. Stop when not CLEAN.
        """
        from .backup import _nice
        from .merge import Stop
        from .push import export_range, is_clean

        if self.m.get("scan") is False:  # the project chose to push without a scan
            return
        if not self.m.get("scan"):
            raise Stop("merge.scan is not set: a scan command (its last line CLEAN), or false to push without one")
        d = tempfile.mkdtemp(prefix="merge-trial-scan-")
        try:
            export_range(tw, None, head, d, spec=[head, "--not"] + [p for p in parents if p])
            argv = [expand_user_path(self.m["scan"])] + [x.replace("{dir}", d) for x in self.m["trial_scan_args"]]
            if not (os.path.isfile(argv[0]) and os.access(argv[0], os.X_OK)):  # noqa: PTH113 - unusable filenames must test false, including on Python 3.11
                raise Stop(f"no private scan {argv[0]} (merge.scan): no push")
            code, out, _ = self.sh.run(_nice(argv), cwd=tempfile.gettempdir(), timeout=int(self.m["scan_timeout_s"]))
            if code is None:
                raise Stop("the private scan of %s took over %d s: no push" % (name, int(self.m["scan_timeout_s"])))  # noqa: UP031
            if not is_clean(out):  # its lines name private things: only the verdict
                raise Stop(f"the private scan of what {name} carries is not CLEAN: no push")
        finally:
            shutil.rmtree(d, ignore_errors=True)

    def attribute(self, tw, files, bases, own):
        """Who each file a trial stopped on conflicts with: the first of bases (main, then main with each PR ahead)
        whose merge into the PR's head already conflicts in it. → [{"file", "with"}].
        """
        files = list(files or [])
        who_of: dict[str, str] = {}
        for who, ref in bases[:-1]:
            left = [f for f in files if f not in who_of]
            if not left:
                break
            self.git(tw, "reset", "-q", "--hard", own)
            self.git(tw, "merge", "--no-ff", "--no-commit", ref)
            hit = set(self.unmerged(tw))
            self.git(tw, "merge", "--abort")
            self.git(tw, "reset", "-q", "--hard", own)
            who_of.update((f, who) for f in left if f in hit)
        return [{"file": f, "with": who_of.get(f, bases[-1][0])} for f in files]

    def push_trial(self, tw, name, head):
        """mq/<PR> → head: a plain push; when the branch holds an older trial that head does not contain, that old
        branch is deleted first (the queue's own throwaway branch: never forced, never a PR's). → (ok, why).
        """
        from .merge import clip

        remote = self.remote_head(tw, name)
        if remote == head:
            return True, None
        if remote and not self.is_ancestor(tw, remote, head):
            code, out, err = self.git(tw, "push", "-q", self.remote, "--delete", name, timeout=300)
            if code != 0:
                return False, f"git push --delete {name}: {clip(err or out, 160)}"
        code, out, err = self.git(tw, "push", "-q", self.remote, f"{head}:refs/heads/{name}", timeout=300)
        return (True, None) if code == 0 else (False, f"git push {name}: {clip(err or out, 160)}")

    def trial_put(self, e2, rec):
        """The record on e2 (still waiting), a line in its log and in the history, the line printed. → rec or None."""
        from .merge import log_line

        line = trial_line(e2["pr"], rec)
        with self.edit() as q:
            e = q.get(e2["id"])
            if e["status"] != "waiting":
                return None
            e["trial"] = rec
            log_line(e, line)
            q.save("trial", e["id"], status=rec["status"], sha=rec.get("sha"))
        self.out(line)
        return rec

    # --- throwing them away ---
    def delete_trial(self, name):
        """The remote's mq/<PR>, when it is there. → None, or why it was not deleted."""
        from .merge import clip

        if not self.remote_head(self.clone, name):
            return None
        code, out, err = self.git(self.clone, "push", "-q", self.remote, "--delete", name, timeout=300)
        return None if code == 0 else clip(err or out or f"exit {code}", 160)

    def discard_trials(self, why):
        """Every open entry's trial thrown away (the PR they stood on stopped): the branches deleted."""
        from .merge import MergeQueue, log_line

        if not os.path.isdir(self.clone):  # noqa: PTH112 - unusable directories must test false, including on Python 3.11
            return
        for e2 in MergeQueue(self.state).entries(open_only=True):
            t = e2.get("trial")
            if not t:
                continue
            err = self.delete_trial(t["branch"]) if t.get("status") == "pushed" else None
            line = "trial %s of #%d thrown away: %s%s" % (  # noqa: UP031
                t["branch"],
                e2["pr"],
                why,
                "" if not err else f" (its branch not deleted: {err})",
            )
            with self.edit() as q:
                e = q.get(e2["id"])
                e["trial"] = None
                log_line(e, line)
                q.save("trial_discard", e["id"], reason=why)
            self.out(line)

    def sweep_trials(self):
        """The queue's mq/<PR> branches that no waiting or riding entry's live trial is: deleted (after a merge)."""
        from .merge import MergeQueue

        if self.depth() <= 0 or not os.path.isdir(self.clone):  # noqa: PTH112 - unusable directories must test false, including on Python 3.11
            return []
        prefix = self.m.get("trial_prefix") or "mq/"
        code, out, _ = self.git(self.clone, "ls-remote", self.remote, f"refs/heads/{prefix}*", timeout=120)
        if code != 0:
            return []
        q = MergeQueue(self.state)
        keep = {
            ((e.get("trial") or {}).get("branch"), (e.get("trial") or {}).get("sha"))
            for e in q.entries(open_only=True)
            if (e.get("trial") or {}).get("status") == "pushed"
        }
        ours = {self.trial_branch(e["pr"]) for e in q.entries()}
        lines = []
        for ln in out.splitlines():
            parts = ln.split()
            if len(parts) != 2 or not parts[1].startswith("refs/heads/"):
                continue
            name = parts[1][len("refs/heads/") :]
            if name in ours and (name, parts[0]) not in keep:
                err = self.delete_trial(name)
                lines.append("trial {} deleted{}".format(name, "" if not err else f": not deleted ({err})"))
        return lines

    # --- at its own turn ---
    def trial_tree_line(self, e, wt, head):
        """After the PR's own sync: its tree against its trial's (CI by the tree pays when they are one)."""
        t = e.get("trial") or {}
        if t.get("status") not in TRIAL_OK or not t.get("tree"):
            return None
        tree = self.tree(wt, head)
        if tree == t["tree"]:
            return "the tree of {} ({}) is the trial {}'s ({}): CI has tested it".format(
                head[:7], tree[:7], t["branch"], (t.get("sha") or "?")[:7]
            )
        return "the tree of {} ({}) is not the trial {}'s ({}): CI anew".format(
            head[:7], (tree or "?")[:7], t["branch"], t["tree"][:7]
        )


# --- what is said ---

TRIAL_BRIEF = i18n.Labels("mergetrials.brief", ("stop", "golden", "blocked", "failed"))
TRIAL_CELL = i18n.Labels("mergetrials.cell", ("stop", "golden", "blocked", "failed"))


def conflicts_ru(rec):
    return ", ".join(t("merge.conflict_with", other=c["with"], file=c["file"]) for c in rec.get("conflicts") or [])


def trial_line(n, rec):
    """The runner's line for a trial record."""
    from .merge import clip

    st, name = rec.get("status"), rec.get("branch")
    head = "trial %s (#%d on %s)" % (name, n, rec.get("on"))  # noqa: UP031
    auto = "; the queue resolved: {}".format("; ".join(rec["auto"])) if rec.get("auto") else ""
    if st == "pushed":
        return "{}: {} pushed after the scan, tree {}: CI on {}{}".format(
            head, (rec.get("sha") or "?")[:7], (rec.get("tree") or "?")[:7], name, auto
        )
    if st == "same":
        return "{}: its head {} has them already: the head is the trial (nothing pushed)".format(
            head, (rec.get("sha") or "?")[:7]
        )
    if st == "stop" and rec.get("conflicts"):
        return t(
            "mergetrials.line_conflict",
            head=head,
            n=n,
            conflicts=conflicts_ru(rec),
            reason=clip(rec.get("reason"), 240),
        )
    return "{}: {}: {}".format(
        head,
        {"stop": "it would stop", "golden": "not pushed", "blocked": "not built", "failed": "failed"}.get(st, st),
        clip(rec.get("reason"), 240),
    )


def trial_brief(n, rec):
    """`merge --md`'s words for a trial (in the table's language)."""
    from .merge import clip

    st = rec.get("status")
    if st == "pushed":
        return t(
            "mergetrials.brief_pushed", n=n, branch=rec["branch"], sha=(rec.get("sha") or "?")[:7], on=rec.get("on")
        )
    if st == "same":
        return t("mergetrials.brief_same", n=n, sha=(rec.get("sha") or "?")[:7], on=rec.get("on"))
    if st == "stop" and rec.get("conflicts"):
        return t("merge.conflict_n", n=n, conflicts=conflicts_ru(rec))
    return "#%d — %s: %s" % (n, TRIAL_BRIEF.get(st, st), clip(rec.get("reason"), 140))  # noqa: UP031


def trial_cell(rec):
    """The short tail of a waiting entry's state cell."""
    st = rec.get("status")
    if st == "pushed":
        return t("mergetrials.cell_pushed", branch=rec["branch"], sha=(rec.get("sha") or "?")[:7])
    if st == "same":
        return t("mergetrials.cell_same")
    if st == "stop" and rec.get("conflicts"):
        return t("mergetrials.cell_conflict", conflicts=conflicts_ru(rec))
    return TRIAL_CELL.get(st) or t("mergetrials.cell_other", status=st)
