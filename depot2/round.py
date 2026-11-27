"""One team round: prep → round 1 (independent) → round 2 (the debate) → the stakes debited and
at once returned → the report. Calls run one after another, all on the same model, effort and
--add-dir list; reading and thinking cost the characters nothing. A round a fatal error aborted goes on with
`round --resume` (its saved answers stay); every call's raw stream passes the private scan first (push.gate_raw).
"""

import copy
import datetime
import json
import os
import shutil
import subprocess
import tempfile

from . import bundle as bundle_mod
from . import characters, effort, extras, prompts, push, schema
from . import stack as stack_mod
from .executors import CallFailed, CallSpec, FatalError, describe, run_call
from .i18n import t
from .ledger import AUTO_REASON, Ledger
from .persona import Persona
from .persona import genders as personas_genders
from .report import render as render_report
from .store import (
    atomic_write,
    now_iso,
    read_json,
    sha256_text,
    today,
    write_json,
    write_new,
)


class RoundStop(RuntimeError):
    pass


class RoundAborted(RuntimeError):
    pass


def to_utc(ts):
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    d = datetime.datetime.fromisoformat(ts)
    if d.tzinfo is None:
        d = d.astimezone()  # local time
    return d.astimezone(datetime.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_round_id(state, kind, pr=None, only=None):
    base = f"{today()}-{kind}" + ("-pr%d" % int(pr) if pr is not None else "") + (f"-solo-{only}" if only else "")  # noqa: UP031
    rid, n = base, 2
    while os.path.exists(os.path.join(state.rounds_dir(), rid)):
        rid = "%s-%d" % (base, n)  # noqa: UP031
        n += 1
    return rid


def last_review_since(state):
    rd = state.rounds_dir()
    best = None
    if os.path.isdir(rd):
        for d in os.listdir(rd):
            rj = read_json(os.path.join(rd, d, "round.json"))
            if (
                rj and rj.get("kind") == "review" and rj.get("status") in ("done", "imported") and not rj.get("pr")
            ):  # a one-PR review does not move the merge window
                ts = to_utc(rj["created"])
                best = ts if best is None or ts > best else best
    return best


class Guard:
    """The fuse stays a share: a call stops the round when any window it saw is round.stop_at_utilization
    full. Before each call it also adds, to the window's last reading, the spend since that reading and
    call when that would reach the stop.
    """

    def __init__(self, cfg) -> None:
        self.cfg = cfg
        self.budget = float(cfg["round"]["budget_mtok"])
        self.max_calls = int(cfg["round"]["max_calls"])
        self.stop_at = float(cfg["round"]["stop_at_utilization"])
        self.spent = 0.0  # MTok
        self.calls = 0
        self.stopped = None
        self.util_first = None
        self.util_last = None  # ... and after its last one
        self.week = None  # the usage window's last reading (a share) ...
        self.since = 0.0  # ... and the MTok spent from the call that read it on

    def check(self, call_cap):
        """call_cap: the next call's cap, MTok."""

        def f(x):
            return effort.fmt(x, self.cfg)

        if self.stopped:
            raise RoundStop(self.stopped)
        if self.calls >= self.max_calls:
            raise RoundStop("cap: %d calls per round" % self.max_calls)  # noqa: UP031
        if self.spent + call_cap > self.budget + 1e-9:
            raise RoundStop(f"cap: {f(self.spent)} spent + {f(call_cap)} for this call > {f(self.budget)} per round")
        if self.week is not None:
            ahead = self.week * 100 + (self.since + call_cap) / effort.per_pct(self.cfg)
            if ahead >= self.stop_at * 100:
                raise RoundStop(
                    "fuse: the window {} + {} since + {} for this call ≈ {}%UW ≥ the stop at {}".format(
                        "%.1f%%UW" % (self.week * 100),
                        f(self.since),
                        f(call_cap),
                        f"{ahead:.2f}",
                        "%.0f%%UW" % (self.stop_at * 100),
                    )
                )

    def record(self, res):
        self.calls += 1
        m = res.get("mtok")
        m = effort.call_mtok(res, self.cfg) if m is None else m
        self.spent += m
        w = res.get("week_utilization")
        if w is not None:
            self.week, self.since = w, m  # the window was read as the call began: its spend comes after
        else:
            self.since += m
        u = res.get("utilization")
        if u is not None:
            self.util_first = u if self.util_first is None else self.util_first
            self.util_last = u
        if u is not None and u >= self.stop_at:
            self.stopped = f"usage window {u * 100:.0f}% full (stop at {self.stop_at * 100:.0f}%)"


def repo_snapshot(cfg, out=print, commit_wanted=None):
    """A fresh read-only copy of origin/main (or, for a one-PR review, the PR's head commit) for
    every pass: git fetch, then git archive into a temp dir. Returns (dir, commit), or
    (None, None) when there is no clone.
    """
    r = cfg.get("deep_repo") or {}
    clone = os.path.expanduser(r.get("clone") or "")
    if not clone or not os.path.isdir(clone):
        return None, None

    def git(*args, **kw):
        return subprocess.run(["git", "-C", clone, *list(args)], capture_output=True, timeout=120, **kw)

    if r.get("fetch"):
        f = git("fetch", "-q", "origin", commit_wanted or "main")
        if f.returncode != 0:
            out(
                "warning: git fetch failed, reading what is already fetched: {}".format(
                    f.stderr.decode("utf-8", "replace").strip()[:200]
                )
            )
    rev = git("rev-parse", "--verify", (commit_wanted or r.get("ref", "origin/main")) + "^{commit}")
    if rev.returncode != 0:
        return None, None
    commit = rev.stdout.decode().strip()
    d = tempfile.mkdtemp(prefix="team-repo-")
    arch = subprocess.Popen(["git", "-C", clone, "archive", "--format=tar", commit], stdout=subprocess.PIPE)
    tar = subprocess.run(["tar", "-x", "-C", d], stdin=arch.stdout, stderr=subprocess.PIPE, timeout=300)
    arch.stdout.close()
    if arch.wait(timeout=60) != 0 or tar.returncode != 0:
        shutil.rmtree(d, ignore_errors=True)
        raise RuntimeError("git archive {} failed: {}".format(commit, tar.stderr.decode("utf-8", "replace")[:200]))
    return d, commit


def positions_from(answer, point_ids, free_max):
    out, notes = {}, []
    for item in (answer or {}).get("points") or []:
        pid = item["id"]
        if pid not in point_ids:
            notes.append(t("round.unknown_point", id=pid))
            continue
        if pid in out:
            continue
        comment = (item.get("comment") or "").strip()
        stake = max(0, int(item.get("stake") or 0))
        if stake == 0:
            comment = _one_line(comment, free_max)
        out[pid] = {"position": item["position"], "stake": stake, "comment": comment}
    return out, notes


def trim_to(positions, available, order, free_max):
    """Keep the total stake within the balance: cut the smallest stakes first (the later point
    first on a tie). Returns notes of what the code cut.
    """
    total = sum(a["stake"] for a in positions.values())
    excess = total - max(0, available)
    notes = []
    if excess <= 0:
        return notes
    staked = [pid for pid in positions if positions[pid]["stake"] > 0]
    staked.sort(key=lambda pid: (positions[pid]["stake"], -order.index(pid)))
    for pid in staked:
        if excess <= 0:
            break
        a = positions[pid]
        cut = min(a["stake"], excess)
        notes.append("%s: %d → %d" % (pid, a["stake"], a["stake"] - cut))  # noqa: UP031
        a["stake"] -= cut
        excess -= cut
        if a["stake"] == 0:
            a["comment"] = _one_line(a["comment"], free_max)
    return notes


def _one_line(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


SOLO_KINDS = ("ballot", "review", "discuss")


def solo_caps(cfg, lock_calls):
    """A solo round's caps, scaled to one character: solo.budget_mtok, or round.budget_mtok / 5 raised
    to the caps of the calls it plans (round 1 + the lock pass), so the scaling never refuses a
    planned call; and one call per pass. MTok.
    """
    planned = float(cfg["free"]["budget_mtok_r1"]) + (float(cfg["lock"]["budget_mtok"]) if lock_calls else 0.0)
    given = cfg["solo"]["budget_mtok"]
    budget = (
        float(given) if given is not None else max(float(cfg["round"]["budget_mtok"]) / len(characters.IDS), planned)
    )
    return round(budget, 4), min(int(cfg["round"]["max_calls"]), 1 + lock_calls)


def read_dirs(cfg):
    """The dirs every pass reads besides the snapshot: config read_dirs that exist."""
    return [os.path.expanduser(d) for d in cfg["read_dirs"] if os.path.isdir(os.path.expanduser(d))]


def run_round(
    state,
    cfg,
    kind,
    input_path=None,
    contexts=(),
    since=None,
    dry_run=False,
    runner=None,
    out=print,
    gh=None,
    snapshot=None,
    pr=None,
    brainstorm=None,
    only=None,
    resume=None,
):
    snapshot = snapshot or repo_snapshot
    if resume is not None:
        rec = resumable(state, resume)
        holder = {"pr": rec.get("pr"), "only": rec.get("only"), "resume": rec}
        try:
            return _run_round(
                state,
                cfg,
                rec["kind"],
                rec.get("input"),
                rec.get("contexts") or (),
                rec.get("since"),
                dry_run,
                runner,
                out,
                gh,
                snapshot,
                holder,
            )
        finally:
            if holder.get("dir"):
                shutil.rmtree(holder["dir"], ignore_errors=True)
    holder = {"pr": pr, "only": only}
    if only is not None:
        if only not in characters.IDS:
            raise ValueError(f"unknown character {only!r}")
        if kind not in SOLO_KINDS:
            raise ValueError("a solo round is {}; a brainstorm is the whole team's".format("/".join(SOLO_KINDS)))
    if (kind == "brainstorm") != bool(brainstorm):
        raise ValueError("--kind brainstorm goes with --brainstorm bs-NNNN, and only it")
    if brainstorm:
        entry = extras.Brainstorms(state).get(brainstorm)
        if entry["status"] != "approved":
            raise ValueError("{} is {}: only an approved brainstorm runs".format(brainstorm, entry["status"]))
        if input_path:
            raise ValueError("a brainstorm round reads its own topic file ({}), no --input".format(entry["topic_file"]))
        input_path = entry["topic_file"]
        holder["brainstorm"] = entry
    try:
        return _run_round(state, cfg, kind, input_path, contexts, since, dry_run, runner, out, gh, snapshot, holder)
    finally:
        if holder.get("dir"):
            shutil.rmtree(holder["dir"], ignore_errors=True)


def resumable(state, rid):
    """The saved round `round --resume` goes on with: only an aborted one. It stopped on a fatal error in round 1 or 2
    (a fatal error in the lock pass keeps the round, done), before any stake, order or lock was debited, so going on
    debits nothing twice. A brainstorm is not resumed: its fee came back when it aborted (`_brainstorm_failed`).
    """
    rdir = os.path.join(state.rounds_dir(), rid)
    rec = read_json(os.path.join(rdir, "round.json"))
    if rec is None:
        raise ValueError(f"no round {rid} in {state.rounds_dir()}")
    if rec.get("status") != "aborted":
        raise ValueError(
            "round {} is {}: --resume goes on only with an aborted round (a fatal error in round 1 or 2)".format(
                rid, rec.get("status")
            )
        )
    if rec.get("kind") == "brainstorm" or rec.get("brainstorm_of"):
        raise ValueError(
            f"round {rid} is a brainstorm: its fee came back when it aborted, so it is not resumed; the "
            "owner approves it again and it runs anew"
        )
    if not os.path.isfile(os.path.join(rdir, "bundle.md")):
        raise ValueError(f"round {rid} has no bundle.md: nothing to go on with")
    return rec


def quarantine_note(q):
    """The report's line for a raw stream the private scan sent to the quarantine (push.gate_raw)."""
    return t("round.quarantine", file=q["file"], why=q["why"], to=q["to"])


def saved_answers(rdir):
    """{(character, "r1"|"r2"): the answer} the round saved in answers/ (the lock pass's are never resumed)."""
    d = os.path.join(rdir, "answers")
    out = {}
    for f in sorted(os.listdir(d)) if os.path.isdir(d) else ():
        cid, _, stage = f[: -len(".json")].rpartition("-") if f.endswith(".json") else ("", "", "")
        if cid in characters.IDS and stage in ("r1", "r2"):
            out[(cid, stage)] = read_json(os.path.join(d, f))
    return out


def _run_round(state, cfg, kind, input_path, contexts, since, dry_run, runner, out, gh, snapshot, holder):
    if runner is None:

        def runner(spec):
            return run_call(spec, cfg, validate=schema.validate)

    ledger = Ledger(state)
    pr = holder.get("pr")
    only = holder.get("only")
    resume = holder.get("resume")  # the aborted round's record (`round --resume`), else None
    cast = [only] if only else list(characters.IDS)  # who answers in this round
    stk = stack_mod.Stack(state, cfg)
    if resume is None:
        if kind == "review" and since is None and pr is None:
            since = last_review_since(state)
        since_utc = to_utc(since) if since else None
        b = bundle_mod.build(
            kind, cfg, input_path, contexts, since_utc, pr=pr, state=state, **({"gh": gh} if gh else {})
        )
        # The results of the orders done since they were last shown: the characters read the facts.
        shown = extras.Orders(state).unshown_done()
        if shown:
            b.add(
                t("round.orders_results"),
                _orders_text(shown),
                cfg["bundle"]["context_max_chars"],
                t("round.orders_results_what"),
            )
        # The stack (with its ids and locks) and the owner's ballot queue: what the team works within.
        b.add(t("round.stack"), stk.md(heading=False), cfg["bundle"]["context_max_chars"], t("round.stack_what"))
        b.add(
            t("round.ballots"),
            stack_mod.Ballots(state).md(heading=False),
            cfg["bundle"]["context_max_chars"],
            t("round.ballots_what"),
        )
        points = [dict(p) for p in b.points] + extras.standing_points(cfg)  # X1, X2 after the input's own
        bundle_mod.check_unique(points)
        context = b.text(cfg["bundle"]["max_chars"])
    else:  # the saved bundle and points: the answers kept were given to exactly these
        b = None
        shown = [o for o in extras.Orders(state).unshown_done() if o["id"] in (resume.get("orders_shown") or ())]
        points = [dict(p) for p in resume["points"] if not (p.get("source") == "proposal" and p.get("author"))]
        with open(os.path.join(state.rounds_dir(), resume["id"], "bundle.md"), encoding="utf-8") as f:
            context = f.read()
    lifted = _locks_lifted_since_last_round(state, stk)
    balances = ledger.balances()
    personas = {cid: Persona(state, cid) for cid in characters.IDS}
    genders = personas_genders(state)
    rules = prompts.rules(cfg, kind, genders)
    sch1 = schema.round1(kind, [p["id"] for p in points], cfg, solo=bool(only))
    if only:
        cfg = copy.deepcopy(cfg)
        cfg["round"]["budget_mtok"], cfg["round"]["max_calls"] = solo_caps(cfg, 0 if stk.empty() else 1)
    dirs = read_dirs(cfg)
    free = cfg["free"]
    free_max = cfg["round"]["free_comment_max_chars"]
    model, effort_level, tools = cfg["model"], cfg["effort"], list(cfg["tools"])

    def r1_prompts(readable):
        task1 = prompts.task_r1(kind, points, cfg, free["max_tool_turns_r1"], readable, solo=bool(only))
        out_ = {}
        for cid in cast:
            ident = prompts.identity(personas[cid], cid, balances[cid], cfg["thoughts_in_prompt"])
            out_[cid] = prompts.layout(cfg, rules, context, ident, task1)
        return out_

    if dry_run and resume is not None:
        _print_resume_plan(out, state, cfg, resume, cast, stk)
        return None
    if dry_run:
        planned = ["<a snapshot of %s>" % (f"PR #{pr}'s head" if pr else cfg["deep_repo"].get("ref")), *dirs]
        _print_plan(
            out,
            cfg,
            kind,
            b,
            points,
            r1_prompts(planned),
            balances,
            sch1,
            planned,
            only=only,
            lock_pass=not stk.empty(),
        )
        if holder.get("brainstorm"):
            e = holder["brainstorm"]
            out(
                "brainstorm {}: called by {} in {}; topic file {}".format(
                    e["id"], characters.name(e["initiator"]), e["round"], e["topic_file"]
                )
            )
        out(
            "results of %d done orders go into the bundle%s"  # noqa: UP031
            % (
                len(shown),
                (": " + ", ".join(o["id"] for o in shown)) if shown else "",
            )
        )
        out(
            "the stack: %d in slots, %d waiting; the lock pass (round 3): %s"  # noqa: UP031
            % (
                len(stk.in_slots()),
                len(stk.queue()),
                "skipped, the stack is empty"
                if stk.empty()
                else "up to %d call%s, up to %d tool turns each, cap %s, %d tok. a lock"  # noqa: UP031
                % (
                    len(cast),
                    "" if len(cast) == 1 else "s",
                    cfg["lock"]["max_tool_turns"],
                    effort.fmt(cfg["lock"]["budget_mtok"], cfg),
                    cfg["lock"]["price"],
                ),
            )
        )
        out("the ballot queue: %d open" % len(stack_mod.Ballots(state).entries(open_only=True)))  # noqa: UP031
        return None

    settings = {
        "model": model,
        "effort": effort_level,
        "tools": tools,
        "free": free,
        "layout": cfg["prompt_layout"],
        "round": cfg["round"],
        "brainstorm": cfg["brainstorm"],
        "orders": cfg["orders"],
        "lock": cfg["lock"],
        "stack": cfg["stack"],
        "usage": {k: cfg["usage"][k] for k in ("mtok_per_pct", "usd_per_mtok")},
    }
    guard = Guard(cfg)
    costs = []
    notes = []  # what the report should say about the process
    quarantined = []  # the raw streams the private scan sent to the quarantine (push.gate_raw)
    saved = {}  # (character, stage) -> the answer an aborted pass saved (`round --resume`)
    if resume is None:
        rid = new_round_id(state, kind, pr, only)
        rdir = os.path.join(state.rounds_dir(), rid)
        for sub in ("prompts", "raw", "answers"):
            os.makedirs(os.path.join(rdir, sub), exist_ok=True)
        _w(state, os.path.join(rdir, "bundle.md"), context)
        rec = {
            "id": rid,
            "kind": kind,
            "created": now_iso(),
            "status": "running",
            "input": os.path.abspath(os.path.expanduser(input_path)) if input_path else None,
            "contexts": [os.path.abspath(os.path.expanduser(c)) for c in contexts or ()],
            "since": since_utc,
            "pr": pr,
            "pr_title": (b.pr or {}).get("title"),
            "sources": b.sources,
            "cuts": b.cuts,
            "points": points,
            "balances_before": balances,
            "settings": settings,
            "orders_shown": [o["id"] for o in shown],
        }
        if only:
            rec["only"] = only
        if holder.get("brainstorm"):
            e = holder["brainstorm"]
            rec["brainstorm_of"] = {"id": e["id"], "initiator": e["initiator"], "round": e["round"]}
        commit_wanted, snap_cfg = (b.pr or {}).get("headRefOid"), cfg
    else:
        # The same dir, bundle and points; today's caps (the owner may raise them with --set before going on).
        # The calls already made count against them: all their spend, and in the call cap those whose answers stay
        # (a failed or fatal call is asked again, so it takes no planned call's place: a solo round keeps its lock).
        rid = resume["id"]
        rdir = os.path.join(state.rounds_dir(), rid)
        os.makedirs(os.path.join(rdir, "raw"), exist_ok=True)
        saved = saved_answers(rdir)
        old = read_json(os.path.join(rdir, "costs.json")) or {}
        costs.extend(old.get("items") or [])
        guard.spent = sum(c.get("mtok") or 0.0 for c in costs)
        guard.calls = sum(1 for c in costs if c.get("status") == "ok")
        guard.util_first = (old.get("utilization") or {}).get("before")
        quarantined.extend(resume.get("quarantined") or [])
        notes += [quarantine_note(q) for q in quarantined]
        rec = dict(resume)
        why = rec.pop("reason", None)
        rec.setdefault("resumed", []).append(
            {
                "at": now_iso(),
                "aborted": rec.pop("finished", None),
                "reason": why,
                "kept": ["{}-{}".format(*k) for k in sorted(saved)],
                "repo_commit": resume.get("repo_commit"),
            }
        )
        rec.update(status="running", points=points, settings=settings)
        notes.append(
            t(
                "round.resumed",
                why=(why or "—")[:200],
                n=len(saved),
                kept=", ".join("{}-{}".format(*k) for k in sorted(saved)) or "—",
            )
        )
        # the tree the kept answers read, without a fetch: it was fetched for them
        commit_wanted = resume.get("repo_commit")
        snap_cfg = dict(cfg, deep_repo=dict(cfg["deep_repo"], fetch=False))
    _wj(state, os.path.join(rdir, "round.json"), rec)

    # Every pass reads the same fresh snapshot (origin/main, or the PR's head), made once, and
    # the same read_dirs: round 1 and the debate get one --add-dir list.
    snap_dir, rec["repo_commit"] = snapshot(snap_cfg, out, commit_wanted)
    holder["dir"] = snap_dir  # removed when the round ends
    readable = ([snap_dir] if snap_dir else []) + dirs
    rec["readable"] = readable

    def call(cid, stage, system_text, user_text, sch, budget, timeout, max_tool_turns):
        guard.check(budget)
        sys_path = os.path.join(rdir, "prompts", f"system-{sha256_text(system_text)}.md")
        if not os.path.exists(sys_path):
            write_new(sys_path, system_text)
            state.touch(sys_path)
        _w(state, os.path.join(rdir, "prompts", f"{cid}-{stage}.user.md"), user_text)
        raw_path = os.path.join(rdir, "raw", f"{cid}-{stage}.jsonl")
        spec = CallSpec(
            f"{rid}/{cid}-{stage}",
            model,
            effort_level,
            sys_path,
            user_text,
            sch,
            tools=tools,
            max_tool_turns=max_tool_turns,
            budget_mtok=budget,
            budget_usd=effort.to_usd(budget, cfg),
            timeout_s=timeout,
            cwd=rdir,
            add_dirs=readable,
            deny=cfg["deny_read"],
            raw_path=raw_path,
        )
        res = None
        try:
            res = runner(spec)
            return res
        except (CallFailed, FatalError) as e:
            res = e.result or {
                "call": spec.call_id,
                "model": model,
                "effort": effort_level,
                "status": "fatal" if isinstance(e, FatalError) else "failed",
                "reason": str(e),
                "cost_usd": None,
            }
            raise
        finally:
            if res is not None:
                q = push.gate_raw(state, cfg, raw_path, out)
                if q:
                    quarantined.append(q)
                    notes.append(quarantine_note(q))
                state.touch(raw_path)
                res["mtok"] = round(effort.call_mtok(res, cfg), 6)
                costs.append({k: v for k, v in res.items() if k not in ("structured", "rate_limit")})
                guard.record(res)
                out(describe(res, cfg))
                _wj(state, os.path.join(rdir, "costs.json"), _cost_summary(costs, guard))

    point_ids = [p["id"] for p in points]
    answers = {}  # cid -> the round-1 answer
    finals = {}  # cid -> {pid: position}
    rebuttals = []
    trims = {}
    proposals, invalid = [], []  # the brainstorm proposals of round 1, and the ones the code refused
    votes, took_part = {}, {}  # cid -> {proposal: (vote, line)}; cid -> its debate call succeeded
    order_asks = []  # (cid, stage, the order as asked)
    context2 = context
    try:
        # --- round 1: independent ------------------------------------------------------------
        # A resume after round 2 had begun keeps round 1 as it was: the debate's answers saw its table.
        r1_closed = any(stage == "r2" for _, stage in saved)
        for cid, (system_text, user_text) in r1_prompts(readable).items():
            if (cid, "r1") in saved:  # kept from the aborted pass: no call; its thought was kept then
                a1 = saved[(cid, "r1")]
            elif r1_closed:
                notes.append(t("round.r1_unasked", name=characters.name(cid)))
                continue
            else:
                try:
                    res = call(
                        cid,
                        "r1",
                        system_text,
                        user_text,
                        sch1,
                        free["budget_mtok_r1"],
                        free["timeout_s"],
                        free["max_tool_turns_r1"],
                    )
                except CallFailed as e:
                    notes.append(t("round.r1_failed", name=characters.name(cid), error=e))
                    continue
                except RoundStop as e:
                    notes.append(t("round.r1_skipped", name=characters.name(cid), error=e))
                    continue
                a1 = res["structured"]
                _wj(state, os.path.join(rdir, "answers", f"{cid}-r1.json"), a1)
                personas[cid].add_thought(a1.get("inner_thought"), rid, res["call"])
            answers[cid] = a1
            prop, why_not = extras.check_proposal(a1.get("brainstorm_proposal"), cfg)
            if prop:
                prop.update(id="BR%d" % (len(proposals) + 1), initiator=cid)  # noqa: UP031
                proposals.append(prop)
            elif why_not:
                invalid.append(
                    {
                        "initiator": cid,
                        "status": "failed",
                        "reasons": [t("round.proposal_refused", why=why_not)],
                        "topic": (a1.get("brainstorm_proposal") or {}).get("topic") or "",
                    }
                )
            if a1.get("order"):
                order_asks.append((cid, "r1", a1["order"]))

        # --- positions after round 1, and the new points --------------------------------------
        for cid in characters.IDS:
            pos, pnotes = positions_from(answers.get(cid), point_ids, free_max)
            finals[cid] = pos
            notes += [f"{characters.name(cid)}: {n}" for n in pnotes]
        n = 0
        for cid in characters.IDS:
            for prop in (answers.get(cid) or {}).get("proposals") or []:
                n += 1
                pid = "N%d" % n  # noqa: UP031
                points.append(
                    {
                        "id": pid,
                        "title": prop["title"].strip(),
                        "question": prop["question"].strip(),
                        "recommendation": "",
                        "source": "proposal",
                        "author": cid,
                    }
                )
                finals[cid][pid] = {"position": "agree", "stake": max(0, int(prop.get("stake") or 0)), "comment": ""}
        point_ids = [p["id"] for p in points]
        for cid in characters.IDS:
            cut = trim_to(finals[cid], ledger.balance(cid), point_ids, free_max)
            if cut:
                trims.setdefault(cid, []).extend(cut)

        # --- round 2: the debate (none in a solo round: nobody to debate with) ---------------------
        if point_ids and not only:
            context2 = context + "\n\n" + prompts.r1_table(points, finals)
            if proposals:
                context2 += "\n\n" + prompts.proposals_table(proposals, cfg)
            pids = [p["id"] for p in proposals]
            sch2 = schema.round2(point_ids, cfg, characters.IDS, pids)
            for cid in characters.IDS:
                bal = ledger.balance(cid)
                if (cid, "r2") in saved:  # kept from the aborted pass: no call; its thought was kept then
                    a2 = saved[(cid, "r2")]
                else:
                    ident = prompts.identity(personas[cid], cid, bal, cfg["thoughts_in_prompt"])
                    task = prompts.task_r2(
                        cid, finals[cid], points, bal, cfg, free["max_tool_turns_r2"], readable, pids
                    )
                    system_text, user_text = prompts.layout(cfg, rules, context2, ident, task)
                    try:
                        res = call(
                            cid,
                            "r2",
                            system_text,
                            user_text,
                            sch2,
                            free["budget_mtok_r2"],
                            free["timeout_s"],
                            free["max_tool_turns_r2"],
                        )
                    except CallFailed as e:
                        notes.append(t("round.r2_failed", name=characters.name(cid), error=e))
                        continue
                    except RoundStop as e:
                        notes.append(t("round.r2_skipped", name=characters.name(cid), error=e))
                        continue
                    a2 = res["structured"]
                    _wj(state, os.path.join(rdir, "answers", f"{cid}-r2.json"), a2)
                    personas[cid].add_thought(a2.get("inner_thought"), rid, res["call"])
                took_part[cid] = True
                for v in a2.get("brainstorm_votes") or []:
                    votes.setdefault(cid, {}).setdefault(v["proposal"], (v["vote"], " ".join(v["line"].split())))
                if a2.get("order"):
                    order_asks.append((cid, "r2", a2["order"]))
                pos2, pnotes = positions_from(a2, point_ids, free_max)
                finals[cid].update(pos2)
                notes += [f"{characters.name(cid)}: {x}" for x in pnotes]
                cut = trim_to(finals[cid], bal, point_ids, free_max)
                if cut:
                    trims.setdefault(cid, []).extend(cut)
                for rb in a2.get("rebuttals") or []:
                    if rb["to"] != cid:
                        rebuttals.append(
                            {"from": cid, "to": rb["to"], "point": rb["point"], "text": rb["text"].strip()}
                        )
    except FatalError as e:
        rec.update(status="aborted", reason=str(e), finished=now_iso(), points=points, quarantined=quarantined)
        if holder.get("brainstorm"):  # the brainstorm died: its fee comes back, the one exception
            rec["brainstorm_refunds"] = _brainstorm_failed(
                state, ledger, holder["brainstorm"], rid, f"fatal error in {rid}: {str(e)[:200]}"
            )
        _wj(state, os.path.join(rdir, "round.json"), rec)
        state.commit(f"team: round {rid} ABORTED: {str(e)[:120]}")
        raise RoundAborted(  # noqa: B904
            "round {} aborted, no stakes debited{}: {}{}".format(
                rid,
                ", the brainstorm's fee returned" if holder.get("brainstorm") else "",
                e,
                ""
                if holder.get("brainstorm")
                else f"; the answers saved stay: depot2 round --resume {rid} calls only for the missing ones",
            )
        )

    stake_txs = []
    for cid in characters.IDS:
        for pid in point_ids:
            a = finals[cid].get(pid)
            if a and a["stake"] > 0:
                title = next(p["title"] for p in points if p["id"] == pid)
                stake_txs.append(ledger.stake(cid, a["stake"], rid, pid, reason=title))
    back_txs = ledger.auto_refund_round(rid, AUTO_REASON)
    # The team's own brainstorm, then the orders, then the locks: each paid from what is left after
    # the stakes came back, in this order.
    bs = _brainstorm_check(state, cfg, ledger, rid, proposals, invalid, votes, took_part, genders)
    orders = _place_orders(state, cfg, ledger, rid, order_asks, snap_dir)

    # --- round 3: the lock pass ------------------------------------------------------------
    # Each character decides alone, after the debate and the round's debits, with the stack in
    # the bundle. No calls when the stack is empty. It counts against the round's caps.
    fatal = None
    locks = {
        "pass": "skipped",
        "why": t("round.stack_empty"),
        "calls": 0,
        "placed": [],
        "refused": [],
        "lifted": lifted,
    }
    lock_asks = []
    if not stk.empty():
        locks.update(**{"pass": "ran", "why": None})
        lc = cfg["lock"]
        context3 = context2 + "\n\n" + prompts.outcome_text(points, finals, bs, orders)
        sch3 = schema.lock_pass([o["id"] for o in stk.objects()])
        for cid in cast:
            bal = ledger.balance(cid)
            if bal < int(lc["price"]):
                locks["refused"].append(
                    {
                        "character": cid,
                        "object": None,
                        "reason": t("round.lock_cannot_pay", bal=int(bal), price=int(lc["price"])),
                    }
                )
                continue
            ident = prompts.identity(personas[cid], cid, bal, cfg["thoughts_in_prompt"])
            system_text, user_text = prompts.layout(
                cfg, rules, context3, ident, prompts.task_r3(bal, cfg, lc["max_tool_turns"], readable)
            )
            try:
                res = call(
                    cid,
                    "r3",
                    system_text,
                    user_text,
                    sch3,
                    float(lc["budget_mtok"]),
                    float(lc["timeout_s"]),
                    int(lc["max_tool_turns"]),
                )
            except CallFailed as e:
                notes.append(t("round.r3_failed", name=characters.name(cid), error=e))
                continue
            except RoundStop as e:
                notes.append(t("round.r3_skipped", name=characters.name(cid), error=e))
                continue
            except FatalError as e:
                fatal = f"the lock pass stopped: {e}"
                notes.append(t("round.r3_stopped", error=e))
                break
            locks["calls"] += 1
            a3 = res["structured"]
            _wj(state, os.path.join(rdir, "answers", f"{cid}-r3.json"), a3)
            personas[cid].add_thought(a3.get("inner_thought"), rid, res["call"])
            if a3.get("lock"):
                lock_asks.append((cid, a3["lock"]))
        placed, refused = _place_locks(state, cfg, ledger, stk, rid, lock_asks)
        locks["placed"] += placed
        locks["refused"] += refused
    for o in shown:
        extras.Orders(state).add(o["id"], o["status"], shown_in=rid)
    if holder.get("brainstorm"):
        extras.Brainstorms(state).add(holder["brainstorm"]["id"], "done", ran_in=rid)
    balances_after = ledger.balances()
    summary = _cost_summary(costs, guard)
    rec.update(
        mtok=summary["mtok"],
        status="done",
        finished=now_iso(),
        points=points,
        finals=finals,
        rebuttals=rebuttals,
        trims=trims,
        notes=notes,
        stakes=[x["id"] for x in stake_txs],
        auto_refunds=[x["id"] for x in back_txs],
        brainstorm=bs,
        orders=orders,
        locks=locks,
        stack_history_n=len(stk.history()),
        balances_after=balances_after,
        quarantined=quarantined,
        cost_usd=summary["total_usd"],
        tokens=summary["tokens"],
        calls=summary["calls"],
        utilization=summary["utilization"],
        genders=genders,
        ledger_seq=ledger.last_seq(),
    )
    _wj(state, os.path.join(rdir, "round.json"), rec)
    _wj(state, os.path.join(rdir, "costs.json"), summary)
    # The report reads only what was just saved, so `depot2 report --round` rebuilds it exactly.
    report = render_report(state, rid, cfg)
    _w(state, os.path.join(rdir, "report.md"), report)
    add_to_ballots(state, rec)  # the owner's ballot queue: every finished round, as waiting
    state.commit("team: round %s (%d calls, %s)" % (rid, summary["calls"], effort.fmt(summary["mtok"], cfg)))  # noqa: UP031
    out("")
    out(
        "round %s: %d calls, %d tool turns, %d tokens, %.0fs, %s; window %s; report %s"  # noqa: UP031
        % (
            rid,
            summary["calls"],
            summary["tool_turns"],
            summary["tokens"]["total"],
            summary["duration_s"],
            effort.fmt(summary["mtok"], cfg),
            _window(summary["utilization"]),
            os.path.join(rdir, "report.md"),
        )
    )
    return {"id": rid, "dir": rdir, "report": report, "costs": summary, "fatal": fatal}


def add_to_ballots(state, rec):
    """Append a finished round to the owner's ballot queue, its points in the report's order."""
    from .report import order_points

    order = [p["id"] for p, _ in order_points(rec.get("points") or [], rec.get("finals") or {})]
    return stack_mod.Ballots(state).add(rec, order, stack_mod.round_title(rec))


def _locks_lifted_since_last_round(state, stk):
    """The unlocks since the last finished round (each round saves how long stack-history.jsonl
    was when it ended): the report lists them.
    """
    seen = 0
    rd = state.rounds_dir()
    for d in os.listdir(rd) if os.path.isdir(rd) else ():
        rj = read_json(os.path.join(rd, d, "round.json")) or {}
        if rj.get("status") == "done":
            seen = max(seen, rj.get("stack_history_n") or 0)
    out = []
    for h in stk.history()[seen:]:
        if h["event"] == "unlock":
            lk = h["lock"]
            out.append(
                {
                    "object": h["object"],
                    "id": lk["id"],
                    "by": lk["by"],
                    "condition": lk["condition"],
                    "how": h.get("how"),
                    "reason": h.get("reason"),
                }
            )
    return out


def _place_locks(state, cfg, ledger, stk, rid, asks):
    """Validate the locks by code, then debit the price (not returned): one per object, one per
    character per round, only on an object in the stack, only with the money.
    """
    price = int(cfg["lock"]["price"])
    placed, refused, seen = [], [], set()
    for cid, ask in asks:
        item = {
            "character": cid,
            "object": (ask.get("object") or "").strip(),
            "condition": " ".join((ask.get("condition") or "").split()),
            "why": " ".join((ask.get("why") or "").split()),
        }
        why_not = None
        if cid in seen:
            why_not = t("round.lock_second")
        elif not item["object"] or not item["condition"] or not item["why"]:
            why_not = t(
                "round.lock_unfilled", fields=", ".join(k for k in ("object", "condition", "why") if not item[k])
            )
        elif not any(o["id"] == item["object"] for o in stk.objects()):
            why_not = t("round.lock_no_object", id=item["object"])
        elif stk.get(item["object"]).get("lock"):
            why_not = t("round.lock_taken", id=item["object"], lock=stk.get(item["object"])["lock"]["id"])
        elif ledger.balance(cid) < price:
            why_not = t("round.no_tokens", bal=int(ledger.balance(cid)), price=price)
        seen.add(cid)
        if why_not:
            item["reason"] = why_not
            refused.append(item)
            continue
        tx = ledger.lock(
            cid, price, rid, item["object"], "lock on {}: {}".format(item["object"], item["condition"][:80])
        )
        lock = stk.lock(item["object"], cid, item["condition"], item["why"], rid, tx["id"], price)
        item.update(id=lock["id"], tx=tx["id"], price=price, title=stk.get(item["object"])["title"])
        placed.append(item)
    return placed, refused


def _orders_text(orders):
    out = []
    for o in orders:
        out.append(t("round.order_head", id=o["id"], name=characters.name(o["character"]), round=o["round"]))
        out += [
            t("round.order_command", command=o["command"], seeds=o["seeds"], hardware=o["hardware"]),
            t("round.order_threshold", text=o["threshold"]),
            t("round.order_forecast", text=o["forecast"]),
            t("round.order_why", text=o["why"]),
        ]
        out += [t("round.order_result", text=r) for r in o.get("results") or []]
        if o.get("note"):
            out.append(t("round.order_note", text=o["note"]))
    return "\n".join(out)


def _brainstorm_check(state, cfg, ledger, rid, proposals, invalid, votes, took_part, genders):
    """The quorum decides every proposal; the one approved is paid at once (the fee is not
    returned), its topic file written and its entry queued. Returns what the report shows.
    """
    if not proposals and not invalid:
        return None
    log = extras.Brainstorms(state)
    history = {}
    for it in log.items():
        history[it["initiator"]] = history.get(it["initiator"], 0) + 1
    results = extras.evaluate(proposals, votes, took_part, ledger.balances(), cfg, history, genders)
    out = {"quorum": cfg["brainstorm"]["quorum"], "proposals": [], "approved": None}
    for r in results:
        item = {k: r[k] for k in ("id", "initiator", "topic", "why", "questions", "status", "reasons", "fees")}
        item["votes"] = {cid: list(v) if v else None for cid, v in r["votes"].items()}
        item["took_part"] = {cid: bool(took_part.get(cid)) for cid in characters.IDS}
        if r["status"] == "approved":
            bid = log.next_id()
            txs = [
                ledger.brainstorm_fee(
                    cid,
                    r["fees"][cid],
                    rid,
                    bid,
                    "initiator" if cid == r["initiator"] else "member",
                    "the team's brainstorm {}: «{}»".format(bid, r["topic"][:80]),
                )
                for cid in characters.IDS
                if r["fees"][cid] > 0
            ]
            entry = {
                "id": bid,
                "round": rid,
                "initiator": r["initiator"],
                "topic": r["topic"],
                "why": r["why"],
                "questions": r["questions"],
            }
            path = extras.write_topic(cfg, entry, genders.get(r["initiator"]))
            log.add(
                bid,
                "approved",
                round=rid,
                initiator=r["initiator"],
                topic=r["topic"],
                why=r["why"],
                questions=r["questions"],
                topic_file=path,
                votes=item["votes"],
                fees=[x["id"] for x in txs],
            )
            item.update(brainstorm=bid, topic_file=path, fee_txs=[x["id"] for x in txs])
            out["approved"] = bid
        out["proposals"].append(item)
    out["proposals"] += invalid
    return out


def _brainstorm_failed(state, ledger, entry, rid, reason):
    """The brainstorm round died on a fatal error (or never ran): every fee comes back."""
    entries = ledger.entries()
    refunds = [
        ledger.brainstorm_refund(ledger.get(x, entries), reason, round_id=rid)["id"] for x in entry.get("fees") or []
    ]
    extras.Brainstorms(state).add(
        entry["id"], "failed" if rid else "cancelled", reason=reason, refunds=refunds, ran_in=rid
    )
    return refunds


def _place_orders(state, cfg, ledger, rid, asks, snap_dir):
    """Validate the orders by code, then debit the price: one per character per round."""
    if not asks:
        return None
    log = extras.Orders(state)
    price = int(cfg["orders"]["price"])
    placed, refused, seen = [], [], set()
    for cid, stage, ask in asks:
        o, why_not = extras.check_order(ask, snap_dir, cfg)
        if o is None and why_not is None:
            continue  # left empty
        item = {"character": cid, "stage": stage, "command": " ".join(str(ask.get("command") or "").split())}
        second = cid in seen
        if second:
            why_not = t("round.order_second")
        elif o is not None and ledger.balance(cid) < price:
            why_not = t("round.no_tokens", bal=int(ledger.balance(cid)), price=price)
        if why_not:
            item["reason"] = why_not
            refused.append(item)
            if not second:
                seen.add(cid)
            continue
        seen.add(cid)
        oid = log.next_id()
        tx = ledger.order(cid, price, rid, oid, "order {}: {}".format(oid, o["command"][:80]))
        log.add(
            oid,
            "queued",
            character=cid,
            round=rid,
            stage=stage,
            tx=tx["id"],
            price=price,
            **{k: o[k] for k in (*extras.ORDER_FIELDS, "script")},
        )
        item.update(id=oid, tx=tx["id"], price=price, **{k: o[k] for k in extras.ORDER_FIELDS})
        placed.append(item)
    return {"placed": placed, "refused": refused}


def _window(u):
    if not u or u.get("before") is None:
        return "n/a"
    return "{:.0f}% → {:.0f}%".format(u["before"] * 100, u["after"] * 100)


def _cost_summary(costs, guard=None):
    total = sum(c.get("cost_usd") or 0.0 for c in costs)
    tok = {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0}
    for c in costs:
        for k in tok:
            tok[k] += (c.get("usage") or {}).get(k, 0)
    tok["total"] = sum(tok.values())
    return {
        "calls": len(costs),
        "mtok": round(sum(c.get("mtok") or 0.0 for c in costs), 6),
        "mtok_from_cost_calls": sum(1 for c in costs if effort.by_cost_only(c)),
        "stream_calls": sum(1 for c in costs if c.get("usage_from_stream")),
        "total_usd": round(total, 6),
        "unknown_cost_calls": sum(1 for c in costs if c.get("cost_usd") is None),
        "duration_s": round(sum(c.get("duration_s") or 0.0 for c in costs), 1),
        "turns": sum(c.get("num_turns") or 0 for c in costs),
        "tool_turns": sum(c.get("tool_turns") or 0 for c in costs),
        "tokens": tok,
        "utilization": {"before": guard.util_first, "after": guard.util_last} if guard else None,
        "by_model": _by(costs, "model"),
        "items": costs,
    }


def _by(costs, key):
    out = {}
    for c in costs:
        k = c.get(key)
        d = out.setdefault(k, {"calls": 0, "mtok": 0.0, "usd": 0.0})
        d["calls"] += 1
        d["mtok"] = round(d["mtok"] + (c.get("mtok") or 0.0), 6)
        d["usd"] = round(d["usd"] + (c.get("cost_usd") or 0.0), 6)
    return out


def _w(state, path, text):
    atomic_write(path, text)
    state.touch(path)


def _wj(state, path, obj):
    write_json(path, obj)
    state.touch(path)


def _print_resume_plan(out, state, cfg, rec, cast, stk):
    """`round --resume R --dry-run`: what the aborted pass kept, the calls still to make, the spend so far."""
    rdir = os.path.join(state.rounds_dir(), rec["id"])
    saved = saved_answers(rdir)
    old = read_json(os.path.join(rdir, "costs.json")) or {}

    def names(ids):
        return ", ".join(characters.name(c) for c in ids)

    out("DRY RUN: no model calls, nothing written.")
    out(
        "RESUME round {} ({}), aborted {}: {}".format(
            rec["id"], rec["kind"], rec.get("finished") or "?", (rec.get("reason") or "—")[:200]
        )
    )
    out("kept from the aborted pass: %s" % (", ".join("{}-{}".format(*k) for k in sorted(saved)) or "nothing"))
    r1_closed = any(stage == "r2" for _, stage in saved)
    r1 = [] if r1_closed else [c for c in cast if (c, "r1") not in saved]
    out(
        "round 1: %s"
        % (
            ("calls for " + names(r1))
            if r1
            else "no calls" + (" (round 2 had begun: round 1 stays as it was)" if r1_closed else "")
        )
    )
    if rec.get("only"):
        out("round 2: none (a solo round: nobody to debate with)")
    else:
        r2 = [c for c in characters.IDS if (c, "r2") not in saved]
        out("round 2: %s" % (("calls for " + names(r2)) if r2 else "no calls"))
    out(
        "the lock pass (round 3): %s"
        % (
            "skipped, the stack is empty"
            if stk.empty()
            else "as in any round, cap {} a call".format(effort.fmt(cfg["lock"]["budget_mtok"], cfg))
        )
    )
    out(
        "spent so far: %d calls, %s; they count against the round caps now: %s, %d calls"  # noqa: UP031
        % (
            len(old.get("items") or []),
            effort.fmt(old.get("mtok") or 0.0, cfg),
            effort.fmt(cfg["round"]["budget_mtok"], cfg),
            cfg["round"]["max_calls"],
        )
    )


def _print_plan(out, cfg, kind, b, points, r1_prompts, balances, sch1, readable, only=None, lock_pass=True):
    out("DRY RUN: no model calls, nothing written.")
    if only:
        out(f"SOLO round: only {characters.name(only)} answers (round 1 and the lock pass; no debate, no brainstorm)")
    out(f"kind {kind}; sources:")
    for s in b.sources:
        out(f"  - {s}")
    for c in b.cuts:
        out(f"  cut: {c}")
    out(
        "points: %d (%s)"  # noqa: UP031
        % (
            len(points),
            ", ".join(p["id"] for p in points[:20]) + (" …" if len(points) > 20 else ""),
        )
    )
    out("layout %s; schema %d chars" % (cfg["prompt_layout"], len(json.dumps(sch1))))  # noqa: UP031
    sys_sizes = {len(s) for s, _ in r1_prompts.values()}
    for cid, (s, u) in r1_prompts.items():
        out(
            "  %-17s balance %3d  system %6d chars (~%d tok)  user %6d chars (~%d tok)"  # noqa: UP031
            % (characters.name(cid), balances[cid], len(s), len(s) // 3, len(u), len(u) // 3)
        )
    if len(sys_sizes) == 1 and cfg["prompt_layout"] == "cache" and not only:
        out("  the system prompt is identical for every character: the calls after the first read it from the cache")
    f = cfg["free"]
    tools = ",".join(cfg["tools"]) or "none"
    out(
        "every pass: {}, effort {}, tools {} (read-only, deny rules on top)".format(
            cfg["model"], cfg["effort"] or "(not passed)", tools
        )
    )
    out("every pass reads the same --add-dir list (round 1 and round 2):")
    for d in readable:
        out(f"  --add-dir {d}")
    missing = [d for d in cfg["read_dirs"] if not os.path.isdir(os.path.expanduser(d))]
    for d in missing:
        out(f"  (read_dirs: {d} does not exist, left out)")
    out("  deny rules on top: %d (depot2 config lists them)" % len(cfg["deny_read"]))  # noqa: UP031
    out(
        f"planned calls, one after another (caps in %UW (MTok), 1%UW = {effort.per_pct(cfg):g} MTok; the CLI's --max-budget-usd "
        f"gets the cap x usage.usd_per_mtok {effort.usd_per_mtok(cfg):g}):"
    )
    n = len(r1_prompts)
    out(
        "  round 1: %d call%s, up to %d tool turns each, cap %s, %ds"  # noqa: UP031
        % (n, "" if n == 1 else "s", f["max_tool_turns_r1"], effort.fmt(f["budget_mtok_r1"], cfg), f["timeout_s"])
    )
    if only:
        out("  round 2: none (a solo round: nobody to debate with)")
    else:
        out(
            "  round 2: %d call%s, up to %d tool turns each, cap %s, %ds"  # noqa: UP031
            % (n, "" if n == 1 else "s", f["max_tool_turns_r2"], effort.fmt(f["budget_mtok_r2"], cfg), f["timeout_s"])
        )
    out(
        "round caps: %s, %d calls%s"  # noqa: UP031
        % (
            effort.fmt(cfg["round"]["budget_mtok"], cfg),
            cfg["round"]["max_calls"],
            " (scaled to one character)" if only else "",
        )
    )
    out(
        "the fuse: stop at {:.0f}% of a usage window, and refuse a call when the window's last reading + the "
        "spend since + its cap would reach {}".format(
            cfg["round"]["stop_at_utilization"] * 100, effort.share(cfg["round"]["stop_at_utilization"], cfg)
        )
    )
    out("stakes: debited when the round ends, then at once returned (auto_refund), before the report")
    bc, oc = cfg["brainstorm"], cfg["orders"]
    out(
        "standing questions: %s"
        % (", ".join("{} «{}»".format(q["id"], q["title"]) for q in cfg["standing_questions"]) or "none")
    )
    if only:
        out("the team's brainstorm: not in a solo round (it needs the team's quorum)")
    else:
        out(
            "the team's brainstorm: quorum %s, fee %d from the initiator + %d from each other (not returned)"  # noqa: UP031
            % (bc["quorum"], bc["fee_initiator"], bc["fee_other"])
        )
    out(
        "orders: %d tok. each, one per character per round, hardware %s; the orchestrator runs them"  # noqa: UP031
        % (oc["price"], "/".join(oc["hardware"]))
    )
