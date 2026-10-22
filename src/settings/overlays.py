"""Discovery and merge of per-image sndbx.overlay.json5 documents."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from aiset.jsonsrc import Document
from aiset.merge import merge

OVERLAY_NAME = "sndbx.overlay.json5"
ALLOWED_TOP_LEVEL = {"version", "sandboxes", "envids"}
# Demo sandboxes may use a public image while their overlay lives in the
# local demo image directory.
DEMO_IMAGE_DIRS = {
    "sandbox-1": "default_sandbox_1",
    "default-sandbox-1": "default_sandbox_1",
}


class OverlayError(Exception):
    """Invalid overlay layout or content outside the descriptor."""


@dataclass(frozen=True)
class OverlayDocument:
    """One overlay file and the sandbox ids it declares."""

    path: Path
    directory: str
    document_id: str
    label: str
    sandbox_ids: tuple[str, ...]


def overlay_dir_for(root_dir: Path, sandbox_id: str, sandbox: dict[str, Any]) -> Path | None:
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
        path = root_dir / "images" / candidate
        if path.is_dir():
            return path
    for candidate in candidates:
        path = root_dir / "images" / candidate.replace("-", "_")
        if path.is_dir():
            return path
    return None


def stored_top_level_keys(path: Path) -> list[str]:
    """Read the top-level keys of an overlay without resolving placeholders."""
    document = Document(path.read_text(encoding="utf-8"), origin=path.name)
    stored = document.stored_value(document.root)
    return list(stored) if isinstance(stored, dict) else []


def stored_sandbox_ids(path: Path) -> list[str]:
    """Read sandbox ids declared by an overlay file."""
    document = Document(path.read_text(encoding="utf-8"), origin=path.name)
    stored = document.stored_value(document.root)
    items = stored.get("sandboxes", {}).get("items", {}) if isinstance(stored, dict) else {}
    return list(items) if isinstance(items, dict) else []


def discover_overlays(root_dir: Path, root_effective: dict[str, Any]) -> list[OverlayDocument]:
    """Find, validate and label every images/*/sndbx.overlay.json5."""
    root_sandboxes = root_effective.get("sandboxes", {}).get("items", {})
    overlays: list[OverlayDocument] = []
    covered: dict[str, Path] = {}

    for path in sorted((root_dir / "images").glob(f"*/{OVERLAY_NAME}")):
        directory = path.parent.name
        top_level = stored_top_level_keys(path)
        unexpected = set(top_level) - ALLOWED_TOP_LEVEL
        if unexpected:
            raise OverlayError(
                f"{path}: unsupported top-level section(s): {', '.join(sorted(unexpected))}"
            )
        ids = stored_sandbox_ids(path)
        if not ids:
            raise OverlayError(f"{path}: sandboxes.items is empty")
        for sandbox_id in ids:
            if sandbox_id not in root_sandboxes:
                raise OverlayError(f"{path}: sandbox {sandbox_id!r} is not registered in config.json5")
            if sandbox_id in covered:
                raise OverlayError(
                    f"{path}: sandbox {sandbox_id!r} is already covered by {covered[sandbox_id]}"
                )
            expected = overlay_dir_for(root_dir, sandbox_id, root_sandboxes[sandbox_id])
            if expected is None:
                raise OverlayError(f"{path}: no image directory found for sandbox {sandbox_id!r}")
            if expected.name != directory:
                raise OverlayError(
                    f"{path}: sandbox {sandbox_id!r} belongs to images/{expected.name}"
                )
            covered[sandbox_id] = path
        if len(ids) == 1:
            document_id = f"sandbox:{ids[0]}"
            label = f"Sandbox: {ids[0]}"
        else:
            document_id = f"sandbox-group:{directory}"
            label = f"Image: {directory} ({', '.join(ids)})"
        overlays.append(
            OverlayDocument(
                path=path,
                directory=directory,
                document_id=document_id,
                label=label,
                sandbox_ids=tuple(ids),
            )
        )
    return overlays


def merge_runtime(root_effective: dict[str, Any], overlay_effectives: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Merge overlay defaults under the root; the root wins every conflict."""
    runtime = copy.deepcopy(root_effective)
    runtime.setdefault("sandboxes", {}).setdefault("items", {})
    for effective in overlay_effectives.values():
        for sandbox_id, overlay_sandbox in (
            effective.get("sandboxes", {}).get("items", {}) or {}
        ).items():
            base = runtime["sandboxes"]["items"].get(sandbox_id, {})
            runtime["sandboxes"]["items"][sandbox_id] = merge(
                base, overlay_sandbox, prefer="base"
            )
        if "envids" in effective:
            runtime["envids"] = merge(
                runtime.get("envids", {}), effective["envids"], prefer="base"
            )
    return runtime
