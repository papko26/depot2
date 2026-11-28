"""`python3 -m depot2 …`, or `python3 path/to/depot2/__main__.py …` from a checkout."""

import sys
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).absolute().parents[1]))
    from depot2.cli import main
else:
    from .cli import main

sys.exit(main())
