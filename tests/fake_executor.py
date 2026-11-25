#!/usr/bin/env python3
"""ok       a valid structured answer (FAKE_STRUCTURED, JSON)
tools    tool uses forever, one per assistant message (the turn-cap test)
quota    an API 429 usage-limit result
auth     "Not logged in"
crash    exit 1 with no result
budget   error_max_budget_usd
rejected a rate-limit event with status rejected
hang     sleep (the timeout test)
"""

import json
import os
import sys
import time


def emit(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def result(**kw):
    base = {"type": "result", "subtype": "success", "is_error": False, "num_turns": 2,
            "total_cost_usd": 0.0123, "result": "", "api_error_status": None,
            "usage": {"input_tokens": 3, "cache_creation_input_tokens": 100,
                      "cache_read_input_tokens": 900, "output_tokens": 50,
                      "output_tokens_details": {"thinking_tokens": 20}},
            "modelUsage": {"fake-model-1": {}}}
    base.update(kw)
    return base


def minimal(schema):
    """The smallest value that fits the schema (for end-to-end tests)."""
    if "enum" in schema:
        return schema["enum"][0]
    t = schema.get("type")
    if t == "object":
        return {k: minimal(schema["properties"][k]) for k in schema.get("required", [])}
    if t == "array":
        return [minimal(schema["items"]) for _ in range(schema.get("minItems", 0))]
    if t == "integer":
        return schema.get("minimum", 0)
    if t == "string":
        return "тест"
    return None


def main():
    mode = os.environ.get("FAKE_MODE", "ok")
    if os.environ.get("FAKE_ARGV"):
        with open(os.environ["FAKE_ARGV"], "w") as f:
            json.dump({"argv": sys.argv[1:], "stdin": sys.stdin.read(),
                       "env": sorted(k for k in os.environ if k.startswith(("CLAUDE", "ANTHROPIC")))}, f)
    emit({"type": "system", "subtype": "init", "tools": [], "mcp_servers": []})
    if mode == "rejected":
        emit({"type": "rate_limit_event", "rate_limit_info": {"status": "rejected",
                                                              "rateLimitType": "five_hour"}})
    else:
        emit({"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed", "unifiedWindows": {"seven_day": {"utilization": 0.25}}}})
    if mode == "tools":
        for i in range(200):
            emit({"type": "assistant", "message": {"id": "msg_%d" % i, "usage": {
                "input_tokens": 1, "cache_read_input_tokens": 100, "output_tokens": 10}, "content": [
                {"type": "tool_use", "name": "Read", "input": {"file_path": "x"}}]}})
            time.sleep(0.05)
        emit(result())
        return 0
    if mode == "hang":
        time.sleep(60)
        return 0
    if mode == "crash":
        sys.stderr.write("boom: something broke\n")
        return 1
    if mode == "quota":
        emit(result(is_error=True, api_error_status=429, result="usage limit reached"))
        return 1
    if mode == "auth":
        emit(result(is_error=True, terminal_reason="api_error", result="Not logged in · Please run /login",
                    total_cost_usd=0))
        return 1
    if mode == "budget":
        emit(result(is_error=True, subtype="error_max_budget_usd", errors=["Reached maximum budget"]))
        return 1
    if os.environ.get("FAKE_STRUCTURED"):
        structured = json.loads(os.environ["FAKE_STRUCTURED"])
    else:
        structured = minimal(json.loads(sys.argv[sys.argv.index("--json-schema") + 1]))
    emit({"type": "assistant", "message": {"id": "msg_so", "content": [
        {"type": "tool_use", "name": "StructuredOutput", "input": structured}]}})
    emit(result(structured_output=structured))
    return 0


if __name__ == "__main__":
    sys.exit(main())
