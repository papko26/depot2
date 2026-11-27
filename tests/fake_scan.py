#!/usr/bin/env python3
"""A stand-in for the private scan in the push tests. It knows one "private" string, $FAKE_SCAN_HIT.

- `--root DIR …` (what the push carries, exported by the tool): every file under DIR.
- `--repo DIR …` (the files on disk): every file under DIR but .git/.
"""

import json
import os
import subprocess
import sys


def files_under(d, skip_git):
    out = []
    for root, dirs, fs in os.walk(d):
        if skip_git:
            dirs[:] = [x for x in dirs if x != ".git"]
        for f in fs:
            out.append(os.path.relpath(os.path.join(root, f), d))
    return sorted(out)


def calls(log):
    if not log or not os.path.exists(log):
        return 0
    with open(log) as f:
        return sum(1 for ln in f if ln.strip())


def move_head(state, n):
    p = os.path.join(state, "scan-moved-%d.txt" % n)  # noqa: UP031
    with open(p, "w") as f:
        f.write("written during the scan %d\n" % n)  # noqa: UP031
    subprocess.run(["git", "-C", state, "add", "--", os.path.basename(p)], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            state,
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@localhost",
            "commit",
            "-q",
            "-m",
            "moved during the scan",
        ],
        check=True,
    )


def main():
    argv = sys.argv[1:]
    hit = os.environ.get("FAKE_SCAN_HIT", "")
    mode = os.environ.get("FAKE_SCAN_RESULT", "")
    log = os.environ.get("FAKE_SCAN_LOG")
    n = calls(log)
    if "--root" in argv:
        d, skip_git = argv[argv.index("--root") + 1], False
    elif "--repo" in argv:
        d, skip_git = argv[argv.index("--repo") + 1], True
    else:
        sys.stderr.write(f"fake scan: wants --root or --repo, got {argv}\n")
        return 2
    seen = files_under(d, skip_git)
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"argv": argv, "files": seen}) + "\n")
    commit = os.environ.get("FAKE_SCAN_COMMIT", "")
    if commit and n == 0:
        move_head(commit, n)
    hits = []
    for rel in seen:
        try:
            with open(os.path.join(d, rel), encoding="utf-8", errors="replace") as f:
                for ln in f:
                    if hit and hit in ln:
                        hits.append(f"HIT file {rel}: {ln.strip()}")
        except OSError:
            pass
    if mode in ("dirty", "dirty0") and not hits:
        hits.append("HIT file somewhere.md: a line with %s in it" % (hit or "a private word"))
    print(f"== files under {d}")
    for h in hits:
        print(h)
    print("== total hits: %d" % len(hits))  # noqa: UP031
    if hits:
        print("NOT CLEAN")
        return 0 if mode == "dirty0" else 1
    if mode == "spaced":
        print("CLEAN ")
        return 0
    print("CLEAN")
    if mode == "late":
        print("one more line after the verdict")
    return 1 if mode == "clean1" else 0


if __name__ == "__main__":
    sys.exit(main())
