#!/usr/bin/env python3
"""A stand-in for gh in the merge-queue tests: no network. Its state is a JSON file, $FAKE_GH_STATE:

- "bare": the local bare repository that plays GitHub's; "git": the real git binary;
- "prs": {"301": {"number", "title", "state", "headRefName", "baseRefName", "isDraft", "body"}}: `pr view` adds
  headRefOid (the bare's branch head; none without a "bare": the marks' tests read only the body) and mergeCommit;
  `pr merge --merge --match-head-commit SHA` makes the
  merge commit in the bare (refused when the head moved, or the PR is in "merge_refuse"); `pr list --state all|open|merged`
  lists them (number, headRefName, state), the newest first;
- `run cancel <id>`: the run reads "cancelled" (completed; its queued deploy job cancelled, never run) until `run rerun`,
  after "cancel_lag" more `run list` calls (the cancel lands late); refused for a completed run or when "cancel_refuse";
- "runners": the repository's self-hosted runners (`api repos/…/actions/runners`), as GitHub gives them;
- "variables": the repository variables, `variable set <name> --body <value>` (refused once "variable_refuse_after"
  sets went through: 0 refuses every one);
- "usage_runs", "usage_jobs", "artifacts": what the Actions minutes and storage read;
- "failed_log": {"<branch>": text}: what `run view <id> --log-failed` prints for a run on that branch;
Every call is a JSON line in $FAKE_GH_LOG. Its "now" is the tests' clock ($FAKE_GH_NOW).
"""

import datetime
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

T0 = "2015-11-12T00:00:00Z"
T1 = "2015-11-12T00:03:00Z"
STATES = {  # state → (run status, run conclusion)
    "queued": ("queued", ""),
    "in_progress": ("in_progress", ""),
    "stuck": ("in_progress", ""),
    "success": ("completed", "success"),
    "failure": ("completed", "failure"),
    "never_started": ("completed", "failure"),
    "deploy_skipped": ("completed", "success"),
    "deploy_stuck": ("in_progress", ""),
    "cancelled": ("completed", "cancelled"),
    "ci_stuck": ("in_progress", ""),
}
CD_LABELS = ["self-hosted", "primary"]  # the deploy job's runs-on: CD_RUNNER
CI_LABELS = ["self-hosted", "primary"]


def now_z(minutes_ago=0):
    """The tests' clock ($FAKE_GH_NOW: a file with the epoch seconds the merge queue's fake clock shows), never
    the real one: a job's age is then exactly what the test says, on any machine.
    """
    path = os.environ.get("FAKE_GH_NOW")
    if path:
        with Path(path).open() as f:
            now = datetime.datetime.fromtimestamp(float(f.read()), datetime.UTC)
    else:
        now = datetime.datetime.now(datetime.UTC)
    return (now - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%SZ")


def ran(name, concl):
    return {
        "name": name,
        "status": "completed",
        "conclusion": concl,
        "started_at": T0,
        "completed_at": T1,
        "created_at": T0,
        "runner_name": "runner-1",
        "labels": ["self-hosted"],
        "steps": [{"name": "run"}],
    }


def jobs_for(state, main):
    deploy = "Deploy"
    if state == "success":
        return [ran("Test and build", "success")] + ([ran(deploy, "success")] if main else [])
    if state == "deploy_skipped":
        j = ran(deploy, "skipped")
        j.update(runner_name=None, steps=[])
        return [ran("Test and build", "success"), j]
    if state == "failure":
        return [ran("Test and build", "failure")]
    if state == "in_progress":
        return [
            {
                "name": "Test and build",
                "status": "in_progress",
                "conclusion": None,
                "started_at": T0,
                "created_at": now_z(),
                "runner_name": "runner-1",
                "labels": [],
                "steps": [{"name": "run"}],
            }
        ]
    if state == "queued":
        return [
            {
                "name": "Test and build",
                "status": "queued",
                "conclusion": None,
                "started_at": None,
                "created_at": now_z(),
                "runner_name": None,
                "labels": [],
                "steps": [],
            }
        ]
    if state == "stuck":
        return [
            {
                "name": "Test and build",
                "status": "queued",
                "conclusion": None,
                "started_at": None,
                "created_at": now_z(60.5),
                "runner_name": None,
                "labels": [],
                "steps": [],
            }
        ]
    if state == "deploy_stuck":
        return [
            ran("Test and build", "success"),
            {
                "name": deploy,
                "status": "queued",
                "conclusion": None,
                "started_at": None,
                "created_at": now_z(20),
                "runner_name": None,
                "labels": CD_LABELS,
                "steps": [],
            },
        ]
    if state == "ci_stuck":
        return [
            {
                "name": "Test and build",
                "status": "queued",
                "conclusion": None,
                "started_at": None,
                "created_at": now_z(20),
                "runner_name": None,
                "labels": CI_LABELS,
                "steps": [],
            }
        ]
    if state == "cancelled":
        return [
            ran("Test and build", "success"),
            {
                "name": deploy,
                "status": "completed",
                "conclusion": "cancelled",
                "started_at": None,
                "created_at": T0,
                "completed_at": T1,
                "runner_name": None,
                "labels": CD_LABELS,
                "steps": [],
            },
        ]
    if state == "never_started":
        return [
            ran("Test and build", "success"),
            {
                "name": deploy if main else "Lint and typecheck",
                "status": "queued",
                "conclusion": None,
                "started_at": T0,
                "completed_at": None,
                "created_at": T0,
                "runner_name": None,
                "labels": [],
                "steps": [],
            },
        ]
    return []


def opt(argv, name):
    return argv[argv.index(name) + 1] if name in argv else None


def git(d, *args, cwd=None):
    return subprocess.run([d["git"], *list(args)], cwd=cwd, capture_output=True, text=True)


def head_of(d, branch):
    p = git(d, "--git-dir", d["bare"], "rev-parse", "--verify", "-q", "refs/heads/" + branch)
    return p.stdout.strip() or None


def out(obj):
    sys.stdout.write(json.dumps(obj) + "\n")
    return 0


def fail(msg, code=1):
    sys.stderr.write(msg + "\n")
    return code


def pr_view(d, n, argv=()):
    pr = d["prs"].get(str(n))
    if pr is None:
        return fail(f"no pull requests found for {n}")
    pr = dict(pr)
    seq = d.get("view_states", {}).get(str(n))
    if seq and "state" in (opt(list(argv), "--json") or "").split(","):
        pr["state"] = seq.pop(0) if len(seq) > 1 else seq[0]
    pr.setdefault("url", f"https://github.com/x/y/pull/{n}")
    pr.setdefault("mergeCommit", None)
    if pr["state"] == "OPEN" and d.get("bare"):
        pr["headRefOid"] = head_of(d, pr["headRefName"])
    return out(pr)


def pr_list(d, argv):
    """`gh pr list --state all --json number,headRefName,state`: every PR, the newest first."""
    want = opt(argv, "--state") or "open"
    prs = sorted(d["prs"].values(), key=lambda p: -int(p["number"]))
    return out(
        [
            {"number": p["number"], "headRefName": p["headRefName"], "state": p["state"]}
            for p in prs
            if want == "all" or p["state"] == want.upper()
        ]
    )


def pr_merge(d, n, argv):
    pr = d["prs"].get(str(n))
    if pr is None or pr["state"] != "OPEN":
        return fail(f"pull request #{n} is not open")
    if int(n) in d.get("merge_refuse", []):
        return fail("GraphQL: Pull request is not mergeable")
    want = opt(argv, "--match-head-commit")
    head = head_of(d, pr["headRefName"])
    if want and want != head:
        return fail(f"head branch was modified. Review and try the merge again (head {head}, wanted {want})")
    tmp = tempfile.mkdtemp(prefix="fake-gh-merge-")
    try:
        ident = ["-c", "user.name=fake-gh", "-c", "user.email=gh@localhost"]
        for args in (["clone", "-q", d["bare"], tmp],):
            git(d, *args)
        git(d, "checkout", "-q", pr["baseRefName"], cwd=tmp)
        p = git(
            d,
            *(
                [
                    *ident,
                    "merge",
                    "--no-ff",
                    "-q",
                    "origin/" + pr["headRefName"],
                    "-m",
                    "Merge pull request #{} from x/{}".format(n, pr["headRefName"]),
                ]
            ),
            cwd=tmp,
        )
        if p.returncode != 0:
            return fail(f"merge conflict: {p.stdout}" + p.stderr)
        git(d, "push", "-q", "origin", pr["baseRefName"], cwd=tmp)
        sha = git(d, "rev-parse", "HEAD", cwd=tmp).stdout.strip()
    finally:
        subprocess.run(["rm", "-rf", tmp])
    pr["state"] = "MERGED"
    pr["headRefOid"] = head
    pr["mergeCommit"] = {"oid": sha}
    return 0


def run_list(d, argv):
    branch, sha = opt(argv, "--branch"), opt(argv, "--commit")
    key = f"{branch}@{sha}"
    ids = d.setdefault("ids", {})
    r = d.get("runs", {}).get(str(ids.get(key)))
    if r is not None and r.get("cancelled") == r["attempt"]:  # cancelled: the script waits for its rerun
        if d.get("cancel_lag"):
            d["cancel_lag"] -= 1  # the cancel has not landed yet: the run as it was
        else:
            r["state"] = "cancelled"
        state, rid = r["state"], ids[key]
        status, concl = STATES[state]
        return out(
            [
                {
                    "databaseId": rid,
                    "status": status,
                    "conclusion": concl,
                    "headSha": sha,
                    "event": "push" if branch == "main" else "pull_request",
                    "createdAt": T0,
                    "attempt": r["attempt"],
                    "url": "https://github.com/x/y/actions/runs/%d" % rid,  # noqa: UP031
                }
            ]
        )
    script = d["ci"].get(branch) or ["success"]
    pos = d.setdefault("pos", {}).get(branch, 0)
    item = script[min(pos, len(script) - 1)]
    hold = item.endswith("!")
    state = item.rstrip("!")
    if not hold:
        d["pos"][branch] = pos + 1
    if state == "none":
        return out([])
    if key not in ids:
        ids[key] = 9000 + len(ids)
    rid = ids[key]
    runs = d.setdefault("runs", {})
    r = runs.setdefault(str(rid), {"branch": branch, "sha": sha, "attempt": 1})
    r["state"] = state
    status, concl = STATES[state]
    return out(
        [
            {
                "databaseId": rid,
                "status": status,
                "conclusion": concl,
                "headSha": sha,
                "event": "push" if branch == "main" else "pull_request",
                "createdAt": T0,
                "attempt": r["attempt"],
                "url": "https://github.com/x/y/actions/runs/%d" % rid,  # noqa: UP031
            }
        ]
    )


def rerun(d, rid):
    r = d.get("runs", {}).get(str(rid))
    if r is None:
        return fail(f"run {rid} not found")
    if STATES.get(r["state"], ("",))[0] != "completed":
        return fail(f"run {rid} cannot be rerun; its workflow file may be broken or it is still running")
    r["attempt"] += 1
    d["pos"][r["branch"]] = d["pos"].get(r["branch"], 0) + 1
    return 0


def cancel(d, rid):
    r = d.get("runs", {}).get(str(rid))
    if r is None:
        return fail(f"run {rid} not found")
    if d.get("cancel_refuse"):
        return fail("HTTP 500: cancel refused")
    if STATES.get(r["state"], ("",))[0] == "completed":
        return fail("Cannot cancel a workflow run that is completed")
    r["cancelled"] = r["attempt"]
    return 0


def run_view(d, rid, argv):
    r = d.get("runs", {}).get(str(rid))
    if r is None:
        return fail(f"run {rid} not found")
    if "--log-failed" in argv:
        sys.stdout.write(d.get("failed_log", {}).get(r["branch"], ""))
        return 0
    return out({"databaseId": int(rid), "status": STATES[r["state"]][0], "conclusion": STATES[r["state"]][1]})


def variable_set(d, argv):
    left = d.get("variable_refuse_after")
    if left is not None:
        if left <= 0:
            return fail("HTTP 403: Resource not accessible by integration")
        d["variable_refuse_after"] = left - 1
    d.setdefault("variables", {})[argv[2]] = opt(argv, "--body")
    return 0


def api(d, argv):
    path = next(a for a in argv[1:] if not a.startswith("--"))
    if path.split("?")[0].endswith("/actions/runners"):
        return out([{"total_count": len(d.get("runners", [])), "runners": d.get("runners", [])}])
    if "/actions/runs/" in path and "/jobs" in path:
        rid = path.split("/actions/runs/")[1].split("/")[0]
        if rid in d.get("usage_jobs", {}):
            return out([{"jobs": d["usage_jobs"][rid]}])
        r = d.get("runs", {}).get(rid)
        if r is None:
            return fail("HTTP 404: Not Found")
        return out([{"total_count": 1, "jobs": jobs_for(r["state"], r["branch"] == "main")}])
    if "/actions/runs?" in path:
        return out([{"workflow_runs": d.get("usage_runs", [])}])
    if "/actions/artifacts" in path:
        return out([{"artifacts": d.get("artifacts", [])}])
    return fail(f"fake gh: no such endpoint {path}")


def main():
    argv = sys.argv[1:]
    path = os.environ["FAKE_GH_STATE"]
    with Path(path).open() as f:
        d = json.load(f)
    log = os.environ.get("FAKE_GH_LOG")
    if log:
        with Path(log).open("a") as f:
            f.write(json.dumps(argv) + "\n")
    nf = d.get("net_fail", {})
    key = " ".join(argv[:2])
    if nf.get(key):
        nf[key] -= 1
        rc = fail("error connecting to api.github.com\ncheck your internet connection or https://githubstatus.com")
    elif argv[:2] == ["pr", "view"]:
        rc = pr_view(d, argv[2], argv)
    elif argv[:2] == ["pr", "merge"]:
        rc = pr_merge(d, argv[2], argv)
    elif argv[:2] == ["pr", "list"]:
        rc = pr_list(d, argv)
    elif argv[:2] == ["run", "list"]:
        rc = run_list(d, argv)
    elif argv[:2] == ["run", "rerun"]:
        rc = rerun(d, argv[2])
    elif argv[:2] == ["run", "cancel"]:
        rc = cancel(d, argv[2])
    elif argv[:2] == ["run", "view"]:
        rc = run_view(d, argv[2], argv)
    elif argv[:2] == ["variable", "set"]:
        rc = variable_set(d, argv)
    elif argv[:1] == ["api"]:
        rc = api(d, argv)
    else:
        rc = fail(f"fake gh: unknown command {argv}")
    with Path(path + ".tmp").open("w") as f:
        json.dump(d, f)
    Path(path + ".tmp").replace(path)
    return rc


if __name__ == "__main__":
    sys.exit(main())
