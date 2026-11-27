"""The executors' runs, as the depot sees them: `agents start|attach|show|stop|list`.

The depot starts no run itself. The orchestrator asks it first (`agents start`: the gates — the role and the car's
stage, the usage window's stop, one live run per car), starts the run with its own executor, and links it to the run's
log (`agents attach`); the depot reads that log when asked (no daemon, through executors/) and books the spend on the
car once, when the run is over.

agents.json          {"schema": 1, "next": n, "agents": [...]}: every run, in the order they started
agents-history.jsonl every change of a run ({"agent": ag-NNNN, "event": start|attach|finish|stop, ...})
"""

import datetime
import re
from pathlib import Path

from . import effort, executors, gates, i18n, stages
from .i18n import t
from .store import absolute_user_path, append_jsonl, expand_user_path, now_iso, read_json, write_json


def parse_ts(text):
    """An ISO time, '…Z' too -> an aware datetime, or None."""
    if not isinstance(text, str) or not text:
        return None
    t = text.strip()
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    m = re.match(r"^(.*T\d\d:\d\d:\d\d)\.(\d+)(.*)$", t)
    if m:  # Python 3.8 takes 3 or 6 digits only
        t = "{}.{}{}".format(m.group(1), (m.group(2) + "000000")[:6], m.group(3))
    try:
        d = datetime.datetime.fromisoformat(t)
    except ValueError:
        return None
    return d if d.tzinfo else d.astimezone()


SCHEMA = 1
ROLES = ("sketch", "plan", "build", "docs", "mergefix")
ROLE_RU = i18n.Labels("agents.role", ROLES)
# The stages a role may start from (build and docs: their own rules, above).
ROLE_STAGES = {"sketch": ("bogie", "sketch"), "plan": ("sketch", "project"), "mergefix": ("acceptance", "queued")}
STATES = ("working", "silent", "finished", "stopped", "unattached", "lost")
STATE_RU = i18n.Labels("agents.state", STATES)


def state_means():
    """What each state word means, in one line (the agents table's legend)."""
    return t("agents.state_means")


RE_AGENT = re.compile(r"^ag-\d{4}$")
RE_AGENT_ID = re.compile(r"^(?:agent-)?([A-Za-z0-9_-]{4,64})$")


class AgentError(ValueError):
    pass


# --- the run's log: the executor's (executors/) ------------------------------------------------------------------


def read_transcript(path, tail=10, cfg=None):
    """The run's log read by the executor: {turns, mtok, first_at, last_at, actions, actions_n, finished, ...} or None."""
    return executors.read_transcript(cfg, path, tail)


def find_transcript(cfg, agent_id):
    """The run's log by the executor's id of the run: one path, or AgentError."""
    return executors.find_transcript(cfg, agent_id)


def _tilde(path):
    home = expand_user_path("~")
    path = absolute_user_path(path)
    return "~" + path[len(home) :] if home not in ("", "~", "/") and path.startswith(home + "/") else path


def parse_usage(text):
    """--usage 0.62 | 62 | 62% -> 0.62 (a share of the usage window)."""
    t = str(text).strip().replace(",", ".")
    pct = t.endswith("%")
    try:
        v = float(t.rstrip("%").strip())
    except ValueError:
        raise AgentError(f"--usage is the usage window's share: 0.62, 62 or 62%, got {text!r}")  # noqa: B904
    if pct or v > 1:
        v = v / 100.0
    if not 0 <= v <= 1:
        raise AgentError(f"--usage is the usage window's share, 0-100 %, got {text!r}")
    return v


def _pct(u):
    return ("%.1f" % (u * 100)).rstrip("0").rstrip(".") + " %"


# --- the agents ----------------------------------------------------------------------------------------------------


class Agents:
    def __init__(self, state, cfg, tracks=None) -> None:
        self.state = state
        self.cfg = cfg
        self.path = state.path("agents.json")
        self.history_path = state.path("agents-history.jsonl")
        self.data = read_json(self.path) or {"schema": SCHEMA, "next": 1, "agents": []}
        self._tracks = tracks
        self.lines: list[str] = []  # what a command prints besides its table: the spend booked, the stack's lines

    @property
    def tracks(self):
        if self._tracks is None:
            from .tracks import Tracks

            self._tracks = Tracks(self.state, self.cfg)
        return self._tracks

    def agents(self):
        return list(self.data["agents"])

    def get(self, aid):
        for a in self.data["agents"]:
            if a["id"] == aid:
                return a
        raise AgentError(f"no agent {aid} (depot2 agents --all)")

    def _save(self, event, ag, **fields):
        write_json(self.path, self.data)
        self.state.touch(self.path)
        rec = {"ts": now_iso(), "event": event, "agent": ag["id"], "car": ag["car"]}
        rec.update({k: v for k, v in fields.items() if v is not None})
        append_jsonl(self.history_path, rec)
        self.state.touch(self.history_path)
        return rec

    # --- reading ---------------------------------------------------------------------------------------------------

    def reading(self, ag):
        return (
            read_transcript(ag["transcript"], int(self.cfg["agents"]["tail"]), self.cfg)
            if ag.get("transcript")
            else None
        )

    def status(self, ag, rd=None, now=None):
        """(state, words): working | silent | finished | stopped | unattached | lost."""
        if ag.get("state") == "stopped":
            s = ag.get("stopped") or {}
            return "stopped", t(
                "agents.st_stopped", at=str(s.get("ts") or "")[:16].replace("T", " "), why=s.get("why") or ""
            )
        if ag.get("state") == "finished":
            return "finished", t("agents.st_finished_at", at=str(ag.get("finished") or "")[:16].replace("T", " "))
        if not ag.get("transcript"):
            return "unattached", t("agents.st_unattached", id=ag["id"])
        if rd is None:
            return "lost", t("agents.st_lost", path=ag["transcript"])
        if rd["finished"]:
            return "finished", t("agents.st_finished_by_user") if rd["stopped_by_user"] else STATE_RU["finished"]
        last = parse_ts(rd["last_at"])
        now = now or datetime.datetime.now().astimezone()
        mins = int((now - last).total_seconds() // 60) if last else None
        if mins is not None and mins >= float(self.cfg["agents"]["silent_min"]):
            from .stack import age_ru

            return "silent", t("agents.st_silent", age=age_ru(last.isoformat(), now))
        return "working", STATE_RU["working"]

    def is_live(self, ag, rd=None):
        """Not finished and not stopped: by its record, and by its transcript when it has one."""
        if ag.get("state") in ("finished", "stopped"):
            return False
        return not (rd or {}).get("finished")

    def view(self, ag, now=None, rd=None):
        """An agent as the tables and `--json` show it: its record, its state and what its transcript says now."""
        rd = rd if rd is not None else self.reading(ag)
        state, words = self.status(ag, rd, now)
        booked = ag.get("booked") or {}
        mtok = booked.get("mtok") if booked else (rd or {}).get("mtok")
        turns = booked.get("turns") if booked else (rd or {}).get("turns")
        acts = (rd or {}).get("actions") or []
        out = dict(ag)
        out.update(
            state=state,
            state_ru=STATE_RU[state],
            state_text=words,
            live=self.is_live(ag, rd),
            role_ru=ROLE_RU.get(ag["role"], ag["role"]),
            turns=turns,
            spent_mtok=mtok,
            spent_text=effort.fmt(mtok, self.cfg) if mtok is not None else "—",
            last_action=acts[-1] if acts else None,
            last_at=(rd or {}).get("last_at"),
            actions=acts,
            actions_n=(rd or {}).get("actions_n"),
            model=(rd or {}).get("model"),
            effort=(rd or {}).get("effort"),
            slot=self.slot_of(ag),
        )
        return out

    def slot_of(self, ag):
        try:
            _, c = self.tracks.car(ag["car"])
        except ValueError:
            return None
        st = c.get("st")
        if not st or not self.tracks.stack._dep_open(st):
            return None
        stk = self.tracks.stack
        o = stk.get(st)
        n = None
        if o["where"] == "slot":
            n = t("agents.slot_n", n=[x["id"] for x in stk.in_slots()].index(st) + 1)
        elif o["where"] == "post":
            n = t("agents.post_n", n=[x["id"] for x in stk.posted()].index(st) + 1)
        return {"object": st, "where": o["where"], "n": n}

    def on_car(self, ref, live=True):
        return [a for a in self.data["agents"] if a["car"] == ref and (not live or self.is_live(a, self.reading(a)))]

    # --- the end of a run: the spend on the car, once ---------------------------------------------------------------

    def settle(self, by="agents"):
        """Every live agent whose transcript says it finished: finished, and its spend booked on its car (once)."""
        for ag in self.data["agents"]:
            if ag.get("state") == "live" and ag.get("transcript"):
                rd = self.reading(ag)
                if rd is not None and rd["finished"]:
                    ag["state"] = "finished"
                    ag["finished"] = now_iso()
                    self._book(ag, rd, by)
                    self._save("finish", ag, booked=ag.get("booked"))

    def _book(self, ag, rd, by):
        """The agent's spend on its car: `spent_mtok` += its MTok, `transcript` = its file; once (`booked`)."""
        if ag.get("booked") or rd is None:
            return
        tr = self.tracks
        trk, c = tr.car(ag["car"])
        mtok = float(rd["mtok"])
        c["spent_mtok"] = round(float(c.get("spent_mtok") or 0) + mtok, 4)
        c["transcript"] = ag["transcript"]
        tr._save(
            "agent_spent",
            trk["id"],
            car=c["n"],
            agent=ag["id"],
            spent_mtok=mtok,
            total_mtok=c["spent_mtok"],
            transcript=ag["transcript"],
            by=by,
        )
        if c.get("st") and tr.stack._dep_open(c["st"]):  # its stack object shows the same spend
            tr.stack.set(c["st"], spent_mtok=c["spent_mtok"])
        ag["booked"] = {"ts": now_iso(), "mtok": mtok, "turns": rd["turns"]}
        self.lines.append(
            t(
                "agents.booked_finished" if ag.get("state") == "finished" else "agents.booked_stopped",
                id=ag["id"],
                car=ag["car"],
                mtok=effort.fmt(mtok, self.cfg),
                total=effort.fmt(c["spent_mtok"], self.cfg),
            )
        )

    # --- writing ---------------------------------------------------------------------------------------------------

    def car_of(self, ref):
        """A car by its ref, or by its live stack object (st-NNNN): (track, car, 'tr-NNNN:N')."""
        tr = self.tracks
        ref = str(ref or "").strip()
        if re.match(r"^st-\d{4}$", ref):
            t, c = tr.car_by_stack(ref)
            if c is None:
                raise AgentError(f"{ref} is no track's car: an agent runs on a car (tr-NNNN:N)")
        else:
            t, c = tr.car(ref)
        return t, c, "{}:{}".format(t["id"], c["n"])

    def start_unmet(self, trk, c, ref, role, usage):
        """What the gates of `agents start` miss: {"role"|"window"|"agent"|"DoR"|"limit": [text]}; and whether a build
        goes into a slot first.
        """
        tr = self.tracks
        stk = tr.stack
        stage = tr.stage_of(c)
        unmet: dict[str, list[str]] = {"role": [], "window": [], "agent": []}
        to_slot = False
        in_slot = bool(c.get("st")) and any(o["id"] == c["st"] for o in stk.in_slots())
        if role == "build":
            if not in_slot:
                to_slot = True
                if stage not in (*stages.PLANNING, "build"):
                    unmet["role"].append(t("agents.role_build_stage", role=ROLE_RU[role], stage=stages.ru(stage)))
                else:
                    obj = stk.get(c["st"]) if c.get("st") and stk._dep_open(c["st"]) else None
                    unmet.update(tr.build_unmet(trk, c, obj, tr.parked(c)))
        elif role == "docs":  # the stage holds the build's slot (stages.py): the agent works there, takes none
            if stage != "docs":
                unmet["role"].append(
                    t(
                        "agents.role_docs_stage",
                        role=ROLE_RU[role],
                        stage=stages.ru(stage),
                        docs=stages.ru("docs"),
                        ref=ref,
                    )
                )
            elif not in_slot:  # only a forced `car stage` leaves it there out of a slot
                unmet["role"].append(
                    t(
                        "agents.role_docs_slot",
                        role=ROLE_RU[role],
                        docs=stages.ru("docs"),
                        st=c.get("st") or ref,
                        ref=ref,
                    )
                )
        elif stage not in ROLE_STAGES[role]:
            unmet["role"].append(
                t(
                    "agents.role_stages",
                    role=ROLE_RU[role],
                    stage=stages.ru(stage),
                    stages=", ".join(f"«{stages.ru(s)}»" for s in ROLE_STAGES[role]),
                )
            )
        stop = float(self.cfg["usage"]["stop_at"])
        if usage is None:
            unmet["window"].append(t("agents.no_usage", stop=_pct(stop)))
        elif usage >= stop:
            unmet["window"].append(t("agents.over_stop", usage=_pct(usage), stop=_pct(stop)))
        for other in self.on_car(ref):
            _, words = self.status(other, self.reading(other))
            unmet["agent"].append(
                t("agents.busy", id=other["id"], role=ROLE_RU.get(other["role"], other["role"]), words=words)
            )
        return unmet, to_slot

    def start(self, ref, role, by=None, usage=None, force=False, why=None):
        """The gates (soft), then the record; a build not in a slot goes into one first (`tracks car stack --start`).
        Returns the new agent.
        """
        if role not in ROLES:
            raise AgentError("a role is one of {}".format(", ".join(ROLES)))
        self.settle(by="agents start")  # an agent that finished meanwhile no longer holds the car
        trk, c, ref = self.car_of(ref)
        unmet, to_slot = self.start_unmet(trk, c, ref, role, usage)
        cmd = f"depot2 agents start {ref} --role {role}"
        passed = gates.check(unmet, force, why, ref, t("agents.target", role=ROLE_RU[role]), cmd, error=AgentError)
        if to_slot:  # its own gate again, with what --force passed (the stage's history row says forced)
            _, msgs = self.tracks.car_stack(ref, start=True, force=force and bool(passed), why=why)
            self.lines += self.tracks.stack.notes + msgs
        ag = {
            "id": "ag-%04d" % int(self.data["next"]),  # noqa: UP031
            "car": ref,
            "role": role,
            "by": (by or t("agents.by_orchestrator")).strip(),
            "started": now_iso(),
            "state": "live",
            "usage": usage,
            "stop_at": float(self.cfg["usage"]["stop_at"]),
            "st": c.get("st"),
        }
        if passed:
            ag.update(forced=True, why=why.strip(), gates=passed)
        self.data["next"] = int(self.data["next"]) + 1
        self.data["agents"].append(ag)
        self._save(
            "start",
            ag,
            role=role,
            by=ag["by"],
            usage=usage,
            st=ag["st"],
            forced=ag.get("forced"),
            why=ag.get("why"),
            gates=ag.get("gates"),
        )
        return ag

    def attach(self, aid, agent_id, transcript=None):
        ag = self.get(aid)
        if ag.get("state") != "live":
            raise AgentError(
                "{} is {}: only a live agent is attached".format(aid, STATE_RU.get(ag.get("state"), ag.get("state")))
            )
        m = RE_AGENT_ID.match(str(agent_id or "").strip())
        if not m:
            raise AgentError(f"a run id is letters and digits (the executor's run id), got {agent_id!r}")
        agent_id = m.group(1)
        if transcript:
            path = absolute_user_path(transcript)
            if not Path(path).is_file():
                raise AgentError(f"no file {transcript}")
        else:
            path = find_transcript(self.cfg, agent_id)
        path = _tilde(path)
        for other in self.data["agents"]:
            if other is not ag and (other.get("agent_id") == agent_id or other.get("transcript") == path):
                raise AgentError("{} is {}'s already".format(agent_id, other["id"]))
        if ag.get("agent_id") and (ag["agent_id"], ag.get("transcript")) != (agent_id, path):
            raise AgentError("{} is attached to {} already ({})".format(aid, ag["agent_id"], ag.get("transcript")))
        if ag.get("agent_id"):
            return ag
        ag.update(agent_id=agent_id, transcript=path, attached=now_iso())
        self._save("attach", ag, agent_id=agent_id, transcript=path)
        self.settle(by="agents attach")
        return ag

    def stop(self, aid, why, by=None):
        """The orchestrator stopped it (or will): recorded; the spend so far booked on the car."""
        ag = self.get(aid)
        if not (why or "").strip():
            raise AgentError("agents stop needs a reason: --why «…»")
        if ag.get("state") != "live":
            raise AgentError("{} is {} already".format(aid, STATE_RU.get(ag.get("state"), ag.get("state"))))
        ag["state"] = "stopped"
        ag["stopped"] = {"ts": now_iso(), "why": why.strip(), "by": (by or t("agents.by_orchestrator")).strip()}
        self._book(ag, self.reading(ag), "agents stop")
        self._save("stop", ag, why=why.strip(), by=ag["stopped"]["by"], booked=ag.get("booked"))
        return ag

    # --- the tables ------------------------------------------------------------------------------------------------

    def views(self, include_all=False, now=None):
        out = [self.view(a, now) for a in self.data["agents"]]
        return out if include_all else [v for v in out if v["live"]]

    def md(self, include_all=False, now=None):
        rows = self.views(include_all, now)
        L = [t("agents.heading"), ""]
        if not rows:
            L.append(t("agents.none") if include_all else t("agents.none_live"))
        else:
            L.append(t("agents.table"))
            L.append("|---|---|---|---|---|---|---|---|---|---|")
            for v in rows:
                L.append(
                    "| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                        v["id"],
                        v["car"],
                        v["role_ru"],
                        _cell(v["by"]),
                        _when(v["started"], now),
                        _cell(v["state_text"] + (" · " + t("agents.forced_mark") if v.get("forced") else "")),
                        v["turns"] if v["turns"] is not None else "—",
                        v["spent_text"],
                        _cell(action_text(v["last_action"])),
                        _cell(slot_text(v["slot"])),
                    )
                )
        L.append("")
        L.append(t("agents.legend", means=state_means()))
        return "\n".join(L) + "\n"

    def show_md(self, aid, now=None):
        v = self.view(self.get(aid), now)
        L = ["## {} — {}, {}".format(v["id"], v["role_ru"], v["car"]), ""]
        L.append(
            t(
                "agents.show_who_usage",
                by=v["by"],
                at=_when(v["started"], now),
                usage=_pct(v["usage"]),
                stop=_pct(v["stop_at"]),
            )
            if v.get("usage") is not None
            else t("agents.show_who", by=v["by"], at=_when(v["started"], now))
        )
        if v.get("forced"):
            L.append(t("agents.show_forced", why=v.get("why"), gates=gates.gates_text(v.get("gates"))))
        L.append(t("agents.show_state", text=v["state_text"]))
        if v.get("agent_id"):
            L.append(
                t("agents.show_agent", agent=v["agent_id"], path=v["transcript"])
                + (
                    (", {} {}".format(v.get("model") or "", v.get("effort") or "")).rstrip(", ")
                    if v.get("model")
                    else ""
                )
            )
        L.append(
            t(
                "agents.show_turns_booked" if v.get("booked") else "agents.show_turns",
                turns=v["turns"] if v["turns"] is not None else "—",
                spent=v["spent_text"],
            )
        )
        if v["slot"]:
            L.append(t("agents.show_stack", slot=slot_text(v["slot"])))
        if v["actions"]:
            L.append("")
            L.append(t("agents.show_actions", n=len(v["actions"]), total=v.get("actions_n")))
            for x in v["actions"]:
                L.append("- {} {}".format(_hm(x.get("at")), action_text(x)))
        return "\n".join(L) + "\n"

    def to_json(self, include_all=False, now=None):
        return {
            "v": 1,
            "generated": now_iso(),
            "all": bool(include_all),
            "silent_min": self.cfg["agents"]["silent_min"],
            "stop_at": self.cfg["usage"]["stop_at"],
            "roles": [{"role": r, "ru": ROLE_RU[r]} for r in ROLES],
            "agents": self.views(include_all, now),
        }


def live_by_object(state, cfg, places):
    """{st: the newest live agent on that object's car, short} for `stack --json` (places: Stack.car_places()).
    Read only: a finished one is booked by the next `agents` command.
    """
    if not Path(state.path("agents.json")).exists():
        return {}
    ags = Agents(state, cfg)
    by_car = {}
    for a in ags.data["agents"]:
        if a.get("state") == "live":
            by_car[a["car"]] = a  # the newest wins
    out = {}
    for st, pl in places.items():
        a = by_car.get(pl.get("car"))
        if a is None:
            continue
        rd = ags.reading(a)
        if not ags.is_live(a, rd):
            continue
        state_, words = ags.status(a, rd)
        acts = (rd or {}).get("actions") or []
        out[st] = {
            "id": a["id"],
            "role": a["role"],
            "role_ru": ROLE_RU.get(a["role"], a["role"]),
            "state": state_,
            "state_ru": STATE_RU[state_],
            "state_text": words,
            "turns": (rd or {}).get("turns"),
            "spent_mtok": (rd or {}).get("mtok"),
            "last_action": acts[-1] if acts else None,
            "last_at": (rd or {}).get("last_at"),
        }
    return out


def action_text(act):
    if not act:
        return "—"
    return "{}: {}".format(act["name"], act["arg"]) if act.get("arg") else act["name"]


def agent_cell(a):
    if not a:
        return ""
    last = a.get("last_action")
    if last:
        return t(
            "agents.cell_last",
            id=a["id"],
            role=a["role_ru"],
            state=a["state_text"],
            action=action_text(last),
            at=_hm(last.get("at")),
        )
    return t("agents.cell", id=a["id"], role=a["role_ru"], state=a["state_text"])


def slot_text(slot):
    if not slot:
        return "—"
    where = t("agents.where_queue") if slot["where"] == "queue" else slot["where"]
    return "{} {}".format(slot["n"] or where, slot["object"])


def _hm(iso):
    d = parse_ts(iso)
    return d.astimezone().strftime("%H:%M") if d else "--:--"


def _when(iso, now=None):
    from .stack import age_ru

    d = parse_ts(iso)
    if d is None:
        return "—"
    return "{} ({})".format(d.astimezone().strftime("%Y-%m-%d %H:%M"), age_ru(d.isoformat(), now))


def _cell(text):
    return str(text or "").replace("|", "\\|").replace("\n", " ")
