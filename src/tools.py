"""
MCP tool implementations for sndbx
Core operations: file read/write, command execution, SSH access
"""

import os
import json
import asyncio
from pathlib import Path
from typing import Dict, Any, Optional

from logging_utils import get_logger
from mcp_proxy import McpProxyError, VmMcpClient, tool_is_allowed

logger = get_logger("mcp")


READ_FILE_SCRIPT = 'tail -n +{offset} "$1"'
READ_FILE_LIMIT_SCRIPT = 'tail -n +{offset} "$1" | head -n {limit}'
WRITE_FILE_SCRIPT = 'mkdir -p -- "$(dirname -- "$1")" && cat > "$1"'
READ_FILE_RAW_SCRIPT = 'cat -- "$1"'


TOOL_CATALOG = [
    {
        "name": "execute_command",
        "handler": "tool_execute_command",
        "description": "Execute a shell command inside the sandbox associated with the resolved environment.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "command": {
                    "type": "string",
                    "description": "Shell command to execute inside the sandbox.",
                }
            },
            "required": ["command"],
        },
    },
    {
        "name": "read_file",
        "handler": "tool_read_file",
        "description": "Read text content from a sandbox file with optional line offset and line limit. Use files under /root/ (~/) and avoid the filesystem root /. If needed, create nested directories under /root/ with execute_command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Path to a file under /root/ (~/). Do not target the filesystem root /.",
                },
                "offset": {
                    "type": "integer",
                    "description": "Zero-based line offset to start reading from. Defaults to 0.",
                },
                "limit": {
                    "type": "integer",
                    "description": "Maximum number of lines to return. If omitted, returns until EOF.",
                },
            },
            "required": ["path"],
        },
    },
    {
        "name": "write_file",
        "handler": "tool_write_file",
        "description": "Write or overwrite text content in a sandbox file, creating parent directories if needed. Place files under /root/ (~/) and avoid writing to the filesystem root /. For deeper directory trees under /root/, create them with execute_command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {
                    "type": "string",
                    "description": "Destination path under /root/ (~/). Do not write directly to the filesystem root /.",
                },
                "content": {
                    "type": "string",
                    "description": "Text content to write into the target file.",
                },
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "sandbox_status",
        "handler": "tool_sandbox_status",
        "description": "Return current sandbox runtime status for the resolved environment.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "sandbox_start",
        "handler": "tool_sandbox_start",
        "description": "Start the sandbox for the resolved environment.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "sandbox_stop",
        "handler": "tool_sandbox_stop",
        "description": "Stop the sandbox for the resolved environment.",
        "inputSchema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "mcp_proxy_call",
        "handler": "tool_mcp_proxy_call",
        "description": "Forward a JSON-RPC call to an MCP backend configured in sandbox mcp_bindings.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "backend_id": {
                    "type": "string",
                    "description": "Backend identifier from sandbox mcp_bindings. If omitted, the first binding is used.",
                },
                "request": {
                    "type": "object",
                    "description": "JSON-RPC request object to forward to the selected backend.",
                },
                "timeout_sec": {
                    "type": "number",
                    "description": "Timeout in seconds for backend HTTP requests.",
                },
            },
            "required": ["request"],
        },
    },
    {
        "name": "patch_file",
        "handler": "tool_patch_file",
        "description": "Apply a SEARCH/REPLACE block patch to a sandbox file using text replacement. Use target files under /root/ (~/) and avoid the filesystem root /. If directories are missing, create them under /root/ with execute_command.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "file_path": {
                    "type": "string",
                    "description": "Target file path under /root/ (~/). Do not patch files directly in the filesystem root /.",
                },
                "patch_content": {
                    "type": "string",
                    "description": "Patch in block format with SEARCH/REPLACE sections.",
                },
                "encoding": {
                    "type": "string",
                    "description": "Text encoding used for read/write operations. Defaults to utf-8.",
                },
                "errors": {
                    "type": "string",
                    "description": "Encoding error mode: strict, ignore, or replace. Defaults to strict.",
                },
            },
            "required": ["file_path", "patch_content"],
        },
    },
]


def register_all_tools(mcp_server, handlers):
    """Register all MCP tools from a single metadata catalog.

    Output: None.
    Input: MCP server instance and ToolHandlers instance.
    """

    for tool in TOOL_CATALOG:
        handler = getattr(handlers, tool["handler"])
        mcp_server.register_tool(
            tool["name"],
            handler,
            description=tool["description"],
            input_schema=tool["inputSchema"],
        )


class ToolHandlers:
    """MCP tool handlers"""

    def __init__(self, sandbox_manager, config):
        self.sandbox_manager = sandbox_manager
        self.config = config

    async def tool_execute_command(self, params, envid, token):
        """
        Execute a bash command inside the sandbox.

        Parameters:
        - params (dict):
            - command (str): The bash command to execute. Required.
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the command executed successfully.
            - output (str): The output of the command.
            - sandbox_id (str): The ID of the sandbox where the command was executed.
        """
        command = params.get("command", "")
        if not command:
            return {"error": "command parameter is required"}
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_execute_command sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.execute_command(sandbox_id, command)
        logger.info("tool_execute_command finished sandbox=%s success=%s", sandbox_id, success)
        return {"success": success, "output": output, "sandbox_id": sandbox_id}

    async def tool_read_file(self, params, envid, token):
        """
        Read a file from the sandbox with optional offset and limit.

        Parameters:
        - params (dict):
            - path (str): The path to the file to read. Required.
            - offset (int): The starting line number (0-based). Optional, defaults to 0.
            - limit (int): The maximum number of lines to return. Optional, defaults to None (no limit).
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the file was read successfully.
            - content (str): The content of the file (with applied offset and limit).
            - path (str): The path of the file that was read.
            - sandbox_id (str): The ID of the sandbox where the file was read.
        """
        path = params.get("path", "")
        offset = params.get("offset", 0)
        limit = params.get("limit", None)

        if not path:
            return {"error": "path parameter is required"}

        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}

        logger.info("tool_read_file sandbox=%s path=%s offset=%s limit=%s", sandbox_id, path, offset, limit)

        if limit is not None:
            script = READ_FILE_LIMIT_SCRIPT.format(
                offset=int(offset) + 1, limit=int(limit)
            )
        else:
            script = READ_FILE_SCRIPT.format(offset=int(offset) + 1)
        argv = ["sh", "-c", script, "sh", path]
        success, output = self.sandbox_manager.exec_argv(sandbox_id, argv)
        logger.info("tool_read_file finished sandbox=%s success=%s", sandbox_id, success)

        return {
            "success": success,
            "content": output,
            "path": path,
            "sandbox_id": sandbox_id
        }

    async def tool_write_file(self, params, envid, token):
        """
        Write content to a file inside the sandbox.

        Parameters:
        - params (dict):
            - path (str): The path to the file to write. Required.
            - content (str): The content to write to the file. Required.
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the file was written successfully.
            - path (str): The path of the file that was written.
            - sandbox_id (str): The ID of the sandbox where the file was written.
            - message (str): A success message or error details.
        """
        path = params.get("path", "")
        content = params.get("content", "")
        if not path:
            return {"error": "path parameter is required"}
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_write_file sandbox=%s path=%s bytes=%s", sandbox_id, path, len(str(content)))
        argv = ["sh", "-c", WRITE_FILE_SCRIPT, "sh", path]
        success, output = self.sandbox_manager.exec_argv(sandbox_id, argv, stdin_data=str(content))
        logger.info("tool_write_file finished sandbox=%s success=%s", sandbox_id, success)
        return {"success": success, "path": path, "sandbox_id": sandbox_id,
                "message": "File written" if success else output}

    async def tool_sandbox_status(self, params, envid, token):
        """
        Retrieve the status of a sandbox.

        Parameters:
        - params (dict): Empty dictionary (no parameters required).
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - id (str): The ID of the sandbox.
            - running (bool): Whether the sandbox is currently running.
            - container_id (str): The container ID of the sandbox.
            - ip (str): The IP address of the sandbox.
            - error (str): Any error message if the sandbox is not found.
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_status sandbox=%s", sandbox_id)
        status = self.sandbox_manager.get_status(sandbox_id)
        return {"id": status.id, "running": status.running,
                "container_id": status.container_id, "ip": status.ip, "error": status.error}

    async def tool_sandbox_start(self, params, envid, token):
        """
        Start a sandbox.

        Parameters:
        - params (dict): Empty dictionary (no parameters required).
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the sandbox was started successfully.
            - message (str): A success message or error details.
            - sandbox_id (str): The ID of the sandbox that was started.
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_start sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.start_sandbox(sandbox_id)
        return {"success": success, "message": output, "sandbox_id": sandbox_id}

    async def tool_sandbox_stop(self, params, envid, token):
        """
        Stop a sandbox.

        Parameters:
        - params (dict): Empty dictionary (no parameters required).
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the sandbox was stopped successfully.
            - message (str): A success message or error details.
            - sandbox_id (str): The ID of the sandbox that was stopped.
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_sandbox_stop sandbox=%s", sandbox_id)
        success, output = self.sandbox_manager.stop_sandbox(sandbox_id)
        return {"success": success, "message": output, "sandbox_id": sandbox_id}

    async def tool_mcp_proxy_call(self, params, envid, token):
        """
        Proxy a JSON-RPC call to an MCP backend published by the sandbox.

        Parameters:
        - params (dict):
            - backend_id (str): The backend identifier from mcp_bindings. If omitted, the first binding is used.
            - request (dict): The JSON-RPC request object to forward to the selected backend.
            - timeout_sec (number): Timeout in seconds for backend HTTP requests.
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the call reached the backend.
            - response (dict): The proxied backend response payload.
            - error (str): Any error message if the proxy call fails.
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"success": False, "error": f"No sandbox for envid {envid}"}
        logger.info("tool_mcp_proxy_call sandbox=%s backend_id=%s", sandbox_id, params.get("backend_id", ""))

        sandbox_cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {})
        rows = sandbox_cfg.get("mcp_bindings", [])
        if not isinstance(rows, list) or not rows:
            return {"success": False,
                    "error": f"No mcp_bindings configured for sandbox {sandbox_id}"}

        requested_backend_id = str(params.get("backend_id", "")).strip()
        selected = None
        for row in rows:
            if not isinstance(row, dict):
                continue
            backend_id = str(row.get("backend_id", "")).strip()
            if backend_id == requested_backend_id or not requested_backend_id:
                selected = row
                break
        if not selected:
            return {"success": False,
                    "error": f"Backend {requested_backend_id} not found in sandbox {sandbox_id}"}

        request = params.get("request")
        if not isinstance(request, dict) or not request:
            return {"success": False, "error": "request must be a JSON-RPC object"}

        allowed, blocked = tool_is_allowed(request, selected.get("tools"))
        if not allowed:
            return {"success": False,
                    "error": f"Tool '{blocked}' is not published by this backend"}

        try:
            vm_port = int(selected.get("vm_port"))
        except (TypeError, ValueError):
            return {"success": False,
                    "error": "Selected mcp_binding has no valid vm_port"}

        endpoint = self.sandbox_manager.published_endpoint(sandbox_id, vm_port)
        if endpoint is None:
            container_ip = self.sandbox_manager.get_container_ip(sandbox_id)
            if not container_ip:
                return {"success": False,
                        "error": f"Could not determine how to reach guest port {vm_port}"}
            endpoint = (container_ip, vm_port)

        auth_mode = str(selected.get("auth_mode", "token_passthrough")).strip()
        forward_token = token if auth_mode == "token_passthrough" else None

        try:
            startup_timeout = float(selected.get("startup_timeout_sec", 10) or 10)
        except (TypeError, ValueError):
            startup_timeout = 10.0
        try:
            read_timeout = float(selected.get("read_timeout_sec", 30) or 30)
        except (TypeError, ValueError):
            read_timeout = 30.0
        try:
            request_timeout = float(params.get("timeout_sec", 0) or 0)
        except (TypeError, ValueError):
            request_timeout = 0.0
        if request_timeout > 0:
            read_timeout = request_timeout

        client = VmMcpClient(
            endpoint[0],
            endpoint[1],
            startup_timeout=startup_timeout,
            read_timeout=read_timeout,
        )
        try:
            response = await client.call(request, token=forward_token)
        except McpProxyError as error:
            return {"success": False, "error": str(error)}
        except Exception as error:  # noqa: BLE001 - transport failure
            return {"success": False, "error": f"proxy call failed: {error}"}
        return {"success": True, "response": response}

    async def tool_patch_file(self, params, envid, token):
        """
        Apply a block-style patch to a file inside the sandbox.

        Parameters:
        - params (dict):
            - file_path (str): The path to the file to patch. Required.
            - patch_content (str): The content of the patch. Required.
            - encoding (str): The file encoding. Optional, defaults to 'utf-8'.
            - errors (str): Encoding error handling mode ('strict', 'ignore', 'replace'). Optional, defaults to 'strict'.
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - success (bool): Whether the patch was applied successfully.
            - message (str): A success message or error details.
            - file_path (str): The path of the patched file.
            - sandbox_id (str): The ID of the sandbox where the file was patched.
        """
        file_path = params.get("file_path", "")
        patch_content = params.get("patch_content", "")
        encoding = params.get("encoding", "utf-8")
        errors = params.get("errors", "strict")

        if not file_path:
            return {"error": "file_path parameter is required"}
        if not patch_content:
            return {"error": "patch_content parameter is required"}

        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}

        logger.info("tool_patch_file sandbox=%s file_path=%s", sandbox_id, file_path)

        try:
            search_text, replace_text = self._parse_patch_content(patch_content)
        except ValueError as error:
            return {"error": str(error)}

        read_ok, content = self.sandbox_manager.exec_argv(
            sandbox_id,
            ["sh", "-c", READ_FILE_RAW_SCRIPT, "sh", file_path],
            encoding=encoding,
            errors=errors,
        )
        if not read_ok:
            return {"success": False, "message": content.strip() or "Could not read the file"}
        if search_text not in content:
            return {
                "success": False,
                "message": "Patch search text was not found in the file",
                "file_path": file_path,
                "sandbox_id": sandbox_id,
            }

        patched = content.replace(search_text, replace_text, 1)
        write_ok, write_output = self.sandbox_manager.exec_argv(
            sandbox_id,
            ["sh", "-c", WRITE_FILE_SCRIPT, "sh", file_path],
            stdin_data=patched,
            encoding=encoding,
            errors=errors,
        )
        logger.info("tool_patch_file finished sandbox=%s success=%s", sandbox_id, write_ok)

        return {
            "success": write_ok,
            "message": "Patch applied successfully" if write_ok else write_output,
            "file_path": file_path,
            "sandbox_id": sandbox_id
        }

    def _parse_patch_content(self, patch_content):
        """
        Parse the block-style patch content into search and replace parts.

        Parameters:
        - patch_content (str): The content of the patch.

        Returns:
        tuple: A tuple containing (search_text, replace_text).

        Raises:
        ValueError: If the patch content is not in the correct format.
        """
        if "<<<<<<< SEARCH" not in patch_content or "=======" not in patch_content or ">>>>>>> REPLACE" not in patch_content:
            raise ValueError("Invalid patch format. Ensure it contains '<<<<<<< SEARCH', '=======', and '>>>>>>> REPLACE'.")

        search_text = patch_content.split("<<<<<<< SEARCH\n", 1)[1].split("\n=======\n", 1)[0]
        replace_text = patch_content.split("\n=======\n", 1)[1].split("\n>>>>>>> REPLACE", 1)[0]

        return search_text, replace_text

    def _get_sandbox_for_envid(self, envid):
        """Get sandbox ID for an envid"""
        envids_config = self.config.get("envids", {})
        if envid in envids_config:
            return envids_config[envid].get("sandbox")
        return None
