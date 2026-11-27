"""Starting a depot: `depot2 init` (an empty one from the config's team) and `depot2 seed` (from a spec file, a ledger
seed and the first portraits).
"""

import glob
import json
import os
import re

from . import characters, prompts
from .i18n import t
from .ledger import Decisions, Ledger, Pending
from .persona import Persona, genders
from .reflect import changes_since, owner_facts, run_portrait_calls
from .store import read_text, write_json

VOTE_HEAD = re.compile(r"^(B\d+)\.\s+(.*)$")


def init_state(state, brief_path):
    """Create the state repo, an empty config.json (the owner's overrides; `depot2 config`
    prints the effective values) and the specs.
    """
    state.init_git()
    cfg_path = state.path("config.json")
    if not os.path.exists(cfg_path):
        write_json(cfg_path, {})
        state.touch(cfg_path)
    specs = characters.extract_specs(read_text(os.path.expanduser(brief_path)))
    for cid in characters.IDS:
        c = characters.BY_ID[cid]
        text = t("seed.spec", name=c["name"], full=c["full"], brief=os.path.basename(brief_path), spec=specs[cid])
        p = Persona(state, cid)
        p.write_spec(text)
        p.set_gender(p.gender())  # the seeding default, unless the state already has one
    return specs


def init_empty(state, balance=5):
    """A new depot: its git, an empty config.json, a spec per character of the config's team (from its names and
    role) and their opening balances. Refused when the depot has a ledger already.
    """
    led = Ledger(state)
    if os.path.isdir(state.root) and led.entries():
        raise ValueError(f"{state.root} is a depot already (its ledger has entries)")
    state.init_git()
    cfg_path = state.path("config.json")
    if not os.path.exists(cfg_path):
        write_json(cfg_path, {})
        state.touch(cfg_path)
    for cid in characters.IDS:
        c = characters.BY_ID[cid]
        p = Persona(state, cid)
        p.write_spec("# {} («{}»)\n\n{}.\n".format(c["name"], c["full"], characters.role(cid)))
        p.set_gender(p.gender())
    for cid in characters.IDS:
        led.seed(cid, int(balance), "init")
    return list(characters.IDS)


def seed_ledger(state, seed):
    """Opening balances, then each imported round's stakes as open stakes; verify the result."""
    led = Ledger(state)
    if led.entries():
        raise ValueError("the ledger is not empty; seeding runs once")
    for cid in characters.IDS:
        led.seed(cid, int(seed["opening"][cid]), seed["opening_reason"])
    for r in seed.get("rounds", []):
        for cid in characters.IDS:
            for point, amount in sorted((r.get("stakes") or {}).get(cid, {}).items()):
                led.stake(cid, int(amount), r["id"], point, reason=r["points"][point])
        rdir = os.path.join(state.rounds_dir(), r["id"])
        os.makedirs(rdir, exist_ok=True)
        rj = os.path.join(rdir, "round.json")
        write_json(
            rj,
            {
                "id": r["id"],
                "kind": r["kind"],
                "created": r["created"],
                "status": "imported",
                "report": r.get("report"),
                "points": [{"id": k, "title": v} for k, v in sorted(r["points"].items())],
                "note": r.get("note"),
            },
        )
        state.touch(rj)
    got = led.balances()
    want = {cid: int(seed["expect"][cid]) for cid in characters.IDS}
    if got != want:
        raise ValueError(f"seeded balances {got} != expected {want}")
    pend = Pending(state)
    for p in seed.get("pending", []):
        pend.add(p["character"], p["condition"], amount=p.get("amount"), ref=p.get("ref"))
    # The seed is not an owner action: it must not trigger a reflection.
    for cid in characters.IDS:
        Persona(state, cid).update_meta(
            reflected_ledger_seq=led.last_seq(), reflected_decision_seq=Decisions(state).last_seq()
        )
    return got


# --- the first portraits ---------------------------------------------------------------------


def names_of(cid):
    c = characters.BY_ID[cid]
    out = [c["name"]]
    if c["full"] != c["name"]:
        out.append(c["full"])
    return out


def vote_lines(text, cid):
    name = characters.BY_ID[cid]["name"]  # old ballots wrote the bare name
    out = []
    for blk in text.split("\n\n"):
        lines = blk.strip().splitlines()
        if not lines:
            continue
        m = VOTE_HEAD.match(lines[0])
        if not m:
            continue
        for l in lines[1:]:  # noqa: E741
            if l.startswith(name + ":"):
                rest = l[len(name) + 1 :].strip()
                if rest and rest not in ("—", "-"):
                    out.append(f"- {m.group(1)} {m.group(2)[:80]}: {rest}")
    return out


def mention_lines(text, cid, line_max=500, section_max=1500):
    """Lines that name the character, plus the body of any heading that names it (a rounds
    draft keeps each character's own view under a heading of its name).
    """
    out = []
    section_left = 0
    for l in text.splitlines():  # noqa: E741
        s = l.strip()
        named = any(n in s for n in names_of(cid))
        if s.startswith("#"):
            section_left = section_max if named else 0
            if named:
                out.append(s)
            continue
        if not s:
            continue
        if section_left > 0:
            clip = s if len(s) <= line_max else s[: line_max - 1] + "…"
            out.append(clip)
            section_left -= len(clip)
            continue
        if len(s) < 12 or not named:
            continue
        out.append(s if len(s) <= line_max else s[: line_max - 1] + "…")
    return out


def material(cid, ballot_dir, review_dir, cap):
    parts = []
    votes = sorted(glob.glob(os.path.join(ballot_dir, "votes-*.md"))) if ballot_dir else []
    for v in votes:
        lines = vote_lines(read_text(v), cid)
        parts.append((t("seed.part_votes", file=os.path.basename(v)), lines, cap * 2 // 5))
    for dpath in sorted(glob.glob(os.path.join(ballot_dir, "decisions-*.md"))) if ballot_dir else []:
        parts.append(
            (t("seed.part_decisions", file=os.path.basename(dpath)), mention_lines(read_text(dpath), cid), cap // 10)
        )
    reviews = sorted(glob.glob(os.path.join(review_dir, "*.md")), reverse=True) if review_dir else []
    rv = []
    for r in reviews:
        lines = mention_lines(read_text(r), cid)
        if lines:
            rv.append(f"### {os.path.basename(r)}")
            rv.extend(lines)
    parts.append((t("seed.part_reviews"), rv, cap // 2))
    out = []
    for title, lines, part_cap in parts:
        body, used, cut = [], 0, 0
        for l in lines:  # noqa: E741
            if used + len(l) + 1 > part_cap:
                cut += 1
                continue
            body.append(l)
            used += len(l) + 1
        out.append(
            "## {}\n{}{}".format(
                title, "\n".join(body) or t("seed.part_none"), "\n" + t("seed.part_cut", n=cut) if cut else ""
            )
        )
    return "\n\n".join(out)


def seed_portraits(state, cfg, ballot_dir, review_dir, only=None, dry_run=False, out=print, runner=None):
    led = Ledger(state)
    balances = led.balances()
    rules = prompts.reflect_rules(cfg, genders(state)) + (
        "\n\nThis is the FIRST portrait: there is no old one. Build it from the character's spec, "
        "its own past positions and the owner's reactions below, so it is not blank: what I stood "
        "for, where I was paid or refunded, my running disputes."
    )
    jobs = []
    for cid in [only] if only else characters.IDS:
        p = Persona(state, cid)
        if p.version():
            out("%s already has a portrait (v%04d); skipped" % (characters.name(cid), p.version()))  # noqa: UP031
            continue
        owner, dec = changes_since(state, cid)
        ident = prompts.identity(p, cid, balances[cid], 0)
        mat = material(cid, ballot_dir, review_dir, cfg["reflect"]["facts_max_chars"] * 2)
        user = "\n\n".join(
            [ident, t("seed.past"), mat, t("seed.owner_after"), owner_facts(owner, dec), t("seed.task_first")]
        )
        jobs.append((cid, rules, user))
    return run_portrait_calls(state, cfg, jobs, "seed", out=out, runner=runner, dry_run=dry_run, source="seed")


def load_seed(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        return json.load(f)
