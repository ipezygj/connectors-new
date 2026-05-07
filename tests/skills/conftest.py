"""Technical implementation for Hummingbot Gateway V2.1."""

import sys
from pathlib import Path

# Ensure the repository root is importable so tests can resolve
# ``from skills.<module> import ...`` without requiring an editable install.
_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))
