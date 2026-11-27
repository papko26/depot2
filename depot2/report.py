"""The report, built by the CODE in the ballot format, in the run's language (i18n.py).

The report is built only from what the round saved (round.json, costs.json, answers/) and the ledger, so `depot2
report --round R` rebuilds it without a model call. Old rounds may carry the removed paid passes (a `deep` answer,
`deep` notes, `deep_levels`, think transactions): they are read leniently and never shown, except that think
transactions still count in the balance check.
"""

import os

from . import characters, effort, i18n
from .i18n import t
from .ledger import LEGACY, OWNER_TYPES, Ledger
from .persona import genders as personas_genders
from .prompts import KIND_RU, VERDICT_RU
from .store import read_json

# What a round writes as it ends: the stakes, their auto refunds, the brainstorm fee, the orders
# (and an old round's paid passes).
ROUND_TYPES = ("stake", "auto_refund", "brainstorm_fee", "order", "lock", *LEGACY)
STAGES = ("r1", "deep", "r2")  # "deep": an old round's paid pass, read only for its main point
NO_COSTS = {
    "calls": 0,
    "mtok": 0.0,
    "total_usd": 0.0,
    "unknown_cost_calls": 0,
    "duration_s": 0.0,
    "turns": 0,
    "tool_turns": 0,
    "tokens": {"input": 0, "cache_read": 0, "cache_write": 0, "output": 0, "total": 0},
    "utilization": None,
    "by_model": {},
    "items": [],
}


def order_points(points, finals):
    """[(point, total_stake)] sorted by total stake, highest first; stable on ties."""
    totals = []
    for i, p in enumerate(points):
        total = sum((finals.get(cid) or {}).get(p["id"], {}).get("stake", 0) for cid in characters.IDS)
        totals.append((p, total, i))
    totals.sort(key=lambda x: (-x[1], x[2]))
    return [(p, total) for p, total, _ in totals]


def voice_line(cid, a, gender):
    if a and a["position"] == "none" and (a["stake"] or a.get("comment")):
        # An answer to an open question, or a comment without a yes or no.
        s = characters.name(cid)
        if a["stake"]:
            s = t("report.with_stake", text=s, n=int(a["stake"]))
        return s + ": " + (_indent(a["comment"]) if a.get("comment") else "—")
    s = "{}: {}".format(characters.name(cid), characters.agree_word(gender, a["position"]) if a else "—")
    if a and a["stake"]:
        s = t("report.with_stake", text=s, n=int(a["stake"]))
    if a and a.get("comment"):
        s += ": " + _indent(a["comment"])
    return s


# --- turns and stakes ------------------------------------------------------------------------------


def turns_cell(rec, costs, cid):
    allowed = ((rec.get("settings") or {}).get("free") or {}).get("max_tool_turns_r1")
    calls = {c.get("call"): c for c in (costs or {}).get("items") or []}
    r1 = calls.get("{}/{}-r1".format(rec["id"], cid))
    if not r1:
        return t("report.skipped")
    used = r1.get("tool_turns")
    s = "{}/{}".format("?" if used is None else used, "?" if allowed is None else allowed)
    return s if r1.get("status") == "ok" else t("report.call_failed", turns=s)


def stake_rows(rec, entries):
    """auto refunds, whenever they were written: `refund --auto` applies them to an older round
    later), legacy (an old round's think transactions, net), left = before - staked + legacy +
    back, the tx ids, and ok. ok says the ledger agrees: the stakes (and an old round's thinks)
    balance after its stakes (an older round).
    """
    rid = rec["id"]
    before_all, after_all = rec["balances_before"], rec["balances_after"]
    self_refunds = "auto_refunds" in rec  # the round returned its own stakes as it ended
    rows = {}
    for cid in characters.IDS:
        mine = sorted(
            (e for e in entries if e["character"] == cid and e.get("round") == rid and e["type"] in ROUND_TYPES),
            key=lambda e: e["seq"],
        )
        block = [e for e in mine if e["type"] in ("stake", *LEGACY)]
        stakes = [e for e in block if e["type"] == "stake"]
        backs = [e for e in mine if e["type"] == "auto_refund"]
        before = before_all[cid]
        staked = -sum(e["amount"] for e in stakes)
        legacy = sum(e["amount"] for e in block if e["type"] in LEGACY)
        back = sum(e["amount"] for e in backs)
        fee = -sum(e["amount"] for e in mine if e["type"] == "brainstorm_fee")
        ordered = -sum(e["amount"] for e in mine if e["type"] == "order")
        locked = -sum(e["amount"] for e in mine if e["type"] == "lock")
        after_stakes = before - staked + legacy
        left = after_stakes + back - fee - ordered - locked
        ok = True
        if block:
            ok = block[0]["balance_after"] - block[0]["amount"] == before and block[-1]["balance_after"] == after_stakes
        by_id = {e["id"]: e for e in stakes}
        ok = (
            ok
            and len({e.get("ref") for e in backs}) == len(backs)
            and all(e.get("ref") in by_id and e["amount"] == -by_id[e["ref"]]["amount"] for e in backs)
        )
        if self_refunds:
            ok = ok and after_all[cid] == left and (not mine or mine[-1]["balance_after"] == left)
        else:
            ok = ok and after_all[cid] == after_stakes and not fee and not ordered and not locked
        rows[cid] = {
            "before": before,
            "stakes": stakes,
            "staked": staked,
            "back": back,
            "fee": fee,
            "ordered": ordered,
            "locked": locked,
            "legacy": legacy,
            "left": left,
            "ids": [e["id"] for e in mine],
            "ok": ok,
        }
    return rows


def stakes_said(rows):
    staked = sum(r["staked"] for r in rows.values())
    back = sum(r["back"] for r in rows.values())
    if not staked:
        return t("report.no_stakes")
    if back == staked:
        return t("report.stakes_all_back", n=int(staked))
    if not back:
        return t("report.stakes_none_back", n=int(staked))
    return t("report.stakes_some_back", n=int(staked), back=int(back))


# --- loading a saved round -------------------------------------------------------------------------


def load_round(state, rid):
    """(rec, costs, answers, entries, genders): everything the report reads, from the round's files
    and the ledger. No model call.
    """
    rdir = os.path.join(state.rounds_dir(), rid)
    rec = read_json(os.path.join(rdir, "round.json"))
    if rec is None:
        raise ValueError(f"no round {rid} ({rdir})")
    if rec.get("status") != "done":
        raise ValueError("round {} is {}: only a finished round has a report".format(rid, rec.get("status")))
    costs = read_json(os.path.join(rdir, "costs.json")) or dict(NO_COSTS)
    answers = {
        cid: {st: read_json(os.path.join(rdir, "answers", f"{cid}-{st}.json")) for st in STAGES}
        for cid in characters.IDS
    }
    genders = dict(personas_genders(state))
    genders.update(rec.get("genders") or {})  # the genders the round was played with
    return rec, costs, answers, Ledger(state).entries(), genders


def render(state, rid, cfg=None):
    """The report of a saved round, rebuilt from its files and the ledger. cfg: the calibration the
    spend prints with (else the one the round saved).
    """
    return build_report(*load_round(state, rid), cfg=cfg)


def usage_cfg(rec, cfg=None):
    """The calibration to print with: the tool's, else the one the round saved, else the defaults."""
    if cfg is not None:
        return cfg
    from .config import DEFAULTS

    u = dict(DEFAULTS["usage"])
    u.update(((rec or {}).get("settings") or {}).get("usage") or {})
    return {"usage": u}


def solo_budget(rec, cfg):
    """A solo round's scaled cap in MTok, from round.json (an old one saved it in ≈$)."""
    r = (rec.get("settings") or {}).get("round") or {}
    if r.get("budget_mtok") is not None:
        return float(r["budget_mtok"])
    return effort.from_usd(r.get("budget_usd") or 0, cfg)


# --- the report --------------------------------------------------------------------------------


def build_report(rec, costs, answers, ledger_entries, genders, cfg=None):
    rid, kind = rec["id"], rec["kind"]
    ucfg = usage_cfg(rec, cfg)
    points, finals = rec.get("points") or [], rec.get("finals") or {}
    called = rec.get("brainstorm_of")
    rebuttals = rec.get("rebuttals") or []
    trims, notes = rec.get("trims") or {}, rec.get("notes") or []
    counted = {
        cid: (answers.get(cid) or {}).get("deep") or (answers.get(cid) or {}).get("r1") or {} for cid in characters.IDS
    }  # the round-1 answer (an old round's paid pass replaced it)
    verdicts = {cid: counted[cid].get("verdict") for cid in characters.IDS}
    mains = {cid: counted[cid].get("main_point") for cid in characters.IDS}
    rows = stake_rows(rec, ledger_entries)
    only = rec.get("only")
    cast = [only] if only else list(characters.IDS)  # a solo round shows its one character

    L = []
    if only:
        L.append(t("report.title_solo", kind=KIND_RU[kind], name=characters.name(only), rid=rid))
    elif called:
        L.append(
            t(
                "report.title_brainstorm",
                gender=genders.get(called["initiator"]),
                name=characters.name(called["initiator"]),
                rid=rid,
            )
        )
    else:
        L.append(t("report.title", kind=KIND_RU[kind], rid=rid))
    L.append("")
    if only:
        L.append(
            t(
                "report.solo",
                gender=genders[only],
                name=characters.name(only),
                cap=effort.fmt(solo_budget(rec, ucfg), ucfg),
                calls=int(((rec.get("settings") or {}).get("round") or {}).get("max_calls") or 0),
            )
        )
    src = []
    if rec.get("input"):
        src.append(t("report.src_input", path=rec["input"]))
    if called:
        src.append(t("report.src_brainstorm", id=called["id"], round=called["round"]))
    if kind == "review" and rec.get("pr"):
        src.append(t("report.src_pr", pr=rec["pr"], title=rec.get("pr_title") or ""))
    elif kind == "review":
        src.append(t("report.src_since", since=rec["since"]) if rec.get("since") else t("report.src_last10"))
    for s in rec.get("sources") or []:
        if "decisions" in s:
            src.append(t("report.src_decisions", path=s))
    L.append(t("report.sources", list="; ".join(src)))
    if rec.get("cuts"):
        L.append(t("report.cuts", list="; ".join(rec["cuts"])))
    approx = costs.get("unknown_cost_calls")
    tok = costs.get("tokens") or {}
    u = costs.get("utilization") or {}
    L.append(
        t(
            "report.calls_approx" if approx else "report.calls",
            calls=int(costs["calls"]),
            turns=int(costs.get("turns", 0)),
            tool_turns=int(costs.get("tool_turns", 0)),
            secs="{:.0f}".format(costs["duration_s"]),
            total=int(tok.get("total", 0)),
            input=int(tok.get("input", 0)),
            cache_read=int(tok.get("cache_read", 0)),
            cache_write=int(tok.get("cache_write", 0)),
            output=int(tok.get("output", 0)),
        )
    )
    by_cost = costs.get("mtok_from_cost_calls")
    if by_cost is None:
        by_cost = sum(1 for c in costs.get("items") or [] if effort.by_cost_only(c))
    spent = effort.fmt(effort.costs_mtok(costs, ucfg), ucfg)
    window = (
        ("{:.0f}% → {:.0f}%".format(u["before"] * 100, u["after"] * 100))
        if u.get("before") is not None
        else t("report.na")
    )
    L.append(
        t(
            "report.spend_by_cost" if by_cost else "report.spend",
            spent=spent,
            per=f"{effort.per_pct(ucfg):g}",
            by_cost=int(by_cost or 0),
            window=window,
            models=", ".join(sorted(m for m in costs["by_model"] if m)) or "—",
            effort=(rec.get("settings") or {}).get("effort") or t("report.effort_default"),
        )
    )
    L.append(stakes_said(rows))
    if rec.get("repo_commit"):
        L.append(t("report.read_pr_head" if rec.get("pr") else "report.read_main", commit=rec["repo_commit"]))
    L.append("")

    # The team table.
    L.append(t("report.team_head"))
    L.append("")
    if kind == "review":
        L.append(t("report.team_cols_review"))
        L.append("|---|---|---|---|")
        for cid in cast:
            L.append(
                "| {} | {} | {} | {} |".format(
                    characters.name(cid),
                    VERDICT_RU.get(verdicts.get(cid), "—"),
                    _cell(turns_cell(rec, costs, cid)),
                    _cell(mains.get(cid)),
                )
            )
    else:
        L.append(t("report.team_cols"))
        L.append("|---|---|---|")
        for cid in cast:
            L.append(f"| {characters.name(cid)} | {_cell(turns_cell(rec, costs, cid))} | {_cell(mains.get(cid))} |")
    L.append("")
    L.append(t("report.turns_legend"))
    L.append("")

    # The points, by stake.
    L.append(t("report.points_head"))
    L.append("")
    if not points:
        L.append(t("report.no_points"))
    for n, (p, total) in enumerate(order_points(points, finals), 1):
        if total:
            L.append(t("report.point_head_stake", n=n, id=p["id"], title=p["title"], total=int(total)))
        else:
            L.append(t("report.point_head", n=n, id=p["id"], title=p["title"]))
        if p.get("source") == "proposal":
            L.append(t("report.question", text=_indent(p["question"])))
            L.append(t("report.rec_by", name=characters.name(p["author"])))
        else:
            if p.get("question"):
                L.append(t("report.question", text=_indent(_clip(p["question"], 1500))))
            L.append(
                t("report.rec", text=_indent(_clip(p["recommendation"], 1200)))
                if p.get("recommendation")
                else t("report.rec_standing")
                if p.get("source") == "standing"
                else t("report.rec_open")
                if kind in ("discuss", "brainstorm")
                else t("report.rec_none")
            )
        voices = []
        for cid in cast:
            a = (finals.get(cid) or {}).get(p["id"])
            voices.append((-(a["stake"] if a else 0), characters.IDS.index(cid), cid, a))
        voices.sort()
        silent = [cid for _, _, cid, a in voices if not a]
        for _, _, cid, a in voices:
            if a:
                L.append("- " + voice_line(cid, a, genders[cid]))
        if silent:
            L.append("- {}: —".format(", ".join(characters.name(c) for c in silent)))
        L.append("")

    if rebuttals:
        L.append(t("report.debate"))
        L.append("")
        for r in rebuttals:
            L.append(
                "- {} → {} ({}): {}".format(characters.name(r["from"]), characters.name(r["to"]), r["point"], r["text"])
            )
        L.append("")

    L += _brainstorm_section(rec, genders)
    L += _orders_section(rec)
    L += _locks_section(rec)

    if trims or notes:
        L.append(t("report.notes"))
        L.append("")
        for cid, tr in sorted(trims.items()):
            L.append(t("report.trimmed", name=characters.name(cid), list=", ".join(tr)))
        for n in notes:
            L.append(f"- {n}")
        L.append("")

    # The stake ledger: debited when the round ends, and at once returned.
    L.append(t("report.stakes_head"))
    L.append("")
    L.append(stakes_said(rows))
    L.append("")
    fee_all = sum(r["fee"] for r in rows.values())
    ordered_all = sum(r["ordered"] for r in rows.values())
    locked_all = sum(r["locked"] for r in rows.values())
    if fee_all or ordered_all or locked_all:
        returned = (rec.get("settings") or {}).get("lock", {}).get("returned")
        L.append(
            t(
                "report.besides",
                list="; ".join(
                    x
                    for x in (
                        t("report.besides_fee", n=int(fee_all)) if fee_all else "",
                        t("report.besides_orders", n=int(ordered_all)) if ordered_all else "",
                        t("report.besides_locks" if returned else "report.besides_locks_kept", n=int(locked_all))
                        if locked_all
                        else "",
                    )
                    if x
                ),
            )
        )
        L.append("")
    L.append(t("report.stakes_cols"))
    L.append("|---|---|---|---|---|---|---|---|---|")
    ids, mismatch = [], []
    for cid in characters.IDS:
        r = rows[cid]
        ids += r["ids"]
        if not r["ok"]:
            mismatch.append(characters.name(cid))
        if cid not in cast:
            continue
        L.append(
            "| %s | %d | %s | %d | %d | %s | %s | %s | %d |"  # noqa: UP031
            % (
                characters.name(cid),
                r["before"],
                ", ".join("%s: %d" % (e["point"], -e["amount"]) for e in r["stakes"]) or "—",  # noqa: UP031
                r["staked"],
                r["back"],
                _signed(-r["fee"]),
                _signed(-r["ordered"]),
                _signed(-r["locked"]),
                r["left"],
            )
        )
    L.append("")
    legacy = ["%s %d" % (characters.name(cid), rows[cid]["legacy"]) for cid in characters.IDS if rows[cid]["legacy"]]  # noqa: UP031
    if legacy:
        L.append(t("report.legacy", list=", ".join(legacy)))
        L.append("")
    over = [characters.name(cid) for cid in characters.IDS if rows[cid]["left"] < 0]
    ids.sort()
    L.append(
        t(
            "report.total",
            staked=int(sum(r["staked"] for r in rows.values())),
            back=int(sum(r["back"] for r in rows.values())),
            over=t("report.within") if not over else t("report.overspent", names=", ".join(over)),
            ledger=t("report.ledger_mismatch", names=", ".join(mismatch))
            if mismatch
            else (t("report.ledger_ok", first=ids[0], last=ids[-1]) if ids else t("report.ledger_empty")),
        )
    )
    L += _after_round(rec, ledger_entries, rows)
    return "\n".join(L) + "\n"


def _after_round(rec, entries, rows):
    rid = rec["id"]
    owner = sorted((e for e in entries if e.get("round") == rid and e["type"] in OWNER_TYPES), key=lambda e: e["seq"])
    if not owner:
        return []
    end = _end_seq(rec, entries)
    about = {e["id"] for e in owner}
    about |= {e["id"] for e in entries if e.get("round") == rid and e["type"] == "auto_refund"}
    balances_after = {cid: rows[cid]["left"] for cid in characters.IDS}
    shown = [
        cid
        for cid in characters.IDS
        if not rec.get("only") or cid == rec["only"] or any(e["character"] == cid for e in owner)
    ]  # a solo round: its character, and whom the owner touched
    L = ["", t("report.after_head"), ""]
    L.append(t("report.after_cols"))
    L.append("|---|---|---|---|---|---|---|")
    mismatch = []
    for cid in shown:
        mine = [e for e in owner if e["character"] == cid]
        back = sum(e["amount"] for e in mine if e["type"] == "refund")
        paid = sum(e["amount"] for e in mine if e["type"] in ("payout", "order_settle"))
        fined = -sum(e["amount"] for e in mine if e["type"] == "fine")
        other = sum(e["amount"] for e in entries if e["character"] == cid and e["seq"] > end and e["id"] not in about)
        now = balances_after[cid] + back + paid - fined + other
        L.append(
            "| %s | %d | %s | %s | %s | %s | %d |"  # noqa: UP031
            % (
                characters.name(cid),
                balances_after[cid],
                _signed(back),
                _signed(paid),
                _signed(-fined),
                _signed(other),
                now,
            )
        )
        if now != sum(e["amount"] for e in entries if e["character"] == cid):
            mismatch.append(characters.name(cid))
    L.append("")
    L.append(t("report.after_legend"))
    L.append("")
    for e in owner:
        L.append(
            "- {} {}: {}{}{}".format(
                e["id"],
                characters.name(e["character"]),
                _owner_what(e),
                " ({})".format(e["point"]) if e.get("point") else "",
                ": " + _indent(e["reason"]) if e.get("reason") else "",
            )
        )
    L.append("")
    last = max(e["seq"] for e in entries)
    L.append(
        t("report.now_mismatch", names=", ".join(mismatch)) if mismatch else t("report.now_ok", tx="tx-%06d" % last)  # noqa: UP031
    )
    return L


def _end_seq(rec, entries):
    """The ledger's last transaction when the round ended: saved by the round, else its own last
    stake (or an old round's think transaction), else the last one written before it finished.
    """
    if rec.get("ledger_seq") is not None:
        return rec["ledger_seq"]
    own = [e["seq"] for e in entries if e.get("round") == rec["id"] and e["type"] in ("stake", *LEGACY)]
    if own:
        return max(own)
    return max([e["seq"] for e in entries if e["ts"] <= (rec.get("finished") or "")] or [0])


SETTLE_RU = i18n.Labels("report.settle", ("moved", "burn", "refund"))
QUORUM_RU = i18n.Labels("report.quorum_word", ("unanimous", "majority"))


def _brainstorm_section(rec, genders):
    bs = rec.get("brainstorm")
    if not bs:
        return []
    L = [t("report.bs_head"), ""]
    L.append(t("report.quorum", quorum=QUORUM_RU.get(bs.get("quorum"), bs.get("quorum"))))
    L.append("")
    for p in bs.get("proposals") or []:
        cid = p["initiator"]
        if p.get("id"):
            head = t("report.bs_proposal", id=p["id"], topic=p["topic"], name=characters.name(cid))
        else:
            head = t("report.bs_proposal_noid", topic=p.get("topic") or t("report.no_topic"), name=characters.name(cid))
        if p["status"] == "approved":
            head += " — " + t("report.bs_approved", id=p["brainstorm"])
        elif p["status"] == "passed":
            head += " — " + t("report.bs_passed")
        else:
            head += " — " + t("report.bs_failed")
        L.append(head)
        if p.get("why"):
            L.append(t("report.bs_why", text=_indent(p["why"])))
        for i, q in enumerate(p.get("questions") or [], 1):
            L.append(t("report.bs_question", n=i, text=q))
        for c in characters.IDS if p.get("votes") else ():
            v = p["votes"].get(c)
            if not (p.get("took_part") or {}).get(c):
                what = t("report.vote_absent", gender=characters.check_gender(genders.get(c)))
            elif not v:
                what = t("report.vote_none", gender=characters.check_gender(genders.get(c)))
            else:
                what = t("report.vote_yes" if v[0] == "yes" else "report.vote_no") + (": " + v[1] if v[1] else "")
            L.append(f"- {characters.name(c)}: {what}")
        if p.get("fees"):
            fees = ", ".join("%s %d" % (characters.name(c), p["fees"][c]) for c in characters.IDS)  # noqa: UP031
            L.append(
                t("report.bs_fee_paid", fees=fees, first=p["fee_txs"][0], last=p["fee_txs"][-1])
                if p.get("fee_txs")
                else t("report.bs_fee", fees=fees)
            )
        for r in p.get("reasons") or []:
            L.append(t("report.bs_reason", text=r))
        if p["status"] == "approved":
            L.append(t("report.bs_run", file=p["topic_file"], id=p["brainstorm"]))
        L.append("")
    return L


def _orders_section(rec):
    o = rec.get("orders") or {}
    shown = rec.get("orders_shown") or []
    if not (o.get("placed") or o.get("refused") or shown):
        return []
    L = [t("report.orders_head"), ""]
    for x in o.get("placed") or []:
        L.append(
            t(
                "report.order_placed",
                id=x["id"],
                name=characters.name(x["character"]),
                price=int(x["price"]),
                command=x["command"],
                seeds=x["seeds"],
                hardware=x["hardware"],
                threshold=x["threshold"],
                forecast=x["forecast"],
                why=x["why"],
            )
        )
    for x in o.get("refused") or []:
        L.append(
            t(
                "report.order_refused",
                name=characters.name(x["character"]),
                command=x["command"] or "—",
                reason=x["reason"],
            )
        )
    if shown:
        L.append(t("report.orders_shown", ids=", ".join(shown)))
    L.append("")
    return L


def _locks_section(rec):
    lk = rec.get("locks")
    if lk is None:
        return []
    from .stack import lock_means

    L = [t("report.locks_head"), ""]
    if lk["pass"] == "ran":
        L.append(t("report.locks_ran", n=int(lk.get("calls", 0)), means=lock_means()))
    else:
        L.append(t("report.locks_skipped", why=lk.get("why") or "—"))
    for x in lk.get("placed") or []:
        L.append(
            t(
                "report.lock_placed",
                id=x["id"],
                name=characters.name(x["character"]),
                object=x["object"],
                title=x.get("title") or "",
                price=int(x["price"]),
                condition=x["condition"],
                why=x["why"],
            )
        )
    for x in lk.get("refused") or []:
        if x.get("object"):
            L.append(
                t(
                    "report.lock_refused_obj",
                    name=characters.name(x["character"]),
                    object=x["object"],
                    reason=x["reason"],
                )
            )
        else:
            L.append(t("report.lock_refused", name=characters.name(x["character"]), reason=x["reason"]))
    for x in lk.get("lifted") or []:
        L.append(
            t(
                "report.lock_lifted",
                id=x["id"],
                object=x["object"],
                name=characters.name(x["by"]),
                how=t("report.lift_met") if x.get("how") == "met" else t("report.lift_owner"),
                reason=x.get("reason") or "",
            )
        )
    L.append("")
    return L


def _signed(n):
    return "0" if not n else "%s%d" % ("+" if n > 0 else "-", abs(n))  # noqa: UP031


def _owner_what(e):
    kind = e["type"]
    if kind == "refund":
        return t("report.ow_refund", n=int(e["amount"]))
    if kind == "payout":
        return t("report.ow_payout", n=int(e["amount"]))
    if kind == "forfeit":
        return t("report.ow_forfeit", n=int(e.get("stake", 0)))
    if kind == "order_settle":
        return t(
            "report.ow_settle",
            order=e.get("order"),
            outcome=SETTLE_RU.get(e.get("outcome"), e.get("outcome")),
            amount=_signed(e["amount"]),
        )
    taken = -e["amount"]
    asked = e.get("asked", taken)
    return (
        t("report.ow_fine", n=int(taken))
        if taken == asked
        else t("report.ow_fine_part", n=int(taken), asked=int(asked))
    )


def _indent(text):
    """Continuation lines stay inside their list item."""
    return "\n  ".join(l.rstrip() for l in (text or "").strip().splitlines() if l.strip())  # noqa: E741


def _cell(text):
    return " ".join((text or "—").split()).replace("|", "/")


def _clip(text, n):
    text = (text or "").strip()
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"
