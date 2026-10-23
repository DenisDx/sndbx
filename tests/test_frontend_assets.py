"""Tests for the Web UI frontend assets and the fixed frontend defects."""

from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "webui" / "frontend"
INDEX = (FRONTEND / "index.html").read_text(encoding="utf-8")
APP_JS = (FRONTEND / "app.js").read_text(encoding="utf-8")


class VendoredAssetTests(unittest.TestCase):
    """Verify that the console works without the CDN."""

    def test_vendor_assets_are_present(self) -> None:
        """Ship the xterm assets locally."""
        vendor = FRONTEND / "vendor"
        for name in ("xterm.min.js", "addon-fit.min.js", "xterm.min.css"):
            self.assertTrue((vendor / name).is_file(), name)

    def test_vendor_assets_are_not_empty(self) -> None:
        """Store real content, not placeholders."""
        vendor = FRONTEND / "vendor"
        for name in ("xterm.min.js", "addon-fit.min.js", "xterm.min.css"):
            self.assertGreater((vendor / name).stat().st_size, 500, name)

    def test_index_falls_back_to_the_vendor_copies(self) -> None:
        """Reference the local copies from the CDN tags."""
        self.assertIn("/vendor/xterm.min.js", INDEX)
        self.assertIn("/vendor/addon-fit.min.js", INDEX)
        self.assertIn("/vendor/xterm.min.css", INDEX)

    def test_index_still_prefers_the_cdn(self) -> None:
        """Keep the CDN as the primary source."""
        self.assertIn("cdn.jsdelivr.net/npm/xterm@5.3.0/lib/xterm.min.js", INDEX)
        self.assertIn("cdn.jsdelivr.net/npm/xterm@5.3.0/css/xterm.min.css", INDEX)

    def test_frontend_loads_the_vendor_assets(self) -> None:
        """Serve the vendor directory as a static path."""
        self.assertTrue((FRONTEND / "vendor").is_dir())


class FrontendDefectTests(unittest.TestCase):
    """Verify the frontend fixes for the recorded defects."""

    def test_mcp_test_tab_is_initialized_after_login(self) -> None:
        """Initialize the MCP test tab in the login handler too."""
        login_body = APP_JS.split("async function onLogin()", 1)[1].split("\n}", 1)[0]
        self.assertIn("initMCPTest()", login_body)

    def test_auto_start_tooltip_names_the_root_config(self) -> None:
        """Describe where the auto-start value is stored."""
        self.assertIn("run_at_startup=true in the root config.json5", APP_JS)
        self.assertNotIn("data/sandbox_startup_overrides.json", APP_JS)

    def test_console_waits_for_the_terminal_assets(self) -> None:
        """Wait for the terminal assets before opening the console."""
        self.assertIn("async function ensureTerminalLoaded()", APP_JS)
        self.assertIn("await ensureTerminalLoaded()", APP_JS)

    def test_console_loads_the_local_copy_as_a_fallback(self) -> None:
        """Load the vendored scripts when the CDN is unreachable."""
        self.assertIn('loadScript("/vendor/xterm.min.js")', APP_JS)
        self.assertIn('loadScript("/vendor/addon-fit.min.js")', APP_JS)


if __name__ == "__main__":
    unittest.main()
