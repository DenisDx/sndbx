"""Import-path bootstrap for the pinned aiset submodule."""

from __future__ import annotations

import importlib
import sys
from pathlib import Path

REQUIRED_MODULES = ("aiset.resolver", "aiset.service", "aiset.store", "aiset.asgi", "aiset.merge")


def aiset_src_dir(root_dir: Path) -> Path:
    """Return the importable aiset source directory inside the host checkout."""
    return Path(root_dir).resolve() / "aiset" / "src"


def ensure_aiset(root_dir: Path) -> None:
    """Make the real aiset package importable or raise a clear error."""
    try:
        for name in REQUIRED_MODULES:
            importlib.import_module(name)
        return
    except Exception:
        pass

    for name in [name for name in sys.modules if name == "aiset" or name.startswith("aiset.")]:
        sys.modules.pop(name, None)

    source = aiset_src_dir(root_dir)
    if not source.is_dir():
        raise ImportError(
            f"aiset package not found at {source}. "
            "Run 'git submodule update --init --recursive' or install aiset with pip."
        )
    sys.path.insert(0, str(source))
    try:
        for name in REQUIRED_MODULES:
            importlib.import_module(name)
    except Exception as error:
        raise ImportError(f"cannot import aiset from {source}: {error}") from error
