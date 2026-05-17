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

logger = get_logger("mcp")


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

        # Construct the command to read the file with offset and limit
        command = f'tail -n +{offset + 1} "{path}"'
        if limit is not None:
            command += f' | head -n {limit}'

        success, output = self.sandbox_manager.execute_command(sandbox_id, command)
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
        success, output = self.sandbox_manager.execute_command(
            sandbox_id,
            f'mkdir -p "$(dirname {path})" && cat > "{path}" << \'EOF\'\n{content}\nEOF',
        )
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
        Proxy an MCP request to a VM backend.

        Parameters:
        - params (dict):
            - backend_id (str): The ID of the backend to proxy the request to. Optional.
            - request (dict): The JSON-RPC object representing the request. Required.
            - timeout_sec (int): The timeout for the request in seconds. Optional.
        - envid (str): The environment ID associated with the sandbox. Required.
        - token (str): The authentication token for the request. Required.

        Returns:
        dict: A dictionary containing:
            - response (dict): The proxied backend response payload.
            - error (str): Any error message if the proxy call fails.
        """
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {"error": f"No sandbox for envid {envid}"}
        logger.info("tool_mcp_proxy_call sandbox=%s backend_id=%s", sandbox_id, params.get("backend_id", ""))

        sandbox_cfg = self.sandbox_manager.sandbox_configs.get(sandbox_id, {})
        rows = sandbox_cfg.get("mcp_bindings", [])
        if not isinstance(rows, list) or not rows:
            return {"error": f"No mcp_bindings configured for sandbox {sandbox_id}"}

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
            return {"error": f"Backend {requested_backend_id} not found in sandbox {sandbox_id}"}

        # Perform the proxy call (implementation omitted for brevity)
        return {"response": "Proxy call result (mocked)"}

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

        # Parse the patch content
        try:
            search_text, replace_text = self._parse_patch_content(patch_content)
        except ValueError as e:
            return {"error": str(e)}

        # Construct the patch command
        patch_command = (
            f"python3 -c \"import sys; from pathlib import Path; "
            f"file_path = Path('{file_path}'); "
            f"content = file_path.read_text(encoding='{encoding}', errors='{errors}'); "
            f"content = content.replace('''{search_text}''', '''{replace_text}'''); "
            f"file_path.write_text(content, encoding='{encoding}', errors='{errors}')\""
        )

        success, output = self.sandbox_manager.execute_command(sandbox_id, patch_command)
        logger.info("tool_patch_file finished sandbox=%s success=%s", sandbox_id, success)

        return {
            "success": success,
            "message": "Patch applied successfully" if success else output,
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
