"""The project config: the defaults, and how a depot retunes them.

The state dir's config.json is deep-merged over DEFAULTS, then each `--set a.b=value` from the
command line. Values in --set are parsed as JSON when they parse (numbers, lists, true), else kept
as strings. Obsolete keys (OBSOLETE) are dropped with one warning on stderr, never a crash.
"""

import copy
import json
import os
import re
import sys

from . import effort

DEFAULT_STATE = "~/.local/share/depot2/depot"  # or $DEPOT2_STATE, or --state

DEFAULTS = {
    # The project this depot plans and merges, in one line for the team rounds' prompts.
    "project": {"name": "example", "description": "A software project; set project.description in config.json."},
    # The language of every message (i18n.py): en or ru; $DEPOT2_LANG, when set, beats it.
    "language": "en",
    # The team of the rounds (characters.py): null is no team (rounds, tokens, reflection and brainstorms are off,
    # everything else works); a list of characters turns it on (examples/demo/config.json has one).
    "characters": None,
    # Project hooks (hooks.py): module names or paths to .py files, called in this order.
    "plugins": [],
    # The executor (executors/__init__.py): the module that runs a capped call and reads a run's log, its command,
    # and the model and effort it is asked for (null: the runner's own default).
    "executor": "depot2.executors.cli_agent",
    "executor_bin": None,
    "model": None,
    "effort": None,
    # Read-only tools on every round pass; the deny rules below come on top.
    "tools": ["Read", "Grep", "Glob"],
    "free": {
        "max_tool_turns_r1": 4,
        "max_tool_turns_r2": 2,
        "budget_mtok_r1": 0.16,
        "budget_mtok_r2": 0.20,
        "timeout_s": 900,
    },
    # What every round pass may read (--restricted confines the file tools to these): the repo
    # as a fresh read-only snapshot of origin/main (git fetch + git archive into a temp dir,
    # removed after the round; for `--pr N` it is that PR's head), plus read_dirs. Round 1 and
    # the debate get the same --add-dir list.
    "deep_repo": {
        "clone": None,               # the project's clone; null: the rounds read no repo
        "ref": "origin/main",
        "fetch": True,
    },
    "read_dirs": [],                 # more dirs every round pass may read
    "reflect": {
        "budget_mtok": 0.28,
        "timeout_s": 900,
        "portrait_max_chars": 3000,
        "thoughts": 30,
        "facts_max_chars": 12000,
        "summary_en_max_chars": 1500,
    },
    "round": {
        "budget_mtok": 1.8,
        "max_calls": 16,
        "stop_at_utilization": 0.90,
        "proposals_max": 3,           # new points a character may raise (review, discuss)
        "comment_max_chars": 600,
        "free_comment_max_chars": 140,  # a comment without a stake is one short line
        "rebuttals_max": 2,
    },
    "bundle": {
        "max_chars": 120000,
        "decisions_glob": None,        # the latest file matching it goes into every bundle
        "decisions_max_chars": 16000,
        "context_max_chars": 30000,
        "merged_pr_limit": 30,
        "pr_body_max_chars": 3000,
        "open_pr_body_max_chars": 1200,
        "principles_max_chars": 24000,
    },
    # Points every round adds after its input's own: [{"id": "X1", "title": "...", "question": "..."}].
    "standing_questions": [],
    "brainstorm": {
        "quorum": "unanimous",
        "fee_initiator": 10,
        "fee_other": 5,
        "questions_min": 3,
        "questions_max": 5,
        "topics_dir": None,           # null: <state>/topics
    },
    "orders": {
        "price": 5,
        "moved_bonus": 1,
        "hardware": ["cpu"],          # what an order may run on; anything else needs the owner's OK
        "results_dir": None,          # null: <state>/orders
    },
    "stack": {"slots": 4, "queue_max": 10, "post_max": 4},
    "gates": {"dor": ["brief", "questions", "after", "project", "checks"],
              "dod": ["ci", "heavy", "scan", "shots", "principles", "docs"], "checks_since": None},
    "repair": {"bugs": None},       # the bug table (a Markdown file whose first column is the bug id); null: none
    "team_stack": {"slots": 2, "queue_max": 4},
    "usage": {
        "mtok_per_pct": 10.0,         # MTok in 1 % of the usage window (%UW): set it to your runner's own budget
        "usd_per_mtok": 2.5,          # the runner's rough $ per MTok (it takes its caps in $)
        "sizes": {"doc": 0.4, "S": 0.3, "M": 1.0, "L": 1.4},
        "stop_at": 0.9,                # no new agent past this share of the usage window
    },
    "agents": {"projects": None, "silent_min": 20, "tail": 10},  # projects: where the executor keeps the runs' logs
    "trains": {"default_slots": "max 1"},
    "solo": {"budget_mtok": None},
    "lock": {
        "price": 10,
        "returned": False,
        "max_tool_turns": 2,
        "budget_mtok": 0.60,
        "timeout_s": 900,
    },
    "backup": {
        "drive": False,                          # the daily Drive run from any command (off: by hand only)
        "git": {
            "remote": "backup",                  # the remote's NAME in the state's git; the URL is the owner's to add
            "branch": None,                      # null: the current branch
            "every_commits": 10,                 # this many commits the remote lacks start a push
            "retry_min": 30,                     # at most one background start per this many min, doubled per failure
            "timeout_s": 600,                    # per git push
            # The scan before every push (null: no scan), twice; the last line of each must be exactly CLEAN. First
            # what the push carries (its blobs and commit messages, exported to {dir}), then the files on disk.
            "scan": None,
            "scan_pushed_args": ["{dir}"],
            "scan_args": ["{state}"],
            "scan_timeout_s": 1800,
            "quarantine": "~/.local/state/depot2/quarantine",
        },
        "remote": None,                          # the cloud remote in rclone.conf (its root), e.g. "mycloud:"
        "folder": "depot2-backup",               # our own top-level folder, nothing else's
        "crypt_name": "depot2backupcrypt",       # the env-only crypt remote over remote+folder
        "pass_password": None,                   # the pass entries of the crypt password and salt
        "pass_salt": None,
        "forbidden_remotes": [],                 # remote names the backup must never write to
        "forbidden_folders": [],                 # folder names it must never write into
        "rclone_bin": "rclone",
        "pass_bin": "pass",
        "host": None,                            # null: the short hostname, in the archive's name
        "every_h": 24,                           # a success older than this starts a run
        "retry_min": 15,                         # at most one background start per this many min
        "keep_daily": 14,                        # + the first archive of every month, kept
        "drive_trash": True,                     # deleted archives go to Drive's trash (30 days)
        "lock_wait_s": 1800,                     # the snapshot waits this long for the state lock
        "timeout_s": 1800,                       # per rclone call
    },
    "repo": None,                    # the project on GitHub, owner/name (gh -R)
    "web": {"hosts": []},
    "merge": {
        "clone": None,                # the project's main clone: fetch, the dry run's throwaway worktree, the smoke's cwd
        "worktrees": None,            # a PR's branch rides in <worktrees>/<branch>
        "remote": "origin",
        "base": "main",
        "ci_workflow": "ci.yml",      # the workflow whose runs gate a PR and main's merge commit
        "deploy_job": None,           # the job of that workflow that deploys main; null: no deploy to wait for
        "poll_s": 60,
        "run_appear_min": 10,
        "ci_timeout_min": 120,       # the branch's CI on its head, running (not waiting for GitHub)
        "deploy_timeout_min": 60,    # main's CI/CD of the merge commit
        "queued_max_min": 15,
        "github_rerun_after_min": 5,  # a run GitHub left unstarted: `gh run rerun --failed` after this; null: by hand
        "github_reruns_max": 3,
        "github_wait_max_min": 240,  # waiting for GitHub this long: the runner exits, the entry still waits
        # The runners' fallbacks (null: off): a repository variable `var` holds the runs-on labels; when a job waits
        # past queued_max_min and no runner with every label of `main` is online, it is set to `fallback`, the run is
        # cancelled and rerun --failed, and set back once that run is over. ci_fallback names its `jobs` too:
        # {"var": "CI_RUNNER", "main": ["self-hosted", "fast"], "fallback": ["self-hosted", "spare"], "jobs": ["Test"]}
        "cd_fallback": None,
        "ci_fallback": None,
        "githubstatus": ["curl", "-s", "--max-time", "15", "https://www.githubstatus.com/api/v2/components.json"],
        "scan": None,                 # the scan before every push (its last line must be exactly CLEAN); null: none
        "scan_args": ["{worktree}"],
        "scan_timeout_s": 1800,
        # after a prepared resolution, in the worktree, before the merge commit
        "after_resolve": [],          # commands, e.g. [["make", "check"]]
        "after_resolve_timeout_s": 1800,
        "auto": {
            # each rule null: off. generated: {"file": a generated file, "cmd": its generator}, rebuilt after every
            # merge; snapshots: {"glob": snapshot files, "cmd": a command with {test}}; golden: {"file": a recorded
            # file, "cmd": a command with {remote}, {branch} and {out}}, re-recorded after the push; union: path
            # patterns joined when both sides only added text.
            "generated": None,
            "snapshots": None,
            "golden": None,
            "union": [],
        },
        "generate_timeout_s": 1800,
        "golden_timeout_s": 5400,
        "presync_depth": 2,
        "trial_prefix": "mq/",
        # a trial's push is scanned for what it carries that GitHub has not (its merge commit and the files the merge
        # made, exported to {dir}), as the depot's own push does (backup.git.scan_pushed_args)
        "trial_scan_args": ["{dir}"],
        "trial_retry_min": 10,
        "net_retries": 5,
        "net_retry_s": 60,
        "ci_net_reruns": 2,
        "ci_net_patterns": ["HttpClient.Timeout", "download has timed out", "Failed to download action",
                            "Could not resolve host", "TLS handshake timeout", "Connection timed out", "ETIMEDOUT",
                            "ECONNRESET"],
        # After the deploy: the site's HTTP status (a command that prints it; null: none), then the smoke check (a
        # command in the main clone; its verdict: the plugins' smoke_verdict, else its exit code; null: none), then
        # the plugins' after_merge (usage: false skips it).
        "site_check": None,           # e.g. ["curl", "-s", "-o", "/dev/null", "-w", "%{http_code}", "https://example.org/"]
        "site_ok": [200],
        "smoke": None,
        "smoke_timeout_s": 900,
        "usage": True,
    },
    "thoughts_in_prompt": 10,
    # "cache": the shared rules and the bundle are the system prompt (identical for every
    # character, so the calls after the first read it from the prompt cache) and the character comes first in the user
    # turn. "brief": the character in the system prompt, the bundle in the user turn.
    "prompt_layout": "cache",
    # Never readable, on top of --restricted.
    "deny_read": [
        "Read(//proc/**)", "Read(//sys/**)", "Read(//dev/**)",
        "Read(**/.env*)", "Read(**/.dev.vars*)", "Read(**/*.pem)", "Read(**/*.key)",
        "Read(**/.git/**)", "Read(**/node_modules/**)",
    ],
}

EFFORTS = ("low", "medium", "high", "xhigh", "max")
QUORUMS = ("unanimous", "majority")

OBSOLETE = {
    "deep_levels": None,     # the price list of the paid passes
    "deep_dirs": "read_dirs",  # every pass read them already; the new name says so
}
USD_KEYS = (
    ("free", "budget_usd_r1", "budget_mtok_r1"),
    ("free", "budget_usd_r2", "budget_mtok_r2"),
    ("reflect", "budget_usd", "budget_mtok"),
    ("round", "budget_usd", "budget_mtok"),
    ("solo", "budget_usd", "budget_mtok"),
    ("lock", "budget_usd", "budget_mtok"),
)
# Every cap on the spend, in MTok (solo's may be null).
MTOK_KEYS = tuple((sect, new) for sect, _, new in USD_KEYS)
_warned = set()


def repo(cfg):
    """The project on GitHub (config `repo`, owner/name), or a ValueError that says how to set it."""
    r = cfg.get("repo")
    if not isinstance(r, str) or "/" not in r:
        raise ValueError("repo is not set: the project on GitHub, owner/name (config.json \"repo\", or --set repo=…)")
    return r


def deep_merge(base, over):
    out = copy.deepcopy(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def parse_set(items):
    over = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError("--set wants key=value, got %r" % item)
        key, raw = item.split("=", 1)
        try:
            val = json.loads(raw)
        except ValueError:
            val = raw
        node = over
        parts = key.split(".")
        for p in parts[:-1]:
            node = node.setdefault(p, {})
        node[parts[-1]] = val
    return over


def drop_obsolete(over, where, warn=None):
    """Remove OBSOLETE keys from one override dict (config.json or --set), in place. An old name
    passes its value to the new one unless the new one is set too. Returns the warnings."""
    msgs = []
    for key, new in OBSOLETE.items():
        if key not in over:
            continue
        val = over.pop(key)
        if new is None:
            msgs.append("%s: %s is obsolete (the paid passes are gone) and ignored; remove it" % (where, key))
        elif new in over:
            msgs.append("%s: %s is the old name of %s, which is set too; %s ignored; remove it"
                        % (where, key, new, key))
        else:
            over[new] = val
            msgs.append("%s: %s is the old name of %s; used as %s; rename it" % (where, key, new, new))
    for m in msgs:
        if warn is not None:
            warn(m)
    return msgs


def _rate(usage):
    for key in ("usd_per_mtok", "mtok_per_pct"):
        v = usage.get(key)
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError("usage.%s must be a number > 0" % key)
    return float(usage["usd_per_mtok"]), float(usage["mtok_per_pct"])


def convert_usd(over, where, usage, warn=None):
    """The old $ caps (USD_KEYS) of one override dict (config.json or --set), in place: each becomes
    its MTok key, value / usage.usd_per_mtok, unless that key is set in the same dict too. Returns
    the warnings."""
    rate, per_pct = _rate(usage)
    msgs = []
    for sect, old, new in USD_KEYS:
        node = over.get(sect)
        if not isinstance(node, dict) or old not in node:
            continue
        val = node.pop(old)
        if new in node:
            msgs.append("%s: %s.%s is deprecated and %s.%s is set too; %s.%s ignored; remove it"
                        % (where, sect, old, sect, new, sect, old))
            continue
        if val is None:
            node[new] = None
            msgs.append("%s: %s.%s is deprecated; read as %s.%s = null; rename it" % (where, sect, old, sect, new))
            continue
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            raise ValueError("%s: %s.%s must be a number" % (where, sect, old))
        mtok = round(float(val) / rate, 6)
        node[new] = mtok
        msgs.append("%s: %s.%s is deprecated (the limits are in MTok now); read as %s.%s = %s: %s "
                    "(%g / usage.usd_per_mtok %g); rename it"
                    % (where, sect, old, sect, new, "%.6g" % mtok,
                       effort.fmt(mtok, {"usage": {"mtok_per_pct": per_pct}}), val, rate))
    for m in msgs:
        if warn is not None:
            warn(m)
    return msgs


def _warn_once(msg):
    if msg not in _warned:
        _warned.add(msg)
        print("warning: " + msg, file=sys.stderr)


def load(state_root, sets=None, warn=_warn_once):
    """DEFAULTS, then the project's config file ($DEPOT2_CONFIG, when set: it can live in the project's repo), then
    the depot's own config.json, then each --set."""
    layers = []
    project = os.environ.get("DEPOT2_CONFIG")
    if project:
        project = os.path.expanduser(project)
        if not os.path.isfile(project):
            raise ValueError("DEPOT2_CONFIG=%s: no such file" % project)
        with open(project, encoding="utf-8") as f:
            over = json.load(f)
        drop_obsolete(over, project, warn)
        layers.append((project, over))
    path = os.path.join(state_root, "config.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            over = json.load(f)
        drop_obsolete(over, path, warn)
        layers.append((path, over))
    over = parse_set(sets)
    drop_obsolete(over, "--set", warn)
    layers.append(("--set", over))
    # The rate the old $ keys convert at: the effective one, whichever layer sets it.
    probe = copy.deepcopy(DEFAULTS)
    for _, over in layers:
        probe = deep_merge(probe, over)
    if not isinstance(probe.get("usage"), dict):
        raise ValueError("usage must be an object")
    cfg = copy.deepcopy(DEFAULTS)
    for where, over in layers:
        convert_usd(over, where, probe["usage"], warn)
        cfg = deep_merge(cfg, over)
    for sect, key in (("round", "stop_at_utilization"), ("usage", "stop_at")):
        u = cfg[sect].get(key)
        if isinstance(u, (int, float)) and not isinstance(u, bool) and 1 < u <= 100:
            # a share above 1 can only be meant as percent: the fuse would never fire
            cfg[sect][key] = u / 100.0
            if warn is not None:
                warn("%s.%s %g is a share of a window (0.9 = 90 %%); read as %g" % (sect, key, u, u / 100.0))
    check(cfg)
    from . import i18n
    i18n.configure(cfg["language"])
    for sect, key, sub in (("brainstorm", "topics_dir", "topics"), ("orders", "results_dir", "orders")):
        if not cfg[sect].get(key):  # null: a folder of the depot itself
            cfg[sect][key] = os.path.join(state_root, sub)
    from . import characters
    characters.configure(cfg.get("characters"))
    return cfg


def check(cfg):
    from . import i18n
    i18n.check_language(cfg.get("language"))
    if cfg["effort"] is not None and cfg["effort"] not in EFFORTS:
        raise ValueError("effort must be one of %s or null" % (EFFORTS,))
    bad = [t for t in cfg["tools"] if t not in ("Read", "Grep", "Glob")]
    if bad:
        raise ValueError("only read-only tools are allowed, got %s" % bad)
    if cfg["prompt_layout"] not in ("cache", "brief"):
        raise ValueError("prompt_layout must be cache or brief")
    if cfg["brainstorm"]["quorum"] not in QUORUMS:
        raise ValueError("brainstorm.quorum must be one of %s" % (QUORUMS,))
    for k in ("fee_initiator", "fee_other"):
        if int(cfg["brainstorm"][k]) < 0:
            raise ValueError("brainstorm.%s must be >= 0" % k)
    for key in ("stack", "team_stack"):
        if int(cfg[key]["slots"]) < 0 or int(cfg[key]["queue_max"]) < 0:
            raise ValueError("%s.slots and %s.queue_max must be >= 0" % (key, key))
    if int(cfg["stack"].get("post_max", 0)) < 0:
        raise ValueError("stack.post_max must be >= 0")
    from .gates import DOD, DOR
    for key, known in (("dor", DOR), ("dod", DOD)):
        bad = [i for i in cfg["gates"].get(key) or [] if i not in known]
        if bad:
            raise ValueError("gates.%s: unknown %s (known: %s)" % (key, ", ".join(map(str, bad)), ", ".join(known)))
    since = cfg["gates"].get("checks_since")
    if since is not None and not (isinstance(since, str) and re.match(r"^\d{4}-\d{2}-\d{2}$", since)):
        raise ValueError("gates.checks_since must be a day, YYYY-MM-DD (got %r)" % (since,))
    if not isinstance(cfg["usage"]["mtok_per_pct"], (int, float)) or cfg["usage"]["mtok_per_pct"] <= 0:
        raise ValueError("usage.mtok_per_pct must be a number > 0 (MTok in 1%% of the usage window)")
    if not isinstance(cfg["usage"]["usd_per_mtok"], (int, float)) or cfg["usage"]["usd_per_mtok"] <= 0:
        raise ValueError("usage.usd_per_mtok must be a number > 0 (the CLI's ≈$ per MTok)")
    for sect, key in MTOK_KEYS:
        v = cfg[sect].get(key)
        if v is None and sect == "solo":
            continue
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v <= 0:
            raise ValueError("%s.%s must be a number of MTok > 0%s" % (sect, key, " or null" if sect == "solo" else ""))
    u = cfg["round"]["stop_at_utilization"]
    if isinstance(u, bool) or not isinstance(u, (int, float)) or not 0 < u <= 1:
        raise ValueError("round.stop_at_utilization is a share of a window, 0 < x <= 1 (0.9 = 90 %)")
    u = cfg["usage"].get("stop_at")
    if isinstance(u, bool) or not isinstance(u, (int, float)) or not 0 < u <= 1:
        raise ValueError("usage.stop_at is a share of the usage window, 0 < x <= 1 (0.99 = 99 %)")
    ag = cfg["agents"]
    if isinstance(ag.get("silent_min"), bool) or not isinstance(ag.get("silent_min"), (int, float)) or \
            ag["silent_min"] <= 0:
        raise ValueError("agents.silent_min must be a number of minutes > 0")
    if isinstance(ag.get("tail"), bool) or not isinstance(ag.get("tail"), int) or ag["tail"] < 1:
        raise ValueError("agents.tail must be a whole number >= 1")
    for size, pct in cfg["usage"]["sizes"].items():
        if not isinstance(pct, (int, float)) or pct < 0:
            raise ValueError("usage.sizes.%s must be a number of %%UW >= 0" % size)
    if not isinstance(cfg["backup"].get("drive"), bool):
        raise ValueError("backup.drive must be true or false (the daily Drive run)")
    if int(cfg["lock"]["price"]) < 1:
        raise ValueError("lock.price must be >= 1")
    if int(cfg["orders"]["price"]) < 1:
        raise ValueError("orders.price must be >= 1")
    ids = [q["id"] for q in cfg["standing_questions"]]
    if len(set(ids)) != len(ids):
        raise ValueError("standing_questions: duplicate ids %s" % ids)
    if not isinstance(cfg.get("plugins"), list) or not all(isinstance(x, str) and x for x in cfg["plugins"]):
        raise ValueError("plugins must be a list of module names or .py paths")
    if not isinstance(cfg.get("executor"), str) or not cfg["executor"]:
        raise ValueError("executor must be a module name (depot2.executors.cli_agent)")
    if not isinstance(cfg.get("project"), dict):
        raise ValueError("project must be an object: {\"name\": ..., \"description\": ...}")
    from . import characters
    characters.check_config(cfg.get("characters"))
    check_merge(cfg["merge"])


def check_merge(m):
    """The merge queue's settings: numbers where numbers go, every command a list of strings (or null)."""
    def num(key, nullable=False):
        v = m.get(key)
        if v is None and nullable:
            return
        if isinstance(v, bool) or not isinstance(v, (int, float)) or v < 0:
            raise ValueError("merge.%s must be a number >= 0%s" % (key, " or null" if nullable else ""))
    for key in ("poll_s", "run_appear_min", "ci_timeout_min", "deploy_timeout_min", "queued_max_min",
                "github_reruns_max", "github_wait_max_min", "scan_timeout_s", "after_resolve_timeout_s",
                "smoke_timeout_s", "generate_timeout_s", "golden_timeout_s", "presync_depth", "trial_retry_min",
                "net_retries", "net_retry_s", "ci_net_reruns"):
        num(key)
    if not isinstance(m.get("trial_prefix"), str) or not m["trial_prefix"].endswith("/") or len(m["trial_prefix"]) < 2:
        raise ValueError("merge.trial_prefix must be a branch prefix ending in «/» (mq/)")
    tsa = m.get("trial_scan_args")
    if not isinstance(tsa, list) or not all(isinstance(x, str) for x in tsa) or not any("{dir}" in x for x in tsa):
        raise ValueError("merge.trial_scan_args must be a list of strings with {dir} (what the trial's push carries)")
    pats = m.get("ci_net_patterns")
    if not isinstance(pats, list) or not all(isinstance(x, str) and x for x in pats):
        raise ValueError("merge.ci_net_patterns must be a list of strings")
    auto = m.get("auto")
    if auto is not None:
        if not isinstance(auto, dict):
            raise ValueError("merge.auto must be an object (or null: no rule)")
        for rule, key in (("generated", "file"), ("snapshots", "glob"), ("golden", "file")):
            r = auto.get(rule)
            if r is None:
                continue
            if not isinstance(r, dict) or not isinstance(r.get(key), str) or not r[key] or not isinstance(
                    r.get("cmd"), list) or not r["cmd"] or not all(isinstance(x, str) for x in r["cmd"]):
                raise ValueError("merge.auto.%s must be null or {\"%s\": a path, \"cmd\": [a command]}" % (rule, key))
        if not isinstance(auto.get("union") or [], list) or not all(isinstance(x, str) and x for x in auto.get("union") or []):
            raise ValueError("merge.auto.union must be a list of path patterns")
    num("github_rerun_after_min", nullable=True)

    def cmd(key, value):
        if value is None:
            return
        if not isinstance(value, list) or not value or not all(isinstance(x, str) for x in value):
            raise ValueError("merge.%s must be a command: a list of strings%s" % (key, ", or null" if key not in (
                "scan_args",) else ""))
    for key in ("githubstatus", "site_check", "smoke"):
        cmd(key, m.get(key))
    if not isinstance(m.get("scan_args"), list) or not all(isinstance(x, str) for x in m["scan_args"]):
        raise ValueError("merge.scan_args must be a list of strings")
    if not isinstance(m.get("after_resolve"), list):
        raise ValueError("merge.after_resolve must be a list of commands")
    for c in m["after_resolve"]:
        cmd("after_resolve", c)
    for key in ("remote", "base", "ci_workflow"):
        if not isinstance(m.get(key), str) or not m[key]:
            raise ValueError("merge.%s must be a non-empty string" % key)
    for key in ("clone", "worktrees", "deploy_job"):
        if m.get(key) is not None and (not isinstance(m[key], str) or not m[key]):
            raise ValueError("merge.%s must be a non-empty string or null" % key)
    if m.get("scan") not in (None, False) and (not isinstance(m["scan"], str) or not m["scan"]):
        raise ValueError("merge.scan must be a command's path, null (not set: no push) or false (push without a scan)")
    if not isinstance(m.get("site_ok"), list) or not m["site_ok"]:
        raise ValueError("merge.site_ok must be a list of HTTP codes")
    for key in ("cd_fallback", "ci_fallback"):  # the runners' fallbacks (merge.py FALLBACKS)
        fb = m.get(key)
        if fb is None:
            continue
        if not isinstance(fb, dict) or not isinstance(fb.get("var"), str) or not fb["var"] or not all(
                isinstance(fb.get(k), list) and fb[k] and all(isinstance(x, str) and x for x in fb[k])
                for k in ("main", "fallback") + (("jobs",) if key == "ci_fallback" or "jobs" in fb else ())):
            raise ValueError("merge.%s must be null or {\"var\": a name, \"main\": [labels], \"fallback\": [labels]%s}"
                             % (key, ", \"jobs\": [job names]" if key == "ci_fallback" else ""))

