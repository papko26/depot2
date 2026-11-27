import json
import os
import shutil
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["DEPOT2_FSYNC"] = "0"  # before depot2.store is imported; subprocesses inherit it
os.environ["DEPOT2_BACKUP"] = "off"  # no backup check after the commands; test_backup turns it on
os.environ["DEPOT2_CONFIG"] = os.path.join(HERE, "project-config.json")  # the test project's config
os.environ["DEPOT2_LANG"] = "ru"  # the suites read the Russian catalog's texts; test_i18n.py runs English too
os.environ["DEPOT2_RAW_SCAN"] = "off"  # no private scan of the calls' raw streams; test_push turns it on with its fake
sys.path.insert(0, os.path.dirname(HERE))

from depot2 import characters, config
from depot2.ledger import Ledger
from depot2.persona import Persona
from depot2.store import State

FAKE = os.path.join(HERE, "fake_executor.py")
with open(os.environ["DEPOT2_CONFIG"], encoding="utf-8") as _f:
    TEAM = json.load(_f)["characters"]  # the test project's team of five; config.load sets it again for each depot
characters.configure(TEAM)

# The gates (gates.py) came after these suites: they test what they tested before, with the checks the commands
# made then (a stack object's `after`; a post checked nothing: it meant green); test_board.py turns every item on.
GATES_ALL = dict(config.DEFAULTS["gates"])
config.DEFAULTS["gates"] = {"dor": ["after"], "dod": []}
BRIEF_TEXT = "# бриф\n\n## Сверка с кодом\n\n## Проверка по принципам\n"

NO_NET = tempfile.mkdtemp(prefix="team-test-gh-")
with open(os.path.join(NO_NET, "gh"), "w") as _f:
    _f.write('#!/bin/sh\nif [ -n "$FAKE_GH_STATE" ]; then exec {} -S {} "$@"; fi\n'
             'echo "gh: the tests reach no GitHub (no FAKE_GH_STATE)" >&2\nexit 4\n'.format(sys.executable, os.path.join(HERE, "fake_gh.py")))
os.chmod(os.path.join(NO_NET, "gh"), 0o755)
os.environ["PATH"] = NO_NET + os.pathsep + os.environ.get("PATH", "")


def slurp(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


class TempState:
    """A throwaway state dir with five characters, specs and balances."""

    def __init__(self, balances=None, git=False):
        self.dir = tempfile.mkdtemp(prefix="team-test-")
        self.state = State(self.dir)
        if git:
            self.state.init_git()
        led = Ledger(self.state)
        for cid in characters.IDS:
            Persona(self.state, cid).write_spec(f"spec of {cid}")
            led.seed(cid, (balances or {}).get(cid, 5), "test seed")
        self.cfg = config.load(self.dir)
        self.cfg["bundle"]["decisions_glob"] = os.path.join(self.dir, "no-decisions-*.md")
        self.cfg["deep_repo"]["clone"] = ""  # unit tests never fetch or archive the real repo
        self.cfg["read_dirs"] = []

    def close(self):
        shutil.rmtree(self.dir, ignore_errors=True)


BALLOT = """# Бюллетень

### B49. Пункт 40: фильтры поиска
**Приоритет:** на обсуждение
**Вопрос (развёрнуто):** Что значит фильтр поиска и когда к нему вернуться?
**Рекомендую:** вернуться после PR 2-3 поиска; до тех пор держать только задел.
**Источник:** транскрипт.

### B50. Обложки: замена устаревшей
**Вопрос (развёрнуто):** Что значит «заменить»?
**Рекомендую:** одна общая кнопка «обновить».

| B49 | table row | ignored |
"""
