#!/usr/bin/env python3
"""A stand-in for pass in the backup tests. Only `pass show ENTRY` is allowed.

$FAKE_PASS_SECRETS: JSON {entry: secret}. $FAKE_PASS_LOCKED=1: gpg can't decrypt (no cached
passphrase). $FAKE_PASS_LOG gets one JSON line per call: argv, PASSWORD_STORE_GPG_OPTS and
whether stdin is a tty.
"""

import json
import os
import sys


def main():
    argv = sys.argv[1:]
    if os.environ.get("FAKE_PASS_LOG"):
        with open(os.environ["FAKE_PASS_LOG"], "a") as f:
            f.write(
                json.dumps(
                    {
                        "argv": argv,
                        "gpg_opts": os.environ.get("PASSWORD_STORE_GPG_OPTS", ""),
                        "stdin_tty": sys.stdin.isatty() if sys.stdin else None,
                    }
                )
                + "\n"
            )
    if len(argv) != 2 or argv[0] != "show":
        sys.stderr.write(f"fake pass: only `show ENTRY` is allowed, got {argv}\n")
        return 99
    if os.environ.get("FAKE_PASS_LOCKED") == "1":
        sys.stderr.write("gpg: decryption failed: No secret key\n")
        return 2
    secrets = json.loads(os.environ.get("FAKE_PASS_SECRETS", "{}"))
    if argv[1] not in secrets:
        sys.stderr.write(f"Error: {argv[1]} is not in the password store.\n")
        return 1
    print(secrets[argv[1]])
    return 0


if __name__ == "__main__":
    sys.exit(main())
