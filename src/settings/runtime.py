"""Build and cache the aiset service for the sndbx root and its overlays."""

from __future__ import annotations

import copy
import os
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .aiset_path import ensure_aiset

HOST_ROOT = Path(__file__).resolve().parents[2]
ensure_aiset(HOST_ROOT)

from aiset.asgi import create_asgi_app  # noqa: E402,F401
from aiset.errors import AisetError, RevisionConflict  # noqa: E402
from aiset.resolver import Resolver  # noqa: E402
from aiset.service import Principal, Service  # noqa: E402

from config import ConfigError  # noqa: E402
from logging_utils import configure_logging, get_logger  # noqa: E402

from .overlays import (  # noqa: E402
    OverlayDocument,
    OverlayError,
    discover_overlays,
    merge_runtime,
)

DESCRIPTOR_NAME = "config.descriptor.json5"
TYPES_NAME = "config.types.json5"
CONFIG_NAME = "config.json5"


def read_env_file(path: Path) -> Dict[str, str]:
    """Read KEY=VALUE pairs with the same rules as the legacy loader."""
    mapping: Dict[str, str] = {}
    if not path.exists():
        return mapping
    with path.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            mapping[key.strip()] = value.strip()
    return mapping


class SettingsRuntime:
    """One aiset Service, the root document and every image overlay."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root_dir = Path(root_dir).resolve()
        self.env: Dict[str, str] = {}
        self.config: Dict[str, Any] = {}
        self.service: Optional[Service] = None
        self.documents: List[Dict[str, Any]] = []
        self.overlays: List[OverlayDocument] = []
        self._legacy_startup_fallback: Dict[str, bool] = {}
        self._started = False

    def start(self) -> "SettingsRuntime":
        """Register, validate and merge every document; idempotent."""
        if self._started:
            return self
        root = self.root_dir
        config_path = root / CONFIG_NAME
        descriptor_path = root / DESCRIPTOR_NAME
        types_path = root / TYPES_NAME

        if not config_path.exists():
            raise ConfigError(f"Config file not found: {config_path}")
        if not descriptor_path.exists():
            raise ConfigError(f"Descriptor file not found: {descriptor_path}")
        if not types_path.exists():
            raise ConfigError(f"Descriptor types file not found: {types_path}")

        self.env = read_env_file(root / ".env")
        self.env.setdefault("SNDBX_ROOT", str(root))
        descriptor = descriptor_path.read_text(encoding="utf-8")
        typedef_sources = (types_path.read_text(encoding="utf-8"),)

        service = Service()
        self.service = service
        try:
            service.register(
                "main",
                str(config_path),
                descriptor,
                typedef_sources=typedef_sources,
                resolver=Resolver(overrides={}, dotenv=self.env, environ=os.environ),
            )
            main_result = service.reread("main")
            self._require_valid("main", main_result)
            self._migrate_legacy_startup_overrides(service, main_result)
            main_result = service.reread("main")
            root_effective = main_result.effective

            try:
                self.overlays = discover_overlays(root, root_effective)
            except OverlayError as error:
                raise ConfigError(str(error)) from error

            overlay_effective: Dict[str, Dict[str, Any]] = {}
            for overlay in self.overlays:
                service.register(
                    overlay.document_id,
                    str(overlay.path),
                    descriptor,
                    typedef_sources=typedef_sources,
                    resolver=Resolver(overrides={}, dotenv=self.env, environ=os.environ),
                )
                result = service.reread(overlay.document_id)
                self._require_valid(str(overlay.path), result)
                overlay_effective[overlay.document_id] = result.effective

            self.config = merge_runtime(root_effective, overlay_effective)
            for sandbox_id, enabled in self._legacy_startup_fallback.items():
                sandbox = self.config.get("sandboxes", {}).get("items", {}).get(sandbox_id)
                if isinstance(sandbox, dict) and isinstance(enabled, bool):
                    sandbox["run_at_startup"] = enabled
            self._preflight(self.config)
            self.documents = [
                {
                    "id": "main",
                    "label": "Root configuration (overrides)",
                    "sandboxes": [],
                }
            ] + [
                {
                    "id": overlay.document_id,
                    "label": overlay.label,
                    "sandboxes": list(overlay.sandbox_ids),
                }
                for overlay in self.overlays
            ]
        except AisetError as error:
            raise ConfigError(f"Configuration error: {error}") from error

        service.on_after_save(self._after_save)
        self._started = True
        return self

    def _require_valid(self, document: str, result: Any) -> None:
        """Refuse an invalid file or a silent LKG fallback."""
        if result.invalid_on_disk or result.loaded != "file":
            details = "; ".join(
                f"{issue.path or '/'}: {issue.message}"
                for issue in result.issues
                if issue.severity == "error"
            )
            reason = result.reason or ("invalid on disk" if result.invalid_on_disk else result.loaded)
            raise ConfigError(
                f"Configuration document {document!r} is not valid ({reason})"
                + (f": {details}" if details else "")
            )

    def _migrate_legacy_startup_overrides(self, service: Service, result: Any) -> None:
        """Import the old auto-start state file into the root once, then retire it.

        The file is host-owned state, not part of config.json5. When the write
        succeeds, the file is renamed to `.migrated`. If it fails (read-only
        checkout, for example), the values are applied in memory and the file is
        kept with a warning, so the legacy behaviour is not lost.
        """
        import json

        path = self.root_dir / "data" / "sandbox_startup_overrides.json"
        if not path.exists():
            return
        try:
            overrides = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ConfigError(f"Invalid sandbox startup overrides: {error}") from error
        if not isinstance(overrides, dict):
            raise ConfigError("Sandbox startup overrides must be an object")
        sandboxes = result.effective.get("sandboxes", {}).get("items", {})
        operations = []
        for sandbox_id, enabled in overrides.items():
            if sandbox_id not in sandboxes or not isinstance(enabled, bool):
                get_logger("core").warning(
                    "Skipping legacy auto-start override for unknown sandbox %r", sandbox_id
                )
                continue
            operations.append(
                {
                    "op": "set",
                    "path": f"/sandboxes/items/{sandbox_id}/run_at_startup",
                    "value_text": "true" if enabled else "false",
                }
            )
        if operations:
            try:
                service.apply_operations(
                    "main",
                    operations,
                    expected_revision=result.revision,
                    expected_descriptor_revision=result.descriptor_revision,
                    principal=Principal.demo(),
                )
            except Exception as error:  # noqa: BLE001 - startup fallback
                get_logger("core").warning(
                    "Could not import %s into config.json5; applying it in memory: %s",
                    path,
                    error,
                )
                for sandbox_id, enabled in overrides.items():
                    sandbox = sandboxes.get(sandbox_id)
                    if isinstance(sandbox, dict) and isinstance(enabled, bool):
                        sandbox["run_at_startup"] = enabled
                self._legacy_startup_fallback = dict(overrides)
                return
        path.replace(path.with_name(path.name + ".migrated"))
        get_logger("core").info("Imported legacy auto-start overrides and retired %s", path.name)

    def _preflight(self, config: Dict[str, Any]) -> None:
        """Keep the fail-fast behaviour the legacy loader had for root paths."""
        if not str(config.get("root", "")).strip():
            raise ConfigError("Configuration value 'root' is empty; check SNDBX_ROOT in .env")

    def document_catalog(self) -> List[Dict[str, Any]]:
        """Return document ids and labels for the Web UI selector."""
        return copy.deepcopy(self.documents)

    def document_for_sandbox(self, sandbox_id: str) -> Optional[str]:
        """Return the document id that owns a sandbox overlay, if any."""
        for overlay in self.overlays:
            if sandbox_id in overlay.sandbox_ids:
                return overlay.document_id
        return None

    def apply_auto_start(self, sandbox_id: str, enabled: bool, principal: Principal) -> Any:
        """Write run_at_startup to the root document with revision checks."""
        if self.service is None:
            raise ConfigError("Settings runtime is not started")
        current = self.service.reread("main")
        operation = {
            "op": "set",
            "path": f"/sandboxes/items/{sandbox_id}/run_at_startup",
            "value_text": "true" if enabled else "false",
        }
        try:
            result = self.service.apply_operations(
                "main",
                [operation],
                expected_revision=current.revision,
                expected_descriptor_revision=current.descriptor_revision,
                principal=principal,
            )
        except RevisionConflict:
            current = self.service.reread("main")
            result = self.service.apply_operations(
                "main",
                [operation],
                expected_revision=current.revision,
                expected_descriptor_revision=current.descriptor_revision,
                principal=principal,
            )
        self._set_runtime_startup(sandbox_id, enabled)
        return result

    def _set_runtime_startup(self, sandbox_id: str, enabled: bool) -> None:
        """Update one merged in-memory value after a committed write."""
        sandbox = self.config.get("sandboxes", {}).get("items", {}).get(sandbox_id)
        if isinstance(sandbox, dict):
            sandbox["run_at_startup"] = bool(enabled)

    def _after_save(self, previous_stored: Any, new_stored: Any, event: Dict[str, Any]) -> None:
        """Apply the live parts of a committed write to the running host."""
        if self.service is None:
            return
        document = str(event.get("document", ""))
        changed = [str(path) for path in event.get("changed_paths", [])]
        if document != "main":
            return
        if any(path == "/logging" or path.startswith("/logging/") for path in changed):
            effective = self.service.reread("main").effective
            configure_logging(effective, str(self.root_dir))
            if isinstance(effective.get("logging"), dict):
                self.config["logging"] = copy.deepcopy(effective["logging"])
        for path in changed:
            parts = path.strip("/").split("/")
            if len(parts) == 4 and parts[0] == "sandboxes" and parts[1] == "items" and parts[3] == "run_at_startup":
                effective = self.service.reread("main").effective
                sandbox = effective.get("sandboxes", {}).get("items", {}).get(parts[2], {})
                if "run_at_startup" in sandbox:
                    self._set_runtime_startup(parts[2], bool(sandbox["run_at_startup"]))


_RUNTIMES: Dict[str, SettingsRuntime] = {}
_RUNTIME_LOCK = threading.Lock()


def get_runtime(root_dir: str | Path) -> SettingsRuntime:
    """Return the cached runtime for a root, starting it once."""
    key = str(Path(root_dir).resolve())
    with _RUNTIME_LOCK:
        runtime = _RUNTIMES.get(key)
        if runtime is None:
            runtime = SettingsRuntime(key)
            try:
                runtime.start()
            except Exception:
                _RUNTIMES.pop(key, None)
                raise
            _RUNTIMES[key] = runtime
        return runtime
