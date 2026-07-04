"""Enables ``python -m knowledge ...`` as an equivalent of the ``knowledge``
console script.

Added for Item F (embedder daemon): the daemon client spawns
``knowledge daemon run`` via ``[sys.executable, "-m", "knowledge", "daemon",
"run"]`` rather than resolving the installed console script — surviving a
``PATH`` that doesn't (yet) include the venv's bin dir, which the console
script depends on.
"""

from __future__ import annotations

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
