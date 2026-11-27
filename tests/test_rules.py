"""The project's rules for its builders (`depot2 rules`, the config's rules.files): each file shown only as a brief is
— a real .md file inside web.docs.dirs, at most MAX_DOC of it; any other listed with the reason and never read.
Every name and text in it is made up."""

import json
import os
import shutil
import tempfile
from unittest import mock

from test_extras import Base

from depot2 import artifacts, webui


class RulesTest(Base):
    def setUp(self):
        super(RulesTest, self).setUp()
        self.top = tempfile.mkdtemp(prefix="depot2-rules-")
        self.docs = os.path.join(self.top, "docs")
        self.other = os.path.join(self.top, "other")
        for d in (self.docs, self.other):
            os.makedirs(d)
        self.write("docs/builders.md", "# Rules for builders\n\n1. **Tests first.** `make test`\n\n<script>alert(1)</script>\n")
        self.write("docs/plain.md", "no heading here\n")
        self.write("docs/notes.txt", "# not markdown\n")
        self.write("other/secret.md", "# private\n")
        os.symlink(os.path.join(self.other, "secret.md"), os.path.join(self.docs, "link.md"))

    def tearDown(self):
        shutil.rmtree(self.top, ignore_errors=True)
        super(RulesTest, self).tearDown()

    def write(self, rel, text):
        with open(os.path.join(self.top, rel), "w", encoding="utf-8") as f:
            f.write(text)

    def rules(self, files, *argv, dirs=None):
        sets = ["--set", "rules.files=" + json.dumps(files),
                "--set", "web.docs.dirs=" + json.dumps([self.docs] if dirs is None else dirs)]
        return self.team(*(sets + ["rules"] + list(argv)))

    def got(self, files, **kw):
        rc, out, err = self.rules(files, "--json", **kw)
        self.assertEqual(rc, 0, err)
        return json.loads(out)

    def test_none(self):
        rc, out, err = self.team("rules")
        self.assertEqual(rc, 0, err)
        self.assertIn("Правил нет: rules.files в конфиге не называет ни одного файла", out)
        self.assertEqual(json.loads(self.team("rules", "--json")[1]), {"v": 1, "files": [], "n": 0, "shown": 0})

    def test_a_file_its_title_text_and_date(self):
        p, plain = os.path.join(self.docs, "builders.md"), os.path.join(self.docs, "plain.md")
        j = self.got([p, plain])
        self.assertEqual((j["n"], j["shown"]), (2, 2))
        f = j["files"][0]
        self.assertEqual((f["path"], f["title"], f["shown"], f["why"], f["cut"]), (p, "Rules for builders", True, None, False))
        self.assertIn("<script>alert(1)</script>", f["text"])  # as text: the page escapes it
        self.assertEqual((f["bytes"], f["size"]), (os.path.getsize(p), "%d Б" % os.path.getsize(p)))
        self.assertEqual(f["mtime"], artifacts.iso(os.path.getmtime(p)))
        self.assertEqual(j["files"][1]["title"], "plain.md")  # no heading: the file's name
        rc, out, _ = self.rules([p, plain])
        self.assertIn("Файлов 2 (rules.files), показано 2.", out)
        self.assertIn("| Rules for builders | `%s` | %d Б |" % (p, os.path.getsize(p)), out)
        self.assertEqual(self.rules([p, plain], "--md")[1], out)
        home = os.path.expanduser("~")
        if p.startswith(home + "/"):  # a path from ~ is read as the home's
            self.assertTrue(self.got(["~" + p[len(home):]])["files"][0]["shown"])

    def test_what_is_not_shown_says_why_and_is_never_read(self):
        files = [os.path.join(self.other, "secret.md"), os.path.join(self.docs, "link.md"),
                 os.path.join(self.docs, "notes.txt"), os.path.join(self.docs, "gone.md"),
                 os.path.join(self.docs, "..", "other", "secret.md"), os.path.join(self.docs, "builders.md")]
        j = self.got(files)
        self.assertEqual([(f["shown"], f["why"]) for f in j["files"]],
                         [(False, "вне web.docs.dirs"), (False, "вне web.docs.dirs"), (False, "не файл .md"),
                          (False, "нет файла"), (False, "вне web.docs.dirs"), (True, None)])
        self.assertEqual((j["n"], j["shown"]), (6, 1))
        for f in j["files"][:5]:
            self.assertEqual((f["text"], f["bytes"], f["mtime"]), (None, None, None))
        rc, out, _ = self.rules(files)
        self.assertIn("| — | `%s` | ⚠ вне web.docs.dirs | — |" % files[0], out)
        for text in (json.dumps(j), out):
            self.assertNotIn("private", text)
        j = self.got([os.path.join(self.docs, "builders.md")], dirs=[])
        self.assertEqual(j["files"][0]["why"], "web.docs.dirs пуст: ни один файл не показывается")

    def test_a_long_file_is_cut(self):
        self.write("docs/long.md", "# Long\n" + "x" * 300)
        with mock.patch.object(webui, "MAX_DOC", 100):
            f = self.got([os.path.join(self.docs, "long.md")])["files"][0]
        self.assertEqual((f["shown"], f["cut"], len(f["text"])), (True, True, 100))

    def test_the_config(self):
        for bad in ('"docs/builders.md"', '["docs/builders.md"]', "[5]", '[""]', "{}"):
            rc, _, err = self.team("--set", "rules.files=" + bad, "rules")
            self.assertEqual(rc, 1, bad)
            self.assertIn("rules.files must be a list of the rules' Markdown files, each a path from / or ~", err)
        self.assertEqual(self.team("--set", "rules.files=[]", "rules")[0], 0)
