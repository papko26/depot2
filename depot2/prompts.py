"""The prompts. Shared parts are byte-identical for every character so they cache.

A call's prompt is four pieces:
  rules     the shared rules (the team, the tokens, how to answer)
  context   the bundle (and in round 2 the round-1 table), the same for every character
  identity  this character: spec, CURRENT portrait, last thoughts, balance
  task      what to do now
Layout "cache" (default): system = rules + context, user = identity + task.
Layout "brief":           system = rules + identity, user = context + task.
"""

from . import characters, i18n
from .i18n import t

POS_RU = i18n.Labels("prompts.pos", ("agree", "disagree", "none"))
VERDICT_RU = i18n.Labels("prompts.verdict_word", ("on_track", "drifting", "off_course"))
KIND_RU = i18n.Labels("prompts.kind", ("ballot", "review", "discuss", "brainstorm"))


def project_line(cfg):
    """The project in a few lines (config `project`), and who reads the report."""
    pr = (cfg or {}).get("project") or {}
    name, desc = pr.get("name") or t("prompts.project_name"), pr.get("description") or ""
    return t("prompts.project_desc", name=name, desc=desc) if desc else t("prompts.project", name=name)


def team_list(genders):
    """The team, each with its role (and, in a language with grammatical gender, the gender it speaks of itself in,
    from the state)."""
    return "\n".join(t("prompts.team_line", gender=characters.check_gender(genders[c["id"]]),
                        label=characters.label(c["id"]), role=characters.role(c["id"]))
                      for c in characters.CHARACTERS)


def rules(cfg, kind, genders):
    r = cfg["round"]
    return t("prompts.rules", n=len(characters.IDS), project=project_line(cfg), team=team_list(genders),
             free_max=int(r["free_comment_max_chars"]), comment_max=int(r["comment_max_chars"]), kind=KIND_RU[kind])


def identity(state_persona, cid, balance, n_thoughts):
    portrait = state_persona.portrait().strip()
    version = state_persona.version()
    thoughts = state_persona.thoughts(n_thoughts)
    head = t("prompts.identity_head", gender=characters.check_gender(state_persona.gender()),
             label=characters.label(cid))
    said = "\n".join("- [{}] {}".format(th.get("round", ""), th["thought"]) for th in thoughts) or t("prompts.none")
    return head + "\n\n" + t("prompts.identity_body", spec=state_persona.spec().strip() or t("prompts.none"),
                               version=int(version), portrait=portrait or t("prompts.portrait_empty"), thoughts=said,
                               balance=int(balance))


def points_list(points):
    out = []
    for p in points:
        if p.get("author"):
            out.append(t("prompts.point_by", id=p["id"], title=p["title"], name=characters.name(p["author"])))
        else:
            out.append("- {}. {}".format(p["id"], p["title"]))
    return "\n".join(out) or t("prompts.no_points")


def tools_line(turns, dirs):
    return t("prompts.tools", turns=turns, dirs=", ".join(dirs) or "—")


def stakes_line():
    return t("prompts.stakes")


def standing_line(cfg):
    ids = [q["id"] for q in cfg["standing_questions"]]
    if not ids:
        return None
    return t("prompts.standing", ids=t("prompts.and").join(ids))


def brainstorm_line(cfg):
    from .extras import quorum_text
    b = cfg["brainstorm"]
    return t("prompts.brainstorm", qmin=int(b["questions_min"]), qmax=int(b["questions_max"]),
             fee_init=int(b["fee_initiator"]), fee_other=int(b["fee_other"]), quorum=quorum_text(cfg))


def order_line(cfg):
    o = cfg["orders"]
    return t("prompts.order", price=int(o["price"]), hardware=t("prompts.or").join(o["hardware"]),
             bonus=int(o["moved_bonus"]))


def solo_line():
    return t("prompts.solo")


def task_r1(kind, points, cfg, turns, dirs, solo=False):
    r = cfg["round"]
    lines = [t("prompts.task_r1_solo" if solo else "prompts.task_r1"), tools_line(turns, dirs)]
    if solo:
        lines.append(solo_line())
    if points:
        lines += [t("prompts.points_head"), points_list(points), "", t("prompts.points_how")]
    if standing_line(cfg) and points:
        lines.append(standing_line(cfg))
    if kind in ("review", "discuss", "brainstorm"):
        lines.append(t("prompts.proposals", n=int(r["proposals_max"])))
    if kind == "review":
        lines.append(t("prompts.verdict_how"))
    lines += [stakes_line()] + ([] if solo else [brainstorm_line(cfg)]) + [order_line(cfg)]
    return "\n".join(lines)


def compact_answer(points, positions, max_comment=220):
    """{pid: {position, stake, comment}} -> short lines in point order."""
    out = []
    for p in points:
        a = positions.get(p["id"])
        if not a:
            continue
        s = "{}: {}".format(p["id"], POS_RU[a["position"]])
        if a["stake"]:
            s += " (%d)" % a["stake"]  # noqa: UP031
        if a.get("comment"):
            s += ": " + _clip(a["comment"], max_comment)
        out.append("- " + s)
    return "\n".join(out) or t("prompts.no_positions")


def r1_table(points, finals, max_comment=220, title=None):
    """The round-1 results, the same text for every character (it goes into the shared context)."""
    out = [title or t("prompts.r1_title")]
    for p in points:
        if p.get("author"):
            out.append(t("prompts.r1_point_by", id=p["id"], title=p["title"], name=characters.name(p["author"])))
        else:
            out.append("### {}. {}".format(p["id"], p["title"]))
        if p.get("source") == "proposal":
            out.append(_clip(p.get("question", ""), 500))
        elif p.get("recommendation"):
            out.append(t("prompts.r1_rec", text=_clip(p["recommendation"], 300)))
        any_pos = False
        for cid in characters.IDS:
            a = (finals.get(cid) or {}).get(p["id"])
            if not a:
                continue
            any_pos = True
            if a["stake"]:
                s = t("prompts.r1_voice_stake", name=characters.name(cid), pos=POS_RU[a["position"]],
                      n=int(a["stake"]))
            else:
                s = "- {}: {}".format(characters.name(cid), POS_RU[a["position"]])
            if a.get("comment"):
                s += ": " + _clip(a["comment"], max_comment)
            out.append(s)
        if not any_pos:
            out.append(t("prompts.r1_all_silent"))
    return "\n".join(out)


def proposals_table(proposals, cfg):
    """The round-1 brainstorm proposals, the same text for every character (shared context)."""
    if not proposals:
        return ""
    from .extras import fees
    out = [t("prompts.bs_head")]
    for p in proposals:
        f = fees(cfg, p["initiator"])
        out.append(t("prompts.bs_proposal", id=p["id"], topic=p["topic"], name=characters.name(p["initiator"])))
        if p.get("why"):
            out.append(t("prompts.bs_why", text=_clip(p["why"], 500)))
        out += ["%d. %s" % (i, q) for i, q in enumerate(p["questions"], 1)]  # noqa: UP031
        out.append(t("prompts.bs_fee", fees=", ".join("%s %d" % (characters.name(c), f[c]) for c in characters.IDS)))  # noqa: UP031
    return "\n".join(out)


def task_r2(cid, own_positions, points, available, cfg, turns, dirs, proposal_ids=()):
    r = cfg["round"]
    from .extras import quorum_text
    votes = []
    if proposal_ids:
        votes = [t("prompts.r2_votes", ids=", ".join(proposal_ids), quorum=quorum_text(cfg),
                   fee_init=int(cfg["brainstorm"]["fee_initiator"]), fee_other=int(cfg["brainstorm"]["fee_other"]))]
    return "\n".join([t("prompts.task_r2"), t("prompts.r2_tools", tools=tools_line(turns, dirs)), t("prompts.r2_own"), compact_answer(points, own_positions), "", t("prompts.r2_rebut", n=int(r["rebuttals_max"])), t("prompts.r2_points"), t("prompts.r2_sum", n=int(available)), *votes, t("prompts.r2_order", order=order_line(cfg))])


def outcome_text(points, finals, bs, orders):
    """What the lock pass sees: the final positions, the brainstorm result, the orders placed."""
    out = [r1_table(points, finals, title=t("prompts.outcome_title"))]
    out.append("")
    out.append(t("prompts.outcome_bs"))
    if not bs or not bs.get("proposals"):
        out.append(t("prompts.outcome_no_bs"))
    for p in (bs or {}).get("proposals") or []:
        what = t("prompts.outcome_approved", id=p["brainstorm"]) if p["status"] == "approved" else \
            t("prompts.outcome_failed", reasons="; ".join(p.get("reasons") or []))
        out.append(t("prompts.outcome_line", topic=p.get("topic") or "—", name=characters.name(p["initiator"]),
                     what=what))
    placed = (orders or {}).get("placed") or []
    if placed:
        out.append("")
        out.append(t("prompts.outcome_orders"))
        out += ["- {} {}: `{}`".format(o["id"], characters.name(o["character"]), o["command"]) for o in placed]
    return "\n".join(out)


def task_r3(balance, cfg, turns, dirs):
    c = cfg["lock"]
    lock = t("prompts.r3_lock" if c["returned"] else "prompts.r3_lock_paid", price=int(c["price"]))
    return t("prompts.task_r3", balance=int(balance), tools=tools_line(turns, dirs), lock=lock)


def layout(cfg, rules_text, context_text, identity_text, task_text):
    if cfg["prompt_layout"] == "brief":
        return (rules_text + "\n\n" + identity_text,
                context_text + "\n\n" + task_text)
    return (rules_text + "\n\n" + context_text,
            identity_text + "\n\n" + task_text)


def _clip(text, n):
    text = " ".join((text or "").split())
    return text if len(text) <= n else text[: n - 1].rstrip() + "…"


# --- reflection and seeding -------------------------------------------------------------------

def reflect_rules(cfg, genders):
    return t("prompts.reflect_rules", n=len(characters.IDS), project=project_line(cfg), team=team_list(genders),
             portrait_max=int(cfg["reflect"]["portrait_max_chars"]),
             summary_max=int(cfg["reflect"]["summary_en_max_chars"]))
