
"""The team of the rounds: its characters, their names and the grammatical gender they speak in.

The team is optional. The config's `characters` null (the default) is no team: the rounds, the tokens, the reflection
and the brainstorms answer «no team configured» (require()), and everything else (the stack, the cars, the trains, the
gates, the merge queue, the backup) works as it does with one. A list of characters is the team; examples/demo/config.json
has a team of five that shows the shape. A character: `id` (latin, the key in the ledger and the state), `name` (the
lookup key), `full` (the long form), `default_gender` (f or m: what seeding writes into the state, which then rules;
only a language with grammatical gender, Russian, reads it), `spec_marker` (how the team's spec file names it), `role`
(one line for the prompts), and optionally `display`, `aliases` and `title`.
"""

import copy
import re

from . import i18n
from .i18n import t

GENDERS = ("f", "m")
RE_ID = re.compile(r"^[a-z][a-z0-9_]{1,31}$")

Character = dict[str, str | list[str]]

CHARACTERS: list[Character] = []
IDS: list[str] = []
BY_ID: dict[str, Character] = {}


class NoTeam(ValueError):
    """A team command in a depot whose config has no `characters`."""


def require():
    """The team commands' guard: a depot without a team says so, once, instead of failing somewhere inside."""
    if not IDS:
        raise NoTeam(t("team.none"))


def check_config(team):
    """The config's `characters`: null, or a list of at least two characters with unique ids."""
    if team is None:
        return
    if not isinstance(team, list) or len(team) < 2:
        raise ValueError("characters must be null (no team) or a list of at least two characters")
    seen = set()
    for c in team:
        if not isinstance(c, dict):
            raise ValueError("characters: every character is an object")
        for k in ("id", "name", "full", "default_gender", "spec_marker"):
            if not isinstance(c.get(k), str) or not c[k]:
                raise ValueError("characters: {!r} needs {}".format(c.get("id"), k))
        if not RE_ID.match(c["id"]) or c["id"] in seen:
            raise ValueError("characters: id {!r} must be unique, latin lower-case".format(c["id"]))
        if c["default_gender"] not in GENDERS:
            raise ValueError("characters: {} default_gender must be f or m".format(c["id"]))
        seen.add(c["id"])


def configure(team):
    """The team of this process: the config's list, or none. The module's lists change in place."""
    check_config(team)
    chosen = copy.deepcopy(team or [])
    CHARACTERS[:] = chosen
    IDS[:] = [c["id"] for c in chosen]
    BY_ID.clear()
    BY_ID.update({c["id"]: c for c in chosen})


def role(cid):
    return BY_ID[cid].get("role") or t("team.role_default")


def resolve(name):
    key = name.strip().lower()
    for c in CHARACTERS:
        if key in (c["id"], c["name"].lower(), c["full"].lower(), display(c["id"]).lower(), *c.get("aliases", ())):
            return c["id"]
    require()
    raise ValueError(t("team.unknown", name=repr(name), ids=", ".join(IDS)))


def display(cid):
    c = BY_ID[cid]
    return c.get("display", c["name"])


def name(cid):
    return display(cid)


def label(cid):
    c = BY_ID[cid]
    extra = [x for x in ("«{}»".format(c["full"]) if c["full"] != c["name"] else "", c.get("title", "")) if x]
    return display(cid) + (" ({})".format(", ".join(extra)) if extra else "")


def check_gender(g):
    if g not in GENDERS:
        raise ValueError(f"gender must be f or m, got {g!r}")
    return g


def agree_word(gender, position):
    """A character's position on a point, in its own words: agree, disagree, or a dash."""
    if position not in ("agree", "disagree"):
        return "—"
    return t("team." + position, gender=check_gender(gender))


def spec_gender_line(gender):
    """The line that seeding and `depot2 gender` keep in spec.md (empty in a language without grammatical gender)."""
    return t("team.spec_gender", gender=check_gender(gender))


def is_spec_gender_line(line):
    """A spec.md line that spec_gender_line wrote, in any language (an older wording too): set_gender replaces it."""
    return re.search(i18n.rx("in.spec_gender"), line) is not None


def extract_specs(brief_text):
    """Split the team's spec file (a numbered list, one item per character) into one spec per character."""
    lines = brief_text.splitlines()
    starts = []
    for i, line in enumerate(lines):
        s = line.lstrip()
        if len(s) > 3 and s[0].isdigit() and s[1:3] == ". " and "**«" in s:
            starts.append(i)
    if len(starts) < len(CHARACTERS):
        raise ValueError("the spec file has %d character items, expected %d" % (len(starts), len(CHARACTERS)))  # noqa: UP031
    specs = {}
    for n, i in enumerate(starts[:len(CHARACTERS)]):
        end = starts[n + 1] if n + 1 < len(starts) else len(lines)
        # The list ends at the next heading.
        for j in range(i + 1, end):
            if lines[j].startswith("#"):
                end = j
                break
        text = "\n".join(lines[i:end]).strip()
        for c in CHARACTERS:
            if ("«{}»".format(c["spec_marker"])) in lines[i]:
                specs[c["id"]] = text
    missing = [c for c in IDS if c not in specs]
    if missing:
        raise ValueError(f"no spec found for {missing}")
    return specs
