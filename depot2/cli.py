"""depot2 — the command line: the tracks and their cars by stage (the board and its gates), the trains, the stack and
the executors' runs, the merge queue, the queues, the principles, and the team rounds. See README.md (its «Commands»:
every command), or any command's --help.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

from . import (
    characters,
    config,
    i18n,
)
from . import priority as pri
from .executors import FatalError
from .i18n import t
from .ledger import Decisions, Ledger, LedgerError, Pending
from .persona import Persona
from .store import LockBusy, State


def cmd_round(state, cfg, a):
    from .round import RoundAborted, run_round

    if a.resume:
        given = [
            f
            for f, v in (
                ("--kind", a.kind),
                ("--input", a.input),
                ("--context", a.context),
                ("--since", a.since),
                ("--pr", a.pr),
                ("--only", a.only),
                ("--brainstorm", a.brainstorm),
            )
            if v
        ]
        if given:
            raise ValueError(
                "round --resume {} goes on with the round's own kind, input and cast: no {}".format(
                    a.resume, ", ".join(given)
                )
            )
    elif not a.kind:
        raise ValueError("round needs --kind (or --resume ROUND_ID: an aborted round goes on)")
    only = characters.resolve(a.only) if a.only else None
    if a.item and not a.resume:  # the team stack's item this round runs: its `who` makes it solo
        from .tstack import TeamStack

        who = TeamStack(state, cfg).get(a.item).get("who")
        if who not in (None, "all"):
            if only and only != who:
                raise ValueError(f"{a.item} is for {characters.name(who)} alone, not {characters.name(only)}")
            if not only:
                only = who
                print(f"solo round: {characters.name(who)} (from {a.item})")
    try:
        r = run_round(
            state,
            cfg,
            a.kind,
            a.input,
            a.context or (),
            since=a.since,
            dry_run=a.dry_run,
            pr=a.pr,
            brainstorm=a.brainstorm,
            only=only,
            resume=a.resume,
        )
    except RoundAborted as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2
    if r and a.item:
        from . import effort
        from .tstack import TeamStack

        o = TeamStack(state, cfg).record_round(a.item, r["id"], r["costs"])
        print("{}: round {}, {}".format(a.item, r["id"], effort.fmt(o["spent_mtok"], cfg)))
        state.commit("team: tstack {} ← round {}".format(a.item, r["id"]))
    if r:
        print()
        print(r["report"])
        if r.get("fatal"):  # the round is done and saved; its lock pass hit a fatal error
            print("FATAL: {}".format(r["fatal"]), file=sys.stderr)
            return 2
    return 0


def cmd_report(state, cfg, a):
    """Rebuild a round's report.md from what the round saved and the ledger. No model call."""
    from .report import render
    from .store import atomic_write

    text = render(state, a.round, cfg)
    path = os.path.join(state.rounds_dir(), a.round, "report.md")
    atomic_write(path, text)
    state.touch(path)
    print(text, end="")
    state.commit(f"team: report {a.round} rebuilt")
    return 0


def cmd_payout(state, cfg, a):
    cid = characters.resolve(a.character)
    led = Ledger(state)
    tx = led.payout(cid, a.n, a.reason, round_id=a.round, point=a.point, settles=a.settles)
    if a.settles:
        Pending(state).close(a.settles, a.reason, tx_id=tx["id"])
    print("%s %s +%d → %d" % (tx["id"], characters.name(cid), a.n, tx["balance_after"]))  # noqa: UP031
    state.commit("team: payout %s +%d (%s)" % (cid, a.n, a.reason[:80]))  # noqa: UP031
    return 0


def cmd_fine(state, cfg, a):
    """The owner's fine: n tokens from one character or all. Never below zero: prints what was taken."""
    cids = list(characters.IDS) if a.character == "all" else [characters.resolve(a.character)]
    led = Ledger(state)
    for cid in cids:
        tx = led.fine(cid, a.n, a.reason, round_id=a.round, point=a.point)
        taken = -tx["amount"]
        print(
            "%s %s -%d%s → %d"  # noqa: UP031
            % (
                tx["id"],
                characters.name(cid),
                taken,
                "" if taken == a.n else " (fine %d; the balance had only %d)" % (a.n, taken),  # noqa: UP031
                tx["balance_after"],
            )
        )
    state.commit(
        "team: fine %s %d%s (%s)"  # noqa: UP031
        % (
            "all" if a.character == "all" else cids[0],
            a.n,
            "".join(" " + x for x in (a.round, a.point) if x),
            a.reason[:80],
        )
    )
    return 0


def cmd_refund(state, cfg, a):
    if a.auto:
        return cmd_auto_refund(state, cfg, a)
    if not a.reason:
        raise ValueError("refund needs --reason (only --auto writes its own)")
    cid = None if a.character == "all" else characters.resolve(a.character)
    txs = Ledger(state).refund_stakes(cid, a.round, point=a.point, reason=a.reason)
    for tx in txs:
        print(
            "%s %s +%d (%s %s, ref %s) → %d"  # noqa: UP031
            % (
                tx["id"],
                characters.name(tx["character"]),
                tx["amount"],
                tx["round"],
                tx.get("point"),
                tx["ref"],
                tx["balance_after"],
            )
        )
    state.commit(
        "team: refund {} {}{} ({})".format(
            a.character, a.round, " " + a.point if a.point else "", (a.reason or "")[:80]
        )
    )
    return 0


def cmd_auto_refund(state, cfg, a):
    """`refund all --round R --auto`: what a round does as it ends, applied to an older round that
    still has open stakes. auto_refund transactions, not an owner action: no reflection.
    """
    from .ledger import AUTO_REASON

    if a.character != "all" or a.point:
        raise ValueError(
            "--auto returns every open stake of the round: use `refund all --round R --auto`, without --point"
        )
    led = Ledger(state)
    txs = led.auto_refund_round(a.round, a.reason or AUTO_REASON)
    if not txs:
        back = led.auto_refunds(a.round)
        raise LedgerError(
            "nothing open in {}{}".format(
                a.round,
                (
                    ": its %d stakes came back already (%s … %s)"  # noqa: UP031
                    % (len(back), back[0]["id"], back[-1]["id"])
                )
                if back
                else "",
            )
        )
    for tx in txs:
        print(
            "%s %s +%d auto (%s %s, ref %s) → %d"  # noqa: UP031
            % (
                tx["id"],
                characters.name(tx["character"]),
                tx["amount"],
                tx["round"],
                tx.get("point"),
                tx["ref"],
                tx["balance_after"],
            )
        )
    print(
        "%d stakes, %d tokens back in %s (auto_refund: not an owner action, no reflection). "  # noqa: UP031
        "Rebuild its report: depot2 report --round %s" % (len(txs), sum(t["amount"] for t in txs), a.round, a.round)
    )
    state.commit("team: auto refund %s (%d stakes, %d tokens)" % (a.round, len(txs), sum(t["amount"] for t in txs)))  # noqa: UP031
    return 0


def cmd_forfeit(state, cfg, a):
    cid = None if a.character == "all" else characters.resolve(a.character)
    txs = Ledger(state).forfeit_stakes(cid, a.round, point=a.point, reason=a.reason)
    for tx in txs:
        print(
            "%s %s: stake %d forfeited (%s %s, ref %s) → %d"  # noqa: UP031
            % (
                tx["id"],
                characters.name(tx["character"]),
                tx["stake"],
                tx["round"],
                tx.get("point"),
                tx["ref"],
                tx["balance_after"],
            )
        )
    state.commit(
        "team: forfeit {} {}{} ({})".format(
            a.character, a.round, " " + a.point if a.point else "", (a.reason or "")[:80]
        )
    )
    return 0


def cmd_brainstorm(state, cfg, a):
    """`brainstorm list [--all]` / `brainstorm cancel bs-NNNN --reason …` (it never ran: the fee back)."""
    from .extras import Brainstorms
    from .round import _brainstorm_failed

    log = Brainstorms(state)
    if a.action == "list":
        items = log.listing(all_=a.all)
        if a.json:
            print(json.dumps(items, ensure_ascii=False, indent=1))
            return 0
        for b in items:
            print(
                "%s %-9s %s (%s, %s): %s"  # noqa: UP031
                % (
                    b["id"],
                    b["status"],
                    characters.name(b["initiator"]),
                    b["round"],
                    b.get("ran_in") or "not run",
                    b["topic"],
                )
            )
            if b["status"] == "approved":
                print(
                    "    run: depot2 round --kind brainstorm --brainstorm {}   (topic {})".format(
                        b["id"], b["topic_file"]
                    )
                )
        if not items:
            print("no %sbrainstorms" % ("" if a.all else "approved "))
        return 0
    b = log.get(a.id)
    if b["status"] != "approved":
        raise ValueError("{} is {}: only an approved brainstorm that never ran is cancelled".format(a.id, b["status"]))
    refunds = _brainstorm_failed(state, Ledger(state), b, None, "never ran: " + a.reason)
    print("{} cancelled; the fee came back ({})".format(a.id, ", ".join(refunds)))
    state.commit(f"team: brainstorm {a.id} cancelled ({a.reason[:80]})")
    return 0


def cmd_orders(state, cfg, a):
    from .extras import Orders

    items = Orders(state).listing(all_=a.all)
    if a.json:
        print(json.dumps(items, ensure_ascii=False, indent=1))
        return 0
    for o in items:
        print("%s %-8s %s (%s): %s" % (o["id"], o["status"], characters.name(o["character"]), o["round"], o["command"]))  # noqa: UP031
        print(
            "    seeds {}; {}; threshold: {}; forecast: {}; why: {}".format(
                o["seeds"], o["hardware"], o["threshold"], o["forecast"], o["why"]
            )
        )
        for r in o.get("results") or []:
            print(f"    result: {r}")
        if o.get("outcome"):
            print("    settled: {} ({}): {}".format(o["outcome"], o.get("settle_tx"), o.get("settle_reason") or ""))
    if not items:
        print("no %sorders" % ("" if a.all else "open "))
    return 0


def cmd_order(state, cfg, a):
    """`order done ord-NNNN --result F…` (the orchestrator ran it) / `order settle ord-NNNN moved|burn|refund`."""
    import shutil

    from .extras import Orders

    log = Orders(state)
    o = log.get(a.id)
    if a.action == "done":
        if o["status"] != "queued":
            raise ValueError("{} is {}: only a queued order is marked done".format(a.id, o["status"]))
        missing = [f for f in a.result if not os.path.isfile(f)]
        if missing:
            raise ValueError("no such result file: {}".format(", ".join(missing)))
        d = os.path.join(os.path.expanduser(cfg["orders"]["results_dir"]), a.id)
        Path(d).mkdir(parents=True, exist_ok=True)
        dest = []
        for f in a.result:
            to = os.path.join(d, os.path.basename(f))
            if os.path.exists(to):
                raise ValueError(f"{to} exists already; results are never overwritten")
            shutil.copy2(f, to)
            dest.append(to)
        log.add(a.id, "done", results=dest, note=a.note)
        print("{} done: {}".format(a.id, ", ".join(dest)))
        state.commit(f"team: order {a.id} done")
        return 0
    if o["status"] != "done":
        raise ValueError("{} is {}: only a done order is settled".format(a.id, o["status"]))
    led = Ledger(state)
    tx = led.order_settle(led.get(o["tx"]), a.outcome, cfg["orders"]["moved_bonus"], a.reason)
    log.add(a.id, "settled", outcome=a.outcome, settle_tx=tx["id"], settle_reason=a.reason)
    print(
        "%s settled %s: %s %+d → %d"  # noqa: UP031
        % (
            a.id,
            a.outcome,
            characters.name(tx["character"]),
            tx["amount"],
            tx["balance_after"],
        )
    )
    state.commit(f"team: order {a.id} settled {a.outcome} ({a.reason[:80]})")
    return 0


def _est(cfg, a, allow_range=False):
    """None when neither is given."""
    from . import effort

    pct, mt = getattr(a, "est_pct", None), getattr(a, "est_mtok", None)
    if pct is None and mt is None:
        return None
    return effort.est_from(cfg, pct, mt, allow_range=allow_range)


def _clears(a):
    """`--est-pct none` / `--est-mtok none` clears an estimate."""
    return any(str(getattr(a, k, None) or "").strip().lower() in ("none", "-") for k in ("est_pct", "est_mtok"))


def _spent(a):
    """--spent-mtok, or --transcript: the spend summed from a transcript (effort.from_transcript)."""
    from . import effort

    spent, path = getattr(a, "spent_mtok", None), getattr(a, "transcript", None)
    if spent is not None and path:
        raise ValueError("give --spent-mtok or --transcript, not both")
    if path:
        v = effort.from_transcript(os.path.expanduser(path))
        print(f"{path}: {v:.4f} MTok from the transcript")
        return v
    return spent


def _freed_slot(state, cfg):
    from .trains import freed_slot_lines

    return freed_slot_lines(state, cfg)


def _closed_car_object(state, cfg, oid, act):
    from .tracks import CAR_DONE, CAR_RU, Tracks

    t, c = Tracks(state, cfg).car_by_stack(oid)
    if c is not None and c["status"] in CAR_DONE:
        raise ValueError(
            "{} is the object of {}:{}, which is {}: no stack {} — the object is left over, close it: "
            "depot2 stack done {} --car {} --reason «…»; new work is a new car (depot2 tracks car add)".format(
                oid, t["id"], c["n"], CAR_RU[c["status"]], act, oid, c["status"]
            )
        )


def cmd_stack(state, cfg, a):
    """The stack: the agents' slots, the post-stack and the queue waiting for a slot; locks on any object."""
    from .stack import Stack, parse_yes_no
    from .tracks import done_needs_car

    stk = Stack(state, cfg)
    act = a.action
    auto = parse_yes_no(a.autostart) if getattr(a, "autostart", None) is not None else None
    if not act:
        if a.json:
            print(json.dumps(stk.to_json(), ensure_ascii=False, indent=1))
        else:
            print(stk.md(), end="")
        return 0
    from .tracks import Tracks

    link = []  # the stack link: what this means for a track's car (tracks.py on_stack)
    if act == "add":
        o = stk.add(
            a.title,
            brief=a.brief,
            branch=a.branch,
            depends=a.depends or (),
            at=a.at,
            pr=a.pr,
            status=a.status,
            size=a.size,
            est_mtok=_est(cfg, a),
            autostart=bool(auto),
            stage=_planning(a),
            priority=a.priority,
        )
        print(
            "%s added to the queue (%d/%d), autostart %s, %s"  # noqa: UP031
            % (
                o["id"],
                len(stk.queue()),
                stk.queue_max,
                "yes" if o["autostart"] else "no",
                pri.cell(o),
            )
        )
    elif act == "start":
        _closed_car_object(state, cfg, a.id, "start")
        was = stk.get(a.id)["where"]
        o = stk.start(a.id, branch=a.branch, reason=a.reason, force=a.force, why=a.why)
        _forced_line(a.id, stk.last_forced, a.why)
        if was == "post" and o["where"] == "queue":
            print(
                "%s ← the post-stack, back for work: no free slot (%d/%d), the head of the queue"  # noqa: UP031
                % (o["id"], len(stk.in_slots()), stk.slots_n)
            )
        else:
            print(
                "%s → slot (%d/%d)%s"  # noqa: UP031
                % (
                    o["id"],
                    len(stk.in_slots()),
                    stk.slots_n,
                    ", back from the post-stack for work" if was == "post" else "",
                )
            )
        link = Tracks(state, cfg).on_stack(
            "start" if o["where"] == "slot" else "requeue", a.id, reason=a.reason, forced=stk.last_forced, why=a.why
        )
    elif act == "post":
        tr = Tracks(state, cfg)
        _closed_car_object(state, cfg, a.id, "post")
        trk, c = tr.car_by_stack(a.id)
        was = stk.get(a.id)["where"]
        pr = stk.get(a.id).get("pr") or (c.get("pr") if c is not None else None)
        if c is not None:
            gate = tr.car_blockers(trk, c, lock=False)
            if gate:
                raise ValueError(
                    "{} is the object of {}:{}, which waits for {}: its PR is blocked, not ready to merge; "
                    "stack park {} frees the slot and keeps the PR on its track".format(
                        a.id, trk["id"], c["n"], ", ".join(gate), a.id
                    )
                )
        reading = _post_reading(state, cfg, stk, a.id, pr)
        o = stk.post(
            a.id,
            reason=a.reason,
            ci=a.ci,
            pr=pr,
            force=a.force,
            why=a.why,
            dod=_dod(a),
            principles=reading.verdict if reading else None,
        )
        _forced_line(a.id, stk.last_forced, a.why)
        print(
            "%s → the post-stack (%d/%d): PR #%s waits for the owner's merge; %s"  # noqa: UP031
            % (
                a.id,
                len(stk.posted()),
                stk.post_max,
                o["pr"],
                "the slot is free (%d/%d)" % (len(stk.in_slots()), stk.slots_n)  # noqa: UP031
                if was == "slot"
                else "it left the queue",
            )
        )
        link = tr.on_stack("post", a.id, pr=o["pr"], forced=stk.last_forced, why=a.why)
        if reading:
            reading.save("stack post")  # read fresh from the disk: after the post and its car's link wrote
        if was == "slot":
            link += _freed_slot(state, cfg)
    elif act == "set":
        spent = _spent(a)
        o = stk.set(
            a.id,
            status=a.status,
            pr=a.pr,
            branch=a.branch,
            title=a.title,
            brief=a.brief,
            size=a.size,
            est_mtok=_est(cfg, a),
            spent_mtok=spent,
            autostart=auto,
            clear_est=_clears(a),
            ci=a.ci,
            dod=_dod(a),
            note=a.note,
            priority=a.priority,
        )
        print(f"{a.id} updated")
        if a.note is not None:
            print(t("cli.note_added", id=a.id, n=len(o["notes"]), note=o["notes"][-1]))
        if a.pr or spent is not None or a.priority is not None:
            link = Tracks(state, cfg).on_stack("set", a.id, pr=a.pr, spent_mtok=spent, priority=a.priority)
    elif act == "move":
        stk.move(a.id, a.at)
        print("%s → queue position %d" % (a.id, [o["id"] for o in stk.queue()].index(a.id) + 1))  # noqa: UP031
    elif act == "take":
        tr = Tracks(state, cfg)
        tr._stack = stk
        trk, c, lines = tr.take_object(a.id, a.to, n=a.n, size=a.size, title=a.title)
        for line in lines:
            print(line)
        print()
        print(tr.car_md(trk["id"]), end="")
    elif act == "done":
        tr = Tracks(state, cfg)
        trk, c = tr.car_by_stack(a.id)
        o = stk.get(a.id)
        if c is not None and not a.car:
            raise ValueError(done_needs_car(a.id, trk, c))
        if a.car == "back":
            if c is None:
                raise ValueError(f"{a.id} is no track's car's object: --car back sends a car back to its track")
            pr = o.get("pr") or c.get("pr")
            if pr:
                raise ValueError(
                    f"{a.id} has PR #{pr}: a car with a PR goes back to its track with `stack park {a.id}` (the PR "
                    f"waits there), or waits for the merge with `stack post {a.id}`"
                )
        stk.done(a.id, a.reason, outcome=a.car)
        print(f"{a.id} left the stack ({a.reason})")
        link = tr.on_stack("done", a.id, reason=a.reason, car_status=a.car)
        if o["where"] == "slot":
            link += _freed_slot(state, cfg)
    elif act == "park":  # a PR that waits (blocked, e.g. by a train) and no agent works: the slot is freed
        tr = Tracks(state, cfg)
        trk, c = tr.car_by_stack(a.id)
        if c is not None:
            pr = stk.get(a.id).get("pr") or c.get("pr")
            if not pr:
                raise ValueError(
                    "{} is the object of {}:{} and has no PR: a car is parked with its PR "
                    "(stack set {} --pr N first); a car with no PR goes back to its track with "
                    "stack done {} --car back".format(a.id, trk["id"], c["n"], a.id, a.id)
                )
            stk.park(a.id, a.reason, leave=True)
            print("%s parked: it left the stack, the slot is free (%d/%d)" % (a.id, len(stk.in_slots()), stk.slots_n))  # noqa: UP031
            link = tr.on_stack("park", a.id, reason=a.reason, pr=pr)
        else:
            stk.park(a.id, a.reason)
            print(
                "%s parked: slot → the head of the queue, its branch, PR and status kept (%d/%d); "  # noqa: UP031
                "a freed slot never takes it by itself: depot2 stack start %s"
                % (a.id, len(stk.in_slots()), stk.slots_n, a.id)
            )
        link += _freed_slot(state, cfg)
    elif act == "unlock":
        how = "met" if a.met else "owner"
        lock = stk.unlock(a.id, how, a.reason)
        print("{}: {} lifted ({})".format(a.id, lock["id"], "the condition landed" if how == "met" else "the owner"))
        if how == "owner":  # the owner's act on the locker: it opens its reflection gate
            d = Decisions(state).add(
                lock["by"],
                t("cli.unlock_decision", lock=lock["id"], id=a.id, condition=lock["condition"], reason=a.reason),
                round_id=lock.get("round"),
            )
            print("{} recorded for {}".format(d["id"], lock["by"]))
        if cfg["lock"]["returned"] and lock.get("tx"):
            led = Ledger(state)
            tx = led.lock_refund(led.get(lock["tx"]), "unlock {}: {}".format(lock["id"], a.reason))
            print("%s %s +%d (the lock's price back)" % (tx["id"], characters.name(tx["character"]), tx["amount"]))  # noqa: UP031
    for line in stk.notes + link:
        print(line)
    print()
    print(stk.md(), end="")
    state.commit(
        "team: stack {} {}{}".format(
            act,
            getattr(a, "id", None) or getattr(a, "title", "")[:60],
            " → {}:{}".format(trk["id"], c["n"]) if act == "take" else "",
        )
    )
    return 0


def _dod(a):
    """`--heavy --scan --shots`: what the agent handed in for the DoD (gates.py)."""
    return {k: getattr(a, k, None) for k in ("heavy", "scan", "shots") if getattr(a, k, None) is not None}


def _post_reading(state, cfg, stk, oid, pr):
    from . import marks

    if not marks.wanted(cfg) or oid in stk.mq_index()["st"]:
        return None
    reading = marks.Reading(state, cfg, oid, pr=pr)
    for line in reading.lines():
        print(line)
    return reading


def _yes_no(text, flag):
    """`--docs yes|no` and the like: a bool, or None when the flag was not given."""
    from .stack import parse_yes_no

    return None if text is None else parse_yes_no(text, flag)


def _forced_line(oid, forced, why):
    """A move forced past a gate says so, with what it missed (the stage's history keeps it)."""
    from . import gates

    if forced:
        print(t("cli.forced", id=oid, why=why, gates=gates.gates_text(forced)))


def _planning(a):
    """`--stage` on an add: a planning stage (bogie, sketch, project), by its key or its name in any language."""
    from . import stages

    if getattr(a, "stage", None) is None:
        return None
    s = stages.parse(a.stage)
    if s not in stages.PLANNING:
        raise ValueError(t("cli.stage_on_add", stage=stages.ru(s)))
    return s


def cmd_car(state, cfg, a):
    from . import stages

    car = stages.Car(state, cfg, a.id)
    if a.action == "show":
        if a.json:
            print(json.dumps(car.to_json(), ensure_ascii=False, indent=1))
        else:
            print(car.show(), end="")
        return 0
    if a.action in ("ask", "answer", "needs-project"):
        by = a.by or "car " + a.action
        if a.action == "ask":
            q = car.ask(a.text, by=by)
            print(t("cli.question_open", car=car.label, n=q["n"], q=q["q"], id=a.id))
        elif a.action == "answer":
            q = car.answer(a.n, a.answer, by=by)
            print(t("cli.question_closed", car=car.label, n=q["n"], answer=q["answer"]))
        else:
            need = car.needs_project(a.why, off=a.off, by=by)
            print(
                t("cli.project_required", car=car.label, why=need["why"])
                if need
                else t("cli.project_required_off", car=car.label)
            )
        print()
        print(stages.Car(state, cfg, a.id).show(), end="")
        state.commit(f"team: car {a.action} {a.id}")
        return 0
    stage = stages.parse(a.stage)
    warn = [car.tracks.moved_note(a.id), car.tracks.closed_note(car.track, car.car) if car.car is not None else None]
    was, _rows = car.move(stage, why=a.why, by=a.by or "car stage", force=a.force)
    for line in warn:
        if line:
            print(line)
    print(
        "{}: {} → {}{}{}".format(
            car.label,
            stages.ru(was),
            stages.ru(stage),
            (f" ({a.why})") if a.why else "",
            " [--force]" if a.force else "",
        )
    )
    print()
    print(stages.Car(state, cfg, a.id).show(), end="")
    state.commit(f"team: car stage {a.id} {stage}")
    return 0


def cmd_priority(state, cfg, a):
    if a.n is None:
        _kind, ref, rec = pri.get_any(state, cfg, a.id)
        print(t("cli.priority_show", ref=ref, cell=pri.cell(rec), min=pri.MIN, max=pri.MAX, default=pri.DEFAULT))
        return 0
    for line in pri.set_any(state, cfg, a.id, a.n):
        print(line)
    state.commit(f"team: priority {a.id} {pri.parse(a.n)}")
    return 0


def cmd_repair(state, cfg, a):
    from . import claims

    by = a.by or "repair " + a.action
    if a.action == "add":
        obj, ref, claim, lines = claims.add(  # noqa: RUF059
            state,
            cfg,
            a.of,
            bug=a.bug,
            title=a.title,
            brief=a.brief,
            why=a.why,
            size=a.size,
            by=by,
            force=a.force,
        )
        for line in lines:
            print(line)
        for line in _freed_slot(state, cfg):
            print(line)
        state.commit("team: repair add {} ({})".format(obj["id"], ref or obj.get("bug")))
    else:
        _claim, lines = claims.link(state, cfg, a.repair, a.of, why=a.why, by=by, force=a.force)
        for line in lines:
            print(line)
        state.commit(f"team: repair link {a.repair} --of {a.of}")
    return 0


def cmd_claims(state, cfg, a):
    from . import claims

    data = claims.report(state, cfg, since=a.since, until=a.until)
    if a.json:
        print(json.dumps(data, ensure_ascii=False, indent=1))
    else:
        print(claims.report_md(data), end="")
    return 0


def cmd_board(state, cfg, a):
    from .board import Board

    b = Board(state, cfg)
    if a.json:
        print(json.dumps(b.to_json(all_=a.all, view=a.view), ensure_ascii=False, indent=1))
    else:
        print(b.md(all_=a.all, view=a.view, limit=a.limit), end="")
    return 0


def cmd_agents(state, cfg, a):
    from .agents import Agents, parse_usage

    ags = Agents(state, cfg)
    act = a.action
    if act == "start":
        usage = parse_usage(a.usage) if a.usage is not None else None
        try:
            ag = ags.start(a.ref, a.role, by=a.by, usage=usage, force=a.force, why=a.why)
        except ValueError:  # refused: an agent that finished meanwhile is booked all the same
            for line in ags.lines:
                print(line)
            state.commit("team: agents: {}".format("; ".join(ags.lines)[:200]))
            raise
        print(ag["id"])  # the first line: the id alone
        for line in ags.lines:
            print(line)
        if ag.get("forced"):
            from .gates import gates_text

            print(t("cli.forced", id=ag["id"], why=ag["why"], gates=gates_text(ag["gates"])))
        print(t("cli.agent_started", id=ag["id"], role=ags.view(ag)["role_ru"], car=ag["car"], by=ag["by"]))
        state.commit("team: agents start {} {} {}".format(ag["id"], ag["car"], ag["role"]))
        return 0
    if act == "attach":
        ag = ags.attach(a.id, a.agent_id, transcript=a.transcript)
        v = ags.view(ag)
        print(
            t(
                "cli.agent_attached",
                id=ag["id"],
                agent=ag["agent_id"],
                path=ag["transcript"],
                state=v["state_text"],
                turns=v["turns"] if v["turns"] is not None else "—",
                spent=v["spent_text"],
            )
        )
        for line in ags.lines:
            print(line)
        state.commit("team: agents attach {} {}".format(ag["id"], ag["agent_id"]))
        return 0
    if act == "stop":
        ag = ags.stop(a.id, a.why, by=a.by)
        print(t("cli.agent_stopped", id=ag["id"], why=ag["stopped"]["why"]))
        for line in ags.lines:
            print(line)
        state.commit("team: agents stop {}".format(ag["id"]))
        return 0
    # reading: an agent that finished is booked on its car first (once), then the table
    ags.settle()
    if act == "show":
        ags.get(a.id)
        if a.json:
            print(json.dumps(ags.view(ags.get(a.id)), ensure_ascii=False, indent=1))
        else:
            print(ags.show_md(a.id), end="")
    elif a.json:
        print(json.dumps(ags.to_json(include_all=a.all), ensure_ascii=False, indent=1))
    else:
        print(ags.md(include_all=a.all), end="")
    for line in ags.lines:
        print(line, file=sys.stderr if a.json else sys.stdout)
    state.commit("team: agents: {}".format("; ".join(ags.lines)[:200]))
    return 0


def cmd_ballots(state, cfg, a):
    """The owner's ballot queue (ballots.json): every finished round, in order."""
    from .stack import Ballots

    bq = Ballots(state)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(bq.entries(open_only=not a.all), ensure_ascii=False, indent=1))
        else:
            print(bq.md(open_only=not a.all), end="")
        return 0
    if act == "add":
        from .round import add_to_ballots
        from .store import read_json

        rec = read_json(os.path.join(state.rounds_dir(), a.round, "round.json"))
        if not rec or rec.get("status") != "done":
            raise ValueError(f"no finished round {a.round}")
        add_to_ballots(state, rec)
    elif act == "point":
        bq.point(a.round, a.point)
    elif act == "at":
        bq.at(a.round, a.point)
    elif act == "move":
        bq.move(a.round, a.at)
    elif act == "done":
        bq.close(a.round, "answered", a.reason)
    elif act == "drop":
        bq.close(a.round, "dropped", a.reason)
    print(Ballots(state).md(open_only=False), end="")
    state.commit(f"team: ballots {act} {a.round}")
    return 0


def cmd_gender(state, cfg, a):
    cid = characters.resolve(a.character)
    p = Persona(state, cid)
    old = p.meta().get("gender") or f"unset (default {p.gender()})"
    p.set_gender(a.gender)
    print(f"{characters.name(cid)}: gender {old} → {a.gender} (meta.json and spec.md)")
    state.commit(f"team: gender {cid} = {a.gender}")
    return 0


def cmd_decide(state, cfg, a):
    who = "all" if a.character == "all" else characters.resolve(a.character)
    if not (a.text or "").strip():
        raise ValueError("a decision needs its words: --text «…» (nothing recorded)")
    from .stack import Ballots

    bq = Ballots(state)
    point = a.point
    if a.round and a.point and bq.has(a.round):  # the ballot's own spelling of the point (q3 → Q3)
        point = next((p for p in bq.get(a.round)["points_left"] if p.lower() == a.point.lower()), a.point)
    d = Decisions(state).add(who, a.text, round_id=a.round, point=point)
    print("{} recorded for {}".format(d["id"], who))
    if a.round and a.point and bq.has(a.round):
        if bq.point(a.round, point, quiet=True):
            e = bq.get(a.round)
            print("ballot %s: %s answered; %d points left (%s)" % (a.round, point, len(e["points_left"]), e["status"]))  # noqa: UP031
        else:
            left = bq.get(a.round)["points_left"]
            print(
                "ballot {}: no open point {} (open: {}): the ballot is as it was".format(
                    a.round, a.point, ", ".join(left) or "none"
                )
            )
    state.commit("team: decision {} for {}".format(d["id"], who))
    return 0


def cmd_decisions(state, cfg, a):
    from . import decisions

    if a.json:
        print(json.dumps(decisions.to_json(state, grep=a.grep, limit=a.limit), ensure_ascii=False, indent=1))
    else:
        print(decisions.md(state, grep=a.grep, limit=a.limit), end="")
    return 0


def cmd_reflect(state, cfg, a):
    from .reflect import reflect

    only = characters.resolve(a.only) if a.only else None
    try:
        _status, _ = reflect(state, cfg, only=only, dry_run=a.dry_run)
    except FatalError as e:
        state.commit(f"team: reflect stopped: {str(e)[:100]}")
        print(f"FATAL: {e}", file=sys.stderr)
        return 2
    if not a.dry_run:
        state.commit("team: reflect")
    return 0


def cmd_backup(state, cfg, a):
    from . import backup

    return backup.command(state, cfg, a)


def cmd_config(state, cfg, a):
    print(json.dumps(cfg, ensure_ascii=False, indent=1, sort_keys=True))
    return 0


def cmd_ledger(state, cfg, a):
    led = Ledger(state)
    entries = led.entries()
    if a.json:
        out = {
            "balances": led.balances(entries),
            "open_stakes": led.open_stakes(entries=entries),
            "pending": Pending(state).open_items(),
        }
        if a.all:
            out["transactions"] = entries
        print(json.dumps(out, ensure_ascii=False, indent=1))
        return 0
    if a.all:
        for e in entries:
            print(
                "%s %s %-12s %-10s %+4d → %3d  %s %s %s"  # noqa: UP031
                % (
                    e["id"],
                    e["ts"][:16],
                    e["type"],
                    characters.name(e["character"]),
                    e["amount"],
                    e["balance_after"],
                    e.get("round") or "",
                    e.get("point") or "",
                    e.get("reason") or "",
                )
            )
        print()
    bal = led.balances(entries)
    print("%-17s %7s  %s" % ("", "balance", "open stakes"))  # noqa: UP031
    for cid in characters.IDS:
        st = led.open_stakes(cid=cid, entries=entries)
        print(
            "%-17s %7d  %s"  # noqa: UP031
            % (
                characters.name(cid),
                bal[cid],
                ", ".join("%s %s: %d" % (e["round"], e["point"], -e["amount"]) for e in st) or "—",  # noqa: UP031
            )
        )
    print("%-17s %7d" % ("total", sum(bal.values())))  # noqa: UP031
    pend = Pending(state).open_items()
    if pend:
        print("\npending (not in the balances):")
        for p in pend:
            print(
                "  {} {} amount={}: {}".format(
                    p["id"],
                    characters.name(p["character"]),
                    "null" if p["amount"] is None else p["amount"],
                    p["condition"],
                )
            )
    return 0


def cmd_show(state, cfg, a):
    cid = characters.resolve(a.character)
    p = Persona(state, cid)
    led = Ledger(state)
    print("# %s (%s)  balance %d  gender %s" % (characters.name(cid), cid, led.balance(cid), p.gender()))  # noqa: UP031
    print("portrait v%04d, %d versions in history (verified)\n" % (p.version(), p.verify()))  # noqa: UP031
    print(p.portrait().strip() or "(no portrait)")
    print("\n## last thoughts")
    for thought in p.thoughts(cfg["thoughts_in_prompt"]):
        print("- [{}] {}".format(thought.get("round", ""), thought["thought"]))
    st = led.open_stakes(cid=cid)
    if st:
        print("\n## open stakes")
        for e in st:
            print("- %s %s %s: %d (%s)" % (e["id"], e["round"], e["point"], -e["amount"], e.get("reason") or ""))  # noqa: UP031
    if a.spec:
        print("\n## spec\n" + p.spec())
    return 0


def cmd_init(state, cfg, a):
    """A new, empty depot: its git, config.json, the team's specs and balances (the config's `characters`)."""
    from . import seed as seed_mod

    ids = seed_mod.init_empty(state, a.balance)
    print(
        t("init.done", root=state.root, n=len(ids), balance=a.balance)
        if ids
        else t("init.done_no_team", root=state.root)
    )
    state.commit("team: init")
    return 0


def cmd_seed(state, cfg, a):
    from . import seed as seed_mod

    did = False
    if a.specs:
        seed_mod.init_state(state, a.specs)
        print(f"state initialised at {state.root}; specs written")
        did = True
    if a.ledger:
        got = seed_mod.seed_ledger(state, seed_mod.load_seed(a.ledger))
        print("ledger seeded: {}".format(", ".join("%s %d" % (characters.name(c), got[c]) for c in characters.IDS)))  # noqa: UP031
        did = True
    if did:
        state.commit("team: seed ({})".format(", ".join(x for x, y in (("specs", a.specs), ("ledger", a.ledger)) if y)))
    if a.portraits:
        cfg = config.load(state.root, a.set)
        only = characters.resolve(a.only) if a.only else None
        try:
            seed_mod.seed_portraits(
                state,
                cfg,
                os.path.expanduser(a.ballot_dir or "") or None,
                os.path.expanduser(a.review_dir or "") or None,
                only=only,
                dry_run=a.dry_run,
            )
        except FatalError as e:
            state.commit(f"team: seed portraits stopped: {str(e)[:100]}")
            print(f"FATAL: {e}", file=sys.stderr)
            return 2
        if not a.dry_run:
            state.commit("team: seed portraits")
    return 0


def cmd_tstack(state, cfg, a):
    """The team stack: every interaction with the team, 2 slots + 4 waiting, beside the agents' stack."""
    from .tstack import TeamStack

    ts = TeamStack(state, cfg)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(ts.to_json(), ensure_ascii=False, indent=1))
        else:
            print(ts.md(), end="")
        return 0
    if act == "add":
        o = ts.add(
            a.kind,
            title=a.title,
            input_path=a.input,
            who=a.who,
            depends=a.depends or (),
            at=a.at,
            status=a.status,
            size=a.size,
            est_mtok=_est(cfg, a),
            priority=a.priority,
        )
        print("%s added to the team queue (%d/%d), %s" % (o["id"], len(ts.queue()), ts.queue_max, pri.cell(o)))  # noqa: UP031
    elif act == "start":
        o = ts.start(a.id)
        print("%s → team slot (%d/%d)" % (o["id"], len(ts.in_slots()), ts.slots_n))  # noqa: UP031
        cmd = ts.command(o)
        if cmd:
            print(f"run: {cmd}   (then without --dry-run)")
    elif act == "set":
        spent = _spent(a)
        if a.cost is not None:  # deprecated: the spend is MTok now; converted once
            from . import effort

            if spent is not None:
                raise ValueError("give --spent-mtok (or --transcript), not --cost too")
            spent = round(effort.from_usd(a.cost, cfg), 4)
            print(
                f"warning: --cost is deprecated (the spend is in MTok now): read as --spent-mtok {spent:g}, {effort.fmt(spent, cfg)} "
                f"({a.cost:g} / usage.usd_per_mtok {effort.usd_per_mtok(cfg):g})",
                file=sys.stderr,
            )
        ts.set(
            a.id,
            status=a.status,
            title=a.title,
            input=a.input,
            who=a.who,
            kind=a.kind,
            round=a.round,
            cost_usd=a.cost,
            size=a.size,
            est_mtok=_est(cfg, a),
            spent_mtok=spent,
            clear_est=_clears(a),
            priority=a.priority,
        )
        print(f"{a.id} updated")
    elif act == "move":
        ts.move(a.id, a.at)
        print("%s → team queue position %d" % (a.id, [o["id"] for o in ts.queue()].index(a.id) + 1))  # noqa: UP031
    elif act == "done":
        ts.done(a.id, a.reason)
        print(f"{a.id} left the team stack ({a.reason})")
    elif act == "take":
        from .tracks import Tracks

        tr = Tracks(state, cfg)
        tr._tstack = ts
        t, c, lines = tr.take_object(a.id, a.to, n=a.n, size=a.size, title=a.title)
        for line in lines:
            print(line)
        print()
        print(tr.car_md(t["id"]), end="")
    print()
    print(ts.md(), end="")
    state.commit(
        "team: tstack {} {}{}".format(
            act,
            getattr(a, "id", None) or (a.title or a.kind or "")[:60],
            " → {}:{}".format(t["id"], c["n"]) if act == "take" else "",
        )
    )
    return 0


def cmd_tracks(state, cfg, a):
    """The tracks (tracks.json): the themes and their cars."""
    from .tracks import Tracks

    tr = Tracks(state, cfg)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(tr.to_json(), ensure_ascii=False, indent=1))
        else:
            print(tr.md(include_done=a.all), end="")
        return 0
    if act == "import":
        made = tr.import_json(a.file, dry_run=a.dry_run)
        print(
            "%s %d tracks, %d cars from %s"  # noqa: UP031
            % (
                "DRY RUN: would import" if a.dry_run else "imported",
                len(made),
                sum(len(t["cars"]) for t in made),
                a.file,
            )
        )
        print()
        print(tr.md(include_done=True), end="")
        for t in made:
            print()
            print(tr.car_md(t["id"]), end="")
        if not a.dry_run:
            state.commit("team: tracks import (%d tracks)" % len(made))  # noqa: UP031
        return 0
    if act == "car":
        return _cmd_car(state, cfg, tr, a)
    if act == "add":
        t = tr.add(a.title, at=a.at, est_mtok=_est(cfg, a, allow_range=True), note=a.note)
        print("%s added (position %d)" % (t["id"], tr.data["tracks"].index(t) + 1))  # noqa: UP031
        tid = t["id"]
    elif act == "set":
        tr.set(
            a.id, title=a.title, est_mtok=_est(cfg, a, allow_range=True), clear_est=_clears(a), at=a.at, add_note=a.note
        )
        print(f"{a.id} updated")
        tid = a.id
    print()
    print(tr.md(include_done=True), end="")
    state.commit(f"team: tracks {act} {tid}")
    return 0


def _cmd_check(state, cfg, a):
    """`tracks car check REF [--from-pr] | REF N MARK [--line …]`: a car's marks by the principles (marks.py)."""
    from . import marks

    if a.from_pr and a.rest:
        raise ValueError(t("cli.check_either"))
    car, rec = marks.holder(state, cfg, a.ref)
    ref = car.car_ref or a.ref
    if a.from_pr:
        pr = a.pr or next((r.get("pr") for r in car.recs() if r.get("pr")), None)
        if not pr:
            raise ValueError(f"{ref} has no PR: --pr N, or set it first (stack set … --pr N / tracks car set … --pr N)")
        reading = marks.Reading(state, cfg, ref, pr=pr)
        if not reading.plist:
            raise ValueError(t("cli.checks_empty"))
        for line in reading.lines():
            print(line)
        if reading.error:
            raise ValueError(reading.error)
        wrote = reading.save("tracks car check")
        what = f"PR #{pr}"
    elif a.rest:
        pl = marks.plist(state, cfg)
        n, word = a.rest[0], " ".join(a.rest[1:])
        labels = [p["label"] for p in pl]
        m = re.match(r"^(\d+)({}?)$".format(i18n.rx("in.cli.principle_letter")), n, re.IGNORECASE)
        label = marks.norm_label(m.group(1), m.group(2)) if m else None
        if label not in labels:
            raise ValueError(t("cli.no_principle", n=n, labels=", ".join(labels) or t("cli.none_word")))
        mark = marks.mark_of(word)
        if mark is None:
            raise ValueError(t("cli.bad_mark", word=word))
        new = marks.by_hand(rec.get("marks"), label, mark, a.line)
        marks.save(state, cfg, a.ref, new, by="tracks car check", hand=label, mark=mark, line=a.line)
        wrote = True
        what = f"{label} {marks.MARK_RU[mark]}"
        print(
            t("cli.mark_by_hand_line", ref=ref, label=label, mark=marks.MARK_RU[mark], line=a.line)
            if a.line
            else t("cli.mark_by_hand", ref=ref, label=label, mark=marks.MARK_RU[mark])
        )
    else:
        if a.line or a.pr:
            raise ValueError(t("cli.line_pr_only"))
        wrote = False
    car, rec = marks.holder(state, cfg, a.ref)
    print()
    print(marks.md(ref, marks.of(rec, marks.plist(state, cfg))), end="")
    if wrote:
        state.commit(f"team: tracks car check {ref} ({what})")
    return 0


def _cmd_car(state, cfg, tr, a):
    act = a.car_action
    if act == "check":
        return _cmd_check(state, cfg, a)
    if act == "add":
        t, c = tr.car_add(
            a.track,
            a.title,
            n=a.n,
            after=a.after or (),
            size=a.size,
            est_mtok=_est(cfg, a),
            brief=a.brief,
            branch=a.branch,
            pr=a.pr,
            status=a.status or "waiting",
            spent_mtok=a.spent_mtok,
            at=a.at,
            stage=_planning(a),
            priority=a.priority,
        )
        print("{}:{} added, {}".format(t["id"], c["n"], pri.cell(c)))
    elif act == "set":
        before = [tr.moved_note(a.ref), tr.closed_note(*tr.car(a.ref))]  # the car as it was, before the write
        t, c = tr.car_set(
            a.ref,
            title=a.title,
            after=a.after,
            no_after=a.no_after,
            size=a.size,
            est_mtok=_est(cfg, a),
            clear_est=_clears(a),
            spent_mtok=_spent(a),
            brief=a.brief,
            branch=a.branch,
            pr=a.pr,
            status=a.status,
            st=a.st,
            note=a.note,
            note_replace=a.note_replace,
            docs=_yes_no(a.docs, "--docs"),
            priority=a.priority,
            by_hand=True,
        )
        for line in before:
            if line:
                print(line)
        print("{}:{} updated".format(t["id"], c["n"]))
        for line in tr.stage_lines:
            print(line)
    elif act == "move" and (a.to or a.n is not None):
        src = tr.car(a.ref)[0]
        t, c, lines = tr.car_relocate(a.ref, to=a.to, n=a.n, at=a.at, why=a.why)
        for line in lines:
            print(line)
        for x in [t] + ([src] if src is not t else []):  # the new track's table, then the old one's with its pointer
            print()
            print(tr.car_md(x["id"]), end="")
        state.commit("team: tracks car move {} → {}:{}".format(a.ref, t["id"], c["n"]))
        return 0
    elif act == "move":
        if a.at is None:
            raise ValueError("tracks car move: --at K (its place), or --to tr-NNNN / --n NEW (another track or name)")
        if a.why:
            raise ValueError("--why goes with --to or --n")
        t, c = tr.car_move(a.ref, a.at)
        print("%s:%s → position %d" % (t["id"], c["n"], t["cars"].index(c) + 1))  # noqa: UP031
    elif act == "stack":
        t, c = tr.car(a.ref)
        _obj, msgs = tr.car_stack(a.ref, start=a.start, force=a.force, why=a.why)
        for m in tr.stack.notes + msgs:
            print(m)
    print()
    print(tr.car_md(t["id"]), end="")
    state.commit("team: tracks car {} {}:{}".format(act, t["id"], c["n"]))
    return 0


def cmd_track(state, cfg, a):
    """One track: its cars."""
    from .tracks import Tracks

    tr = Tracks(state, cfg)
    if a.json:
        print(json.dumps(tr.track_json(tr.get(a.id)), ensure_ascii=False, indent=1))
    else:
        print(tr.car_md(a.id), end="")
    return 0


def cmd_trains(state, cfg, a):
    """The trains (trains.json): named runs of cars in three states, who goes when, the slot policy."""
    from .trains import Trains

    tr = Trains(state, cfg)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(tr.to_json(), ensure_ascii=False, indent=1))
        else:
            print(tr.md(include_done=a.all), end="")
        return 0
    if act == "next":
        if a.next_json or a.json:
            print(json.dumps(tr.to_json()["next"], ensure_ascii=False, indent=1))
        else:
            print("\n".join(tr.next_lines()))
            for tn, why in tr.next()["waiting"]:
                print(t("cli.train_waits", name=tn["name"], title=tn["title"], why="; ".join(why) or "—"))
        return 0
    lines = []
    if act == "add":
        tn = tr.add(
            a.name,
            a.title,
            cars=a.car or (),
            at=a.at,
            after=a.after or (),
            not_before=a.not_before,
            owner=a.owner_word,
            note=a.when,
            slots=a.slots,
            est_mtok=_est(cfg, a, allow_range=True),
            priority=a.priority,
        )
        print(t("cli.train_added", name=tn["name"], n=tr.data["trains"].index(tn) + 1, prio=pri.cell(tn)))
        name = tn["name"]
    elif act == "set":
        owner = None if a.owner_word is None else a.owner_word == "yes"
        tn = tr.set(
            a.name,
            new_name=a.new_name,
            title=a.title,
            after=a.after,
            no_after=a.no_after,
            not_before=a.not_before,
            owner=owner,
            note=a.when,
            slots=a.slots,
            est_mtok=_est(cfg, a, allow_range=True),
            clear_est=_clears(a),
            at=a.at,
            add_note=a.note,
            priority=a.priority,
        )
        print("{} updated".format(tn["name"]))
        name = tn["name"]
    elif act == "state":
        lines = tr.set_state(a.name, a.to, why=a.why, force=a.force, word=a.word)
        name = a.name
    elif act == "done":
        lines = tr.done(a.name, why=a.why, force=a.force)
        name = a.name
    elif act == "drop":
        lines = tr.drop(a.name, a.why)
        name = a.name
    elif act == "car":
        if a.car_action == "add":
            tr.car_add(a.name, a.ref, at=a.at)
        elif a.car_action == "move":
            tr.car_move(a.name, a.ref, a.at)
        else:
            tr.car_drop(a.name, a.ref, why=a.why)
        print(f"{a.name}: {a.car_action} {a.ref}")
        name = a.name
        act = f"car {a.car_action} {a.ref}"
    for line in lines:
        print(line)
    print()
    print(tr.car_md(name), end="")
    state.commit(f"team: trains {act} {name}")
    return 0


def cmd_train(state, cfg, a):
    """One train: its cars in order."""
    from .trains import Trains

    tr = Trains(state, cfg)
    if a.json:
        print(json.dumps(tr.train_json(tr.get(a.name)), ensure_ascii=False, indent=1))
    else:
        print(tr.car_md(a.name), end="")
    return 0


def cmd_migrate(state, cfg, a):
    """The depot's data, brought to today's form once (migrate.py)."""
    from . import migrate

    lines, n = migrate.run(state, cfg, dry_run=a.dry_run)
    print("\n".join(lines))
    if n and not a.dry_run:
        state.commit("team: migrate the depot to schema %d" % migrate.SCHEMA)  # noqa: UP031
    return 0


def cmd_web(state, cfg, a):
    """Ctrl+C stops it."""
    from . import webui

    try:
        srv = webui.Server(state, sets=a.set, port=a.port, verbose=a.verbose)
    except OSError as e:
        print(t("cli.web_port", name=webui.NAME, port=a.port, err=e), file=sys.stderr)
        return 1
    print(t("cli.web_url", name=webui.NAME, url=srv.url), flush=True)
    if srv.urls:
        print(t("cli.web_hosts", name=webui.NAME, urls=", ".join(srv.urls)), flush=True)
    if srv.dropped:
        bad = ", ".join(json.dumps(v, ensure_ascii=False) for v in srv.dropped)
        print(t("cli.web_bad_host", bad=bad), file=sys.stderr, flush=True)
    if srv.resumed:
        print(t("cli.web_resumed", name=webui.NAME, code=srv.code_line()), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        if not srv.restarting:
            srv.server_close()
    if srv.restarting:
        print(t("cli.web_restart", name=webui.NAME), flush=True)
        webui.reexec(srv)
    return 0


QUEUES = {"wishes": "Wishes", "wish": "Wishes", "think": "Think", "todo": "Todo"}


def cmd_queue(state, cfg, a):
    """The wishes, to-think and todo queues: print, add, set, the status moves, show, import."""
    from . import queues

    name = "wishes" if a.cmd == "wish" else a.cmd
    q = getattr(queues, QUEUES[name])(state, cfg)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(q.to_json(open_only=not a.all), ensure_ascii=False, indent=1))
        else:
            print(q.md(open_only=not a.all, limit=a.limit), end="")
        return 0
    if act == "show":
        print(q.show(a.id), end="")
        return 0
    ids = []
    if act == "add":
        if name == "wishes":
            e = q.add(a.title, words=a.words, date=a.date, area=a.area, priority=a.priority, links=a.link or ())
        elif name == "think":
            e = q.add(
                a.topic,
                questions=a.question or (),
                by=a.by,
                related=a.related or (),
                date=a.date,
                links=a.link or (),
                priority=a.priority,
            )
        else:
            version = a.version or queues.repo_version(cfg)
            e = q.add(
                a.text,
                title=a.title,
                where=a.where or (),
                version=version,
                date=a.date,
                source=a.source,
                kind=a.kind,
                trigger=a.trigger,
                links=a.link or (),
                priority=a.priority,
            )
        print("{} added".format(e["id"]))
        ids = [e["id"]]
    elif act == "set":
        fields = {k: getattr(a, k, None) for k in q.FIELDS}
        fields.update(links=a.link, notes=a.note)
        for k in ("question", "related", "where"):
            if getattr(a, k, None):
                fields["questions" if k == "question" else k] = getattr(a, k)
        q.set(a.id, **{k: v for k, v in fields.items() if v is not None})
        print(f"{a.id} updated")
        ids = [a.id]
    elif act == "take":
        from .stack import Stack
        from .tracks import Tracks
        from .tstack import TeamStack

        known = Stack(state, cfg).known_ids() | TeamStack(state, cfg).known_ids()
        tr = Tracks(state, cfg)
        e, ref, lines = q.take(a.id, a.to, tr, stack=lambda x: x in known, n=a.n, size=a.size, title=a.title)
        for line in lines:
            print(line)
        if ":" in ref:  # the car on its track
            print()
            print(tr.car_md(ref.split(":")[0]), end="")
        ids = [a.id, "→", ref]
    elif act == "round":
        q.to_round(a.id, a.round, cfg)
        print(f"{a.id} → round {a.round}")
        ids = [a.id]
    elif act == "decide":
        decs = {d["id"] for d in Decisions(state).records()} if a.dec else None
        q.decide(a.id, dec=a.dec, reason=a.reason, decisions=decs)
        print("{} decided{}".format(a.id, f" ({a.dec})" if a.dec else ""))
        ids = [a.id]
    elif act == "pr":
        q.in_pr(a.id, a.pr)
        print("%s → PR #%d" % (a.id, a.pr))  # noqa: UP031
        ids = [a.id]
    elif act == "done" and name == "wishes":
        for eid in a.ids:
            q.closable(eid, "done")
        for eid in a.ids:
            q.done(eid, a.reason)
            print("{} → {}".format(eid, q.STATUS_RU["done"]))
        ids = list(a.ids)
    elif act in ("drop", "done", "wont"):
        status = {
            "wishes": {"drop": "declined"},
            "think": {"drop": "dropped"},
            "todo": {"done": "done", "wont": "wont"},
        }[name][act]
        for eid in a.ids:  # every id checked before the first is written
            q.closable(eid, status)
        for eid in a.ids:
            q.to_status(eid, status, reason=a.reason)
            print(f"{eid} → {q.STATUS_RU[status]}")
        ids = list(a.ids)
    for line in q.notes:
        print(line)
    print()
    print(q.md(), end="")
    state.commit("team: {} {} {}".format(name, act, " ".join(ids)[:80]))
    return 0


def cmd_principles(state, cfg, a):
    from .principles import SECTION_TITLE, Principles, QueueError, render_text

    p = Principles(state, cfg)
    act = a.action
    if not act:
        if a.json:
            print(json.dumps(p.to_json(open_only=not a.all), ensure_ascii=False, indent=1))
        else:
            print(p.md(open_only=not a.all, limit=a.limit), end="")
        return 0
    if act == "show":
        print(p.show(a.id), end="")
        return 0
    if act == "checks":
        if a.json:
            print(json.dumps({"v": 2, "checks": p.checks()}, ensure_ascii=False, indent=1))
        else:
            print(p.checks_md(), end="")
        return 0
    if act == "import":
        head, items = p.import_file(a.file, dry_run=a.dry_run)
        n = len(items)
        if a.dry_run:
            lines = len(render_text(head, items, banner=False).splitlines())
            print(t("cli.import_dry", n=n, file=a.file, lines=lines))
            for key, title in SECTION_TITLE.items():
                rows = [it for it in items if it["section"] == key]
                if rows:
                    print(
                        "- {}: {}".format(
                            title,
                            ", ".join(
                                "%d%s%s" % (it["n"], it["suffix"], " ③" if it["check"] else "")  # noqa: UP031
                                for it in rows
                            ),
                        )
                    )
            print(
                "words: %d, subs: %d, check: %s"  # noqa: UP031
                % (
                    sum(len(it["words"]) for it in items),
                    sum(len(it["subs"]) for it in items),
                    ", ".join(
                        "pc-%04d%s" % (it["n"], it["suffix"])  # noqa: UP031
                        for it in items
                        if it["check"]
                    )
                    or "none",
                )
            )
            return 0
        path, changed = p.write()
        if changed is None:
            print(f"{path}: {_rendered(state, changed)}")
        print(
            "imported %d principles from %s; the render equals it line for line, but the banner and the sections' "  # noqa: UP031
            "headings" % (n, a.file)
        )
        if changed is not None:
            print(f"{path} is rendered from the depot now and read-only (chmod a-w): change it with depot2 principles")
        print()
        print(p.md(), end="")
        state.commit("team: principles import (%d principles)" % n)  # noqa: UP031
        return 0
    if act == "render":
        if a.out == "-":
            print(p.render(), end="")
            return 0
        to_target = not a.out or os.path.abspath(os.path.expanduser(a.out)) == p.target()
        drift = p.drift() if to_target and not p.live_target() else None  # a copy writes no live file: no drift
        if drift and not a.overwrite:
            raise QueueError(
                f"{p.target()} was edited by hand ({drift}): carry the change over with depot2 principles set|add, or "
                "write the depot's render over it: principles render --overwrite"
            )
        path, changed = p.write(a.out)
        print(
            "{}: {}".format(path, _rendered(state, changed, "rendered from the depot", "already as the depot has it"))
        )
        return 0
    if act == "head" and a.text is None:
        print("\n".join(p.head()).rstrip("\n") + "\n", end="")
        return 0
    drift = None if p.live_target() else p.drift()
    if drift and not a.overwrite:
        raise QueueError(
            f"{p.target()} was edited by hand ({drift}): nothing changed. Carry the edit over with this command, then "
            "--overwrite (the render writes over the hand edit)"
        )
    if act == "head":
        p.set_head(a.text)
        print("the head of PRINCIPLES.md replaced (the history keeps the old one)")
        if p.target():
            path, changed = p.write()
            print(f"{path}: {_rendered(state, changed)}")
        print()
        print("\n".join(p.head()).rstrip("\n") + "\n", end="")
        state.commit("team: principles head")
        return 0
    check = {"yes": True, "no": False}.get(getattr(a, "check", None))
    if act == "add":
        e = p.add(
            a.section,
            title=a.title,
            text=a.text,
            words=a.words or (),
            subs=a.sub or (),
            date=a.date,
            links=a.link or (),
            check=bool(check),
            order=a.order,
        )
        print("{} added (principle {})".format(e["id"], e["n"]))
    elif act == "set":
        e = p.set(
            a.id,
            words=a.words or (),
            subs=a.sub or (),
            sub_set=a.sub_set,
            sub_drop=a.sub_drop,
            section=a.section,
            title=a.title,
            text=a.text,
            date=a.date,
            order=a.order,
            gap=a.gap,
            check=check,
            links=a.link,
            notes=a.note,
        )
        print("{} updated".format(e["id"]))
    elif act == "replace":
        e = p.replace(a.id, a.by, reason=a.reason)
        print(t("cli.principle_replaced", id=e["id"], by=a.by))
    else:  # drop
        e = p.drop(a.id, a.reason)
        print(t("cli.principle_dropped", id=e["id"]))
    if p.target():
        path, changed = p.write()
        print(f"{path}: {_rendered(state, changed)}")
    print()
    print(p.show(e["id"]), end="")
    state.commit("team: principles {} {}".format(act, e["id"]))
    return 0


def _rendered(state, changed, yes="rendered again", no="unchanged"):
    if changed is None:
        return (
            f"not rendered: it is the live depot's and this depot ({state.root}) another — a copy writes no live file; "
            "principles render --out PATH renders it elsewhere"
        )
    return yes if changed else no


def cmd_merge(state, cfg, a):
    from . import merge

    act = a.action
    if not act:
        if a.json:
            print(json.dumps(merge.to_json(state, all_=a.all), ensure_ascii=False, indent=1))
        else:
            print(merge.md(state, cfg, all_=a.all), end="")
        return 0
    if act == "run":
        if a.ensure:
            if a.dry_run or a.once:
                raise ValueError("merge run --ensure starts the whole runner: not with --dry-run or --once")
            rc, lines = merge.ensure(state, cfg, sets=a.set)
            for line in lines:
                print(line)
            return rc
        r = merge.Rider(state, cfg)
        if a.dry_run:
            for line in r.plan():
                print(line)
            return 0
        return r.run(once=a.once)
    if act == "prune-worktrees":
        for line in merge.prune_worktrees(state, cfg, dry_run=a.dry_run):
            print(line)
        return 0
    if act == "report":
        if a.json:
            print(json.dumps(merge.report_json(state, last=a.last), ensure_ascii=False, indent=1))
        else:
            print(merge.report(state, cfg, last=a.last), end="")
        return 0
    if act == "resolve":
        res = merge.Resolutions(state)
        if a.res_action in (None, "list"):
            items = res.items()
            for it in items:
                print(
                    "%s  %s  ours %s  theirs %s  %d bytes  %s%s"  # noqa: UP031
                    % (
                        it["added"][:16],
                        it["file"],
                        it["ours"][:12],
                        it["theirs"][:12],
                        it["bytes"],
                        it["key"][:25] + "…",
                        "  ({})".format(it["note"]) if it.get("note") else "",
                    )
                )
            if not items:
                print("no prepared resolutions")
            return 0
        if a.res_action == "add":
            it, replaced = res.add(a.file, a.ours, a.theirs, a.src, note=a.note, replace=a.replace)
            print(
                "%s a prepared resolution for %s: ours %s, theirs %s (%d bytes, sha256 %s)"  # noqa: UP031
                % (
                    "replaced" if replaced else "added",
                    it["file"],
                    it["ours"][:12],
                    it["theirs"][:12],
                    it["bytes"],
                    it["sha256"][:12],
                )
            )
            state.commit("team: merge resolve add {} ({})".format(it["file"], it["key"][:25]))
            return 0
        it = res.drop(a.key)
        print("dropped the prepared resolution for {} ({}): {}".format(it["file"], it["key"][:25], a.reason))
        state.commit("team: merge resolve drop {} ({})".format(it["file"], a.reason[:60]))
        return 0
    if act == "add":
        e, lines = merge.add(state, cfg, a.pr, a.word, at=a.at, force=a.force, why=a.why)
    elif act == "move":
        e, lines = merge.move(state, cfg, a.ref, a.at)
    elif act == "handoff":
        e, lines = merge.handoff(state, cfg, a.ref, a.reason)
    elif act == "resume":
        e, lines = merge.resume(state, cfg, a.ref, a.reason)
    elif act == "drop":
        e, lines = merge.drop(state, cfg, a.ref, a.reason)
    else:  # rerun
        e, lines = merge.rerun(state, cfg, a.ref)
    for line in lines:
        print(line)
    print()
    print(merge.md(state, cfg), end="")
    state.commit("team: merge %s %s #%d" % (act, e["id"], e["pr"]))  # noqa: UP031
    return 0


def cmd_queues(state, cfg, a):
    from .queues import migrate, overview

    if a.action == "migrate":
        lines, changed = migrate(state, cfg, dry_run=a.dry_run)
        print(t("cli.queues_migrate_dry" if a.dry_run else "cli.queues_migrate", n=changed))
        for line in lines:
            print("- " + line)
        if not a.dry_run and changed:
            state.commit("team: queues migrate (stack autostart, stages, from; %d)" % changed)  # noqa: UP031
        return 0
    text, rows = overview(state, cfg)
    if a.json:
        print(
            json.dumps(
                [{"queue": r[0], "open": r[1], "head": r[2], "command": r[3], "priority": r[4]} for r in rows],
                ensure_ascii=False,
                indent=1,
            )
        )
    else:
        print(text, end="")
    return 0


def _label_words(labels):
    """{a typed word: its key} over a label family's words in every language (a plain dict: its own words)."""
    if isinstance(labels, i18n.Labels):
        return {i18n.fold(w): k for k in labels for w in i18n.forms(f"{labels.prefix}.{k}")}
    return {i18n.fold(v): k for k, v in labels.items()}


def _train_state(text):
    from .trains import STATE_RU

    s = (text or "").strip().lower()
    s = _label_words(STATE_RU).get(i18n.fold(s), s)
    if s in ("design", "impl", "merge"):
        return s
    if s == "arrived":
        raise argparse.ArgumentTypeError(t("cli.train_state_arrived"))
    if s == "scrapped":
        raise argparse.ArgumentTypeError(t("cli.train_state_scrapped"))
    raise argparse.ArgumentTypeError(t("cli.train_state_bad", text=repr(text)))


def _unrecognized(argv, extra):
    msg = "unrecognized arguments: {}".format(" ".join(extra))
    try:
        i = argv.index(extra[0])
    except ValueError:
        return msg
    words, opt = None, None
    if i >= 1 and argv[i - 1].startswith("--") and "=" in argv[i - 1]:
        opt, _, val = argv[i - 1].partition("=")
        words = [val]
    elif i >= 2 and argv[i - 2].startswith("--") and "=" not in argv[i - 2] and not argv[i - 1].startswith("-"):
        opt, words = argv[i - 2], [argv[i - 1]]
    if not opt or extra[0].startswith("-"):
        return msg
    k = i
    while k < len(argv) and not argv[k].startswith("-"):
        k += 1
    words += argv[i:k]
    return msg + ' — if «{}» belongs to {}, the value goes in quotes: {} "{}"'.format(
        " ".join(argv[i:k]), opt, opt, " ".join(words)
    )


def _force_args(q, what):
    q.add_argument("--force", action="store_true", help=f"past {what} (soft): only with --why, recorded as forced")
    q.add_argument("--why", help="why it goes past the gate (with --force)")


def _dod_args(q):
    q.add_argument("--heavy", help="Heavy (the full test run elsewhere): green, red, local, n/a or words")
    q.add_argument("--scan", help="the private scan: clean (its last line CLEAN), dirty, or words")
    q.add_argument("--shots", help="the screenshots: yes (looked at), n/a (nothing to see), or words")


def _take_args(q):
    q.add_argument("id")
    q.add_argument("--n", help="the new car's name (default: the next free number; a taken one is refused)")
    q.add_argument("--size", help="the new car's size, a key of usage.sizes (doc S M L)")
    q.add_argument("--title", help="the new car's title (default: the entry's)")


def _effort_args(q, spent=False):
    q.add_argument("--size", help="a key of usage.sizes (doc S M L), in %%UW")
    q.add_argument("--est-pct", help="the expected effort in %%UW (instead of the size's)")
    q.add_argument("--est-mtok", help="… or in MTok")
    if spent:
        q.add_argument("--spent-mtok", type=float, help="the spend in MTok (price-weighted input-equivalent)")
        q.add_argument(
            "--transcript", metavar="FILE", help="… or summed from an agent's transcript (.jsonl or a task's .output)"
        )


def _migrate_first(state, cfg, a):
    """The depot's data from before today's form are brought to it once, before any command (migrate.py); the web
    view never writes (it says the depot waits), `migrate` runs it itself.
    """
    from . import migrate

    if a.cmd in ("seed", "init", "web", "migrate") or not migrate.pending(state):
        return
    with state.lock():
        lines, n = migrate.run(state, cfg)
        if n:
            state.commit("team: migrate the depot to schema %d" % migrate.SCHEMA)  # noqa: UP031
    print("\n".join(lines), file=sys.stderr)


ID_OF = {
    "stack": "st-NNNN",
    "tstack": "tm-NNNN",
    "wishes": "wl-NNNN",
    "wish": "wl-NNNN",
    "think": "th-NNNN",
    "todo": "td-NNNN",
    "agents": "ag-NNNN",
    "agent": "ag-NNNN",
    "brainstorm": "bs-NNNN",
    "order": "ord-NNNN",
    "principles": "pc-NNNN",
    "principle": "pc-NNNN",
    "tracks": "tr-NNNN",
    "track": "tr-NNNN",
    "car": "the car: tr-NNNN:N, st-NNNN, wl-NNNN or th-NNNN",
}
ARG_HELP = {
    "id": "{id}",
    "ids": "one or more: {id}",
    "ref": "the car, tr-NNNN:N (an old ref of a moved car finds it)",
    "track": "the track, tr-NNNN",
    "name": "the train's name (a slug, e.g. search-v1)",
    "pr": "the PR's number",
    "file": "the file it reads",
    "text": "what it is, in words",
    "character": "a character: builder, painter, counter, chronicler, keeper, or the Russian name",
    "n": "how many tokens (a whole number, 1 or more)",
    "outcome": "moved: the price back + 1; burn: nothing back; refund: the price back",
    "gender": "f (feminine) or m (masculine)",
    "round": "the round's id (its folder under rounds/)",
    "point": "the point's id in that round: B1, Q2, N3 …",
    "json": "the data as JSON (README «JSON formats»)",
    "all": "the closed ones too",
    "reason": "why, in words (kept in the history)",
    "why": "why, in words (kept in the history)",
    "note": "a note, kept in its notes",
    "title": "its title",
    "brief": "its brief: the file's path",
    "branch": "its git branch",
    "status": "its status, in words",
    "at": "its place in the list, from 1",
    "depends": "an object it waits for, st-/tm-NNNN (repeatable)",
    "autostart": "yes or no: a freed slot takes it by itself (trains next); no — the owner's word, stack start",
    "after": "what it waits for (repeatable): a car of its track (3, B2a), tr-NNNN:N, a track tr-NNNN, a train's name, "
    "st-/tm-NNNN, #PR",
    "no_after": "it waits for nothing (clears --after)",
    "start": "into a free slot now: the gate into the build (the DoR, the slots, its train)",
    "when": "the start's condition in words (shown, never checked)",
    "slots": '"max N" or all-free: the slots its cars may take at once',
    "met": "the condition landed (the reason carries the evidence)",
    "owner": "the owner lifts it (written as his decision about the locker)",
    "kind": "its kind",
    "input": "the input file the round reads",
    "who": "all, or one character",
    "answer": "the answer, in words",
    "by": "who (kept in the history)",
    "link": "a link: dec-…, wl-…, tr-…:…, #PR or a URL (repeatable)",
    "area": "where it belongs",
    "date": "YYYY-MM-DD (default today)",
    "words": "the owner's words, verbatim",
    "topic": "what to think about",
    "question": "one question (repeatable)",
    "related": "a related id (repeatable)",
    "where": "FILE:LINE (repeatable)",
    "version": "the project's version: its release + main's commit",
    "source": "PR / agent / tester / owner",
    "trigger": "a reminder's trigger, e.g. «after the v2 train is deployed»",
    "dec": "the decision, dec-NNNNNN (checked against decisions.jsonl)",
    "result": "a result file (repeatable; copied into orders.results_dir/<id>/)",
    "only": "one character only",
    "spec": "its spec too",
    "dry_run": "say what it would do, write nothing",
    "ballot_dir": "where the ballot files are",
    "review_dir": "where the review files are",
    "to": "tr-NNNN (a new car), tr-NNNN:N (a car that is), st-/tm-NNNN (an object, linked only)",
}


def _fill_help(ap, cmd=None):
    """Every argument of every command and subcommand gets a help (ARG_HELP), every subcommand its line; the test
    `tests/test_cli_docs.py` holds both.
    """
    for a in ap._actions:
        if isinstance(a, argparse._SubParsersAction):
            for name, sp in a.choices.items():
                _fill_help(sp, cmd or name)
        elif not isinstance(a, argparse._HelpAction) and not a.help and a.dest in ARG_HELP:
            a.help = ARG_HELP[a.dest].replace("{id}", ID_OF.get(cmd, "its id"))


def build_parser():
    ap = argparse.ArgumentParser(prog="depot2", description=__doc__, epilog=t("about.note"))
    ap.add_argument(
        "--state",
        default=os.environ.get("DEPOT2_STATE", config.DEFAULT_STATE),
        help="the state dir (default %(default)s)",
    )
    ap.add_argument(
        "--set",
        action="append",
        default=[],
        metavar="KEY=VALUE",
        help="override a config value for this run, e.g. stack.slots=6",
    )
    sub = ap.add_subparsers(dest="cmd")
    sub.required = True

    p = sub.add_parser("round", help="run a team round")
    p.add_argument("--kind", choices=("ballot", "review", "discuss", "brainstorm"), help="required, but with --resume")
    p.add_argument("--brainstorm", metavar="BS_ID", help="brainstorm: the approved entry to run (bs-NNNN)")
    p.add_argument("--input", help="questions file (ballot), topic file (discuss), notes (review)")
    p.add_argument("--context", action="append", help="another file for the bundle (repeatable)")
    p.add_argument("--since", help="review: PRs merged after this ISO time (default: the last review)")
    p.add_argument("--pr", type=int, help="review: ONE PR (gh pr view: body and files) instead of the merges")
    p.add_argument(
        "--only",
        metavar="CHARACTER",
        help="a solo round: ONE character (id or name); round 1 and the lock pass, no debate",
    )
    p.add_argument(
        "--item", metavar="TM_ID", help="the team stack item this round runs: it records the round and its cost"
    )
    p.add_argument(
        "--resume",
        metavar="ROUND_ID",
        help="go on with a round a fatal error aborted: its answers saved stay, only the missing are called",
    )
    p.add_argument("--dry-run", action="store_true", help="no model calls: prompt sizes and the plan")
    p.set_defaults(fn=cmd_round, mutates=True)

    p = sub.add_parser("report", help="rebuild a round's report.md from its saved data (no model call)")
    p.add_argument("--round", required=True, metavar="ROUND_ID", help="e.g. 2031-04-02-discuss-2")
    p.set_defaults(fn=cmd_report, mutates=True)

    p = sub.add_parser("payout", help="record the owner's payout")
    p.add_argument("character")
    p.add_argument("n", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--round")
    p.add_argument("--point")
    p.add_argument("--settles", help="close a pending item (pend-NNNN) with this payout")
    p.set_defaults(fn=cmd_payout, mutates=True)

    p = sub.add_parser("fine", help="the owner's fine: take n tokens (never below zero)")
    p.add_argument("character", help="a character or 'all'")
    p.add_argument("n", type=int)
    p.add_argument("--reason", required=True)
    p.add_argument("--round")
    p.add_argument("--point")
    p.set_defaults(fn=cmd_fine, mutates=True)

    p = sub.add_parser(
        "refund",
        help="refund open stakes of an older round (the owner's call); "
        "--auto: return them as a round does when it ends",
    )
    p.add_argument("character", help="a character or 'all'")
    p.add_argument("--round", required=True)
    p.add_argument("--point")
    p.add_argument("--reason", help="required, except with --auto")
    p.add_argument(
        "--auto",
        action="store_true",
        help="all only: auto_refund every open stake of the round (not an owner action, no reflection)",
    )
    p.set_defaults(fn=cmd_refund, mutates=True)

    p = sub.add_parser("forfeit", help="close open stakes of an older round the owner does NOT return")
    p.add_argument("character", help="a character or 'all'")
    p.add_argument("--round", required=True)
    p.add_argument("--point")
    p.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_forfeit, mutates=True)

    p = sub.add_parser("brainstorm", help="the team's own brainstorms: list, or cancel one that never ran")
    bsub = p.add_subparsers(dest="action")
    bsub.required = True
    q = bsub.add_parser("list", help="the approved ones (--all: done, failed, cancelled too)")
    q.add_argument("--all", action="store_true")
    q.add_argument("--json", action="store_true", help=t("cli.help.queues_json"))
    q.set_defaults(fn=cmd_brainstorm, mutates=False)
    q = bsub.add_parser("cancel", help="it never ran: every fee comes back (brainstorm_refund)")
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q.set_defaults(fn=cmd_brainstorm, mutates=True)

    p = sub.add_parser("orders", help="the queue of measurement and test-run orders")
    p.add_argument("--all", action="store_true", help="settled ones too")
    p.add_argument("--json", action="store_true", help=t("cli.help.queues_json"))
    p.set_defaults(fn=cmd_orders, mutates=False)

    p = sub.add_parser("order", help="an order: done (the orchestrator ran it) or settle (the owner decides)")
    osub = p.add_subparsers(dest="action")
    osub.required = True
    q = osub.add_parser("done", help="record the results (copied into orders.results_dir/<id>/)")
    q.add_argument("id")
    q.add_argument("--result", nargs="+", required=True, metavar="FILE")
    q.add_argument("--note")
    q.set_defaults(fn=cmd_order, mutates=True)
    q = osub.add_parser("settle", help="moved = the price back + 1; burn = nothing; refund = the price back")
    q.add_argument("id")
    q.add_argument("outcome", choices=("moved", "burn", "refund"))
    q.add_argument("--reason", required=True)
    q.set_defaults(fn=cmd_order, mutates=True)

    p = sub.add_parser(
        "stack", help="the agents' slots, the post-stack and the queue (stack.json); no action: print it"
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    ssub = p.add_subparsers(dest="action")
    q = ssub.add_parser("add", help="into the queue")
    q.add_argument("--title", required=True)
    q.add_argument("--brief")
    q.add_argument("--branch")
    q.add_argument("--pr", type=int)
    q.add_argument("--status")
    q.add_argument("--depends", action="append", metavar="ST_ID")
    q.add_argument("--at", type=int, help="the queue position, from 1")
    q.add_argument(
        "--autostart",
        metavar="yes|no",
        help="a freed slot takes it by itself (trains next); default no: the owner's word, stack start",
    )
    q.add_argument(
        "--stage", help="its first stage: bogie, sketch or project (default: sketch with --brief, else bogie)"
    )
    _effort_args(q)
    pri.add_arg(q)
    q = ssub.add_parser(
        "start",
        help="queue → slot (whatever its autostart says; a parked object comes back); "
        "post-stack → a slot, or the head of the queue (back for work); refused for "
        "the object of a merged or dropped car",
    )
    q.add_argument("id")
    q.add_argument("--branch")
    q.add_argument("--reason", help="why it comes back (from the post-stack: the owner's word)")
    _force_args(q, "the gate into the build: the DoR, the slots")
    q = ssub.add_parser(
        "post",
        help="slot → the post-stack: its PR is ready (CI green) and waits only for the "
        "owner's merge; the slot is free (stack.post_max, 4); refused for the object "
        "of a merged or dropped car",
    )
    q.add_argument("id")
    q.add_argument("--reason")
    q.add_argument("--ci", help="the PR's CI as last seen (default: the one recorded, else green)")
    _dod_args(q)
    _force_args(q, "the gate into the acceptance: the DoD, the post-stack's places")
    q = ssub.add_parser("set", help="change an object's fields")
    q.add_argument("id")
    for f in ("--status", "--branch", "--title", "--brief"):
        q.add_argument(f)
    q.add_argument("--pr", type=int)
    q.add_argument("--ci", help="the PR's CI as last seen: green, red, pending or words (stamped now)")
    _dod_args(q)
    q.add_argument("--autostart", metavar="yes|no")
    q.add_argument("--note", help="append a dated line to the object's notes (the web shows them)")
    _effort_args(q, spent=True)  # --est-pct none / --est-mtok none clears the estimate
    pri.add_arg(q)  # a car's object: its car takes it too (one car)
    q = ssub.add_parser("move", help="reorder the queue")
    q.add_argument("id")
    q.add_argument("--at", type=int, required=True)
    q = ssub.add_parser(
        "take",
        help="an object that is no car becomes a new car of the track, and stays in its place as "
        "the car's object (its stage, brief, PR, size; from: st-NNNN)",
    )
    _take_args(q)
    q.add_argument("--to", required=True, metavar="TRACK", help="tr-NNNN")
    q = ssub.add_parser(
        "done", help="merged, closed or dropped: it leaves the stack (a track's car's object: --car is required)"
    )
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q.add_argument(
        "--car",
        choices=("merged", "dropped", "back"),
        help="what it becomes; required for a track's car's object (never guessed): merged, dropped, or "
        "back (a car with no PR returns to its track as it was, its stack link cleared)",
    )
    q = ssub.add_parser(
        "park",
        help="a PR that waits frees its slot: a car's object goes back to its track "
        "(status pr, PR kept); any other object → the head of the queue",
    )
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q = ssub.add_parser("unlock", help="lift a lock: --met (the condition landed) or --owner")
    q.add_argument("id")
    g = q.add_mutually_exclusive_group(required=True)
    g.add_argument("--met", action="store_true")
    g.add_argument("--owner", action="store_true")
    q.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_stack, mutates=False)

    p = sub.add_parser("ballots", help="the owner's ballot queue (ballots.json); no action: print it")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="answered and dropped too")
    bsub = p.add_subparsers(dest="action")
    q = bsub.add_parser("add", help="a round that already exists")
    q.add_argument("--round", required=True)
    for name, hlp in (("point", "mark a point answered"), ("at", "where the presentation stopped")):
        q = bsub.add_parser(name, help=hlp)
        q.add_argument("round")
        q.add_argument("point")
    q = bsub.add_parser("move", help="reorder the queue")
    q.add_argument("round")
    q.add_argument("--at", type=int, required=True)
    q = bsub.add_parser("done", help="the owner answered it")
    q.add_argument("round")
    q.add_argument("--reason")
    q = bsub.add_parser("drop", help="dropped without an answer")
    q.add_argument("round")
    q.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_ballots, mutates=False)

    p = sub.add_parser(
        "tstack", help="the team stack (team-stack.json): every interaction with the team; no action: print it"
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    tsub = p.add_subparsers(dest="action")
    from .tstack import KINDS as DEPOT2_KINDS

    q = tsub.add_parser("add", help="into the team queue")
    q.add_argument("--kind", required=True, choices=DEPOT2_KINDS)
    q.add_argument("--title")
    q.add_argument("--input", help="the topic, questions or notes file")
    q.add_argument("--who", default="all", help="all (default) or one character: a solo round")
    q.add_argument("--depends", action="append", metavar="TM_OR_ST_ID")
    q.add_argument("--at", type=int, help="the queue position, from 1")
    q.add_argument("--status")
    _effort_args(q)
    pri.add_arg(q)
    q = tsub.add_parser("start", help="queue → team slot (prints the command that runs it)")
    q.add_argument("id")
    q = tsub.add_parser("set", help="change an item's fields")
    q.add_argument("id")
    for f in ("--status", "--title", "--input", "--who", "--round"):
        q.add_argument(f)
    q.add_argument("--kind", choices=DEPOT2_KINDS)
    q.add_argument("--cost", type=float, help="deprecated: the round's ≈$, read as --spent-mtok at usage.usd_per_mtok")
    _effort_args(q, spent=True)
    pri.add_arg(q)
    q = tsub.add_parser("move", help="reorder the team queue")
    q.add_argument("id")
    q.add_argument("--at", type=int, required=True)
    q = tsub.add_parser("done", help="it leaves the team stack")
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q = tsub.add_parser(
        "take",
        help="an item of the team queue becomes a new car of the track (a bogie, from: tm-NNNN) "
        "and leaves the team stack; one in a team slot is refused (its round runs)",
    )
    _take_args(q)
    q.add_argument("--to", required=True, metavar="TRACK", help="tr-NNNN")
    p.set_defaults(fn=cmd_tstack, mutates=False)

    p = sub.add_parser("tracks", help="the tracks (tracks.json): the themes and their cars; no action: print them")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="the tracks every car of which is closed too")
    tsub = p.add_subparsers(dest="action")
    q = tsub.add_parser("add", help="a new track (a theme)")
    q.add_argument("--title", required=True)
    q.add_argument("--est-pct", help="the track's own estimate in %%UW (a number or a range like 8-11)")
    q.add_argument("--est-mtok", help="… or in MTok (a number or a range like 100-130)")
    q.add_argument("--at", type=int, help="its place in the list, from 1")
    q.add_argument("--note")
    q = tsub.add_parser("set", help="change a track")
    q.add_argument("id")
    q.add_argument("--title")
    q.add_argument("--est-pct", help="%%UW, a number or a range; 'none' clears it")
    q.add_argument("--est-mtok", help="MTok, a number or a range; 'none' clears it")
    q.add_argument("--at", type=int, help="move it in the list")
    q.add_argument("--note")
    q = tsub.add_parser("import", help="one-off: the tracks from a JSON the orchestrator writes")
    q.add_argument("file")
    q.add_argument("--dry-run", action="store_true", help="print the tables, write nothing")
    q = tsub.add_parser("car", help="a track's cars: add, set, move, stack, check")
    csub = q.add_subparsers(dest="car_action")
    csub.required = True
    c = csub.add_parser("add", help="a new car at the end (or --at N)")
    c.add_argument("track")
    c.add_argument("--title", required=True)
    c.add_argument("--n", help="its name on the track (default: the next number)")
    c.add_argument(
        "--after",
        action="append",
        metavar="REF",
        help="a car of this track (2, B2a), tr-NNNN:car, tr-NNNN, st-/tm-, #PR",
    )
    _effort_args(c)
    c.add_argument("--spent-mtok", type=float, help="its actual, in MTok")
    for f in ("--brief", "--branch"):
        c.add_argument(f)
    c.add_argument("--pr", type=int)
    c.add_argument(
        "--status",
        choices=("waiting", "ready", "running", "pr", "merged", "dropped"),
        help="its first status (default waiting)",
    )
    c.add_argument("--at", type=int)
    c.add_argument("--stage", help="its first stage: bogie, sketch or project (default: by its status and brief)")
    pri.add_arg(c)
    c = csub.add_parser("set", help="change a car (tr-NNNN:N)")
    c.add_argument("ref")
    c.add_argument("--title")
    c.add_argument("--after", action="append", metavar="REF")
    c.add_argument("--no-after", action="store_true")
    _effort_args(c, spent=True)
    for f in ("--brief", "--branch"):
        c.add_argument(f)
    c.add_argument("--note", help="append a dated line to the car's notes")
    c.add_argument("--note-replace", metavar="TEXT", help="replace every note line by this one ('' clears them)")
    c.add_argument("--pr", type=int)
    c.add_argument(
        "--status",
        choices=("waiting", "ready", "running", "pr", "merged", "dropped"),
        help="by hand, for a car with no object in the stack; refused while its object is there (the "
        "stack's commands move both: stack done --car, stack start, stack park)",
    )
    c.add_argument(
        "--st",
        metavar="ST_ID",
        help="link the car to its stack object: only when its own left the stack, never to another car's",
    )
    c.add_argument("--docs", metavar="yes|no", help=t("cli.help.car_docs"))
    pri.add_arg(c)  # its live stack object takes it too (one car)
    c = csub.add_parser(
        "move",
        help="its place on the track (--at K); or to another track (--to tr-NNNN) or name "
        "(--n NEW), with all its history, the old ref finding it",
    )
    c.add_argument("ref")
    c.add_argument("--at", type=int, help="its place in the track's list (with --to: in the new track's)")
    c.add_argument(
        "--to",
        metavar="TRACK",
        help="the track it goes to, tr-NNNN; refused for a car that arrived or "
        "rolls out, or whose PR is in the merge queue",
    )
    c.add_argument("--n", metavar="NEW", help="its new name (default with --to: its name, when free there)")
    c.add_argument("--why", help="the reason, kept on the car, its old place and the history")
    c = csub.add_parser("stack", help=t("cli.help.car_stack"))
    c.add_argument("ref")
    c.add_argument("--start", action="store_true")
    _force_args(c, "the gate into the build (with --start): the DoR, the slots")
    c = csub.add_parser("check", help=t("cli.help.car_check"))
    c.add_argument("ref", help="tr-NNNN:N, or a stack object st-NNNN (its car's, else its own)")
    c.add_argument("rest", nargs="*", metavar="N MARK", help=t("cli.help.check_rest"))
    c.add_argument("--line", help="the mark's words (how it is done; what is broken and what instead)")
    c.add_argument("--from-pr", action="store_true", help="read the PR's section into the marks")
    c.add_argument("--pr", type=int, help="with --from-pr: this PR instead of the car's")
    p.set_defaults(fn=cmd_tracks, mutates=False)

    p = sub.add_parser("track", help="one track: its cars")
    p.add_argument("id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.set_defaults(fn=cmd_track, mutates=False)

    p = sub.add_parser("trains", help=t("cli.help.trains"))
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="the arrived and scrapped ones too")
    tsub = p.add_subparsers(dest="action")
    q = tsub.add_parser("add", help=t("cli.help.trains_add"))
    q.add_argument("--name", required=True, help="its name: a slug, e.g. search-v1")
    q.add_argument("--title", required=True)
    q.add_argument("--car", action="append", metavar="REF", help="a car tr-NNNN:N, in order (repeatable)")
    q.add_argument(
        "--after",
        action="append",
        metavar="REF",
        help="a track tr-NNNN, a car tr-NNNN:N, a train's name, st-/tm-NNNN or #PR (repeatable)",
    )
    q.add_argument("--not-before", metavar="ISO", help="e.g. 2031-04-08T15:00 (local time)")
    q.add_argument("--owner-word", action="store_true", help=t("cli.help.owner_word"))
    q.add_argument("--when", metavar="TEXT", help="the start condition in words (shown, not checked)")
    q.add_argument("--slots", help="max N (default trains.default_slots) or all-free")
    q.add_argument("--est-pct", help="the train's own estimate in %%UW (a number or a range like 8-11)")
    q.add_argument("--est-mtok", help="… or in MTok (a number or a range like 100-130)")
    q.add_argument("--at", type=int, help="the queue position, from 1")
    pri.add_arg(q)
    q = tsub.add_parser("set", help="change a train")
    q.add_argument("name")
    q.add_argument("--name", dest="new_name", metavar="NEW_NAME", help="rename it (the start rules naming it follow)")
    q.add_argument("--title")
    q.add_argument(
        "--after",
        action="append",
        metavar="REF",
        help="a track tr-NNNN, a car tr-NNNN:N, a train's name, st-/tm-NNNN or #PR (repeatable)",
    )
    q.add_argument("--no-after", action="store_true", help="clear the after list (before any --after)")
    q.add_argument("--not-before", metavar="ISO", help="'none' clears it")
    q.add_argument("--owner-word", nargs="?", const="yes", choices=("yes", "no"), help=t("cli.help.owner_word_set"))
    q.add_argument("--when", metavar="TEXT")
    q.add_argument("--slots")
    q.add_argument("--est-pct", help="%%UW, a number or a range; 'none' clears it")
    q.add_argument("--est-mtok", help="MTok, a number or a range; 'none' clears it")
    q.add_argument("--at", type=int, help="move it in the queue")
    q.add_argument("--note")
    pri.add_arg(q)
    q = tsub.add_parser(
        "state",
        help="move it: design|impl|merge — forward through its gate, back (merge → impl → "
        "design) with no gate; it arrives by `trains done`, is scrapped by `trains drop`",
    )
    q.add_argument("name")
    q.add_argument("to", metavar="STATE", type=_train_state, help="design, impl or merge")
    q.add_argument(
        "--word",
        help="merge: the owner's words (verbatim): every open car's PR goes into the merge queue, "
        "all or nothing (every car checked as `merge add` checks it first)",
    )
    _force_args(q, "the gate of the way forward: impl — the start rules and every car's DoR; merge — every PR green")
    q = tsub.add_parser("done", help="every car is merged or dropped: it arrived")
    q.add_argument("name")
    _force_args(q, "the gate: every car closed")
    q = tsub.add_parser("drop", help="scrap it: its open cars stay on their tracks")
    q.add_argument("name")
    q.add_argument("--why", required=True)
    q = tsub.add_parser("next", help=t("cli.help.trains_next"))
    q.add_argument(
        "--json",
        dest="next_json",
        action="store_true",
        help="the data as JSON (`trains --json next` too; README «Tracks and trains»)",
    )
    q = tsub.add_parser("car", help="a train's cars: add, move, drop (the car stays on its track)")
    csub = q.add_subparsers(dest="car_action")
    csub.required = True
    c = csub.add_parser("add", help="a car tr-NNNN:N at the end (or --at N)")
    c.add_argument("name")
    c.add_argument("ref")
    c.add_argument("--at", type=int)
    c = csub.add_parser("move", help="its place in the train")
    c.add_argument("name")
    c.add_argument("ref")
    c.add_argument("--at", type=int, required=True)
    c = csub.add_parser("drop", help="out of the train")
    c.add_argument("name")
    c.add_argument("ref")
    c.add_argument("--why")
    p.set_defaults(fn=cmd_trains, mutates=False)

    p = sub.add_parser("train", help="one train: its cars in order")
    p.add_argument("name")
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.set_defaults(fn=cmd_train, mutates=False)

    p = sub.add_parser(
        "migrate",
        help="bring the depot's data to today's form, once (schema 1 → 5: tracks and "
        "trains, the marks by the principles, the priorities, the cars merged by hand "
        "before the merge queue's last ride → arrived); any command runs it first",
    )
    p.add_argument("--dry-run", action="store_true", help="say what it would do, write nothing")
    p.set_defaults(fn=cmd_migrate, mutates=True)

    p = sub.add_parser(
        "priority",
        help="the priority 1-10 (10 — the most important, default 5) of a train or a car in "
        "any form: print it, or set it (the same as --priority N on its "
        "set). A free slot takes the trains by it, then the cars (trains next)",
    )
    p.add_argument(
        "id",
        help="a train's name, tr-NNNN:N, st-/tm-NNNN, wl-/th-/td-NNNN (a car and its live stack "
        "object are one car: both get it)",
    )
    p.add_argument("n", nargs="?", help="1-10 (P7 too; high|normal|later = 8 / 5 / 2); left out: print it")
    p.set_defaults(fn=cmd_priority, mutates=True)

    p = sub.add_parser("car", help=t("cli.help.car"))
    csub = p.add_subparsers(dest="action")
    csub.required = True
    c = csub.add_parser(
        "stage", help="move its stage, on the route; the moves a command makes are refused, each with its command"
    )
    c.add_argument("id", help="st-NNNN, tr-NNNN:N, wl-NNNN or th-NNNN")
    c.add_argument(
        "stage",
        help="bogie sketch project build docs acceptance queued rolling arrived scrapped (or the "
        "Russian name); build ⇄ docs is car stage's own, in a slot",
    )
    c.add_argument("--why", help="the reason; required for a step back and for scrapped")
    c.add_argument("--by", help=t("cli.help.stage_by"))
    c.add_argument(
        "--force",
        action="store_true",
        help="a correction past the route and the commands' moves (with --why; recorded as forced)",
    )
    c.set_defaults(fn=cmd_car, mutates=True)
    c = csub.add_parser("show", help="its stage, where it may go, the next gate, its history")
    c.add_argument("id")
    c.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    c.set_defaults(fn=cmd_car, mutates=False)
    c = csub.add_parser("ask", help="an open question: the DoR waits for its answer")
    c.add_argument("id")
    c.add_argument("text", help="the question, in words")
    c.add_argument("--by", help="who asks (default: car ask)")
    c.set_defaults(fn=cmd_car, mutates=True)
    c = csub.add_parser("answer", help="close an open question with its answer")
    c.add_argument("id")
    c.add_argument("n", type=int, help="the question's number")
    c.add_argument("--answer", required=True)
    c.add_argument("--by", help=t("cli.help.answer_by"))
    c.set_defaults(fn=cmd_car, mutates=True)
    c = csub.add_parser("needs-project", help=t("cli.help.needs_project"))
    c.add_argument("id")
    g = c.add_mutually_exclusive_group(required=True)
    g.add_argument("--why", help=t("cli.help.needs_why"))
    g.add_argument("--off", action="store_true", help="not required any more")
    c.add_argument("--by")
    c.set_defaults(fn=cmd_car, mutates=True)

    p = sub.add_parser(
        "board",
        help="the board: every live car in the column of its stage (Kanban), the columns' "
        "limits and the next gates; --view stack: the stack's table",
    )
    p.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    p.add_argument("--md", action="store_true", help="the markdown (the default)")
    p.add_argument("--all", action="store_true", help="the arrived and scrapped cars too")
    p.add_argument("--view", choices=("all", "stack"), default="all", help=t("cli.help.board_view"))
    p.add_argument("--limit", type=int, default=15, help="the cars shown per column in --md (0: all)")
    p.set_defaults(fn=cmd_board, mutates=False)

    p = sub.add_parser("repair", help=t("cli.help.repair"))
    rsub = p.add_subparsers(dest="action")
    rsub.required = True
    c = rsub.add_parser(
        "add",
        help="a claim: a new repair car (kind repair) at the head of the stack's queue, autostart, "
        "out of turn; the culprit's claims += 1",
    )
    c.add_argument(
        "--of",
        required=True,
        help="the culprit: st-NNNN, tr-NNNN:N or #PR; or the bug's id in BUGS.md when the culprit is not known yet",
    )
    c.add_argument("--bug", help="the bug's id in BUGS.md, with a culprit in --of")
    c.add_argument("--title", help=t("cli.help.repair_title"))
    c.add_argument("--brief", help="the repair's brief (without one the DoR waits for it)")
    c.add_argument("--size", help="S, M or L")
    c.add_argument("--why", help="what the defect is (the claim's reason); with --force, why past the checks")
    c.add_argument("--by", help=t("cli.help.repair_by"))
    c.add_argument(
        "--force",
        action="store_true",
        help="past the checks (the culprit not on production, a bug not "
        "in BUGS.md): only with --why, recorded as forced",
    )
    c.set_defaults(fn=cmd_repair, mutates=True)
    c = rsub.add_parser(
        "link",
        help="the culprit of a repair, found later; or a defect repaired inside another car "
        "(--why: what it was): the culprit's claims += 1",
    )
    c.add_argument("repair", help="the repair: st-NNNN, tr-NNNN:N or #PR")
    c.add_argument("--of", required=True, help="the culprit: st-NNNN, tr-NNNN:N or #PR")
    c.add_argument("--why", help="what the defect was (required when the repair is no repair car)")
    c.add_argument("--by", help="who (default: repair link)")
    c.add_argument(
        "--force",
        action="store_true",
        help="past the checks (the culprit not on production, a repair "
        "moved to another culprit): only with --why, recorded",
    )
    c.set_defaults(fn=cmd_repair, mutates=True)

    p = sub.add_parser("claims", help=t("cli.help.claims"))
    p.add_argument("--since", help="from this day, YYYY-MM-DD (included)")
    p.add_argument("--until", help="to this day, YYYY-MM-DD (included)")
    p.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    p.add_argument("--md", action="store_true", help="the markdown (the default)")
    p.set_defaults(fn=cmd_claims, mutates=False)

    from .agents import ROLES as AGENT_ROLES

    p = sub.add_parser(
        "agents",
        aliases=["agent"],
        help="the agents' runs (agents.json): the gates before the orchestrator starts one, its "
        "transcript, what it does now; no action: the live ones",
    )
    p.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="the finished and stopped ones too")
    asub = p.add_subparsers(dest="action")
    q = asub.add_parser("start", help=t("cli.help.agents_start"))
    q.add_argument("ref", help="the car, tr-NNNN:N (or its stack object st-NNNN)")
    q.add_argument("--role", required=True, choices=AGENT_ROLES, help=t("cli.help.agents_role"))
    q.add_argument(
        "--usage",
        metavar="SHARE",
        help="the usage window now, as the orchestrator read it: 0.62, 62 or 62%% (stop at usage.stop_at, 0.99)",
    )
    q.add_argument("--by", help=t("cli.help.agents_by"))
    _force_args(q, "the gates (the role, the window, a second agent, the DoR and the slots)")
    q = asub.add_parser(
        "attach",
        help="link it to its transcript: found by the run id under agents.projects "
        "(the executor's log dir, agents.projects)",
    )
    q.add_argument("id", metavar="AG")
    q.add_argument("agent_id", metavar="AGENT_ID", help="the executor's id of the run")
    q.add_argument("--transcript", metavar="PATH", help="the .jsonl itself, when the search cannot find it")
    q = asub.add_parser("show", help="one agent: its fields and its last actions")
    q.add_argument("id", metavar="AG")
    q.add_argument("--json", action="store_true")
    q = asub.add_parser(
        "stop", help="record its stop (the orchestrator stops the process): its spend so far on the car"
    )
    q.add_argument("id", metavar="AG")
    q.add_argument("--why", required=True)
    q.add_argument("--by", help=t("cli.help.agents_stop_by"))
    p.set_defaults(fn=cmd_agents, mutates=True)  # reading books a finished agent's spend on its car

    p = sub.add_parser(
        "web",
        help="depot2's web view, seven tabs: tracks, trains, board, merge queue, agents, "
        "decisions and principles, queues; search, copy icons, a car's panel and brief; "
        "127.0.0.1 only, no authorization, read only (Ctrl+C stops it)",
    )
    p.add_argument("--port", type=int, default=8765, help="the port (default 8765; 0: any free one)")
    p.add_argument("--verbose", action="store_true", help="log every request to stderr")
    p.set_defaults(fn=cmd_web, mutates=False)

    from .queues import AREAS
    from .queues import KINDS as TODO_KINDS

    for name, aliases, hlp in (
        ("wishes", ["wish"], "the owner's wishes not scheduled yet (wishes.json)"),
        ("think", [], "open questions for a brainstorm, a round or the owner (think.json)"),
        ("todo", [], "leftovers and reminders (todo.json): date, version, place"),
    ):
        p = sub.add_parser(name, aliases=aliases, help=hlp + "; no action: print it")
        p.add_argument("--json", action="store_true")
        p.add_argument("--md", action="store_true", help="the markdown table (the default)")
        p.add_argument("--all", action="store_true", help="closed ones too")
        p.add_argument("--limit", type=int, help="the first N rows")
        qsub = p.add_subparsers(dest="action")
        q = qsub.add_parser("add", help="a new open entry (its id never reused)")
        q.add_argument("--date", help="YYYY-MM-DD (default today)")
        q.add_argument("--link", action="append")
        if name == "wishes":
            q.add_argument("--title", required=True)
            q.add_argument("--words", help="the owner's words, verbatim")
            q.add_argument("--area", choices=AREAS, default="product")
            pri.add_arg(q, synonym=True)
        elif name == "think":
            q.add_argument("--topic", required=True)
            q.add_argument("--question", action="append")
            q.add_argument("--by", help="who raised it")
            q.add_argument("--related", action="append")
            pri.add_arg(q, synonym=True)
        else:
            pri.add_arg(q, synonym=True)
            q.add_argument("--text", required=True, help="what it is, in words")
            q.add_argument("--title")
            q.add_argument("--where", action="append", metavar="FILE:LINE")
            q.add_argument("--version", help="default: package.json + origin/main of the main clone")
            q.add_argument("--source", help="PR / agent / tester / owner")
            q.add_argument("--kind", choices=TODO_KINDS, default="bug")
            q.add_argument("--trigger", help="a reminder's trigger, e.g. «after the v2 train is deployed»")
        q = qsub.add_parser("set", help="change an open entry's fields (--note, --link, --question, --where append)")
        q.add_argument("id")
        q.add_argument("--date")
        q.add_argument("--link", action="append")
        q.add_argument("--note", action="append")
        if name == "wishes":
            for f in ("--title", "--words"):
                q.add_argument(f)
            q.add_argument("--area", choices=AREAS)
            pri.add_arg(q, synonym=True)
        elif name == "think":
            q.add_argument("--topic")
            q.add_argument("--by")
            q.add_argument("--question", action="append")
            q.add_argument("--related", action="append")
            pri.add_arg(q, synonym=True)
        else:
            for f in ("--text", "--title", "--version", "--source", "--trigger"):
                q.add_argument(f)
            pri.add_arg(q, synonym=True)
            q.add_argument("--where", action="append")
            q.add_argument("--kind", choices=TODO_KINDS)
        q = qsub.add_parser("show", help="every field of one entry")
        q.add_argument("id")
        q = qsub.add_parser(
            "take",
            help="it becomes a track's car and closes «→ tr-NNNN:N»: --to tr-NNNN "
            "a new bogie (its title, words and date in a note, from: the entry), tr-NNNN:N "
            "a car that is (the note and from too), st-/tm-NNNN a stack object (linked only)",
        )
        _take_args(q)
        q.add_argument("--to", required=True, metavar="TRACK_CAR_OR_ST")
        if name == "wishes":
            q = qsub.add_parser("drop", help="declined")
            q.add_argument("ids", nargs="+")
            q.add_argument("--reason", required=True)
            q = qsub.add_parser("done", help=t("cli.help.queue_done"))
            q.add_argument("ids", nargs="+")
            q.add_argument("--reason", required=True)
        elif name == "think":
            q = qsub.add_parser("round", help="link it to a team round (a round id or a tm-NNNN)")
            q.add_argument("id")
            q.add_argument("--round", required=True, help="a round's id, or the team stack item tm-NNNN")
            q = qsub.add_parser("decide", help="decided: --dec dec-NNNNNN and/or --reason")
            q.add_argument("id")
            q.add_argument("--dec")
            q.add_argument("--reason")
            q = qsub.add_parser("drop", help="no longer a question")
            q.add_argument("ids", nargs="+")
            q.add_argument("--reason", required=True)
        else:
            q = qsub.add_parser("pr", help="a PR takes it")
            q.add_argument("id")
            q.add_argument("--pr", type=int, required=True)
            q = qsub.add_parser("done", help="done (several ids at once)")
            q.add_argument("ids", nargs="+")
            q.add_argument("--reason")
            q = qsub.add_parser("wont", help="we won't (several ids at once)")
            q.add_argument("ids", nargs="+")
            q.add_argument("--reason", required=True)
        p.set_defaults(fn=cmd_queue, mutates=False)

    from .principles import SECTIONS as PC_SECTIONS

    p = sub.add_parser(
        "principles",
        aliases=["principle"],
        help="the design principles (principles.json): PRINCIPLES.md is rendered from them, the three "
        "checks; no action: print them",
    )
    p.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="the replaced and dropped ones too")
    p.add_argument("--limit", type=int, help="the first N rows")
    psub = p.add_subparsers(dest="action")
    sections = "one of {}".format(", ".join(k for k, _ in PC_SECTIONS))
    for act in ("add", "set"):
        q = psub.add_parser(
            act,
            help="a new principle: the next number (never reused)"
            if act == "add"
            else "change one: fields replaced (the history keeps them), --words and --sub appended",
        )
        if act == "set":
            q.add_argument("id")
        q.add_argument("--section", required=act == "add", help=sections + " (or its heading)")
        q.add_argument("--title", help="the bold part")
        q.add_argument("--text", help="the body, one line, verbatim")
        q.add_argument(
            "--words",
            action="append",
            help='the owner\'s words, verbatim; "YYYY-MM-DD: …" for a date (default --date, else today); repeatable',
        )
        q.add_argument("--sub", action="append", help="a sub at the end (leading spaces nest it); repeatable")
        q.add_argument("--date", help="YYYY-MM-DD (default today)")
        q.add_argument("--order", type=float, help="its place in the section (default: the end)")
        q.add_argument("--check", choices=("yes", "no"), help="part of the third check (`principles checks`)")
        q.add_argument("--link", action="append", help="dec-…, wl-…, tr-…:…")
        if act == "set":
            q.add_argument("--sub-set", nargs=2, metavar=("N", "TEXT"), help="sub N (from 1) becomes TEXT")
            q.add_argument("--sub-drop", type=int, metavar="N", help="sub N (from 1) goes")
            q.add_argument("--gap", type=int, help="blank lines after it in the file")
            q.add_argument("--note", action="append")
        q.add_argument("--overwrite", action="store_true", help="the file was edited by hand: render over it")
    q = psub.add_parser("show", help="every field of one principle, and its lines in the file")
    q.add_argument("id")
    q = psub.add_parser("replace", help="replaced by another one (out of the file, in --all)")
    q.add_argument("id")
    q.add_argument("--by", required=True, metavar="PC_ID", help="the principle that replaces it")
    q.add_argument("--reason")
    q.add_argument("--overwrite", action="store_true", help="the file was edited by hand: render over it")
    q = psub.add_parser("drop", help="no longer a principle (out of the file, in --all)")
    q.add_argument("id")
    q.add_argument("--reason", required=True)
    q.add_argument("--overwrite", action="store_true", help="the file was edited by hand: render over it")
    q = psub.add_parser(
        "head",
        help="the file's head (before the first section); --text replaces its text under the "
        "title, the history keeps the old one, the file is rendered again",
    )
    q.add_argument(
        "--text",
        help="the new text, verbatim (several lines allowed; a first line «# …» replaces the "
        "title too); without it: print the head",
    )
    q.add_argument("--overwrite", action="store_true", help="the file was edited by hand: render over it")
    q = psub.add_parser("import", help="one-off: today's PRINCIPLES.md, without loss (then it is rendered, read-only)")
    q.add_argument("file")
    q.add_argument("--dry-run", action="store_true", help="check the round trip and print what it would make")
    q = psub.add_parser("render", help="write PRINCIPLES.md from the depot (read-only between renders)")
    q.add_argument("--out", metavar="PATH", help="another file (- prints it); default: the imported one")
    q.add_argument("--overwrite", action="store_true", help="the file was edited by hand: render over it")
    q = psub.add_parser("checks", help=t("cli.help.principles_checks"))
    q.add_argument("--json", action="store_true")
    q.add_argument("--md", action="store_true", help="the markdown (the default)")
    p.set_defaults(fn=cmd_principles, mutates=False)

    p = sub.add_parser(
        "merge", help="the merge queue: the owner's word, one PR at a time (merge-queue.json); no action: print it"
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    p.add_argument("--all", action="store_true", help="merged and dropped too")
    msub = p.add_subparsers(dest="action")
    q = msub.add_parser("add", help="a PR into the queue, on the owner's word (verbatim; refused without it)")
    q.add_argument("pr", type=int)
    q.add_argument("--word", help=t("cli.help.merge_word"))
    q.add_argument(
        "--at",
        type=int,
        help="its place among the open entries, from 1 (default: the end); never ahead of one that rides",
    )
    _force_args(q, t("cli.help.merge_force"))
    q = msub.add_parser("move", help="a waiting or stopped entry to another place (a fix rides first)")
    q.add_argument("ref", help="mq-NNNN or a PR")
    q.add_argument("--at", type=int, required=True)
    q = msub.add_parser("handoff", help=t("cli.help.merge_handoff"))
    q.add_argument("ref", help="mq-NNNN or a PR")
    q.add_argument("--reason", required=True)
    q = msub.add_parser(
        "run",
        help="the one runner: the PRs one at a time, in order (exit 0 empty, 1 stopped, 3 left waiting for GitHub)",
    )
    q.add_argument("--dry-run", action="store_true", help="the plan: no push, no merge, nothing written")
    q.add_argument("--once", action="store_true", help="ride one PR, then exit")
    q.add_argument(
        "--ensure",
        action="store_true",
        help="a runner rides (its lock held): say so; the queue holds PRs "
        "and none rides: start one detached (log .merge/run.log)",
    )
    q = msub.add_parser(
        "resume",
        help="the stopped PR waits again (after the fix): from the start, or from the deploy when it was merged",
    )
    q.add_argument("ref", nargs="?", help="mq-NNNN or a PR (default: the stopped one)")
    q.add_argument("--reason")
    q = msub.add_parser("drop", help="out of the queue (a stopped one: the queue goes on)")
    q.add_argument("ref", help="mq-NNNN or a PR")
    q.add_argument("--reason", required=True)
    q = msub.add_parser("rerun", help=t("cli.help.merge_rerun"))
    q.add_argument("ref", help="mq-NNNN or a PR")
    q = msub.add_parser(
        "prune-worktrees",
        help="the worktrees of merged branches under merge.worktrees removed by the "
        "rules of the one after a merge (clean, HEAD in origin/main): what "
        "went, what stayed and why, the space freed",
    )
    q.add_argument("--dry-run", action="store_true", help="say what would go: nothing removed, nothing written")
    q = msub.add_parser(
        "report",
        help="short, for the owner: the last merges, their deploy, smoke, the Actions minutes and storage, the queue",
    )
    q.add_argument("--last", type=int, default=1, help="this many merges (default 1)")
    q.add_argument("--json", action="store_true")
    q = msub.add_parser("resolve", help="prepared conflict resolutions, keyed by the two sides' blobs")
    rsub = q.add_subparsers(dest="res_action")
    r = rsub.add_parser("add", help="the resolved file for one conflict (ours = the branch's blob, theirs = main's)")
    r.add_argument("file", help="the conflicting path in the repo")
    r.add_argument("--ours", required=True, help="git rev-parse :2:<file> (the branch's side)")
    r.add_argument("--theirs", required=True, help="git rev-parse :3:<file> (main's side)")
    r.add_argument("--from", dest="src", required=True, help="the resolved file, worked out and tested beforehand")
    r.add_argument("--note")
    r.add_argument("--replace", action="store_true", help="change one that is there")
    r = rsub.add_parser("list", help="the prepared resolutions, with their keys")
    r = rsub.add_parser("drop", help="take one out (the history keeps it)")
    r.add_argument("key", help="<ours>-<theirs> (or its first 12+ characters)")
    r.add_argument("--reason", required=True)
    p.set_defaults(fn=cmd_merge, mutates=False)

    p = sub.add_parser(
        "queues",
        help="the heads of every queue with counts (stack, merge queue, team stack, trains, "
        "tracks, ballots, wishes, to-think, todo, principles)",
    )
    p.add_argument("--json", action="store_true")
    p.add_argument("--md", action="store_true", help="the markdown table (the default)")
    qsub = p.add_subparsers(dest="action")
    q = qsub.add_parser(
        "migrate",
        help="one shot, idempotent: the stack's autostart = no written explicitly, every "
        "car's stage, the cars' `from` (the priorities: `depot2 migrate`)",
    )
    q.add_argument("--dry-run", action="store_true", help="print what it would move, write nothing")
    p.set_defaults(fn=cmd_queues, mutates=False)

    p = sub.add_parser("gender", help="set a character's grammatical gender (meta.json + spec.md)")
    p.add_argument("character")
    p.add_argument("gender", choices=("f", "m"))
    p.set_defaults(fn=cmd_gender, mutates=True)

    p = sub.add_parser("decide", help="record the owner's decision about a character (or all)")
    p.add_argument("character", help="a character or 'all'")
    p.add_argument("--text", required=True, help="the decision's words (an empty one is refused)")
    p.add_argument("--round")
    p.add_argument("--point")
    p.set_defaults(fn=cmd_decide, mutates=True)

    p = sub.add_parser(
        "decisions", help="the owner's decisions (decisions.jsonl), newest first; only reads (decide writes)"
    )
    p.add_argument("--json", action="store_true", help="a stable format (README «JSON formats»)")
    p.add_argument("--md", action="store_true", help="the list (the default)")
    p.add_argument(
        "--grep",
        metavar="TEXT",
        help="only those with every word of TEXT, any case, in the id, the text, who, the round or the point",
    )
    p.add_argument("--limit", type=int, help="the newest N (after --grep)")
    p.set_defaults(fn=cmd_decisions, mutates=False)

    p = sub.add_parser("reflect", help="rewrite the portraits the owner's actions touched")
    p.add_argument("--only")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_reflect, mutates=True)

    p = sub.add_parser(
        "backup", help="the state's backup: a git push every 10 commits, Drive by its flag (no option: --status)"
    )
    g = p.add_mutually_exclusive_group()
    g.add_argument("--status", action="store_true", help="the push's and Drive's last success and error (no network)")
    g.add_argument(
        "--push", action="store_true", help="the private scans, then git push <remote> <the scanned sha>:<branch>, now"
    )
    g.add_argument("--drive", action="store_true", help="the encrypted Drive backup by hand (alone: --now)")
    d = p.add_mutually_exclusive_group()
    d.add_argument("--now", action="store_true", help="--drive: run it now, in the foreground")
    d.add_argument("--verify", action="store_true", help="--drive: download the newest archive, unpack it, git fsck")
    d.add_argument("--dry-run", action="store_true", help="--drive: read the keys, list the folder; write nothing")
    p.add_argument("--auto", action="store_true", help=argparse.SUPPRESS)  # a background run (--push or --drive)
    p.add_argument("--archive", help="--drive --verify: this archive instead of the newest")
    p.set_defaults(fn=cmd_backup, mutates=False)

    p = sub.add_parser("config", help="print the effective config (defaults + config.json + --set)")
    p.set_defaults(fn=cmd_config, mutates=False)

    p = sub.add_parser("ledger", help="balances, open stakes, pending awards")
    p.add_argument("--all", action="store_true", help='every transaction too (with --json: "transactions")')
    p.add_argument("--json", action="store_true")
    p.set_defaults(fn=cmd_ledger, mutates=False)

    p = sub.add_parser("show", help="a character's portrait, thoughts and stakes")
    p.add_argument("character")
    p.add_argument("--spec", action="store_true")
    p.set_defaults(fn=cmd_show, mutates=False)

    p = sub.add_parser(
        "init", help="a new, empty depot at --state: its git, config.json, the team's specs and balances"
    )
    p.add_argument("--balance", type=int, default=5, help="each character's opening tokens (default 5)")
    p.set_defaults(fn=cmd_init, mutates=True)

    p = sub.add_parser("seed", help="one-off: state, specs, ledger, first portraits")
    p.add_argument("--specs", metavar="SPEC_FILE", help="init the state and write the specs")
    p.add_argument("--ledger", metavar="SEED_JSON", help="seed the ledger (once)")
    p.add_argument("--portraits", action="store_true", help="write the first portraits (model calls)")
    p.add_argument("--only")
    p.add_argument("--ballot-dir", help="the past ballots' dir the first portraits read (none: no past positions)")
    p.add_argument("--review-dir", help="the past reviews' dir the first portraits read (none: no past positions)")
    p.add_argument("--dry-run", action="store_true")
    p.set_defaults(fn=cmd_seed, mutates=True)

    _fill_help(ap)
    return ap


# The commands of the team (characters.py): a depot whose config has no `characters` answers them «no team configured».
TEAM_COMMANDS = (
    "round",
    "report",
    "payout",
    "fine",
    "refund",
    "forfeit",
    "brainstorm",
    "orders",
    "order",
    "tstack",
    "gender",
    "reflect",
    "ledger",
    "show",
    "seed",
)


def main(argv=None):
    ap = build_parser()
    argv = sys.argv[1:] if argv is None else list(argv)
    a, extra = ap.parse_known_args(argv)
    if extra:
        ap.error(_unrecognized(argv, extra))
    if a.cmd in (
        "stack",
        "ballots",
        "tstack",
        "tracks",
        "trains",
        "wishes",
        "wish",
        "think",
        "todo",
        "queues",
        "principles",
        "principle",
    ) and getattr(a, "action", None) not in (None, "next", "show", "checks"):
        a.mutates = True  # an action changes the file; printing it does not
    if a.cmd == "merge" and (
        a.action in ("add", "move", "handoff", "resume", "drop", "rerun")
        or (a.action == "resolve" and getattr(a, "res_action", None) in ("add", "drop"))
    ):
        a.mutates = True  # `merge run` takes the state's lock for each write itself: it runs for hours
    state = State(a.state)
    if a.cmd not in ("seed", "init") and not os.path.isdir(state.root):
        print(f"no state at {state.root} (run: depot2 init)", file=sys.stderr)
        return 1
    try:
        cfg = config.load(state.root, a.set) if os.path.isdir(state.root) else config.load("/nonexistent", a.set)
        if a.cmd in TEAM_COMMANDS:
            characters.require()
        _migrate_first(state, cfg, a)
        if a.mutates and not getattr(a, "dry_run", False):
            with state.lock():
                return a.fn(state, cfg, a)
        return a.fn(state, cfg, a)
    except (LedgerError, ValueError, LockBusy) as e:  # StackError, TrainError and QueueError are ValueErrors
        print(f"error: {e}", file=sys.stderr)
        return 1
    except FatalError as e:
        print(f"FATAL: {e}", file=sys.stderr)
        return 2
    finally:  # the state's backup: a local count or a stamp check, a detached run when due, one ⚠ line
        from .backup import after_command

        after_command(state, a)
