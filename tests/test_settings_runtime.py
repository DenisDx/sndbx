"""Runtime bootstrap, overlay merge and auto-start write tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from aiset.service import Principal  # noqa: E402
from config import ConfigError, load_config  # noqa: E402
from settings.runtime import SettingsRuntime  # noqa: E402

from tests.settings_fixtures import TempRoot  # noqa: E402


class RuntimeTests(unittest.TestCase):
    def test_root_wins_and_overlay_adds(self) -> None:
        with TempRoot() as root:
            runtime = SettingsRuntime(root).start()
            demo = runtime.config["sandboxes"]["items"]["demo"]
            self.assertEqual(demo["title"], "Root demo")
            self.assertEqual(demo["memory"], "1G")
            self.assertTrue(demo["configure_apt_mirror"])
            self.assertEqual(runtime.config["root"], str(root))
            self.assertIn("sandbox:demo", {doc["id"] for doc in runtime.document_catalog()})

    def test_missing_overlay_is_allowed(self) -> None:
        with TempRoot() as root:
            (root / "images" / "demo" / "sndbx.overlay.json5").unlink()
            runtime = SettingsRuntime(root).start()
            self.assertEqual(runtime.config["sandboxes"]["items"]["demo"]["title"], "Root demo")
            self.assertEqual([doc["id"] for doc in runtime.document_catalog()], ["main"])

    def test_invalid_root_is_refused(self) -> None:
        with TempRoot() as root:
            path = root / "config.json5"
            path.write_text(path.read_text() + "\n// broken\n", encoding="utf-8")
            path.write_text(path.read_text().replace("version: \"1.0\",", "version: \"1.0\",\n  unknown_key: 1,"), encoding="utf-8")
            with self.assertRaises(ConfigError):
                SettingsRuntime(root).start()

    def test_invalid_overlay_section_is_refused(self) -> None:
        with TempRoot() as root:
            overlay = root / "images" / "demo" / "sndbx.overlay.json5"
            overlay.write_text(
                '{ logging: { level: "info" }, sandboxes: { items: { demo: {} } } }',
                encoding="utf-8",
            )
            with self.assertRaises(ConfigError):
                SettingsRuntime(root).start()

    def test_apply_auto_start_writes_root(self) -> None:
        with TempRoot() as root:
            runtime = SettingsRuntime(root).start()
            runtime.apply_auto_start("demo", True, Principal.demo())
            text = (root / "config.json5").read_text(encoding="utf-8")
            self.assertIn("run_at_startup: true", text)
            self.assertTrue(runtime.config["sandboxes"]["items"]["demo"]["run_at_startup"])
            self.assertTrue((root / "config.json5.back").exists())

    def test_load_config_facade_returns_settings(self) -> None:
        with TempRoot() as root:
            data = load_config(str(root))
            self.assertEqual(data["config"]["title"], "test sndbx")
            self.assertIsNotNone(data["settings"])
            self.assertIn("sandbox:demo", {doc["id"] for doc in data["settings"].document_catalog()})

    def test_missing_config_has_legacy_error(self) -> None:
        with TempRoot() as root:
            (root / "config.json5").unlink()
            with self.assertRaises(ConfigError) as caught:
                SettingsRuntime(root).start()
            self.assertIn("Config file not found", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
