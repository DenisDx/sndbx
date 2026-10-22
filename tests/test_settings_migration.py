"""Root-trim migration preserves the effective runtime configuration."""

from __future__ import annotations

import copy
import shutil
import tempfile
import unittest
from pathlib import Path

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from settings.runtime import SettingsRuntime  # noqa: E402
from scripts.migrate_sandbox_overlays import (  # noqa: E402
    import_startup_overrides,
    trim_root_at,
)

from tests.test_settings_parity import leaf_differences  # noqa: E402


class MigrationTests(unittest.TestCase):
    def test_trim_preserves_effective_config(self) -> None:
        if not (ROOT / "config.json5").exists():
            self.skipTest("live config.json5 is not present")
        temp = Path(tempfile.mkdtemp(prefix="sndbx-migration-"))
        try:
            for name in ("config.json5", "config.descriptor.json5", "config.types.json5"):
                shutil.copy2(ROOT / name, temp / name)
            env_lines = (ROOT / ".env").read_text(encoding="utf-8").splitlines()
            env_lines = [
                line
                for line in env_lines
                if not line.strip().startswith("SNDBX_ROOT=")
            ]
            env_lines.append(f"SNDBX_ROOT={temp}")
            (temp / ".env").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
            if (ROOT / "data" / "sandbox_startup_overrides.json").exists():
                (temp / "data").mkdir()
                shutil.copy2(
                    ROOT / "data" / "sandbox_startup_overrides.json",
                    temp / "data" / "sandbox_startup_overrides.json",
                )
            for overlay in (ROOT / "images").glob("*/sndbx.overlay.json5"):
                target = temp / "images" / overlay.parent.name
                target.mkdir(parents=True)
                shutil.copy2(overlay, target / overlay.name)

            before = SettingsRuntime(temp).start()
            expected = copy.deepcopy(before.config)
            import_startup_overrides(temp, False)
            trim_root_at(temp, False)

            after = SettingsRuntime(temp).start().config
            self.assertEqual(list(leaf_differences(expected, after)), [])
            text = (temp / "config.json5").read_text(encoding="utf-8")
            self.assertNotIn("shared_directories", text)
            self.assertIn("run_at_startup", text)
        finally:
            shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
