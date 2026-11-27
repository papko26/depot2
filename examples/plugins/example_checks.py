"""An example depot2 plugin: a project's own checks, as hooks (see depot2/hooks.py for the interface).

Name it in the depot's config.json to use it:

    "plugins": ["examples/plugins/example_checks.py"]

It answers three hooks for an imaginary project whose smoke check prints one JSON object per line,
{"check": "home page", "ok": true} or {"check": "login", "ok": false, "error": "..."}, keeps its test snapshots in
tests/__snapshots__/<name>.snap, and wants the merge report to say how long the queue's step took. Copy it, rename it,
and replace the bodies with what your project checks.
"""

import json
import os
import time


def smoke_verdict(code, out):
    """(ok, summary) from the smoke check's lines: every check ok, at least one, and exit 0."""
    checks = []
    for line in (out or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and "check" in obj:
            checks.append(obj)
    if not checks:
        return None  # not this plugin's format: the next plugin, or the exit code, decides
    bad = [c for c in checks if not c.get("ok")]
    ok = code == 0 and not bad
    parts = ["checks %d/%d ok" % (len(checks) - len(bad), len(checks))]  # noqa: UP031
    parts += ["{}: {}".format(c.get("check"), str(c.get("error") or "failed")[:80]) for c in bad[:3]]
    if code is None:
        parts.append("timed out")
    elif code:
        parts.append("exit %d" % code)  # noqa: UP031
    return ok, ", ".join(parts)


def snapshot_test(path):
    """tests/__snapshots__/parser.test.py.snap -> tests/parser.test.py (None for any other path)."""
    head, name = os.path.split(path)
    if os.path.basename(head) != "__snapshots__" or not name.endswith(".snap"):
        return None
    return os.path.join(os.path.dirname(head), name[: -len(".snap")])


def after_merge(cfg, entry):
    """One line for the merge report: when the merge queue finished this entry."""
    return {"line": "finished at {}".format(time.strftime("%H:%M")), "pr": entry.get("pr")}


def round_context(cfg, kind):
    """Nothing extra for a team round in this example; a project could add its glossary here."""
    return
