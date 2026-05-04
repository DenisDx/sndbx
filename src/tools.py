"""
MCP tool implementations for sndbx
Core operations: file read/write, command execution, SSH access
"""

import os
import logging
from pathlib import Path
from typing import Dict, Any, Optional
import asyncio


logger = logging.getLogger(__name__)


class ToolHandlers:
    """MCP tool handlers"""
    
    def __init__(self, sandbox_manager, config: Dict[str, Any]):
        self.sandbox_manager = sandbox_manager
        self.config = config
    
    async def tool_execute_command(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Execute bash command in sandbox"""
        command = params.get('command', '')
        if not command:
            return {'error': 'command parameter is required'}
        
        # Resolve sandbox ID from envid
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        success, output = self.sandbox_manager.execute_command(sandbox_id, command)
        return {
            'success': success,
            'output': output,
            'sandbox_id': sandbox_id
        }
    
    async def tool_read_file(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Read file from sandbox"""
        path = params.get('path', '')
        if not path:
            return {'error': 'path parameter is required'}
        
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        # Use cat command to read file
        success, output = self.sandbox_manager.execute_command(
            sandbox_id,
            f'cat "{path}" 2>&1'
        )
        
        return {
            'success': success,
            'content': output,
            'path': path,
            'sandbox_id': sandbox_id
        }
    
    async def tool_write_file(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Write file to sandbox"""
        path = params.get('path', '')
        content = params.get('content', '')
        
        if not path:
            return {'error': 'path parameter is required'}
        
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        # Escape content for shell
        escaped = content.replace('\'', '\'"\'"\'')
        
        success, output = self.sandbox_manager.execute_command(
            sandbox_id,
            f'mkdir -p "$(dirname {path})" && cat > "{path}" << \'EOF\'\n{content}\nEOF'
        )
        
        return {
            'success': success,
            'path': path,
            'sandbox_id': sandbox_id,
            'message': 'File written' if success else output
        }
    
    async def tool_sandbox_status(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Get sandbox status"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        status = self.sandbox_manager.get_status(sandbox_id)
        
        return {
            'id': status.id,
            'running': status.running,
            'container_id': status.container_id,
            'ip': status.ip,
            'error': status.error
        }
    
    async def tool_sandbox_start(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Start sandbox"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        success, output = self.sandbox_manager.start_sandbox(sandbox_id)
        return {
            'success': success,
            'message': output,
            'sandbox_id': sandbox_id
        }
    
    async def tool_sandbox_stop(self, params: Dict[str, Any], envid: str, token: str) -> Dict[str, Any]:
        """Stop sandbox"""
        sandbox_id = self._get_sandbox_for_envid(envid)
        if not sandbox_id:
            return {'error': f'No sandbox for envid {envid}'}
        
        success, output = self.sandbox_manager.stop_sandbox(sandbox_id)
        return {
            'success': success,
            'message': output,
            'sandbox_id': sandbox_id
        }
    
    def _get_sandbox_for_envid(self, envid: str) -> Optional[str]:
        """Get sandbox ID for an envid"""
        envids_config = self.config.get('envids', {})
        if envid in envids_config:
            return envids_config[envid].get('sandbox')
        return None
