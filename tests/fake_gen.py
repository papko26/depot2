#!/usr/bin/env python3
"""The merge queue's generators in the merge-queue tests, run in the cwd:

- `generated`: docs/GENERATED.md lists the repository's files;
- `snapshots TEST -u`: rewrites the snapshot of TEST from strings.txt (red when it holds BROKEN);
- `golden REF OUT`: OUT/golden/ranking.golden.json from REF's ranking.txt ($FAKE_GEN_GOLDEN_FAIL: the job fails).

Every call is a JSON line in $FAKE_GEN_LOG.
"""

import json
import os
import subprocess
import sys


def main():
    argv = sys.argv[1:]
    log = os.environ.get("FAKE_GEN_LOG")
    if log:
        with open(log, "a") as f:
            f.write(json.dumps({"argv": argv, "cwd": os.getcwd()}) + "\n")
    if argv[0] == "generated":
        out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"], stdout=subprocess.PIPE,
                             universal_newlines=True, check=True).stdout.split()
        files = sorted(f for f in out if f != "docs/GENERATED.md")
        with open("docs/GENERATED.md", "w") as f:
            f.write("files: %d\n\n%s\n" % (len(files), "\n".join(files)))
        return 0
    if argv[0] == "snapshots":
        test = argv[1]
        with open("strings.txt") as f:
            words = f.read().split()
        d, name = os.path.split(test)
        with open(os.path.join(d, "__snapshots__", name + ".snap"), "w") as f:
            f.write("snap: %s\n" % " ".join(sorted(words)))
        return 1 if "BROKEN" in words else 0
    if argv[0] == "golden":
        if os.environ.get("FAKE_GEN_GOLDEN_FAIL"):
            sys.stderr.write("primary-ci: the job failed\n")
            return 1
        rows = subprocess.run(["git", "show", argv[1] + ":ranking.txt"], stdout=subprocess.PIPE,
                              universal_newlines=True, check=True).stdout.split()
        os.makedirs(os.path.join(argv[2], "golden"), exist_ok=True)
        with open(os.path.join(argv[2], "golden", "ranking.golden.json"), "w") as f:
            f.write("golden of %s\n" % " ".join(sorted(rows)))
        return 0
    sys.stderr.write("fake gen: unknown %s\n" % argv)
    return 2


if __name__ == "__main__":
    sys.exit(main())
