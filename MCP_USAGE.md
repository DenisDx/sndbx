# MCP Usage Guide

This document describes how to interact with the sndbx MCP infrastructure via Python.

There are two scenarios:
- **Scenario A — sndbx MCP server**: call sandbox management tools directly (file I/O, shell commands, sandbox lifecycle, proxy calls to VM backends).
- **Scenario B — VM MCP backends (direct)**: connect directly to MCP servers running inside a VM sandbox (filesystem, shell, git) via published ports, bypassing the sndbx proxy layer.

---

## Protocol basics

### sndbx MCP server (Scenario A)

Transport: **TCP, newline-delimited JSON (JSONL)**  
Default port: `30081`  
Authentication: token in every request body

Each request is a single JSON object followed by `\n`. The response is also a single JSON line.

Request shape:
```json
{
  "id": "1",
  "method": "<tool_name>",
  "params": { ... },
  "token": "<your_token>",
  "envid": "<your_envid>"
}
```

Response shape:
```json
{
  "id": "1",
  "result": { ... },
  "error": null
}
```

### VM MCP backends (Scenario B)

Transport: **TCP, JSONL**  
Default ports: `39011` (filesystem), `39012` (shell), `39013` (git)  
Authentication: none (trust-based, localhost only by default)

These are standard MCP servers. Each new TCP connection requires an **initialization handshake** before any real call:

1. Client sends `initialize` request
2. Server responds with server info
3. Client sends `notifications/initialized` notification
4. Client sends the real request
5. Client reads the real response, then closes the connection

---

## Scenario A — sndbx MCP server

### Helper (reuse in all examples)

```python
import asyncio
import json

MCP_HOST = "127.0.0.1"
MCP_PORT = 30081
TOKEN = "test-token-123456789"
ENVID = "default-env-token"   # maps to sandbox-1
# ENVID = "mcp-toolbox-env-token"  # maps to mcp-toolbox-1

_req_id = 0

def next_id():
    global _req_id
    _req_id += 1
    return str(_req_id)

async def sndbx_call(method: str, params: dict, envid: str = ENVID) -> dict:
    """Send one request to the sndbx MCP server and return the parsed response."""
    reader, writer = await asyncio.open_connection(MCP_HOST, MCP_PORT)
    try:
        req = {"id": next_id(), "method": method, "params": params,
               "token": TOKEN, "envid": envid}
        writer.write((json.dumps(req) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=30)
        return json.loads(line)
    finally:
        writer.close()
        await writer.wait_closed()
```

---

### A1 — sandbox_status

```python
async def example_sandbox_status():
    resp = await sndbx_call("sandbox_status", {})
    print(resp)
    # {"id":"1","result":{"id":"sandbox-1","running":true,"container_id":"abc...","ip":"172.x.x.x","error":null},"error":null}

asyncio.run(example_sandbox_status())
```

---

### A2 — sandbox_start / sandbox_stop

```python
async def example_lifecycle():
    # Start
    resp = await sndbx_call("sandbox_start", {})
    print("start:", resp["result"])

    # Stop
    resp = await sndbx_call("sandbox_stop", {})
    print("stop:", resp["result"])

asyncio.run(example_lifecycle())
```

---

### A3 — execute_command

```python
async def example_execute():
    resp = await sndbx_call("execute_command", {"command": "uname -a && whoami"})
    r = resp["result"]
    print("success:", r["success"])
    print("output:", r["output"])

asyncio.run(example_execute())
```

---

### A4 — read_file

```python
async def example_read_file():
    resp = await sndbx_call("read_file", {"path": "/etc/os-release"})
    r = resp["result"]
    if r.get("success"):
        print(r["content"])
    else:
        print("error:", r)

asyncio.run(example_read_file())
```

---

### A5 — write_file

```python
async def example_write_file():
    resp = await sndbx_call("write_file", {
        "path": "/tmp/hello.txt",
        "content": "Hello from sndbx MCP!\n"
    })
    print(resp["result"])
    # {"success": true, "path": "/tmp/hello.txt", ...}

asyncio.run(example_write_file())
```

---

### A6 — mcp_proxy_call (proxy to VM backend)

`mcp_proxy_call` is the bridge between the sndbx MCP server and MCP servers running inside the VM.
Use `envid = "mcp-toolbox-env-token"` to route through the `mcp-toolbox-1` sandbox.

Parameters:
- `backend_id` — one of `"filesystem"`, `"bash"`, `"git"` (omit to use first)
- `request` — a standard MCP JSON-RPC request object
- `timeout_sec` — optional, default 15

#### A6a — tools/list on the filesystem backend

```python
async def example_proxy_tools_list():
    resp = await sndbx_call(
        "mcp_proxy_call",
        {
            "backend_id": "filesystem",
            "request": {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/list",
                "params": {}
            },
            "timeout_sec": 20
        },
        envid="mcp-toolbox-env-token"
    )
    result = resp["result"]
    if result.get("success"):
        tools = result["response"].get("result", {}).get("tools", [])
        for t in tools:
            print(t["name"], "—", t.get("description", "")[:60])
    else:
        print("error:", result)

asyncio.run(example_proxy_tools_list())
```

#### A6b — read a file via the filesystem backend

```python
async def example_proxy_read_file():
    resp = await sndbx_call(
        "mcp_proxy_call",
        {
            "backend_id": "filesystem",
            "request": {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "read_text_file",
                    "arguments": {"path": "/root/shared/README.md"}
                }
            }
        },
        envid="mcp-toolbox-env-token"
    )
    print(json.dumps(resp["result"]["response"], indent=2))

asyncio.run(example_proxy_read_file())
```

#### A6c — run a shell command via the bash backend

```python
async def example_proxy_bash():
    resp = await sndbx_call(
        "mcp_proxy_call",
        {
            "backend_id": "bash",
            "request": {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "run_command",
                    "arguments": {"command": "ls -la /root/shared"}
                }
            },
            "timeout_sec": 30
        },
        envid="mcp-toolbox-env-token"
    )
    print(json.dumps(resp["result"]["response"], indent=2))

asyncio.run(example_proxy_bash())
```

#### A6d — git log via the git backend

```python
async def example_proxy_git():
    resp = await sndbx_call(
        "mcp_proxy_call",
        {
            "backend_id": "git",
            "request": {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "git_log",
                    "arguments": {"max_count": 5}
                }
            }
        },
        envid="mcp-toolbox-env-token"
    )
    print(json.dumps(resp["result"]["response"], indent=2))

asyncio.run(example_proxy_git())
```

---

## Scenario B — direct connection to VM backends

Use this when you want to call VM-side MCP servers directly without going through sndbx.
The ports are published on `127.0.0.1` (configurable via `MCP_TOOLBOX_BIND_HOST`).

| Backend    | Default port |
|------------|-------------|
| filesystem | 39011       |
| shell      | 39012       |
| git        | 39013       |

### Helper

```python
import asyncio
import json

VM_HOST = "127.0.0.1"

async def vm_mcp_call(port: int, method: str, params: dict,
                      req_id: int = 1, timeout: float = 15.0) -> dict:
    """Connect to a VM MCP backend, do the initialize handshake, call method, return response."""
    reader, writer = await asyncio.open_connection(VM_HOST, port)
    try:
        async def send(obj):
            writer.write((json.dumps(obj) + "\n").encode())
            await asyncio.wait_for(writer.drain(), timeout=timeout)

        async def recv() -> dict:
            while True:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
                if not line:
                    raise EOFError("Backend closed connection")
                text = line.decode(errors="replace").strip()
                if not text:
                    continue
                try:
                    return json.loads(text)
                except json.JSONDecodeError:
                    continue  # skip non-JSON banner lines

        # MCP handshake
        await send({
            "jsonrpc": "2.0", "id": 0, "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "my-client", "version": "1.0"}
            }
        })
        await recv()  # initialize result (discard)
        await send({"jsonrpc": "2.0", "method": "notifications/initialized"})

        # Real request
        await send({"jsonrpc": "2.0", "id": req_id, "method": method, "params": params})
        return await recv()
    finally:
        writer.close()
        await writer.wait_closed()
```

---

### B1 — list available tools (filesystem backend)

```python
async def example_direct_tools_list():
    resp = await vm_mcp_call(39011, "tools/list", {})
    tools = resp.get("result", {}).get("tools", [])
    for t in tools:
        print(f"  {t['name']}: {t.get('description','')[:70]}")

asyncio.run(example_direct_tools_list())
```

---

### B2 — list directory contents (filesystem backend)

```python
async def example_direct_list_dir():
    resp = await vm_mcp_call(
        39011,
        "tools/call",
        {"name": "list_directory", "arguments": {"path": "/root/shared"}}
    )
    print(json.dumps(resp, indent=2))

asyncio.run(example_direct_list_dir())
```

---

### B3 — read a file (filesystem backend)

```python
async def example_direct_read():
    resp = await vm_mcp_call(
        39011,
        "tools/call",
        {"name": "read_text_file", "arguments": {"path": "/root/shared/hello.txt"}}
    )
    content = resp.get("result", {}).get("content", [])
    for block in content:
        if block.get("type") == "text":
            print(block["text"])

asyncio.run(example_direct_read())
```

---

### B4 — write a file (filesystem backend)

```python
async def example_direct_write():
    resp = await vm_mcp_call(
        39011,
        "tools/call",
        {
            "name": "write_file",
            "arguments": {
                "path": "/root/shared/test_output.txt",
                "content": "Written via direct VM MCP connection\n"
            }
        }
    )
    print(json.dumps(resp, indent=2))

asyncio.run(example_direct_write())
```

---

### B5 — run a shell command (shell backend)

```python
async def example_direct_bash():
    resp = await vm_mcp_call(
        39012,
        "tools/call",
        {"name": "run_command", "arguments": {"command": "df -h && uptime"}}
    )
    print(json.dumps(resp, indent=2))

asyncio.run(example_direct_bash())
```

---

### B6 — git log (git backend)

```python
async def example_direct_git_log():
    resp = await vm_mcp_call(
        39013,
        "tools/call",
        {"name": "git_log", "arguments": {"max_count": 10}}
    )
    print(json.dumps(resp, indent=2))

asyncio.run(example_direct_git_log())
```

---

### B7 — discover tool names first, then call

Some backends may use different tool names. Use `tools/list` first when unsure:

```python
async def example_discover_and_call():
    # 1. Discover
    resp = await vm_mcp_call(39012, "tools/list", {})
    tools = resp.get("result", {}).get("tools", [])
    print("Available shell tools:")
    for t in tools:
        print(f"  {t['name']}")

    # 2. Pick the first tool and call it
    if tools:
        tool_name = tools[0]["name"]
        call_resp = await vm_mcp_call(
            39012,
            "tools/call",
            {"name": tool_name, "arguments": {"command": "echo hello"}}
        )
        print(json.dumps(call_resp, indent=2))

asyncio.run(example_discover_and_call())
```

---

## Running multiple calls efficiently

Each call in Scenario B opens a new TCP connection (MCP backends via socat are stateless per connection). To run several calls in parallel:

```python
async def example_parallel():
    results = await asyncio.gather(
        vm_mcp_call(39011, "tools/list", {}),
        vm_mcp_call(39012, "tools/list", {}),
        vm_mcp_call(39013, "tools/list", {}),
    )
    for port, resp in zip([39011, 39012, 39013], results):
        names = [t["name"] for t in resp.get("result", {}).get("tools", [])]
        print(f"Port {port}: {names}")

asyncio.run(example_parallel())
```

---

## Error handling

```python
async def example_with_error_handling():
    try:
        resp = await sndbx_call("execute_command", {"command": "ls /nonexistent"})
        if resp.get("error"):
            print("MCP error:", resp["error"])
        else:
            r = resp["result"]
            if not r.get("success"):
                print("Command failed:", r.get("output"))
            else:
                print(r["output"])
    except asyncio.TimeoutError:
        print("Request timed out")
    except ConnectionRefusedError:
        print("Could not connect — is sndbx running?")

asyncio.run(example_with_error_handling())
```

---

## Quick reference

### sndbx MCP tools (Scenario A)

| Method             | Key params                       | Description                          |
|--------------------|----------------------------------|--------------------------------------|
| `sandbox_status`   | —                                | Container status and IP              |
| `sandbox_start`    | —                                | Start the sandbox container          |
| `sandbox_stop`     | —                                | Stop the sandbox container           |
| `execute_command`  | `command`                        | Run bash command inside sandbox      |
| `read_file`        | `path`                           | Read a file from sandbox             |
| `write_file`       | `path`, `content`                | Write a file to sandbox              |
| `mcp_proxy_call`   | `backend_id`, `request`, `timeout_sec` | Forward call to VM MCP backend |

### VM MCP backend ports (Scenario B)

| Port  | Env var                    | Backend            |
|-------|----------------------------|--------------------|
| 39011 | `MCP_TOOLBOX_FS_PORT`      | filesystem server  |
| 39012 | `MCP_TOOLBOX_BASH_PORT`    | shell server       |
| 39013 | `MCP_TOOLBOX_GIT_PORT`     | git server         |
