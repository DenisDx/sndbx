"""Descriptor and overlay completeness tests."""

from __future__ import annotations

import unittest
from pathlib import Path

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from aiset.descriptor import (  # noqa: E402
    _collect_typedefs,
    parse_descriptor,
    validate_descriptor,
)
from aiset.resolver import Resolver  # noqa: E402
from aiset.service import Service  # noqa: E402

from settings.overlays import discover_overlays  # noqa: E402


class DescriptorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.descriptor = (ROOT / "config.descriptor.json5").read_text(encoding="utf-8")
        self.types = (ROOT / "config.types.json5").read_text(encoding="utf-8")
        self.templates = _collect_typedefs(parse_descriptor(self.types))

    def test_descriptor_is_strictly_valid(self) -> None:
        diagnostic = validate_descriptor(
            self.descriptor, mode="strict", known_templates=self.templates
        )
        issues = [issue.to_dict() for issue in diagnostic.issues]
        self.assertTrue(diagnostic.valid, issues)

    def test_shipped_documents_load_without_errors(self) -> None:
        paths = [ROOT / "config.json5.example"]
        if (ROOT / "config.json5").exists():
            paths.insert(0, ROOT / "config.json5")
        paths.extend(sorted((ROOT / "images").glob("*/sndbx.overlay.json5")))
        for path in paths:
            with self.subTest(path=path.name):
                service = Service()
                service.register(
                    "d",
                    str(path),
                    self.descriptor,
                    typedef_sources=(self.types,),
                    resolver=Resolver(overrides={}, dotenv={}, environ={}),
                )
                result = service.reread("d")
                errors = [issue for issue in result.issues if issue.severity == "error"]
                self.assertFalse(result.invalid_on_disk, [issue.to_dict() for issue in errors])
                self.assertEqual(result.loaded, "file")
                self.assertEqual([issue.to_dict() for issue in errors], [])

    def test_every_sandbox_has_one_overlay(self) -> None:
        config_path = ROOT / "config.json5"
        if not config_path.exists():
            self.skipTest("live config.json5 is not present")
        service = Service()
        service.register(
            "d",
            str(config_path),
            self.descriptor,
            typedef_sources=(self.types,),
            resolver=Resolver(overrides={}, dotenv={}, environ={}),
        )
        effective = service.reread("d").effective
        sandboxes = set(effective.get("sandboxes", {}).get("items", {}))
        overlays = discover_overlays(ROOT, effective)
        covered = {sandbox_id for overlay in overlays for sandbox_id in overlay.sandbox_ids}
        self.assertEqual(covered, sandboxes)

    def test_overlays_have_no_outside_sections(self) -> None:
        for path in sorted((ROOT / "images").glob("*/sndbx.overlay.json5")):
            with self.subTest(path=path.name):
                from aiset.jsonsrc import Document

                document = Document(path.read_text(encoding="utf-8"))
                stored = document.stored_value(document.root)
                self.assertLessEqual(
                    set(stored), {"version", "sandboxes", "envids"}, path
                )


if __name__ == "__main__":
    unittest.main()
