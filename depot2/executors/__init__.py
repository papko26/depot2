"""Executors: the outside runners that do a car's work or answer a team round's call.

depot2 never thinks for itself. Two kinds of work leave the process, and both go through the executor named in the
config (`executor`: a module name, `depot2.executors.cli_agent` by default):

- a capped call (`run_call`): a team round's pass or a reflection. depot2 hands over a `CallSpec` (the prompts, the
  answer's JSON schema, the read-only tools and dirs, the caps) and gets back a result dict, or `CallFailed` (this call
  failed: refunded, the round goes on) or `FatalError` (stop everything: never retried);
- a run's log (`find_transcript`, `read_transcript`): `agents attach` links a car's run to the log its executor
  writes, and `agents show|list` read that log on demand: the last actions, the tokens, done or not.

An executor module defines `run_call(spec, cfg, validate=None)`, `find_transcript(cfg, run_id)` and
`read_transcript(path, tail=10)`. The adapter shipped here (`cli_agent.py`) runs a command-line agent that streams
JSON events on stdout and writes a JSONL log per run; it is one module, and the tests reach it only through
`tests/fake_executor.py`. Another runner is another module with the same three functions.
"""

import importlib

from .. import effort

DEFAULT = "depot2.executors.cli_agent"


class FatalError(RuntimeError):
    """Stop everything: auth, quota, API, or a broken runner. Never retried."""

    def __init__(self, msg, result=None):
        RuntimeError.__init__(self, msg)
        self.result = result


class CallFailed(RuntimeError):
    """This call failed (its cap, timeout, or a bad answer). Refund it; the round goes on."""

    def __init__(self, msg, result=None):
        RuntimeError.__init__(self, msg)
        self.result = result


class CallSpec:
    """One capped call: what the executor runs and within which limits."""

    def __init__(self, call_id, model, effort, system_path, user, schema, tools=(),
                 max_tool_turns=0, budget_usd=1.0, timeout_s=300, cwd=None, add_dirs=(),
                 deny=(), raw_path=None, budget_mtok=None):
        self.call_id = call_id
        self.model = model
        self.effort = effort
        self.system_path = system_path
        self.user = user
        self.schema = schema
        self.tools = list(tools)
        self.max_tool_turns = int(max_tool_turns)
        self.budget_usd = float(budget_usd)  # the runner's own cap, in its currency
        self.budget_mtok = None if budget_mtok is None else float(budget_mtok)  # the depot's cap, MTok
        self.timeout_s = float(timeout_s)
        self.cwd = cwd
        self.add_dirs = list(add_dirs)
        self.deny = list(deny)
        self.raw_path = raw_path


def adapter(cfg):
    """The executor module the config names."""
    name = (cfg or {}).get("executor") or DEFAULT
    try:
        return importlib.import_module(name)
    except ImportError as e:
        raise FatalError(f"executor {name}: {e}")  # noqa: B904


def run_call(spec, cfg, validate=None):
    return adapter(cfg).run_call(spec, cfg, validate=validate)


def find_transcript(cfg, run_id):
    return adapter(cfg).find_transcript(cfg, run_id)


def read_transcript(cfg, path, tail=10):
    return adapter(cfg).read_transcript(path, tail)


def total_tokens(usage):
    """input + cache write + cache read + output: what a call made the model process."""
    u = usage or {}
    return u.get("input", 0) + u.get("cache_write", 0) + u.get("cache_read", 0) + u.get("output", 0)


def describe(res, cfg):
    """One line per call: what it was, its spend (MTok and the window's share), the tokens and turns it took."""
    u = res.get("usage") or {}
    util = res.get("utilization")
    mtok = res.get("mtok") if res.get("mtok") is not None else effort.call_mtok(res, cfg)
    return ("%-30s %-16s %-6s %6.1fs  %s%s  %s%d tok (in %d, cache r %d w %d, out %d)  turns %s, "  # noqa: UP031
            "tool turns %d%s  %s%s" % (
                res.get("call"), res.get("model"), res.get("effort") or "-", res.get("duration_s") or 0,
                "≈" if res.get("usage_from_stream") or effort.by_cost_only(res) else "", effort.fmt(mtok, cfg),
                "≈" if res.get("usage_from_stream") else "", total_tokens(u),
                u.get("input", 0), u.get("cache_read", 0), u.get("cache_write", 0), u.get("output", 0),
                res.get("num_turns"), res.get("tool_turns", 0),
                ("  window %.0f%%" % (util * 100)) if util is not None else "",
                res.get("status"), (": " + res["reason"]) if res.get("reason") else ""))
