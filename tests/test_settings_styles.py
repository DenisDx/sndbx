"""Static checks for the host stylesheet and asset load order."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FRONTEND = ROOT / "src" / "webui" / "frontend"


class SettingsStyleTests(unittest.TestCase):
    def test_asset_order(self) -> None:
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        aiset_css = html.index("/api/settings/assets/aiset.css")
        host_theme = html.index("/api/settings/assets/aiset-host-theme.css")
        host_css = html.index("/settings.css")
        self.assertLess(aiset_css, host_theme)
        self.assertLess(host_theme, host_css)

        aiset_js = html.index("/api/settings/assets/aiset.js")
        tree_js = html.index("/api/settings/assets/aiset-tree.js")
        app_js = html.index("/app.js")
        self.assertLess(aiset_js, tree_js)
        self.assertLess(tree_js, app_js)

    def test_theme_uses_variables_and_scopes_selectors(self) -> None:
        css = (FRONTEND / "settings.css").read_text(encoding="utf-8")
        self.assertIn("--aiset-accent", css)
        self.assertIn("--aiset-surface", css)
        self.assertIn("color-scheme: dark", css)
        self.assertIn(".aiset-tree-editor", css)
        self.assertIn(".aiset-form", css)
        unscoped = re.findall(
            r"(?m)^\s*(input|button|select|textarea|table|div|span)\s*\{", css
        )
        self.assertEqual(unscoped, [])

    def test_required_classes_are_painted_by_library_css(self) -> None:
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not available")
        assets = ROOT / "aiset" / "src" / "aiset" / "assets"
        script = r'''
const fs = require("fs");
const vm = require("vm");
global.window = { innerWidth: 1280, innerHeight: 800 };
const stub = () => ({ style: {}, dataset: {}, children: [], setAttribute() {},
  getAttribute() { return null; }, addEventListener() {},
  appendChild(child) { this.children.push(child); return child; },
  querySelector() { return null; }, querySelectorAll() { return []; } });
global.document = { createElement: stub, createElementNS: stub,
  getElementById: () => null, querySelector: () => null,
  querySelectorAll: () => [], addEventListener() {} };
for (const file of AISET_FILES) {
  vm.runInThisContext(fs.readFileSync(file, "utf8"), { filename: file });
}
process.stdout.write(JSON.stringify(window.aiset.classes()));
'''.replace("AISET_FILES", json.dumps([str(assets / "aiset.js"), str(assets / "aiset-tree.js")]))
        output = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
        inventory = json.loads(output.stdout)
        library_css = (assets / "aiset.css").read_text(encoding="utf-8")
        required = {entry["name"] for entry in inventory if entry.get("required")}
        missing = sorted(name for name in required if f".{name}" not in library_css)
        self.assertEqual(missing, [])

    def test_editor_mount_target_exists(self) -> None:
        html = (FRONTEND / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="settings-tree"', html)
        self.assertIn('id="settings-document"', html)


if __name__ == "__main__":
    unittest.main()
