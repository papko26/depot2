"""One character's identity on disk: spec, the current portrait and every past version, thoughts.

characters/<id>/
  spec.md            the character's spec (its config entry, then the owner's edits)
  portrait.md        the current self-portrait (a copy of the newest version)
  history/vNNNN.md   every portrait version ever written, never overwritten or deleted
  portraits.jsonl    the version index: version, full sha256, source, call
  thoughts.jsonl     inner thoughts, one per answer
  meta.json          the reflection markers (what the last reflection already saw)

The CODE replaces a portrait; the model only proposes the text.
"""

import os
from pathlib import Path

from .store import (
    WriteCheckError,
    append_jsonl,
    atomic_write,
    now_iso,
    read_json,
    read_jsonl,
    read_text,
    sha256_text,
    write_json,
    write_new,
)


class Persona:
    def __init__(self, state, cid) -> None:
        self.state = state
        self.cid = cid
        self.dir = state.char_dir(cid)
        self.spec_path = str(Path(self.dir, "spec.md"))
        self.portrait_path = str(Path(self.dir, "portrait.md"))
        self.history_dir = str(Path(self.dir, "history"))
        self.index_path = str(Path(self.dir, "portraits.jsonl"))
        self.thoughts_path = str(Path(self.dir, "thoughts.jsonl"))
        self.meta_path = str(Path(self.dir, "meta.json"))

    # --- spec ---------------------------------------------------------------------------------

    def spec(self):
        return read_text(self.spec_path, "")

    def write_spec(self, text):
        atomic_write(self.spec_path, text.strip() + "\n")
        self.state.touch(self.spec_path)

    # --- portrait -----------------------------------------------------------------------------

    def portrait(self):
        return read_text(self.portrait_path, "")

    def versions(self):
        return read_jsonl(self.index_path)

    def version(self):
        v = self.versions()
        return v[-1]["version"] if v else 0

    def replace_portrait(self, text, source, cap, call_id=None, note=None, summary_en=None, summary_cap=1500):
        """Store a new portrait version, keep every old one, and verify all of it.

        Returns the index record. Over-long text is cut at the last paragraph or sentence end
        is stored next to the version as vNNNN.en.md.

        """
        text, truncated = cut_text(text, cap)
        if not text.strip():
            raise ValueError("empty portrait")
        text += "\n"
        versions = self.versions()
        old = self.portrait()
        if versions:
            # The current file must still be the newest recorded version before we move on.
            if sha256_text(old) != versions[-1]["sha256"]:
                raise WriteCheckError(
                    "%s: portrait.md differs from v%04d; refusing to replace"  # noqa: UP031
                    % (self.cid, versions[-1]["version"])
                )
        elif old:
            raise WriteCheckError(f"{self.cid}: portrait.md exists without an index")
        n = (versions[-1]["version"] if versions else 0) + 1
        hist = str(Path(self.history_dir, "v%04d.md" % n))  # noqa: UP031
        write_new(hist, text)
        self.state.touch(hist)
        en = None
        if summary_en and summary_en.strip():
            en_text = cut_text(summary_en, summary_cap)[0] + "\n"
            en = str(Path(self.history_dir, "v%04d.en.md" % n))  # noqa: UP031
            write_new(en, en_text)
            self.state.touch(en)
        atomic_write(self.portrait_path, text)
        self.state.touch(self.portrait_path)
        digest = sha256_text(text)
        rec = {
            "version": n,
            "sha256": digest,
            "chars": len(text),
            "ts": now_iso(),
            "source": source,
            "path": os.path.relpath(hist, self.dir),
            "previous": versions[-1]["version"] if versions else None,
            "truncated": truncated,
        }
        if call_id:
            rec["call"] = call_id
        if note:
            rec["note"] = note
        if en:
            rec["summary_en_path"] = os.path.relpath(en, self.dir)
            rec["summary_en_sha256"] = sha256_text(read_text(en))
        append_jsonl(self.index_path, rec)
        self.state.touch(self.index_path)
        self.verify()
        return rec

    def verify(self):
        """Every version file matches its full sha256, and portrait.md is the newest one."""
        versions = self.versions()
        for v in versions:
            body = read_text(os.path.join(self.dir, v["path"]))  # noqa: PTH118 - preserve lexical path components
            if body is None or sha256_text(body) != v["sha256"]:
                raise WriteCheckError("%s v%04d does not match its sha256" % (self.cid, v["version"]))  # noqa: UP031
            if v.get("summary_en_path"):
                en = read_text(os.path.join(self.dir, v["summary_en_path"]))  # noqa: PTH118 - preserve lexical path components
                if en is None or sha256_text(en) != v["summary_en_sha256"]:
                    raise WriteCheckError("%s v%04d.en does not match its sha256" % (self.cid, v["version"]))  # noqa: UP031
        if versions and sha256_text(self.portrait()) != versions[-1]["sha256"]:
            raise WriteCheckError("%s portrait.md is not v%04d" % (self.cid, versions[-1]["version"]))  # noqa: UP031
        return len(versions)

    def summary_en(self):
        """The newest version's English summary, or None when that version has none."""
        v = self.versions()
        if not v or not v[-1].get("summary_en_path"):
            return None
        return read_text(os.path.join(self.dir, v[-1]["summary_en_path"]))  # noqa: PTH118 - preserve lexical path components

    # --- thoughts -----------------------------------------------------------------------------

    def thoughts(self, n=None):
        t = read_jsonl(self.thoughts_path)
        return t if n is None else t[-n:]

    def add_thought(self, text, round_id, call_id):
        text = " ".join((text or "").split())
        if not text:
            return None
        rec = {"ts": now_iso(), "round": round_id, "call": call_id, "thought": text}
        append_jsonl(self.thoughts_path, rec)
        self.state.touch(self.thoughts_path)
        return rec

    # --- grammatical gender ------------------------------------------------------------------

    def gender(self):
        """'f' or 'm' from meta.json; the seeding default only when the state has none."""
        from . import characters

        g = self.meta().get("gender")
        return g if g in characters.GENDERS else characters.BY_ID[self.cid]["default_gender"]

    def set_gender(self, gender):
        """Write the gender to meta.json and its line to spec.md (replacing any older one)."""
        from . import characters

        characters.check_gender(gender)
        keep = [l for l in self.spec().rstrip("\n").split("\n") if not characters.is_spec_gender_line(l)]  # noqa: E741
        while keep and not keep[-1].strip():
            keep.pop()
        line = characters.spec_gender_line(gender)
        self.write_spec("\n".join(keep) + ("\n\n" + line if line else "\n"))
        self.update_meta(gender=gender)
        if self.gender() != gender or characters.spec_gender_line(gender) not in self.spec():
            raise WriteCheckError(f"{self.cid}: gender did not read back")
        return gender

    # --- reflection markers -------------------------------------------------------------------

    def meta(self):
        return read_json(self.meta_path, {}) or {}

    def update_meta(self, **kw):
        m = self.meta()
        m.update(kw)
        write_json(self.meta_path, m)
        self.state.touch(self.meta_path)
        return m


def cut_text(text, cap):
    """(text, truncated): cut at the last paragraph or sentence end under the cap."""
    text = (text or "").strip()
    if len(text) <= cap:
        return text, False
    cut = text[:cap]
    for sep in ("\n\n", ". ", ".\n", "\n"):
        i = cut.rfind(sep)
        if i > cap // 2:
            cut = cut[: i + 1]
            break
    return cut.strip(), True


def genders(state):
    """{cid: 'f' | 'm'} as the state says."""
    from . import characters

    return {cid: Persona(state, cid).gender() for cid in characters.IDS}
