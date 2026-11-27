"""Reflection: rewrite the portraits of the characters the owner's actions touched.

Gate: a character reflects only when something changed for it since its last reflection: an
owner payout, refund, forfeit, fine or order settlement for it, or an owner decision about it (or
about everyone). The code's auto refunds and the team's own brainstorm fees and orders do not count.
One call each.
The code stores the new portrait as a new version and keeps every old one. No background loop:
this runs only when called.
"""

import os

from . import characters, effort, i18n, prompts, push, schema
from .executors import (
    CallFailed,
    CallSpec,
    FatalError,
    describe,
    run_call,
    total_tokens,
)
from .i18n import t
from .ledger import OWNER_TYPES, Decisions, Ledger
from .persona import Persona, genders
from .round import Guard, RoundStop, to_utc
from .store import atomic_write, now_iso, read_json, sha256_text, write_json, write_new


def changes_since(state, cid, entries=None, decisions=None):
    """The owner's ledger actions and decisions about cid after its last reflection."""
    m = Persona(state, cid).meta()
    ls = m.get("reflected_ledger_seq", 0)
    ds = m.get("reflected_decision_seq", 0)
    entries = Ledger(state).entries() if entries is None else entries
    decisions = Decisions(state).records() if decisions is None else decisions
    owner = [e for e in entries if e["character"] == cid and e["type"] in OWNER_TYPES and e["seq"] > ls]
    dec = [d for d in decisions if d["character"] in (cid, "all") and d["seq"] > ds]
    return owner, dec


def mark_reflected(state, cid):
    Persona(state, cid).update_meta(
        reflected_ledger_seq=Ledger(state).last_seq(),
        reflected_decision_seq=Decisions(state).last_seq(),
        reflected_at=now_iso())


OUTCOME = i18n.Labels("reflect.outcome", ("moved", "burn", "refund"))


def owner_facts(owner, dec):
    lines = []
    for e in owner:
        where = " ".join(x for x in (e.get("round"), e.get("point")) if x)
        common = dict(date=e["ts"][:10], where=(f" ({where})") if where else "", reason=e.get("reason") or "")
        if e["type"] == "forfeit":
            lines.append(t("reflect.forfeit", stake=int(e.get("stake", 0)), **common))
            continue
        if e["type"] == "fine":
            if -e["amount"] == e.get("asked", -e["amount"]):
                lines.append(t("reflect.fine", amount=int(e["amount"]), **common))
            else:
                lines.append(t("reflect.fine_part", amount=int(e["amount"]), asked=int(e["asked"]), **common))
            continue
        if e["type"] == "order_settle":
            lines.append(t("reflect.settle", order=e.get("order"),
                           outcome=OUTCOME.get(e.get("outcome"), e.get("outcome")), amount="%+d" % e["amount"],  # noqa: UP031
                           **common))
            continue
        lines.append(t("reflect.payout" if e["type"] == "payout" else "reflect.refund", amount="%+d" % e["amount"],  # noqa: UP031
                       **common))
    for d in dec:
        where = " ".join(x for x in (d.get("round"), d.get("point")) if x)
        lines.append(t("reflect.decision_all" if d["character"] == "all" else "reflect.decision", date=d["ts"][:10],
                       where=(f" ({where})") if where else "", text=d["text"]))
    return "\n".join(lines) or t("prompts.none")


def rounds_since(state, cid, since_iso):
    """This character's final positions in the rounds after its last reflection."""
    rd = state.rounds_dir()
    out = []
    if not os.path.isdir(rd):
        return out
    since = to_utc(since_iso) if since_iso else None
    for d in sorted(os.listdir(rd)):
        rj = read_json(os.path.join(rd, d, "round.json"))
        if not rj or rj.get("status") != "done":
            continue
        if since and to_utc(rj["created"]) <= since:
            continue
        fin = (rj.get("finals") or {}).get(cid) or {}
        out.append("### {}\n{}".format(rj["id"], prompts.compact_answer(rj.get("points") or [], fin)))
    return out


def facts_text(state, cid, owner, dec, cap):
    p = Persona(state, cid)
    led = Ledger(state)
    open_st = led.open_stakes(cid=cid)
    parts = [t("reflect.facts_owner"), owner_facts(owner, dec), "", t("reflect.facts_rounds")]
    parts += rounds_since(state, cid, p.meta().get("reflected_at")) or [t("prompts.none")]
    parts += ["", t("reflect.facts_stakes"),
              "\n".join("- %s %s: %d" % (e.get("round"), e.get("point"), -e["amount"]) for e in open_st)  # noqa: UP031
              or t("prompts.none")]
    text = "\n".join(parts)
    if len(text) > cap:
        text = text[:cap].rstrip() + "\n" + t("bundle.cut_mark")
    return text


def run_portrait_calls(state, cfg, jobs, label, out=print, runner=None, dry_run=False, source="reflect"):
    """jobs: [(cid, system_text, user_text)] → one call each; the code swaps the portrait.

    Returns {cid: "ok" | "failed: …"}; raises FatalError after recording what it did.
    """
    if runner is None:
        def runner(spec):
            return run_call(spec, cfg, validate=schema.validate)
    rc = cfg["reflect"]
    sch = schema.reflection(cfg)
    if dry_run:
        out("DRY RUN: no model calls, nothing written.")
        for cid, s, u in jobs:
            out("  %-17s system %6d chars (~%d tok)  user %6d chars (~%d tok)"  # noqa: UP031
                % (characters.name(cid), len(s), len(s) // 3, len(u), len(u) // 3))
        out("planned: %d × %s effort %s, no tools, cap %s each, portrait ≤ %d chars"  # noqa: UP031
            % (len(jobs), cfg["model"], cfg["effort"] or "(not passed)", effort.fmt(rc["budget_mtok"], cfg),
               rc["portrait_max_chars"]))
        return {}
    if not jobs:
        return {}
    base = os.path.join(state.path("reflections"), "{}-{}".format(now_iso()[:19].replace(":", ""), label))
    d, n = base, 2
    while os.path.exists(d):
        d, n = "%s-%d" % (base, n), n + 1  # noqa: UP031
    os.makedirs(os.path.join(d, "raw"))
    guard = Guard(cfg)
    costs, status, quarantined = [], {}, []
    try:
        for cid, system_text, user_text in jobs:
            try:
                guard.check(float(rc["budget_mtok"]))
            except RoundStop as e:
                status[cid] = f"skipped: {e}"
                continue
            sys_path = os.path.join(d, f"system-{sha256_text(system_text)}.md")
            if not os.path.exists(sys_path):
                write_new(sys_path, system_text)
                state.touch(sys_path)
            up = os.path.join(d, f"{cid}.user.md")
            atomic_write(up, user_text)
            state.touch(up)
            raw = os.path.join(d, "raw", f"{cid}.jsonl")
            spec = CallSpec(f"{os.path.basename(d)}/{cid}", cfg["model"], cfg["effort"],
                            sys_path, user_text, sch, budget_mtok=rc["budget_mtok"],
                            budget_usd=effort.to_usd(rc["budget_mtok"], cfg), timeout_s=rc["timeout_s"], cwd=d,
                            raw_path=raw)
            res = None
            try:
                res = runner(spec)
            except CallFailed as e:
                res = e.result or {"call": spec.call_id, "status": "failed", "reason": str(e)}
                status[cid] = f"failed: {e}"
                continue
            except FatalError as e:
                res = e.result or {"call": spec.call_id, "status": "fatal", "reason": str(e)}
                status[cid] = f"fatal: {e}"
                raise
            finally:
                if res is not None:
                    q = push.gate_raw(state, cfg, raw, out)
                    if q:
                        quarantined.append(q)
                    state.touch(raw)
                    res["mtok"] = round(effort.call_mtok(res, cfg), 6)
                    costs.append({k: v for k, v in res.items() if k not in ("structured", "rate_limit")})
                    guard.record(res)
                    out(describe(res, cfg))
            ans = res["structured"]
            p = Persona(state, cid)
            v = p.replace_portrait(ans["portrait"], source=source, cap=rc["portrait_max_chars"],
                                   call_id=res["call"], note=ans.get("what_changed"),
                                   summary_en=ans.get("summary_en"),
                                   summary_cap=rc["summary_en_max_chars"])
            p.add_thought(ans.get("inner_thought"), label, res["call"])
            mark_reflected(state, cid)
            status[cid] = "ok: v%04d (%d chars%s)" % (v["version"], v["chars"],  # noqa: UP031
                                                     ", truncated" if v["truncated"] else "")
            out("  %s → portrait v%04d, %d chars; %s" % (characters.name(cid), v["version"], v["chars"],  # noqa: UP031
                                                       ans.get("what_changed", "")))
    finally:
        total = sum(c.get("cost_usd") or 0.0 for c in costs)
        tokens = sum(total_tokens(c.get("usage")) for c in costs)
        mtok = sum(c.get("mtok") or 0.0 for c in costs)
        cpath = os.path.join(d, "costs.json")
        write_json(cpath, {"calls": len(costs), "mtok": round(mtok, 6), "total_usd": round(total, 6), "tokens": tokens,
                           "duration_s": round(sum(c.get("duration_s") or 0 for c in costs), 1),
                           "items": costs, "status": status, "quarantined": quarantined})
        state.touch(cpath)
        out("%s: %d calls, %d tokens, %s" % (label, len(costs), tokens, effort.fmt(mtok, cfg)))  # noqa: UP031
    return status


def reflect(state, cfg, only=None, dry_run=False, out=print, runner=None):
    led = Ledger(state)
    entries = led.entries()
    decisions = Decisions(state).records()
    balances = led.balances(entries)
    rules = prompts.reflect_rules(cfg, genders(state))
    jobs, skipped = [], []
    for cid in ([only] if only else characters.IDS):
        owner, dec = changes_since(state, cid, entries, decisions)
        if not owner and not dec:
            skipped.append(cid)
            continue
        p = Persona(state, cid)
        ident = prompts.identity(p, cid, balances[cid], cfg["reflect"]["thoughts"])
        facts = facts_text(state, cid, owner, dec, cfg["reflect"]["facts_max_chars"])
        task = t("reflect.task")
        jobs.append((cid, rules, ident + "\n\n" + facts + "\n\n" + task))
    for cid in skipped:
        out(f"{characters.name(cid)}: nothing changed since the last reflection; skipped")
    status = run_portrait_calls(state, cfg, jobs, "reflect", out=out, runner=runner, dry_run=dry_run)
    return status, skipped
