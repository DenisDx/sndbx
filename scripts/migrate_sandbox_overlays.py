#!/usr/bin/env python3
"""Generate per-image sndbx.overlay.json5 files from the root config.

The root config is parsed with aiset's source-preserving parser, so quoted and
unquoted ${VAR} placeholders are written to the overlay unchanged. The root
keeps `run_at_startup`; every other sandbox field moves to the overlay.

Usage:
    python scripts/migrate_sandbox_overlays.py --dry-run
    python scripts/migrate_sandbox_overlays.py --write-overlays
    python scripts/migrate_sandbox_overlays.py --trim-root [--overrides FILE]

`--trim-root` replaces every root `sandboxes.items.<id>` with
`{ run_at_startup: <current> }` through aiset, after taking a manual backup.
"""

from __future__ import annotations

import argparse
import json
import shutil
import sys
from collections import defaultdict
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
AISET_SRC = ROOT / "aiset" / "src"
if AISET_SRC.is_dir():
    sys.path.insert(0, str(AISET_SRC))

from aiset.jsonsrc import Document, TemplateRef  # noqa: E402
from aiset.service import Principal, Service  # noqa: E402
from aiset.resolver import Resolver  # noqa: E402

DESCRIPTOR = ROOT / "config.descriptor.json5"
TYPES = ROOT / "config.types.json5"
ROOT_CONFIG = ROOT / "config.json5"
OVERLAY_NAME = "sndbx.overlay.json5"
LEGACY_NAME = "config.sndbx.json5"
# Demo sandboxes may use a public image (for example ubuntu:22.04) while their
# host overlay still belongs to the local demo image directory.
DEMO_IMAGE_DIRS = {
    "sandbox-1": "default_sandbox_1",
    "default-sandbox-1": "default_sandbox_1",
}


def read_env(path: Path) -> dict[str, str]:
    """Read simple KEY=VALUE pairs from a .env file."""
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


def parse_root(path: Path) -> dict[str, Any]:
    """Return the stored root document with placeholders kept explicit."""
    document = Document(path.read_text(encoding="utf-8"), origin=path.name)
    return document.stored_value(document.root)


def overlay_dir_for(sandbox_id: str, sandbox: dict[str, Any]) -> Path | None:
    """Resolve images/<dir> for a sandbox id, preferring local_image_id."""
    candidates: list[str] = []
    local_id = sandbox.get("local_image_id")
    if isinstance(local_id, str) and local_id:
        candidates.append(local_id)
    image = sandbox.get("image")
    if isinstance(image, str) and image:
        candidates.append(image.split(":", 1)[0])
    candidates.append(sandbox_id)
    alias = DEMO_IMAGE_DIRS.get(sandbox_id)
    if alias:
        candidates.append(alias)
    for candidate in candidates:
        path = ROOT / "images" / candidate
        if path.is_dir():
            return path
    for candidate in candidates:
        path = ROOT / "images" / candidate.replace("-", "_")
        if path.is_dir():
            return path
    return None


def to_json5(value: Any, indent: int = 0) -> str:
    """Serialize a stored aiset tree, keeping TemplateRef unquoted."""
    pad = "  " * indent
    if isinstance(value, TemplateRef):
        return value.source_text()
    if isinstance(value, dict):
        if not value:
            return "{}"
        lines = ["{"]
        items = list(value.items())
        for index, (key, item) in enumerate(items):
            comma = "," if index < len(items) - 1 else ""
            lines.append(f"{pad}  {json.dumps(key, ensure_ascii=False)}: {to_json5(item, indent + 1)}{comma}")
        lines.append(f"{pad}}}")
        return "\n".join(lines)
    if isinstance(value, list):
        if not value:
            return "[]"
        lines = ["["]
        for index, item in enumerate(value):
            comma = "," if index < len(value) - 1 else ""
            lines.append(f"{pad}  {to_json5(item, indent + 1)}{comma}")
        lines.append(f"{pad}]")
        return "\n".join(lines)
    return json.dumps(value, ensure_ascii=False)


def overlay_text(ids: list[str], items: dict[str, Any]) -> str:
    """Build one overlay document for a set of sandbox ids."""
    header = (
        "// sndbx per-image overlay generated from the root config.\n"
        f"// Sandbox ids: {', '.join(ids)}\n"
        "// These are VM defaults. The root config wins every conflict; a root\n"
        "// array replaces this list entirely. run_at_startup is owned by the root.\n"
    )
    payload = {"sandboxes": {"items": items}}
    return header + to_json5(payload) + "\n"


def build_overlays(root_config: dict[str, Any], effective: dict[str, Any]) -> dict[Path, str]:
    """Group sandbox objects by image directory and render overlay files."""
    sandboxes = root_config.get("sandboxes", {}).get("items", {})
    effective_sandboxes = effective.get("sandboxes", {}).get("items", {})
    grouped: dict[Path, dict[str, Any]] = defaultdict(dict)
    for sandbox_id, sandbox in sandboxes.items():
        if not isinstance(sandbox, dict):
            raise SystemExit(f"sandbox {sandbox_id!r} is not an object")
        directory = overlay_dir_for(sandbox_id, effective_sandboxes.get(sandbox_id, {}))
        if directory is None:
            raise SystemExit(f"no images/<dir> found for sandbox {sandbox_id!r}")
        overlay_sandbox = {
            key: value for key, value in sandbox.items() if key != "run_at_startup"
        }
        grouped[directory][sandbox_id] = overlay_sandbox
    return {
        directory / OVERLAY_NAME: overlay_text(sorted(items), items)
        for directory, items in grouped.items()
    }


def write_overlays(overlays: dict[Path, str], dry_run: bool) -> None:
    """Write overlay files; keep the legacy template as a .bak next to it."""
    for path, text in sorted(overlays.items()):
        print(f"{'would write' if dry_run else 'write'}: {path.relative_to(ROOT)}")
        if dry_run:
            continue
        legacy = path.with_name(LEGACY_NAME)
        if legacy.exists():
            backup = legacy.with_name(LEGACY_NAME + ".pre-overlay.bak")
            shutil.copy2(legacy, backup)
            legacy.unlink()
        path.write_text(text, encoding="utf-8")


def service_for_root(root: Path = ROOT) -> Service:
    """Create a service with the root document registered and reread."""
    descriptor = (root / DESCRIPTOR.name).read_text(encoding="utf-8")
    types = (root / TYPES.name).read_text(encoding="utf-8")
    env = read_env(root / ".env")
    env.setdefault("SNDBX_ROOT", str(root))
    service = Service()
    service.register(
        "main",
        str(root / ROOT_CONFIG.name),
        descriptor,
        typedef_sources=(types,),
        resolver=Resolver(overrides={}, dotenv=env, environ={}),
    )
    service.reread("main")
    return service


def import_startup_overrides(root: Path = ROOT, dry_run: bool = False) -> None:
    """Move data/sandbox_startup_overrides.json into the root config."""
    path = root / "data" / "sandbox_startup_overrides.json"
    if not path.exists():
        print("no legacy startup overrides to import")
        return
    try:
        overrides = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"invalid {path}: {error}") from error
    if not isinstance(overrides, dict):
        raise SystemExit(f"invalid {path}: expected an object")

    service = service_for_root(root)
    result = service.reread("main")
    sandboxes = result.effective.get("sandboxes", {}).get("items", {})
    operations = []
    for sandbox_id, enabled in overrides.items():
        if sandbox_id not in sandboxes or not isinstance(enabled, bool):
            print(f"skip: unknown sandbox or non-boolean value: {sandbox_id!r}")
            continue
        operations.append(
            {
                "op": "set",
                "path": f"/sandboxes/items/{sandbox_id}/run_at_startup",
                "value_text": "true" if enabled else "false",
            }
        )
    if not operations:
        print("no applicable startup overrides")
        return
    print(f"{'would import' if dry_run else 'import'} {len(operations)} startup overrides")
    if dry_run:
        return
    service.apply_operations(
        "main",
        operations,
        expected_revision=result.revision,
        expected_descriptor_revision=result.descriptor_revision,
        principal=Principal.demo(),
    )
    path.replace(path.with_name(path.name + ".migrated"))
    print(f"renamed {path.name} -> {path.name}.migrated")


def trim_root_at(root: Path, dry_run: bool) -> None:
    """Reduce every root sandbox entry to {run_at_startup: <current>}.

    aiset refuses a whole-object set that would drop commented members, so this
    one-time migration edits the source spans of the sandbox objects directly.
    Comments outside those objects are preserved; a manual backup is kept.
    """
    config_path = root / ROOT_CONFIG.name
    legacy = root / "data" / "sandbox_startup_overrides.json"
    if legacy.exists():
        if dry_run:
            print(f"warning: {legacy} still exists; run --import-startup-overrides first")
        else:
            raise SystemExit(
                f"{legacy} still exists; run --import-startup-overrides before --trim-root"
            )
    document = Document(config_path.read_text(encoding="utf-8"), origin=config_path.name)
    stored = document.stored_value(document.root)
    items = stored.get("sandboxes", {}).get("items", {}) if isinstance(stored, dict) else {}
    if not isinstance(items, dict) or not items:
        print("no sandboxes to trim")
        return
    replacements = []
    for sandbox_id, sandbox in items.items():
        node = document.find(f"/sandboxes/items/{sandbox_id}")
        if node is None:
            print(f"skip: node not found for {sandbox_id!r}")
            continue
        enabled = bool(sandbox.get("run_at_startup", False)) if isinstance(sandbox, dict) else False
        value_text = "{ run_at_startup: " + ("true" if enabled else "false") + " }"
        replacements.append((node.start, node.end, value_text))
    print(f"{'would trim' if dry_run else 'trim'} {len(replacements)} root sandbox entries")
    if dry_run:
        return
    backup = config_path.with_name(config_path.name + ".pre-overlay.bak")
    if not backup.exists():
        shutil.copy2(config_path, backup)
    text = document.text
    for start, end, replacement in sorted(replacements, reverse=True):
        text = text[:start] + replacement + text[end:]
    temporary = config_path.with_name(config_path.name + ".trim.tmp")
    temporary.write_text(text, encoding="utf-8")
    # Validate before replacing the live file.
    probe = service_for_root(root)
    probe_result = probe.validate_document(
        "main", text, principal=Principal.demo()
    )
    if not probe_result.valid:
        temporary.unlink(missing_ok=True)
        raise SystemExit(
            "trimmed config did not validate: "
            + "; ".join(issue.message for issue in probe_result.issues[:10])
        )
    temporary.replace(config_path)
    print(f"backup: {backup.name}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="print actions only")
    parser.add_argument("--write-overlays", action="store_true", help="write overlay files")
    parser.add_argument("--trim-root", action="store_true", help="reduce root entries")
    parser.add_argument(
        "--import-startup-overrides",
        action="store_true",
        help="move data/sandbox_startup_overrides.json into the root config",
    )
    args = parser.parse_args()

    if not (args.write_overlays or args.trim_root or args.import_startup_overrides):
        parser.error(
            "choose --write-overlays, --trim-root, --import-startup-overrides or a combination"
        )
    if not ROOT_CONFIG.exists():
        print(f"error: {ROOT_CONFIG} not found", file=sys.stderr)
        return 2

    root_config = parse_root(ROOT_CONFIG)
    effective = (
        __import__("aiset.service", fromlist=["Service"])
        .Service()
    )
    # Effective values are needed only to resolve image/local_image_id placeholders.
    effective_service = service_for_root()
    overlays = build_overlays(root_config, effective_service.reread("main").effective)
    print(f"overlay files: {len(overlays)}; sandbox ids: {len(root_config.get('sandboxes', {}).get('items', {}))}")
    if args.write_overlays:
        write_overlays(overlays, args.dry_run)
    if args.import_startup_overrides:
        import_startup_overrides(ROOT, args.dry_run)
    if args.trim_root:
        trim_root_at(ROOT, args.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
