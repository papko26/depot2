import argparse
import contextlib
import io
import os
import re
import unittest

import helpers

from depot2 import cli as team

README = os.path.join(os.path.dirname(helpers.HERE), "README.md")
WORD = re.compile(r"^[a-z][a-z-]*(\|[a-z][a-z-]*)*$")


def _subs(p):
    for a in p._actions:
        if isinstance(a, argparse._SubParsersAction):
            return a
    return None


def _paths(p, path=()):
    """Every command and subcommand path, aliases left out (`wish` is `wishes`)."""
    s = _subs(p)
    if s is None:
        return
    seen = set()
    for name, sp in s.choices.items():
        if id(sp) not in seen:
            seen.add(id(sp))
            yield (*path, name)
            yield from _paths(sp, (*path, name))


def _resolve(ap, words):
    """The command paths a README line names: `wishes|think|todo take ID …` gives three. A plain word where a
    subcommand stands that is none is returned in `bad`."""
    live, done, bad = [()], [], []
    for w in words:
        w = w.strip("`.,;:)»")
        nxt = []
        for path in live:
            p = ap
            for x in path:
                p = _subs(p).choices[x]
            s = _subs(p)
            if s is None or not WORD.match(w):
                done.append(path)  # no subcommand here, or a value, a flag, a placeholder: the path ends
            elif all(x in s.choices for x in w.split("|")):
                nxt += [(*path, x) for x in w.split("|")]
            else:
                bad.append(" ".join((*path, w)))
                done.append(path)
        live = nxt
        if not live:
            break
    return done + live, bad


def _readme_lines():
    with open(README, encoding="utf-8") as f:
        text = f.read()
    top = set(_subs(team.build_parser()).choices)
    lines, block, commands = [], False, False
    for ln in text.split("\n"):
        if ln.startswith("## "):
            commands = ln.strip() == "## Commands"
        if ln.startswith("```"):
            block = not block
            continue
        m = re.match(r"^\s*(?:python3 -m )?depot2 (.*)$", ln) if block else None
        if m:
            segs = [m.group(1)]
            if commands:
                for seg in m.group(1).split(" | ")[1:]:
                    if seg.split(" ", 1)[0] in top:
                        segs.append(seg)
            lines += [(ln.strip(), s) for s in segs]
    for span in re.findall(r"`(?:python3 -m )?depot2 ([^`]+)`", text):
        lines.append((f"`depot2 {span}`", span))
    return lines


class CliDocsTest(unittest.TestCase):
    def setUp(self):
        self.ap = team.build_parser()

    def test_every_readme_command_exists(self):
        lines = _readme_lines()
        self.assertGreater(len(lines), 100)
        named, bad = set(), []
        for where, rest in lines:
            words = [w for w in rest.split() if not w.startswith("--state")][:6]
            paths, b = _resolve(self.ap, words)
            bad += [f"{x}  ({where[:90]})" for x in b]
            for p in paths:
                for i in range(1, len(p) + 1):
                    named.add(p[:i])
        self.assertEqual(bad, [], "the README names commands depot2 does not have")
        for path in sorted(named):
            out = io.StringIO()
            with contextlib.redirect_stdout(out), self.assertRaises(SystemExit) as e:
                self.ap.parse_args([*list(path), "--help"])
            self.assertEqual(e.exception.code, 0, " ".join(path))
            self.assertIn("usage: depot2 " + " ".join(path), " ".join(out.getvalue().split()))

    def test_every_command_is_in_the_commands_block(self):
        with open(README, encoding="utf-8") as f:
            block = f.read().split("## Commands", 1)[1].split("```", 2)[1]
        lines = [ln for ln in block.split("\n") if ln.startswith("depot2 ")]
        top = set(_subs(self.ap).choices)
        named = set()
        for ln in lines:
            segs = ln[len("depot2 "):].split(" | ")
            for seg in [segs[0]] + [s for s in segs[1:] if s.split(" ", 1)[0] in top]:
                for p in _resolve(self.ap, seg.split()[:6])[0]:
                    for i in range(1, len(p) + 1):
                        named.add(p[:i])
        missing = [" ".join(p) for p in _paths(self.ap) if p not in named]
        self.assertEqual(missing, [], "commands the README's «Commands» block leaves out")

    def test_every_subcommand_and_argument_has_help(self):
        no = []

        def walk(p, path):
            for a in p._actions:
                if isinstance(a, argparse._SubParsersAction):
                    helps = {c.dest: c.help for c in a._choices_actions}
                    for name, sp in a.choices.items():
                        if name in helps and not helps[name]:
                            no.append(" ".join([*path, name]) + ": no help line")
                        walk(sp, [*path, name])
                elif not isinstance(a, argparse._HelpAction) and not a.help:
                    no.append("{} {}".format(" ".join(path), "/".join(a.option_strings) or a.dest))
        walk(self.ap, [])
        self.assertEqual(no, [])


if __name__ == "__main__":
    unittest.main()
