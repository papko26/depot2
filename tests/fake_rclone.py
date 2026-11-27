#!/usr/bin/env python3
"""A stand-in for rclone in the backup tests: no network, no rclone.conf, no real crypt.

The env-defined crypt remote (RCLONE_CONFIG_<NAME>_TYPE=crypt, _REMOTE=<remote>:<folder>) maps to
the local dir $FAKE_RCLONE_DIR/<folder>; files are stored as they are.

- obscure -          a deterministic, reversible stand-in for rclone's obscure
- lsjson             the folder as rclone prints it; exit 3 when it doesn't exist
- copyto SRC DST     local → remote or remote → local; --immutable refuses an existing target
- cryptcheck SRC R:  compares the bytes of SRC's files (filtered by --include) with the remote's
- deletefile R:name  removes one file
- anything else      exit 99: the tool must never call it (sync, move, copy, delete, purge, …)

$FAKE_RCLONE_LOG gets one JSON line per call: argv, the names (never the values) of the
RCLONE_CONFIG_* variables, and whether the crypt passwords are the obscured fake pass secrets.
$FAKE_RCLONE_FAIL=network fails every cloud call like a missing network; $FAKE_RCLONE_SLEEP
slows copyto by that many seconds.
"""

import base64
import json
import os
import shutil
import sys
import time
from pathlib import Path


def obscure(s):
    return "OBS" + base64.b64encode(s[::-1].encode()).decode().rstrip("=")


def crypt_remote():
    for k, v in os.environ.items():
        if k.startswith("RCLONE_CONFIG_") and k.endswith("_TYPE") and v == "crypt":
            name = k[len("RCLONE_CONFIG_") : -len("_TYPE")]
            return name.lower(), os.environ.get(f"RCLONE_CONFIG_{name}_REMOTE", ""), name
    return None, None, None


def main():
    argv = sys.argv[1:]
    name, remote, up = crypt_remote()
    secrets = json.loads(os.environ.get("FAKE_PASS_SECRETS", "{}"))
    ok = None
    if up:
        want = {obscure(v) for v in secrets.values()}
        got = {os.environ.get(f"RCLONE_CONFIG_{up}_{k}", "") for k in ("PASSWORD", "PASSWORD2")}
        ok = got == want and len(got) == 2
    if os.environ.get("FAKE_RCLONE_LOG"):
        with open(os.environ["FAKE_RCLONE_LOG"], "a") as f:
            f.write(
                json.dumps(
                    {
                        "argv": argv,
                        "crypt_env_ok": ok,
                        "env": sorted(k for k in os.environ if k.startswith("RCLONE_CONFIG_")),
                    }
                )
                + "\n"
            )
    if not argv:
        return 1
    cmd, rest = argv[0], argv[1:]
    if cmd == "obscure":
        line = sys.stdin.readline().rstrip("\n")
        print(obscure(line))
        return 0
    flags = [a for a in rest if a.startswith("-")]
    pos, skip = [], False
    for _i, a in enumerate(rest):
        if skip:
            skip = False
            continue
        if a in ("--retries", "--low-level-retries", "--contimeout", "--timeout", "--include"):
            skip = True
            continue
        if not a.startswith("-"):
            pos.append(a)
    include = rest[rest.index("--include") + 1] if "--include" in rest else None
    if cmd not in ("lsjson", "copyto", "cryptcheck", "deletefile"):
        sys.stderr.write(f"fake rclone: {cmd} is forbidden in the backup\n")
        return 99
    if os.environ.get("FAKE_RCLONE_FAIL") == "network":
        sys.stderr.write(
            'ERROR : Failed to create file system: Get "https://www.googleapis.com/drive/v3/files": '
            "dial tcp: lookup www.googleapis.com: no such host\n"
        )
        return 1
    folder = os.path.join(os.environ["FAKE_RCLONE_DIR"], remote.split(":", 1)[1])

    def local_of(path):
        if name and path.startswith(name + ":"):
            return os.path.join(folder, path[len(name) + 1 :]), True
        if ":" in path and not path.startswith("/"):
            sys.stderr.write(f"fake rclone: unknown remote in {path}\n")
            sys.exit(98)
        return path, False

    if cmd == "lsjson":
        if not os.path.isdir(folder):
            sys.stderr.write("ERROR : : error listing: directory not found\n")
            return 3
        out = [
            {"Path": n, "Name": n, "Size": Path(folder, n).stat().st_size, "ModTime": "x", "IsDir": False}
            for n in sorted(os.listdir(folder))
        ]
        print(json.dumps(out))
        return 0
    if cmd == "copyto":
        src, _ = local_of(pos[0])
        dst, _ = local_of(pos[1])
        if "--immutable" in flags and os.path.exists(dst):
            sys.stderr.write("ERROR : immutable file modified\n")
            return 1
        time.sleep(float(os.environ.get("FAKE_RCLONE_SLEEP", "0")))
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.copyfile(src, dst)
        return 0
    if cmd == "cryptcheck":
        src = pos[0]
        bad = 0
        for n in os.listdir(src):
            if include and n != include:
                continue
            r = os.path.join(folder, n)
            if not os.path.exists(r) or open(r, "rb").read() != open(os.path.join(src, n), "rb").read():  # noqa: SIM115
                bad += 1
        return 1 if bad else 0
    if cmd == "deletefile":
        p, is_remote = local_of(pos[0])
        if not is_remote:
            return 97
        os.remove(p)  # noqa: PTH107 - preserve trailing slashes in remote arguments
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
