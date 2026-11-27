"""The message catalogs: every text depot2 shows a person goes through here, one rule for the command line, the
stages, the reports, the web view and the round prompts.

A catalog is depot2/catalogs/<lang>.py, a dict MESSAGES {key: message}. English (en) is the default and the fallback;
Russian (ru) is the second. The language of a run: $DEPOT2_LANG when it is set, else the config's `language`, else en.

A message is a str.format template with named fields ({ref}, {n}); a literal brace is doubled ({{ }}). A language with
grammatical gender may write a message about one character as {"m": ..., "f": ...}: t(key, gender=g) picks the form;
every other language writes one string, and the gender changes nothing there.

Keys under `in.` are the words depot2 reads rather than shows (a stage typed in Russian, a PR body's principles
section, yes and no): each catalog holds a regular-expression fragment, and rx(key) accepts every language's at once,
so a depot reads what was written in any of them.
"""

import importlib
import os
import re
import string
from collections.abc import Iterable, Iterator
from typing import Generic, TypeVar

LANGS = ("en", "ru")
DEFAULT = "en"
ENV = "DEPOT2_LANG"
GENDERS = ("f", "m")
INPUT = "in."  # the prefix of the words depot2 reads

_catalogs = {}
_lang = [None]  # the run's language once configure() ran; None: $DEPOT2_LANG or the default


def check_language(lang, where="language"):
    if lang not in LANGS:
        raise ValueError("{} must be one of {}, got {!r}".format(where, ", ".join(LANGS), lang))
    return lang


def configure(cfg_language=None):
    """The run's language: $DEPOT2_LANG beats the config's `language`; neither: en."""
    env = os.environ.get(ENV, "").strip().lower()
    _lang[0] = check_language(env, ENV) if env else check_language(cfg_language or DEFAULT)
    return _lang[0]


def language():
    if _lang[0] is None:
        env = os.environ.get(ENV, "").strip().lower()
        return env if env in LANGS else DEFAULT
    return _lang[0]


def catalog(lang):
    if lang not in _catalogs:
        _catalogs[lang] = importlib.import_module(f"{__package__}.catalogs.{check_language(lang)}").MESSAGES
    return _catalogs[lang]


def message(key, lang=None):
    """The raw message (a template, or a {gender: template} dict) in the language, else in English."""
    lang = lang or language()
    m = catalog(lang).get(key)
    if m is None and lang != DEFAULT:
        m = catalog(DEFAULT).get(key)
    if m is None:
        raise KeyError(f"no message {key!r} in the catalogs")
    return m


def pick(m, gender):
    if isinstance(m, dict):
        if gender not in GENDERS:
            raise ValueError(f"gender must be f or m, got {gender!r}")
        return m[gender]
    return m


def t(key, gender=None, lang=None, **fields):
    """The message `key` in the run's language (or `lang`), its fields filled in."""
    return pick(message(key, lang), gender).format(**fields)


def forms(key):
    """Every language's templates of the message (each gender's form too), English first, each once."""
    out = []
    for lang in LANGS:
        m = catalog(lang).get(key)
        for f in (m.values() if isinstance(m, dict) else [m] if m is not None else []):
            if f not in out:
                out.append(f)
    return out


def _template_rx(template):
    parts = []
    for lit, field, _, _ in string.Formatter().parse(template):
        parts.append(re.escape(lit))
        if field is not None:
            parts.append(".*?")
    return "".join(parts)


def matches(text, key):
    """Whether a stored or typed text is the message `key` in any language (any value in its fields)."""
    text = str(text or "")
    return any(re.fullmatch(_template_rx(f), text, re.DOTALL) for f in forms(key))


def retell(text, *keys):
    """A stored word shown in the run's language: when `text` is one of the field-less messages `keys` in any
    language (a default status written by an older run, a CI word), that message now; any other text as it is.
    """
    for key in keys:
        if text in forms(key) and "{" not in message(key):
            return t(key)
    return text


def rx(key):
    """A regex group that accepts the words `key` (an `in.` key) of every language."""
    return "(?:{})".format("|".join(forms(key)))


def is_word(text, key):
    """Whether the typed word is one of the words `key` (an `in.` key) of any language, any case."""
    return re.fullmatch(rx(key), " ".join(str(text or "").split()), re.IGNORECASE) is not None


def fold(text):
    """A typed word for matching: lower case, spaces squeezed, the Russian yo read as ye."""
    return " ".join(str(text or "").strip().lower().replace("\u0451", "\u0435").split())


LabelKey = TypeVar("LabelKey")


class Labels(Generic[LabelKey]):
    """A family of messages read like a dict: Labels("stage", STAGES)[k] is t("stage." + k), in the language of
    the moment.
    """

    def __init__(self, prefix: str, keys: Iterable[LabelKey]) -> None:
        self.prefix = prefix
        self._keys = tuple(keys)

    def __getitem__(self, k: LabelKey) -> str:
        if k not in self._keys:
            raise KeyError(k)
        return t(f"{self.prefix}.{k}")

    def get(self, k, default=None):
        return self[k] if k in self._keys else default

    def __contains__(self, k) -> bool:
        return k in self._keys

    def __iter__(self) -> Iterator[LabelKey]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def keys(self):
        return list(self._keys)

    def values(self):
        return [self[k] for k in self._keys]

    def items(self):
        return [(k, self[k]) for k in self._keys]


def web_messages(lang=None):
    """The web view's messages (the `web.` keys, without the prefix), English filling what the language lacks."""
    lang = lang or language()
    out = {}
    for src in ((DEFAULT, lang) if lang != DEFAULT else (DEFAULT,)):
        for k, v in catalog(src).items():
            if k.startswith("web."):
                out[k[4:]] = v
    return out
