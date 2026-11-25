"""`python3 -m depot2 …`, or `python3 path/to/depot2/__main__.py …` from a checkout."""

import os
import sys

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    from depot2.cli import main
else:
    from .cli import main

sys.exit(main())
