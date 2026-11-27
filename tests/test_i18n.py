"""The message catalogs (depot2/i18n.py): en and ru hold the same keys with the same fields, every key the code names
is in them, no Russian text is left outside the Russian catalog, and the language follows the config and $DEPOT2_LANG.
"""

import glob
import importlib
import os
import pkgutil
import re
import string
import unittest
from pathlib import Path

import helpers

import depot2
from depot2 import config, i18n

ROOT = os.path.dirname(helpers.HERE)
PKG = str(Path(ROOT, "depot2"))
CYRILLIC = re.compile("[Ѐ-ӿ]")
# t("key"), i18n.t("key"), rx("in.key"), is_word(x, "in.key"), matches(x, "key"), forms("key"), message("key")
RE_KEY = re.compile(
    r"""(?:\bt|\bi18n\.t|\brx|\bforms|\bmessage)\(\s*["']([a-z0-9_.\-]*[a-z0-9_\-])["']\s*[,)]|"""
    r"""(?:\bis_word|\bmatches)\([^()]*?,\s*["']([a-z0-9_.\-]*[a-z0-9_\-])["']\)"""
)
RE_WEB_KEY = re.compile(r"""\bT\(\s*["']([A-Za-z0-9_.\-]+)["']""")


def fields(template):
    return sorted({f for _, f, _, _ in string.Formatter().parse(template) if f})


def variants(m):
    return list(m.values()) if isinstance(m, dict) else [m]


class CatalogTest(unittest.TestCase):
    def test_both_catalogs_hold_the_same_keys(self):
        en, ru = i18n.catalog("en"), i18n.catalog("ru")
        self.assertEqual(sorted(set(en) - set(ru)), [], "in en.py, not in ru.py")
        self.assertEqual(sorted(set(ru) - set(en)), [], "in ru.py, not in en.py")

    def test_every_message_has_the_same_fields_in_both(self):
        en, ru = i18n.catalog("en"), i18n.catalog("ru")
        for key in en:
            if key.startswith(i18n.INPUT):
                continue  # regex fragments, never formatted
            self.assertIsInstance(en[key], str, f"{key}: the English message is one string (no gender)")
            want = fields(en[key])
            for form in variants(ru[key]):
                self.assertEqual(fields(form), want, f"{key}: ru fields differ from en")
            if isinstance(ru[key], dict):
                self.assertEqual(sorted(ru[key]), ["f", "m"], f"{key}: a gender pair is f and m")
            for form in variants(en[key]) + variants(ru[key]):
                form.format(**{f.split(".")[0].split("[")[0]: "x" for f in want})  # braces balanced, fields named

    def test_the_input_words_compile(self):
        for key in i18n.catalog("en"):
            if key.startswith(i18n.INPUT):
                re.compile(i18n.rx(key))

    def test_every_key_the_code_names_is_in_the_catalog(self):
        en = i18n.catalog("en")
        missing = []
        for path in glob.glob(str(Path(PKG, "**", "*.py")), recursive=True):
            if os.sep + "catalogs" + os.sep in path:
                continue
            with Path(path).open(encoding="utf-8") as f:
                text = f.read()
            for m in RE_KEY.finditer(text):
                key = m.group(1) or m.group(2)
                if key not in en:
                    missing.append(f"{os.path.relpath(path, ROOT)}: {key}")
        with Path(PKG, "web", "index.html").open(encoding="utf-8") as f:
            page = f.read()
        web = i18n.web_messages("en")
        missing += [f"depot2/web/index.html: web.{k}" for k in RE_WEB_KEY.findall(page) if k not in web]
        self.assertEqual(missing, [])

    def test_every_label_family_resolves_in_both(self):
        for mod in pkgutil.walk_packages(depot2.__path__, "depot2."):
            if mod.name.startswith("depot2.catalogs") or mod.name == "depot2.__main__":
                continue
            m = importlib.import_module(mod.name)
            for name, v in vars(m).items():
                if isinstance(v, i18n.Labels):
                    for k in v:
                        for lang in i18n.LANGS:
                            self.assertIn(f"{v.prefix}.{k}", i18n.catalog(lang), f"{mod.name}.{name}")

    def test_no_russian_outside_the_russian_catalog(self):
        """The code, its comments and the web page are English; Russian lives in depot2/catalogs/ru.py alone."""
        bad = []
        paths = [*glob.glob(str(Path(PKG, "**", "*.py")), recursive=True), str(Path(PKG, "web", "index.html"))]
        for path in sorted(paths):
            if os.path.abspath(path) == str(Path(PKG, "catalogs", "ru.py")):
                continue
            with Path(path).open(encoding="utf-8") as f:
                for n, line in enumerate(f, 1):
                    if CYRILLIC.search(line):
                        bad.append("%s:%d" % (os.path.relpath(path, ROOT), n))  # noqa: UP031
        self.assertEqual(bad, [], "%d lines with Russian outside ru.py" % len(bad))  # noqa: UP031


class LanguageTest(unittest.TestCase):
    def setUp(self):
        self.env = os.environ.get(i18n.ENV)

    def tearDown(self):
        if self.env is None:
            os.environ.pop(i18n.ENV, None)
        else:
            os.environ[i18n.ENV] = self.env
        i18n.configure()

    def test_english_is_the_default(self):
        os.environ.pop(i18n.ENV, None)
        self.assertEqual(config.DEFAULTS["language"], "en")
        self.assertEqual(i18n.configure(), "en")
        self.assertEqual(i18n.t("team.agree", gender="f"), "agrees")

    def test_the_config_key_and_the_env(self):
        os.environ.pop(i18n.ENV, None)
        self.assertEqual(i18n.configure("ru"), "ru")
        self.assertEqual(i18n.t("team.agree", gender="f"), "согласна")
        with self.assertRaises(ValueError):
            i18n.configure("fr")
        os.environ[i18n.ENV] = "en"
        self.assertEqual(i18n.configure("ru"), "en")  # the env beats the config
        os.environ[i18n.ENV] = "xx"
        with self.assertRaises(ValueError):
            i18n.configure("en")

    def test_a_bad_language_in_the_config_is_refused(self):
        cfg = config.load("/nonexistent")
        cfg["language"] = "fr"
        with self.assertRaises(ValueError):
            config.check(cfg)

    def test_gender_matters_only_to_russian(self):
        self.assertEqual(i18n.t("team.agree", gender="m", lang="ru"), "согласен")
        self.assertEqual(i18n.t("team.agree", gender="f", lang="ru"), "согласна")
        self.assertEqual(i18n.t("team.agree", gender="m", lang="en"), i18n.t("team.agree", gender="f", lang="en"))

    def test_words_are_read_in_every_language(self):
        self.assertTrue(
            i18n.is_word("Род: мужской", "in.spec_gender") or re.search(i18n.rx("in.spec_gender"), "Род: мужской")
        )
        self.assertTrue(re.search(i18n.rx("in.spec_gender"), "Grammatical gender: f"))


if __name__ == "__main__":
    unittest.main()
