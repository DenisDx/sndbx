"""Effective-config parity between an independent reader and aiset."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
import unittest
from pathlib import Path

import json5

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from aiset.jsonsrc import Document  # noqa: E402
from settings.runtime import SettingsRuntime  # noqa: E402
from tests.settings_fixtures import copy_live_root  # noqa: E402

PLACEHOLDER = re.compile(r"\$\{([A-Z_][A-Z0-9_]*?)(?::-(.*?))?\}")


def leaf_differences(left, right, path=""):
    """Yield (path, description) for every structural or value difference."""
    if type(left) is not type(right):
        yield path, f"{type(left).__name__} != {type(right).__name__}"
        return
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right)):
            if key not in left:
                yield f"{path}/{key}", "only runtime"
            elif key not in right:
                yield f"{path}/{key}", "only reference"
            else:
                yield from leaf_differences(left[key], right[key], f"{path}/{key}")
    elif isinstance(left, list):
        if len(left) != len(right):
            yield path, f"length {len(left)} != {len(right)}"
        for index, (left_item, right_item) in enumerate(zip(left, right)):
            yield from leaf_differences(left_item, right_item, f"{path}/{index}")
    elif left != right:
        yield path, f"{left!r} != {right!r}"


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE lines."""
    mapping: dict[str, str] = {}
    if not path.exists():
        return mapping
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        mapping[key.strip()] = value.strip()
    return mapping


def reference_config(root: Path) -> dict:
    """Independent legacy-style reader: substitute, parse, apply old overrides."""
    env = read_env(root / ".env")
    env.setdefault("SNDBX_ROOT", str(root))
    text = (root / "config.json5").read_text(encoding="utf-8")

    def replace(match: re.Match) -> str:
        name = match.group(1)
        default = match.group(2)
        if name in os.environ:
            return os.environ[name]
        if name in env:
            return env[name]
        if default is not None:
            return default
        raise AssertionError(f"unresolved placeholder {name}")

    reference = json5.loads(PLACEHOLDER.sub(replace, text))
    overrides_path = root / "data" / "sandbox_startup_overrides.json"
    if overrides_path.exists():
        overrides = json.loads(overrides_path.read_text(encoding="utf-8"))
        sandboxes = reference.get("sandboxes", {}).get("items", {})
        for sandbox_id, enabled in overrides.items():
            if sandbox_id in sandboxes and isinstance(enabled, bool):
                sandboxes[sandbox_id]["run_at_startup"] = enabled
    return reference


def root_is_trimmed(root: Path) -> bool:
    """True when every sandbox entry is only {run_at_startup}."""
    document = Document((root / "config.json5").read_text(encoding="utf-8"))
    stored = document.stored_value(document.root)
    sandboxes = stored.get("sandboxes", {}).get("items", {}) if isinstance(stored, dict) else {}
    if not isinstance(sandboxes, dict) or not sandboxes:
        return False
    return all(
        isinstance(sandbox, dict) and set(sandbox) <= {"run_at_startup"}
        for sandbox in sandboxes.values()
    )


class ParityTests(unittest.TestCase):
    def test_live_root_matches_reference(self) -> None:
        if not (ROOT / "config.json5").exists():
            self.skipTest("live config.json5 is not present")
        if root_is_trimmed(ROOT):
            self.skipTest("live root is trimmed; trim parity is covered by test_settings_migration")
        temporary = Path(tempfile.mkdtemp(prefix="sndbx-parity-"))
        try:
            copy_live_root(temporary)
            expected = reference_config(temporary)
            runtime = SettingsRuntime(temporary).start().config
            differences = list(leaf_differences(expected, runtime))
            self.assertEqual(differences, [])
        finally:
            shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
