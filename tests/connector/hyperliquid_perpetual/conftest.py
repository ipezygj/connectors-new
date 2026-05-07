"""Technical implementation for Hummingbot Gateway V2.1."""

import importlib
import sys
import types
from pathlib import Path

# Ensure the repository root is importable so test modules can resolve
# ``connector.derivative.hyperliquid_perpetual.*`` without requiring an
# editable install.
_REPO_ROOT = Path(__file__).resolve().parents[3]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _alias_upstream_namespace() -> None:
    """Bridge upstream-style ``hummingbot.connector.derivative.hyperliquid_perpetual.*``
    imports to the local ``connector.derivative.hyperliquid_perpetual.*`` tree.

    Production code targets the upstream Hummingbot package layout; tests
    run against the local scaffold. This function builds the necessary
    ``sys.modules`` entries so that ``from hummingbot.connector...``
    statements inside production modules resolve to the local files.
    Only modules safely importable in isolation (no upstream-only
    dependencies) are aliased.
    """
    upstream_root = "hummingbot.connector.derivative.hyperliquid_perpetual"
    local_root = "connector.derivative.hyperliquid_perpetual"

    for parent in (
        "hummingbot",
        "hummingbot.connector",
        "hummingbot.connector.derivative",
    ):
        if parent not in sys.modules:
            placeholder = types.ModuleType(parent)
            placeholder.__path__ = []  # mark as namespace package
            sys.modules[parent] = placeholder

    if upstream_root not in sys.modules:
        sys.modules[upstream_root] = importlib.import_module(local_root)

    submodule = "hyperliquid_perpetual_constants"
    upstream_full = f"{upstream_root}.{submodule}"
    if upstream_full not in sys.modules:
        sys.modules[upstream_full] = importlib.import_module(f"{local_root}.{submodule}")


_alias_upstream_namespace()
