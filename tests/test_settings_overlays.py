"""Overlay discovery and merge semantics."""

from __future__ import annotations

import shutil
import tempfile
import unittest
from pathlib import Path

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from settings.overlays import (  # noqa: E402
    OverlayError,
    discover_overlays,
    merge_runtime,
    overlay_dir_for,
)


class MergeTests(unittest.TestCase):
    def test_root_wins_scalars_and_arrays(self) -> None:
        root = {
            "sandboxes": {
                "items": {
                    "demo": {
                        "title": "root",
                        "memory": "1G",
                        "tags": ["root"],
                        "shared_directories": [{"host_path": "/root"}],
                    }
                }
            },
            "envids": {"token": {"sandbox": "demo"}},
        }
        overlay = {
            "sandboxes": {
                "items": {
                    "demo": {
                        "title": "overlay",
                        "memory": "2G",
                        "tags": ["overlay"],
                        "shared_directories": [{"host_path": "/overlay"}],
                        "configure_apt_mirror": True,
                    }
                }
            },
            "envids": {"token": {"sandbox": "other"}, "new": {"sandbox": "demo"}},
        }
        merged = merge_runtime(root, {"sandbox:demo": overlay})
        demo = merged["sandboxes"]["items"]["demo"]
        self.assertEqual(demo["title"], "root")
        self.assertEqual(demo["memory"], "1G")
        self.assertEqual(demo["tags"], ["root"])
        self.assertEqual(demo["shared_directories"], [{"host_path": "/root"}])
        self.assertTrue(demo["configure_apt_mirror"])
        self.assertEqual(merged["envids"]["token"]["sandbox"], "demo")
        self.assertIn("new", merged["envids"])

    def test_overlay_only_key_is_added(self) -> None:
        merged = merge_runtime(
            {"sandboxes": {"items": {"demo": {}}}},
            {"sandbox:demo": {"sandboxes": {"items": {"demo": {"title": "x"}}}}},
        )
        self.assertEqual(merged["sandboxes"]["items"]["demo"]["title"], "x")


class DiscoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.path = Path(tempfile.mkdtemp(prefix="sndbx-overlays-"))
        (self.path / "images" / "demo").mkdir(parents=True)
        (self.path / "images" / "other").mkdir(parents=True)
        self.root = {
            "sandboxes": {
                "items": {
                    "demo": {"image": "demo:latest"},
                    "demo2": {"image": "demo:latest"},
                    "other": {"image": "other:latest"},
                }
            }
        }

    def tearDown(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def write(self, directory: str, text: str) -> Path:
        path = self.path / "images" / directory / "sndbx.overlay.json5"
        path.write_text(text, encoding="utf-8")
        return path

    def test_document_ids(self) -> None:
        self.write(
            "demo",
            '{ sandboxes: { items: { demo: { title: "d" }, demo2: { title: "d2" } } } }',
        )
        self.write("other", '{ sandboxes: { items: { other: { title: "o" } } } }')
        overlays = discover_overlays(self.path, self.root)
        ids = {overlay.document_id for overlay in overlays}
        self.assertEqual(ids, {"sandbox-group:demo", "sandbox:other"})

    def test_rejects_unregistered_sandbox(self) -> None:
        self.write("demo", '{ sandboxes: { items: { missing: { title: "x" } } } }')
        with self.assertRaises(OverlayError):
            discover_overlays(self.path, self.root)

    def test_rejects_wrong_directory(self) -> None:
        self.write("other", '{ sandboxes: { items: { demo: { title: "x" } } } }')
        with self.assertRaises(OverlayError):
            discover_overlays(self.path, self.root)

    def test_rejects_global_section(self) -> None:
        self.write(
            "demo",
            '{ logging: { level: "info" }, sandboxes: { items: { demo: { title: "x" } } } }',
        )
        with self.assertRaises(OverlayError):
            discover_overlays(self.path, self.root)

    def test_demo_alias(self) -> None:
        (self.path / "images" / "default_sandbox_1").mkdir()
        path = overlay_dir_for(self.path, "sandbox-1", {"image": "ubuntu:22.04"})
        self.assertEqual(path.name, "default_sandbox_1")


if __name__ == "__main__":
    unittest.main()
