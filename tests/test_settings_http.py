"""HTTP/ASGI integration tests for the mounted aiset application."""

from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from settings.aiset_path import ensure_aiset

ROOT = Path(__file__).resolve().parents[1]
ensure_aiset(ROOT)

from settings.runtime import SettingsRuntime  # noqa: E402
from webui_server import WebUIServer  # noqa: E402

from tests.settings_fixtures import TempRoot  # noqa: E402


class StubSandboxManager:
    """Minimal sandbox manager surface used by WebUIServer construction."""

    def __init__(self, config: dict[str, Any]) -> None:
        self.sandbox_configs = config.get("sandboxes", {}).get("items", {})


async def asgi_call(app, method: str, path: str, headers: dict[str, str] | None = None, body: bytes = b""):
    """Call an ASGI app and return (status, headers, body bytes)."""
    header_list = [(b"host", b"testserver")]
    for key, value in (headers or {}).items():
        header_list.append((key.lower().encode(), value.encode()))
    target = urlsplit(path)
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "path": target.path,
        "raw_path": target.path.encode(),
        "query_string": target.query.encode(),
        "headers": header_list,
        "scheme": "http",
        "server": ("testserver", 80),
        "client": ("testclient", 1),
        "root_path": "",
    }
    messages = []

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        messages.append(message)

    await app(scope, receive, send)
    status = next(message["status"] for message in messages if message["type"] == "http.response.start")
    response_headers = dict(
        next(message["headers"] for message in messages if message["type"] == "http.response.start")
    )
    payload = b"".join(
        message.get("body", b"") for message in messages if message["type"] == "http.response.body"
    )
    return status, response_headers, payload


def run(coro):
    return asyncio.run(coro)


class SettingsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = TempRoot()
        self.root = self.temp.__enter__()
        self.runtime = SettingsRuntime(self.root).start()
        frontend = self.root / "src" / "webui" / "frontend"
        frontend.parent.mkdir(parents=True, exist_ok=True)
        frontend.symlink_to(ROOT / "src" / "webui" / "frontend", target_is_directory=True)
        self.server = WebUIServer(
            root_dir=self.root,
            config=self.runtime.config,
            sandbox_manager=StubSandboxManager(self.runtime.config),
            settings=self.runtime,
        )
        self.app = self.server.app
        self.token = self.server.sessions.create("admin", ["all"])
        self.cookie = {"cookie": f"sndbx_session={self.token}"}

    def tearDown(self) -> None:
        self.temp.__exit__(None, None, None)

    def test_health_and_documents(self) -> None:
        status, _, body = run(asgi_call(self.app, "GET", "/api/settings/health"))
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])

        status, _, body = run(
            asgi_call(self.app, "GET", "/api/settings/documents", self.cookie)
        )
        self.assertEqual(status, 200, body)
        documents = json.loads(body)["documents"]
        self.assertEqual(documents[0]["id"], "main")
        self.assertIn("sandbox:demo", {item["id"] for item in documents})

    def test_documents_requires_session(self) -> None:
        status, _, _ = run(asgi_call(self.app, "GET", "/api/settings/documents"))
        self.assertIn(status, (401, 403))

    def test_tree_and_assets(self) -> None:
        status, _, body = run(
            asgi_call(self.app, "GET", "/api/settings/tree?document=main", self.cookie)
        )
        self.assertEqual(status, 200, body)
        tree = json.loads(body)
        self.assertTrue(tree["ok"])
        self.assertEqual(tree["path"], "")

        status, headers, body = run(
            asgi_call(self.app, "GET", "/api/settings/assets/aiset.js")
        )
        self.assertEqual(status, 200)
        content_type = next(
            value for key, value in headers.items() if key.lower() == b"content-type"
        )
        self.assertIn(b"javascript", content_type.lower())
        self.assertGreater(len(body), 1000)

        status, _, _ = run(asgi_call(self.app, "GET", "/api/settings/assets/missing.js"))
        self.assertEqual(status, 404)

    def test_viewer_cannot_read_secret_subtree(self) -> None:
        viewer = self.server.sessions.create("viewer", [])
        cookie = {"cookie": f"sndbx_session={viewer}"}
        status, _, _ = run(
            asgi_call(self.app, "GET", "/api/settings/value?document=main&path=/envids", cookie)
        )
        self.assertEqual(status, 403)
        status, _, _ = run(
            asgi_call(self.app, "GET", "/api/settings/value?document=main&path=/title", cookie)
        )
        self.assertEqual(status, 200)

    def test_write_requires_revisions_and_preserves_file(self) -> None:
        tree_status, _, tree_body = run(
            asgi_call(self.app, "GET", "/api/settings/tree?document=main&view=source", self.cookie)
        )
        self.assertEqual(tree_status, 200)
        tree = json.loads(tree_body)
        revision = tree["revision"]
        descriptor_revision = tree["descriptor_revision"]

        operation = {"op": "set", "path": "/sandboxes/items/demo/memory", "value_text": '"3G"'}
        payload = json.dumps(
            {
                "document": "main",
                "operations": [operation],
                "expected_revision": revision,
                "expected_descriptor_revision": descriptor_revision,
            }
        ).encode()
        status, _, body = run(
            asgi_call(
                self.app,
                "POST",
                "/api/settings/operations",
                {**self.cookie, "content-type": "application/json"},
                payload,
            )
        )
        self.assertEqual(status, 200, body)
        self.assertTrue(json.loads(body)["ok"])
        self.assertIn('"3G"', (self.root / "config.json5").read_text(encoding="utf-8"))

        # A second write with the old revision is a conflict.
        status, _, body = run(
            asgi_call(
                self.app,
                "POST",
                "/api/settings/operations",
                {**self.cookie, "content-type": "application/json"},
                payload,
            )
        )
        self.assertEqual(status, 409, body)

        # Missing revisions are 428.
        payload = json.dumps({"document": "main", "operations": [operation]}).encode()
        status, _, body = run(
            asgi_call(
                self.app,
                "POST",
                "/api/settings/operations",
                {**self.cookie, "content-type": "application/json"},
                payload,
            )
        )
        self.assertEqual(status, 428, body)


if __name__ == "__main__":
    unittest.main()
