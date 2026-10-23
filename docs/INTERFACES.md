# sndbx — Interface and Protocol Reference

> Part of the sndbx specification. See [`../SPEC_NEW.md`](../SPEC_NEW.md) for
> the architecture and [`IMAGES.md`](IMAGES.md) for the image contract.

This document is the normative reference for every interface sndbx exposes or
consumes.

| # | Interface | Owner | Transport |
|---|---|---|---|
| 1 | MCP server | sndbx | HTTP + SSE, JSON-RPC 2.0 |
| 2 | Web UI REST API | sndbx | HTTP/JSON, cookie auth |
| 3 | Console WebSocket | sndbx | WebSocket, binary-safe text frames |
| 4 | Settings API | aiset (mounted by sndbx) | HTTP/JSON, session-derived principal |
| 5 | Image hook protocol | sndbx → image | `docker exec` + environment variables |
| 6 | Runtime contract | sndbx ↔ image | environment + probes |
| 7 | Host configuration contract | operator → sndbx | JSON5 documents + `.env` |

---

## 1. MCP server

### 1.1 Listener

| Property | Value |
|---|---|
| Bind | `mcp.bind` (default `0.0.0.0`) |
| Port | `mcp.port` (default `30081`) |
| Server | aiohttp `AppRunner` / `TCPSite` |
| CORS | `Access-Control-Allow-Origin` echoes the request `Origin`, or `*`; `GET,POST,OPTIONS`; `Content-Type,Authorization,Accept,X-MCP-Stream` |

### 1.2 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/` | Streamable HTTP MCP. |
| `POST` | `/mcp` | Streamable HTTP MCP (canonical path). |
| `POST` | `/mcp/v1` | Streamable HTTP MCP (versioned alias). |
| `GET` | `/sse` | Legacy SSE channel. Emits a `ready` event, then `message` / `ping` events every 20 s. |
| `POST` | `/messages` | Legacy JSON-RPC endpoint. Optionally `?session_id=…` to forward the result to the matching SSE channel. |

All `POST` endpoints accept either a single JSON-RPC object or an array of
objects. When `Content-Type` is not `application/json`, the body is parsed as
NDJSON (one JSON object per line).

**Streaming.** A response is streamed as SSE when any of the following holds:

- `?stream=1` / `?stream=true` / `?stream=yes` in the query string,
- `Accept: text/event-stream` in the request headers,
- `X-MCP-Stream: 1|true|yes` in the request headers.

The stream emits one `message` event per response, then a final `done` event
`{"count": N}`.

**Error status codes.** A malformed body yields HTTP `400` with a JSON-RPC
error object. A well-formed request with a failing tool call yields HTTP
`200` with a JSON-RPC `error` member.

### 1.3 Request envelope

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "tools/call",
  "params": {
    "name": "execute_command",
    "arguments": { "command": "uname -a" }
  },
  "token": "test-token-123456789",
  "auth": { "token": "test-token-123456789" },
  "envid": "default-env-token"
}
```

**Supported methods.**

| Method | Behaviour |
|---|---|
| `tools/list` | Returns the full tool descriptors. **Does not require a token.** |
| `resources/list` | Returns `{"resources": []}`. **Does not require a token.** |
| `prompts/list` | Returns `{"prompts": []}`. **Does not require a token.** |
| `tools/call` | `params.name` selects the tool, `params.arguments` are the arguments. **Requires a token.** |

A method that is neither a known MCP method nor a registered tool name is
rejected with `"Unknown tool: <name>"` — this lets a client call a tool by
using `method` directly instead of wrapping it in `tools/call`.

**Token extraction order.** `body.token` → `body.auth.token` →
`Authorization: Bearer <token>`.

### 1.4 Authentication and routing

```text
token ──► validate against mcp.auth.tokens[]
             │ invalid → error "Invalid or missing token"
             ▼
envid ──► users.items[].token == token ?
             │ no  → error "Invalid or missing token"
             ▼
        envid present in that user's envids[] ?  ──yes──► use it
             │ no
             ▼
        use the user's FIRST envid
             │ none → error "No access to requested envid or sandbox"
             ▼
        config.envids[envid].sandbox  →  sandbox id
```

**Consequence.** A token with exactly one envid always targets that sandbox,
even if the request names a different one. A token with several envids can
select between them. A token with none is useless.

### 1.5 Tool catalogue

All tools take their arguments under `params.arguments` when called via
`tools/call`, or directly under `params` when the tool name is used as
`method`.

| Tool | Arguments | Result |
|---|---|---|
| `execute_command` | `command` (string, required) | `{success, output, sandbox_id}` |
| `read_file` | `path` (required), `offset` (int, default 0), `limit` (int, default unlimited) | `{success, content, path, sandbox_id}` |
| `write_file` | `path` (required), `content` (required) | `{success, path, sandbox_id, message}` |
| `patch_file` | `file_path` (required), `patch_content` (required), `encoding` (default `utf-8`), `errors` (default `strict`) | `{success, message, file_path, sandbox_id}` |
| `sandbox_status` | — | `{id, running, container_id, ip, error}` |
| `sandbox_start` | — | `{success, message, sandbox_id}` |
| `sandbox_stop` | — | `{success, message, sandbox_id}` |
| `mcp_proxy_call` | `request` (required), `backend_id`, `timeout_sec` | `{success, response}` or `{success: false, error}` |

**`patch_file` format.** The patch content must contain the three markers:

```text
<<<<<<< SEARCH
<the exact text to find>
=======
<the replacement text>
>>>>>>> REPLACE
```

The implementation is a plain string replacement — the search text must occur
in the file. The markers must be on their own lines.

**`read_file` / `write_file` / `patch_file` implementation notes.** None of
these tools builds a shell command. They run `docker exec -i` with an
argument vector:

| Tool | argv | stdin |
|---|---|---|
| `read_file` | `sh -c 'tail -n +N "$1" \| head -n M' sh <path>` | — |
| `write_file` | `sh -c 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"' sh <path>` | the file content |
| `patch_file` | `sh -c 'cat -- "$1"' sh <path>` then `sh -c 'mkdir -p … && cat > "$1"' sh <path>` | the patched file content |

Consequences:

- The path is an argv entry, never shell text: spaces, quotes and backticks
  in a path are literal.
- File content travels over stdin, so a file body can contain any byte
  sequence — including a line that looks like a heredoc delimiter.
- `write_file` creates parent directories with `mkdir -p`.
- `patch_file` needs **no interpreter inside the guest**: the file is read
  through `cat`, the replacement is applied on the host, and the result is
  written back through `cat`. Only `sh`, `cat`, `mkdir`, `dirname`, `tail` and
  `head` are required in the guest.
- `patch_file` reports a clear error when the search text is not found.

### 1.6 Proxying to a VM-side MCP backend

`mcp_proxy_call` forwards a JSON-RPC request to an MCP server running inside
the sandbox.

**Selection.** The binding is chosen by `backend_id` from the sandbox's
`mcp_bindings`; if `backend_id` is omitted, the first binding is used.

**Allow-list.** When the binding's `tools` list is not `["*"]`, only the
listed tools may be invoked. A `tools/call` naming a tool outside the list is
refused with `{"success": false, "error": "Tool '<name>' is not published by
this backend"}`. Metadata calls such as `tools/list` are always allowed.

**Endpoint.** The host endpoint published for the binding's `vm_port` is used
when the sandbox declares a matching `port_bindings` entry; otherwise the
container IP and the guest port are used. A wildcard `bind_host` resolves to
`127.0.0.1`.

**Authentication.** `auth_mode: "token_passthrough"` adds the caller's MCP
token to the forwarded request.

**Timeouts.** `startup_timeout_sec` bounds the connect and the handshake;
`read_timeout_sec` bounds the call. A `timeout_sec` argument overrides the
read timeout for one call.

**Wire protocol.** TCP with newline-delimited JSON and a mandatory handshake
on every connection:

```text
client → {"jsonrpc":"2.0","id":0,"method":"initialize","params":{…}}
server → {"jsonrpc":"2.0","id":0,"result":{…}}
client → {"jsonrpc":"2.0","method":"notifications/initialized"}
client → {"jsonrpc":"2.0","id":1,"method":"tools/list"}
server → {"jsonrpc":"2.0","id":1,"result":{"tools":[…]}}
```

The tool returns `{"success": true, "response": <backend response>}`;
transport and protocol failures return `{"success": false, "error": "<message>"}`.

### 1.7 Response envelope

A successful call:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "result": {
    "content": [
      { "type": "text", "text": "{\n  \"success\": true,\n  …\n}" }
    ],
    "structuredContent": {
      "success": true,
      "output": "…",
      "sandbox_id": "sandbox-1"
    }
  }
}
```

The handler's raw return value becomes `structuredContent`; the text block is
its JSON rendering. A handler that already returns a `content` list is passed
through unchanged (and `isError` / `structuredContent` are preserved).

An error:

```json
{ "jsonrpc": "2.0", "id": 1, "error": { "code": -32000, "message": "Invalid or missing token" } }
```

Error messages used by the server:

| Message | Cause |
|---|---|
| `Invalid or missing method` | `method` missing or not a string. |
| `Invalid params for tools/call` | `params` is not an object. |
| `Invalid or missing tool name in params.name` | `params.name` missing. |
| `Invalid params.arguments for tools/call` | `params.arguments` is not an object. |
| `Invalid or missing token` | Token absent or unknown. |
| `No access to requested envid or sandbox` | Envid could not be resolved. |
| `Unknown tool: <name>` | No handler registered. |
| `Internal error: <exception>` | The handler raised. |

### 1.8 Legacy SSE handshake

```bash
curl -N 'http://127.0.0.1:30081/sse?session_id=demo-session'
curl -X POST 'http://127.0.0.1:30081/messages?session_id=demo-session' \
  -H 'Content-Type: application/json' \
  -d '{"jsonrpc":"2.0","id":11,"method":"tools/list"}'
```

`GET /sse` emits `event: ready` with the session id, then `event: message`
for each forwarded result, and `event: ping` every 20 s of inactivity.
`POST /messages` always returns the JSON-RPC result in the HTTP body and
additionally forwards it to the SSE channel when the session id matches.

### 1.9 Request log

When `logging.log_mcp_requests` is true, every request and response is
appended to `logs/mcp_requests.jsonl`:

```json
{"type": "request", "ts": "2026-10-22T12:00:00.000000+00:00", "data": { … }}
{"type": "response", "ts": "2026-10-22T12:00:00.001000+00:00", "data": { … }}
```

The file is line-buffered. `logging.mcp_request_log_max_mb` (default 64, `0`
disables) caps its size: when the cap is reached the file is renamed to
`mcp_requests.jsonl.1` and a fresh one is started. The age-based wipe
(`logging.wipe_period` / `wipe_max_age`) also applies to this file.

---

## 2. Web UI REST API

### 2.1 Conventions

- Base URL: `http://<webui.bind>:<webui.port>` (default `30080`).
- All endpoints except `/api/auth/login` and `/api/auth/logout` require the
  `sndbx_session` cookie. A missing or expired session yields `401`.
- Request bodies are JSON. Query parameters are documented per endpoint.
- The frontend is served from `/` and mounted **last**, so API routes always
  take priority over static files.

### 2.2 Authentication

| Method | Path | Body | Response |
|---|---|---|---|
| `POST` | `/api/auth/login` | `{"login": "...", "password": "..."}` | `{"ok": true, "token": "<session token>"}`; sets the `sndbx_session` cookie. `401` on bad credentials. |
| `GET` | `/api/auth/me` | — | `{"ok": true, "login": "...", "permissions": [...]}` |
| `POST` | `/api/auth/logout` | — | `{"ok": true}`; deletes the session file and clears the cookie. |

Cookie attributes: `HttpOnly`, `SameSite=Strict`, `Secure` when the request
scheme is HTTPS, `Max-Age` = `webui.auth.session_ttl`.

### 2.3 Status and health

| Method | Path | Response |
|---|---|---|
| `GET` | `/api/status` | `{service_state, health, containers, images, session}` |

- `service_state` — `"online"` or `"restarting"`.
- `health` — `{docker: {ok, detail}, kata: {ok, detail}, webui: {ok, detail}}`.
  The `kata` check verifies both the shim binary and that Docker lists a
  `kata` runtime.
- `containers` — one row per configured sandbox plus any discovered
  `sndbx-*` container not in the config:
  `{sandbox_id, image, status, ports, ssh_port, run_at_startup}`.
- `images` — `{image, local_id, path, has_dockerfile, has_app_py, built,
  sandboxes, build_status, build_started_at, build_action}`.

### 2.4 Monitoring

| Method | Path | Query | Response |
|---|---|---|---|
| `GET` | `/api/monitor` | `ids` — comma-separated sandbox ids; empty means all configured | `{ok, generated_at, sample_interval_seconds, host, sandboxes}` |

- `400` — an unknown or malformed sandbox id, or more than 32 ids.
- `500` — the collector failed.
- The response is cached for 2 s per id-set.

See `proposal_monitoring.md` for the payload shape and threshold semantics.

### 2.5 Sandbox actions

`POST /api/sandbox/{sandbox_id}/action` with body `{"action": "<name>"}`.

| Action | Effect | Possible failure bodies |
|---|---|---|
| `start` | `docker start`, falling back to create. | `{ok: false, message}` |
| `stop` | `docker stop` with the configured grace period. | `{ok: false, message}` |
| `restart` | stop, then start. | `{ok: false, message}` |
| `recreate` | Remove and recreate — refused if the rootfs changed. | `{ok: false, message}` |
| `auto_start_enable` | Write `run_at_startup: true` to the root config. | `{ok: false, message}` |
| `auto_start_disable` | Write `run_at_startup: false` to the root config. | `{ok: false, message}` |
| `ssh_open` | Reserve a host port and start a `socat` forwarder. | `{ok: false, error, message}` |
| `ssh_close` | Kill the forwarder and release the port. | `{ok: false}` |

`ssh_open` error codes: `no_ssh_keys`, `sandbox_not_running`,
`no_container_ip`, `ssh_setup_failed`, `socat_not_installed`,
`no_ports_available`.

An unknown action yields HTTP `400` with `{"detail": "Unknown action"}`.

### 2.6 Image actions

`POST /api/image/{image_ref}/action` with body `{"action": "<name>"}`.

| Action | Effect | Response |
|---|---|---|
| `build` | `docker build` with the cache. | `{ok, message, building}` |
| `rebuild` | `docker build --no-cache`. | `{ok, message, building}` |
| `update` | `docker build --no-cache --pull`. | `{ok, message, building}` |
| `full_update` | Git fast-forward, then `docker build --no-cache --pull`. | `{ok, message, building}` |

The build runs in a background thread; the response only acknowledges the
start. Progress is observable through `GET /api/status` → `images[].build_status`
(`idle` / `building`) and `build_started_at`.

A second build for the same image is refused with
`"Image '<ref>' is already building"`.

### 2.7 Service, log and Kata repair

| Method | Path | Body / query | Response |
|---|---|---|---|
| `POST` | `/api/service/restart` | — | `{ok: true, message: "Service restart scheduled"}` |
| `GET` | `/api/system-log` | `lines` (20–2000, default 200) | `{ok, source, lines, text}` |
| `POST` | `/api/runtime/kata/repair` | — | `{ok, message, report[], manual_commands[]}` |

- `/api/system-log` reads the user journal first, then the system journal,
  then falls back to `logs/all.log`. `source` reports which one was used.
- `/api/runtime/kata/repair` re-registers the Kata runtime in
  `/etc/docker/daemon.json` using passwordless `sudo`. When it cannot, it
  returns the manual command sequence in `manual_commands`.
- **Known defect:** the endpoint currently always fails with HTTP 500. The
  implementation is unreachable dead code — see
  [`STATUS_AND_ROADMAP.md` §4.1, A1](STATUS_AND_ROADMAP.md#41-application-defects).

### 2.8 MCP passthrough

| Method | Path | Body | Response |
|---|---|---|---|
| `GET` | `/api/mcp/connect-info` | — | `{host, port, default_host, default_url}` |
| `GET` | `/api/mcp/tools` | — | The MCP server's `tools/list` result. |
| `POST` | `/api/mcp/tools` | `{server_url?, token?}` | The MCP server's `tools/list` result. |
| `POST` | `/api/mcp/call` | `{method, params, request?, server_url?, token?}` | The MCP server's JSON-RPC result. |

`server_url` accepts `host:port` or a full URL; the host and port override the
configured ones. `token` overrides the configured token. When neither is
given, the first entry of `mcp.auth.tokens[]` is used.

These endpoints are used by the Web UI's "MCP test" tab. They are a **client**
of the MCP server, not an internal shortcut.

### 2.9 Settings bridge

| Method | Path | Response |
|---|---|---|
| `GET` | `/api/settings/documents` | `{ok: true, documents: [{id, label, sandboxes}]}` |

`id` is `"main"` for the root document, `"sandbox:<id>"` for an overlay that
covers one sandbox, or `"sandbox-group:<dir>"` for an overlay that covers
several. `sandbox` is `[]` for the root.

All other settings endpoints belong to the mounted aiset application — see
[§4](#4-settings-api-aiset).

---

## 3. Console WebSocket

### 3.1 Endpoint

```text
ws://<webui.bind>:<webui.port>/ws/console/{sandbox_id}
```

`wss://` when the page is served over HTTPS.

### 3.2 Handshake

1. The client connects. The server **accepts immediately** (Starlette
   requires the accept before any close).
2. The server reads the `sndbx_session` cookie and validates it.
   - Invalid → the server sends `"\r\n[sndbx] Authentication required. Please log in again.\r\n"`
     and closes with code **4001**.
3. The server checks that the session carries the `"all"` permission.
   - Missing → the server sends
     `"\r\n[sndbx] Full permissions are required to open a console.\r\n"`
     and closes with code **4001**.
4. The server ensures the sandbox is running (starting it if necessary).
   - Failure → sends `"\r\n[sndbx] Unable to start sandbox for console session.\r\n"`
     and closes with code **1011**.
5. On success the server sends
   `"\r\n[sndbx] Console connected to sandbox '<id>'.\r\n"` and starts
   pumping bytes.

### 3.3 Frame format

All frames are **text** (UTF-8). Two directions carry different content:

| Direction | Content |
|---|---|
| Server → client | Raw terminal output bytes, decoded as UTF-8 with `errors="replace"`. |
| Client → server | Either raw keystrokes, or a JSON resize message. |

**Resize message** (produced by the xterm.js FitAddon):

```json
{"type": "resize", "cols": 120, "rows": 40}
```

Any other client text is written verbatim to the container's PTY.

### 3.4 Server-side implementation

```text
host PTY (openpty)
  master_fd ──> loop.add_reader ──> asyncio.Queue ──> WebSocket.send_text
  slave_fd  <── docker exec -it sndbx-<id> bash -lc 'exec bash -i'
```

- The host PTY master is put into **raw mode** (`tty.setraw`) so that the
  host does not echo or line-edit; only the container's line discipline does.
  Without this, every character would appear twice.
- The container process receives `TERM=xterm-256color` and a `PS1` of
  `[sndbx:<sandbox_id>] \u@\h:\w\$ `.
- A resize applies `TIOCSWINSZ` on the master fd and sends `SIGWINCH` to the
  container process.
- On disconnect the reader is removed, the master fd is closed, and the
  container process is terminated (`terminate`, then `kill` after 2 s).

### 3.5 Limitations

- The console is **not** mediated by MCP. It is a direct PTY bridge.
- The authorization is per user, not per sandbox: a session with the `"all"`
  permission may open a console for any configured sandbox. A per-sandbox
  policy does not exist.
- There is no session audit beyond the log line.

---

## 4. Settings API (aiset)

sndbx mounts the `aiset` ASGI application at `/api/settings`. The routes
below are relative to that prefix.

### 4.1 Principal mapping

The principal is derived from the session cookie by
`src/settings/principals.py`:

| Session | Principal |
|---|---|
| No session | `Principal(id="anonymous", permissions=∅)` |
| `permissions` contains `"all"` | `Principal(id=<login>, permissions=ALL_PERMISSIONS)` |
| Any other permission set | `Principal(id=<login>, permissions={settings_read_source, settings_read_effective})` |

Consequence: a Web UI user without `"all"` gets a **read-only** settings
editor, and secret-marked subtrees (such as `envids`) return `403`.

### 4.2 Endpoints

| Method | Path | Purpose |
|---|---|---|
| `GET` | `/api/settings/health` | `{"ok": true, "aiset": "<version>"}` |
| `GET` | `/api/settings/documents` | Document ids visible to the principal. |
| `GET` | `/api/settings/descriptor` | Descriptor for a document; `view=authorized` filters by permission. |
| `GET` | `/api/settings/tree` | Tree snapshot; `document`, `path`, `view` (`source` or `effective`). |
| `GET` | `/api/settings/value` | One value; `document`, `path`, `view`. |
| `POST` | `/api/settings/value` | Set one value; requires `expected_revision` and `expected_descriptor_revision`. |
| `POST` | `/api/settings/validate` | Validate a value without writing. |
| `POST` | `/api/settings/operations` | Apply a batch of operations (dry run = `false`). |
| `POST` | `/api/settings/operations/validate` | Apply operations as a dry run. |
| `POST` | `/api/settings/env` | Report which environment variables are present. |
| `POST` | `/api/settings/document/validate` | Validate a whole document source. |
| `POST` | `/api/settings/document/doctor` | Report drift; `fix` may be `false`, a class name or a list of class names. |
| `POST` | `/api/settings/load` | Reload a document from disk. |
| `POST` | `/api/settings/load/lkg` | Load the last-known-good version. |
| `POST` | `/api/settings/merge` | Pure merge of `base` and `update`. |
| `POST` | `/api/settings/descriptor/validate` | Validate descriptor source. |
| `GET` | `/api/settings/assets/<name>` | Editor assets: `aiset.js`, `aiset-tree.js`, `aiset.css`, `aiset-host-theme.css`. |

`/api/settings/documents` is served by **sndbx** (not aiset) and shadows the
aiset route of the same name, because sndbx adds the human-readable labels
and the sandbox lists.

### 4.3 Operation format

```json
{
  "document": "main",
  "expected_revision": 12,
  "expected_descriptor_revision": 3,
  "operations": [
    { "op": "set", "path": "/sandboxes/items/my-sandbox/run_at_startup", "value_text": "true" }
  ]
}
```

Supported `op` values include `set`, `remove` and `append`/`insert` style
operations on arrays.

### 4.4 Revision semantics

| Situation | HTTP status |
|---|---|
| Revision matches | `200`; the file is rewritten. |
| Revision is stale | `409` (conflict). |
| Revision missing | `428` (precondition required). |

The stored document is edited **source-preserving**: comments, formatting and
untouched bytes survive. A `.back` copy of the previous version is kept
alongside (`config.json5.back`).

### 4.5 Live versus restart

| Change | Effect |
|---|---|
| `logging.*` | Applied immediately — `configure_logging()` is re-run. |
| `sandboxes.items.<id>.run_at_startup` | Applied immediately — the in-memory config is updated. |
| Anything else | Requires a service restart; the editor surfaces `restart_required` and offers a "Restart sndbx" button that posts to `/api/service/restart`. |

---

## 5. Image hook protocol

### 5.1 Invocation

```bash
docker exec --user 0 \
  -e SNDBX_HOOK=on_system_start \
  -e SNDBX_CONTEXT_JSON='<json>' \
  [-e <runtime contract environment>] \
  sndbx-<sandbox_id> python3 /opt/sndbx-image/app.py
```

- `SNDBX_HOOK` — the hook name. Only `on_system_start` is dispatched.
- `SNDBX_CONTEXT_JSON` — the context payload (see below).
- Runtime contract environment variables are injected as `-e NAME=VALUE`.

### 5.2 Context payload

```json
{
  "sandbox_id": "<id>",
  "image_ref": "<image reference>",
  "resolved_mounts": [
    {
      "guest_path": "<absolute guest path>",
      "source_type": "directory" | "file",
      "permission": "rw" | "ro",
      "bind_propagation": "" | "private" | … ,
      "required": true | false
    }
  ],
  "ssh_keys": ["<public key>", …]
}
```

`resolved_mounts` contains only the mounts that were actually accepted during
preflight — optional mounts whose source was missing are absent.

### 5.3 Output contract

- **Exit code 0** — success.
- **Exit code non-zero** — failure. Without a runtime contract the failure is
  logged as a warning; with one, the sandbox start fails.
- **Capability hook** (`runtime_contract.capability_hook: true`) — stdout must
  be a JSON object:

  ```json
  {"provides_postgres": true}
  ```

  The value must match `SNDBX_PROVIDES_POSTGRES` from the runtime contract
  environment. A malformed JSON or a mismatched value aborts the start.

### 5.4 Idempotency

The hook runs after **every** start, not only after creation. Every step must
be safe to repeat. The `default_sandbox_1` hook is the canonical example: it
writes `authorized_keys` only when keys are configured, generates the sshd
host key only when missing, and re-installs the systemd timer idempotently.

---

## 6. Runtime contract

### 6.1 Structure

```json5
runtime_contract: {
  version: 1,                       // must be exactly 1
  capability_hook: false,
  environment: { "NAME": "value" },  // injected as -e NAME=value
  companion_services: [ … ],
  readiness: [ … ]
}
```

### 6.2 Companion services

```json5
{
  name: "postgres",                   // [a-z][a-z0-9_-]*
  host: "host.docker.internal",       // triggers --add-host host.docker.internal:host-gateway
  port: 5432,
  inject: { host_env: "DATABASE_HOST", port_env: "DATABASE_PORT" },
  readiness: { type: "tcp", host: "host.docker.internal", port: 5432 }
}
```

Rules:

- `inject.host_env` and `inject.port_env` must be valid variable names.
- They must not collide with a key in `environment`.
- A companion named `postgres` is **skipped** when
  `SNDBX_PROVIDES_POSTGRES` is truthy.

### 6.3 Readiness probes

| Type | Required fields | Optional |
|---|---|---|
| `http` | `url` (must start with `http://` or `https://`) | `attempts` (≥1), `interval_seconds` (≥0) |
| `tcp` | `host`, `port` (1–65535) | `attempts`, `interval_seconds` |
| `command` | `command` | `attempts`, `interval_seconds` |

The probe runs after the image hook. A probe that never succeeds aborts the
start; the error message includes the last failure.

### 6.4 Command selection

| Configuration | Container command |
|---|---|
| No `runtime_contract` | `sleep infinity` appended to the image's command. |
| With `runtime_contract` | The image's own `ENTRYPOINT`/`CMD` is used unchanged. |

---

## 7. Host configuration contract

### 7.1 Document set

| Document | Path | Role |
|---|---|---|
| Root | `config.json5` | Registry, global settings, override layer. |
| Overlay | `images/<id>/sndbx.overlay.json5` | Per-image sandbox defaults. |
| Descriptor | `config.descriptor.json5` | Schema for both. |
| Types | `config.types.json5` | Type templates referenced by the descriptor. |
| Environment | `.env` | `KEY=VALUE`; secrets and machine-specific values. |

### 7.2 Placeholders

```json5
memory: "${LANGVM_MEMORY:-8G}"   // with a default
root:   "${SNDBX_ROOT}"           // without a default
```

Resolution order: explicit overrides → process environment → `.env` → the
placeholder default. The **placeholder text is stored**, not the resolved
value; resolution happens on read.

A field may only hold a placeholder when its descriptor node declares
`env: {allowed: true}`.

### 7.3 Merge contract

| Situation | Result |
|---|---|
| Key only in the root | Root value. |
| Key only in an overlay | Overlay value is added. |
| Both, scalar | Root wins. |
| Both, object | Deep merge; root members win individually. |
| Both, array | Root array replaces the overlay array entirely. |
| `envids` in both | Merged per key; root entries win. |

### 7.4 Write contract

1. The write carries `expected_revision` and `expected_descriptor_revision`.
2. A stale revision → `409`. A missing revision → `428`.
3. The descriptor validates the new value.
4. The file is rewritten atomically, source-preserving, with a `.back` copy.

### 7.5 `.env` contract

- `KEY=VALUE`, one per line. `#` starts a comment. A line without `=` is
  ignored.
- Values are **not** quoted or escaped; the value is everything after the
  first `=`, trimmed.
- `.env` is read by the manager, not by systemd. The systemd unit has no
  `EnvironmentFile=`.
- `install.sh` **never modifies an existing `.env`** — it only creates one
  from `.env.example` when absent, and rewrites the `SNDBX_ROOT=` line in that
  new file.

### 7.6 Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `SNDBX_ROOT` | — (required) | Absolute checkout path; becomes `config.root`. |
| `SNDBX_INSTANCE` | `sndbx-dev` | Instance label. |
| `LOG_LEVEL` | `info` | Global log level. |
| `LOG_TIMEZONE` | `local` | `local`, `UTC` or an IANA zone. |
| `LOG_WIPE_PERIOD` | `0` | Seconds between log wipe runs; `0` disables. |
| `LOG_WIPE_MAX_AGE` | `0` | Seconds of log history to keep; falls back to `LOG_WIPE_PERIOD`. |
| `MCP_REQUEST_LOG_MAX_MB` | `64` | Size cap for `logs/mcp_requests.jsonl`; `0` disables. |
| `WEBUI_PORT` / `WEBUI_HOST` | `30080` / `0.0.0.0` | Web UI listener. |
| `MCP_PORT` / `MCP_HOST` | `30081` / `0.0.0.0` | MCP listener. |
| `MCP_TOKENS` | `test-token-123456789` | Comma-separated accepted tokens. |
| `SSH_PORT_MIN` / `SSH_PORT_MAX` | `30200` / `30210` | SSH port pool. |
| `SSH_SESSION_TTL` | `3600` | SSH forwarder lifetime in seconds; `0` disables expiry. |
| `ROOT_USER` / `ROOT_PASSWORD` | `admin` / `admin` | Web UI administrator. |
| `MY_SSH_PUBLIC_KEY` | — | Public key injected into local-image sandboxes. |
| `VM_IMAGE`, `VM_MEMORY`, `VM_CPUS`, `VM_DISK_MAX`, `VM_NETWORK_TRAFFIC_MAX`, `VM_SHARED_DIR` | see `.env.example` | Default sandbox settings used by the `default_sandbox_1` overlay. |
| `MCP_TOOLBOX_BIND_HOST`, `MCP_TOOLBOX_FS_PORT`, `MCP_TOOLBOX_BASH_PORT`, `MCP_TOOLBOX_GIT_PORT` | `127.0.0.1`, `39011`–`39013` | MCP toolbox published ports. |

Each image additionally owns a namespace (`LANGVM_*`, `CLAWVM_*`,
`OBSIDIAN_ASSISTANT_*`, `PAPERCLIP_*`, `OPEN_WEBUI_*`, `AIBUSINESS_*`) used by
its overlay. Those variables belong to the image repositories and are
documented in their own `.env.example`.
