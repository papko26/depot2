"""The bundled executor: a command-line agent runner that streams JSON events and logs each run as JSONL.

A capped call (`run_call`) starts `<executor_bin> -p` with the prompts, the answer's schema and the caps on its command
line, reads its event stream (tool uses for the turn cap, the rate-limit window, the final result with its usage) and
kills the process group at the turn cap or the timeout. Auth, quota and API errors are fatal; nothing is retried.

A run's log (`find_transcript`, `read_transcript`): `<agents.projects>/<project>/<session>/subagents/agent-<id>.jsonl`
and its `.meta.json`, read incrementally and cached by path.

This module is the only place that knows the runner's flags and log format; drop it and point `executor` at another.
"""

import collections
import contextlib
import datetime
import glob
import json
import os
import queue
import re
import signal
import subprocess
import threading
import time
from typing import TypedDict

from .. import effort
from ..store import read_json
from . import CallFailed, FatalError

ARG_MAX = 80
ARG_KEYS = (
    "description",
    "file_path",
    "notebook_path",
    "pattern",
    "path",
    "url",
    "query",
    "skill",
    "command",
    "subagent_type",
    "action",
)
NO_ARG = ("SubagentHandback", "SendMessage")
RE_AGENT_ID = re.compile(r"^(?:agent-)?([A-Za-z0-9_-]{4,64})$")


def _error(msg):
    from ..agents import AgentError

    return AgentError(msg)


FATAL_TEXT = re.compile(
    r"not logged in|/login|usage limit|rate.?limit|quota|credit balance|billing|overloaded|"
    r"authenticat|unauthori[sz]ed|invalid api key|issue with the selected model",
    re.IGNORECASE,
)

STRIP_ENV_PREFIXES = ("CLAUDE_CODE_",)
STRIP_ENV = ("CLAUDECODE", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")


def child_env():
    """The caller's environment minus any parent-session and API-key variables."""
    return {k: v for k, v in os.environ.items() if not k.startswith(STRIP_ENV_PREFIXES) and k not in STRIP_ENV}


def build_argv(spec, executor_bin):
    argv = [
        executor_bin,
        "-p",
        "--safe-mode",
        "--restricted",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-prompts",
        "none",
    ]
    if spec.model:
        argv += ["--model", spec.model]
    if spec.effort:
        argv += ["--effort", spec.effort]
    argv += [
        "--system-prompt-file",
        spec.system_path,
        "--json-schema",
        json.dumps(spec.schema, ensure_ascii=False, separators=(",", ":")),
        "--max-budget-usd",
        f"{spec.budget_usd:.2f}",
        "--tools",
        ",".join(spec.tools),
    ]
    if spec.tools:
        # Variadic options go last.
        if spec.add_dirs:
            argv += ["--add-dir", *spec.add_dirs]
        if spec.deny:
            argv += ["--disallowedTools", *spec.deny]
    return argv


def _kill(proc):
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(timeout=5)


def _usage(result):
    u = (result or {}).get("usage") or {}
    return {
        "input": u.get("input_tokens", 0),
        "cache_write": u.get("cache_creation_input_tokens", 0),
        "cache_read": u.get("cache_read_input_tokens", 0),
        "output": u.get("output_tokens", 0),
        "thinking": (u.get("output_tokens_details") or {}).get("thinking_tokens", 0),
    }


USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")


def _sum_usage(usages):
    return {k: sum(u.get(k, 0) or 0 for u in usages) for k in USAGE_KEYS}


def total_tokens(usage):
    """input + cache write + cache read + output: what a call made the model process."""
    u = usage or {}
    return u.get("input", 0) + u.get("cache_write", 0) + u.get("cache_read", 0) + u.get("output", 0)


def max_utilization(rate):
    windows = ((rate or {}).get("unifiedWindows") or {}).values()
    vals = [w.get("utilization") for w in windows if isinstance(w.get("utilization"), (int, float))]
    return max(vals) if vals else None


def week_utilization(rate):
    w = ((rate or {}).get("unifiedWindows") or {}).get("seven_day") or {}
    u = w.get("utilization")
    return u if isinstance(u, (int, float)) and not isinstance(u, bool) else None


def run_call(spec, cfg=None, validate=None, executor_bin=None):
    """Run one call. Returns a result dict; raises CallFailed or FatalError (with .result)."""
    if executor_bin is None:
        executor_bin = (cfg or {}).get("executor_bin") if isinstance(cfg, dict) else cfg
    if not executor_bin:
        raise FatalError("executor_bin is not set: the runner's command (config executor_bin)")
    argv = build_argv(spec, executor_bin)
    t0 = time.monotonic()
    raw = open(spec.raw_path, "w", encoding="utf-8") if spec.raw_path else None  # noqa: SIM115
    try:
        try:
            proc = subprocess.Popen(
                argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=spec.cwd,
                env=child_env(),
                start_new_session=True,
            )
        except OSError as e:
            raise FatalError(f"cannot start {executor_bin}: {e}")  # noqa: B904

        def feed():
            try:
                proc.stdin.write(spec.user.encode("utf-8"))
                proc.stdin.close()
            except (BrokenPipeError, OSError):
                pass

        lines = queue.Queue()

        def read_out():
            for line in proc.stdout:
                lines.put(line)
            lines.put(None)

        err_chunks = []

        def read_err():
            for chunk in iter(lambda: proc.stderr.read(4096), b""):
                if sum(len(c) for c in err_chunks) < 65536:
                    err_chunks.append(chunk)

        threads = [threading.Thread(target=f, daemon=True) for f in (feed, read_out, read_err)]
        for t in threads:
            t.start()

        result = None
        rate = None
        msg_usage = {}  # per assistant message: the tokens of a call killed before its result
        tool_msgs = []
        tool_calls = 0
        kill_reason = None
        deadline = t0 + spec.timeout_s
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                kill_reason = "timeout after %ds" % spec.timeout_s  # noqa: UP031
                _kill(proc)
                break
            try:
                line = lines.get(timeout=min(left, 1.0))
            except queue.Empty:
                continue
            if line is None:
                break
            text = line.decode("utf-8", "replace")
            if raw:
                raw.write(text)
            try:
                ev = json.loads(text)
            except ValueError:
                continue
            typ = ev.get("type")
            if typ == "assistant":
                msg = ev.get("message") or {}
                if msg.get("usage"):
                    msg_usage[msg.get("id") or ev.get("uuid")] = msg["usage"]
                uses = [
                    b
                    for b in (msg.get("content") or [])
                    if b.get("type") == "tool_use" and b.get("name") != "StructuredOutput"
                ]
                if uses:
                    tool_calls += len(uses)
                    mid = msg.get("id") or ev.get("uuid")
                    if mid not in tool_msgs:
                        tool_msgs.append(mid)
                    if len(tool_msgs) > spec.max_tool_turns:
                        kill_reason = "tool-turn cap %d reached" % spec.max_tool_turns  # noqa: UP031
                        _kill(proc)
                        break
            elif typ == "rate_limit_event":
                rate = ev.get("rate_limit_info") or rate
            elif typ == "result":
                result = ev
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            _kill(proc)
        for t in threads[1:]:
            t.join(timeout=5)
        for f in (proc.stdout, proc.stderr):
            f.close()
    finally:
        if raw:
            raw.close()

    stderr = b"".join(err_chunks).decode("utf-8", "replace")
    out = {
        "call": spec.call_id,
        "model": spec.model,
        "effort": spec.effort,
        "duration_s": round(time.monotonic() - t0, 1),
        "cost_usd": (result or {}).get("total_cost_usd"),
        "usage": _usage(result) if result else _usage({"usage": _sum_usage(msg_usage.values())}),
        "usage_from_stream": result is None,
        "num_turns": (result or {}).get("num_turns"),
        "tool_turns": len(tool_msgs),
        "tool_calls": tool_calls,
        "models": sorted(((result or {}).get("modelUsage") or {}).keys()),
        "rate_limit": rate,
        "utilization": max_utilization(rate),
        "week_utilization": week_utilization(rate),
        "exit_code": proc.returncode,
        "status": "ok",
        "reason": None,
        "structured": None,
    }

    def fail(cls, reason):
        out["status"] = "fatal" if cls is FatalError else "failed"
        out["reason"] = reason
        raise cls(reason, out)

    if result is None:
        if kill_reason:
            fail(CallFailed, kill_reason)
        tail = stderr.strip().splitlines()[-3:]
        fail(FatalError, "the runner exited {} without a result: {}".format(proc.returncode, " | ".join(tail)))
    if kill_reason:
        fail(CallFailed, kill_reason)
    if rate and rate.get("status") not in (None, "allowed", "allowed_warning"):
        fail(FatalError, "usage limit: {} ({})".format(rate.get("status"), rate.get("rateLimitType")))
    if result.get("is_error"):
        sub = result.get("subtype") or ""
        text = str(result.get("result") or "") + " " + " ".join(map(str, result.get("errors") or []))
        if sub == "error_max_budget_usd":
            fail(
                CallFailed,
                "budget cap {} reached (--max-budget-usd {:.2f})".format(
                    f"{spec.budget_mtok:.4g} MTok" if spec.budget_mtok is not None else "of the call", spec.budget_usd
                ),
            )
        if result.get("api_error_status") or result.get("terminal_reason") == "api_error" or FATAL_TEXT.search(text):
            fail(FatalError, "API error {}: {}".format(result.get("api_error_status"), text.strip()[:300]))
        if sub.startswith("error_max"):
            fail(CallFailed, f"{sub}: {text.strip()[:200]}")
        fail(FatalError, f"runner error {sub}: {text.strip()[:300]}")
    structured = result.get("structured_output")
    if structured is None:
        fail(CallFailed, "no structured output")
    if validate is not None:
        errs = validate(structured, spec.schema)
        if errs:
            out["structured"] = structured
            fail(CallFailed, "schema: " + "; ".join(errs[:5]))
    out["structured"] = structured
    return out


# --- the transcript ------------------------------------------------------------------------------------------------


class _TranscriptCacheEntry(TypedDict):
    key: tuple[int, int, tuple[int, int] | None, int]
    ino: int
    tail: int
    off: int
    acc: dict[str, object]
    out: dict[str, object]


_CACHE: dict[str, _TranscriptCacheEntry] = {}


def _short(text, n=ARG_MAX):
    t = " ".join(str(text).split())
    home = os.path.expanduser("~")
    if home and home != "~":
        t = t.replace(home + "/", "~/")
    return t if len(t) <= n else t[: n - 1] + "…"


def action_arg(name, inp):
    """A tool call's short argument: its description, its file, its pattern… — never a prompt or a message."""
    if name in NO_ARG or not isinstance(inp, dict):
        return ""
    for k in ARG_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v.strip():
            return _short(v)
    return ""


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


def meta_path(transcript):
    return transcript[: -len(".jsonl")] + ".meta.json" if transcript.endswith(".jsonl") else transcript + ".meta.json"


def _acc(tail):
    """What a reading sums as it goes: the message ids seen (the spend once per message), the tool calls seen, the
    tokens by kind, the last `tail` actions, the first and last times.
    """
    return {
        "seen": set(),
        "tok": dict.fromkeys(effort.WEIGHTS, 0),
        "acts": collections.deque(maxlen=tail),
        "calls": set(),
        "n_calls": 0,
        "first": None,
        "last": None,
    }


def _take(acc, line):
    """One line of a transcript into the sums."""
    try:
        e = json.loads(line)
    except ValueError:
        return
    if not isinstance(e, dict):
        return
    ts = e.get("timestamp") if isinstance(e.get("timestamp"), str) else None
    if ts:
        acc["first"] = acc["first"] or ts
        acc["last"] = ts
    msg = e.get("message")
    if not isinstance(msg, dict):
        return
    u = msg.get("usage")
    if isinstance(u, dict):  # the spend, as effort.from_transcript sums it
        mid = msg.get("id") or e.get("requestId") or e.get("uuid")
        if mid is None or mid not in acc["seen"]:
            acc["seen"].add(mid)
            for api, k in effort.API_KEYS.items():
                acc["tok"][k] += u.get(api) or 0
    if msg.get("role") != "assistant" or not isinstance(msg.get("content"), list):
        return
    for b in msg["content"]:
        if not isinstance(b, dict) or b.get("type") != "tool_use":
            continue
        bid = b.get("id")
        if bid is not None and bid in acc["calls"]:
            continue
        acc["calls"].add(bid)
        acc["n_calls"] += 1
        acc["acts"].append(
            {"at": ts, "name": str(b.get("name") or "?"), "arg": action_arg(b.get("name"), b.get("input"))}
        )


def read_transcript(path, tail=10):
    """One pass over a transcript: {turns, mtok, first_at, last_at, actions: [{at, name, arg}] (the last `tail`),
    actions_n, finished, stopped_by_user, model, effort}; None when the file is not there. Only the numbers, the
    times and the tool calls' names and short arguments are read. A file read before and grown since is read on
    from where that read stopped (_CACHE); a last line without its end is read only when it is whole JSON.
    """
    path = os.path.expanduser(path)
    try:
        st = os.stat(path)
    except OSError:
        return None
    mp = meta_path(path)
    try:
        mst = os.stat(mp)
        mkey = (mst.st_size, mst.st_mtime_ns)
    except OSError:
        mkey = None
    key = (st.st_size, st.st_mtime_ns, mkey, tail)
    hit = _CACHE.get(path)
    if hit and hit["key"] == key and hit["ino"] == st.st_ino:
        return hit["out"]
    if hit and hit["ino"] == st.st_ino and hit["tail"] == tail and st.st_size >= hit["off"]:
        acc, off = hit["acc"], hit["off"]
    else:
        acc, off = _acc(tail), 0
    with open(path, "rb") as f:
        f.seek(off)
        chunk = f.read()
    whole = chunk.rfind(b"\n") + 1
    for line in chunk[:whole].split(b"\n"):
        if line.strip():
            _take(acc, line.decode("utf-8", errors="replace"))
    rest = chunk[whole:]
    if rest.strip():  # a last line still being written waits for the next read, unless it is whole already
        try:
            json.loads(rest.decode("utf-8", errors="replace"))
        except ValueError:
            rest = b""
        else:
            _take(acc, rest.decode("utf-8", errors="replace"))
    off += whole + len(rest)
    meta = read_json(mp) if mkey is not None else None
    meta = meta if isinstance(meta, dict) else {}
    out = {
        "turns": len(acc["seen"]),
        "mtok": round(effort.weighted_mtok(acc["tok"]), 4),
        "first_at": acc["first"],
        "last_at": acc["last"],
        "actions": list(acc["acts"]),
        "actions_n": acc["n_calls"],
        "finished": meta.get("finished") is True,
        "stopped_by_user": meta.get("stoppedByUser") is True,
        "model": meta.get("model"),
        "effort": meta.get("effort"),
    }
    _CACHE[path] = {"key": key, "ino": st.st_ino, "tail": tail, "off": off, "acc": acc, "out": out}
    return out


def find_transcript(cfg, agent_id):
    """The log of a run by its id (the runner's agent id): one file, or AgentError."""
    m = RE_AGENT_ID.match(str(agent_id or "").strip())
    if not m:
        raise _error(f"an agentId is letters and digits (the runner's, e.g. a0f1c2d3e4b5a6978), got {agent_id!r}")
    if not (cfg.get("agents") or {}).get("projects"):
        raise _error("agents.projects is not set: the dir the runner keeps its runs' logs in (or --transcript PATH)")
    root = os.path.expanduser(cfg["agents"]["projects"])
    name = f"agent-{m.group(1)}.jsonl"
    found = sorted(
        set(
            glob.glob(os.path.join(root, "*", "*", "subagents", name))
            + glob.glob(os.path.join(root, "*", "*", "subagents", "**", name), recursive=True)
        )
    )
    if not found:
        raise _error(f"no transcript {name} under {root}/*/*/subagents/ (yet?): --transcript PATH")
    if len(found) > 1:
        raise _error(
            "%d transcripts named %s: %s; say which with --transcript PATH"  # noqa: UP031
            % (len(found), name, ", ".join(_tilde(p) for p in found))
        )
    return found[0]


def _tilde(path):
    home = os.path.expanduser("~")
    path = os.path.abspath(os.path.expanduser(path))
    return "~" + path[len(home) :] if home not in ("", "~", "/") and path.startswith(home + "/") else path
