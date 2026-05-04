"""
Docker-based sandbox manager
Manages lifecycle of Docker containers with Kata runtime
"""

import subprocess
import json
import logging
from typing import Dict, Any, Optional, List
from dataclasses import dataclass


logger = logging.getLogger(__name__)


@dataclass
class SandboxStatus:
    """Status of a sandbox VM"""
    id: str
    running: bool
    container_id: Optional[str] = None
    ip: Optional[str] = None
    error: Optional[str] = None


class DockerSandboxManager:
    """Manages sandboxes via Docker with Kata runtime"""
    
    def __init__(self, config: Dict[str, Any]):
        self.config = config
        self.sandbox_configs = config.get('sandboxes', {}).get('items', {})
    
    def _run_docker_cmd(self, cmd: List[str]) -> tuple[bool, str]:
        """Run docker command and return (success, output)"""
        try:
            result = subprocess.run(
                ['docker'] + cmd,
                capture_output=True,
                text=True,
                timeout=30
            )
            return result.returncode == 0, result.stdout + result.stderr
        except subprocess.TimeoutExpired:
            return False, "Command timed out"
        except Exception as e:
            return False, str(e)

    def _is_storage_opt_unsupported(self, output: str) -> bool:
        """Detect whether Docker rejected storage-opt size for current driver."""
        text = (output or "").lower()
        hints = [
            "unknown flag: --storage-opt",
            "unknown option",
            "storage-opt",
            "storage driver",
            "not supported",
            "invalid option",
        ]
        return any(hint in text for hint in hints)
    
    def get_status(self, sandbox_id: str) -> SandboxStatus:
        """Get current status of a sandbox"""
        success, output = self._run_docker_cmd([
            'inspect',
            f'sndbx-{sandbox_id}'
        ])
        
        if not success:
            return SandboxStatus(
                id=sandbox_id,
                running=False,
                error="Container not found or not running"
            )
        
        try:
            data = json.loads(output)
            if data:
                container = data[0]
                return SandboxStatus(
                    id=sandbox_id,
                    running=container.get('State', {}).get('Running', False),
                    container_id=container.get('Id', ''),
                    ip=container.get('NetworkSettings', {}).get('IPAddress'),
                )
        except (json.JSONDecodeError, IndexError, KeyError):
            pass
        
        return SandboxStatus(
            id=sandbox_id,
            running=False,
            error="Could not parse container status"
        )
    
    def create_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Create a new sandbox container"""
        if sandbox_id not in self.sandbox_configs:
            return False, f"Sandbox {sandbox_id} not in configuration"
        
        sandbox_cfg = self.sandbox_configs[sandbox_id]
        image = sandbox_cfg.get('image', 'ubuntu:22.04')
        memory = sandbox_cfg.get('memory', '2G')
        cpus = sandbox_cfg.get('cpus', 2)
        disk_max = sandbox_cfg.get('disk_max')
        # Reserved for future implementation. For now this is a config-only field.
        _network_traffic_max = sandbox_cfg.get('network_traffic_max')
        
        base_cmd = [
            'run',
            '--name', f'sndbx-{sandbox_id}',
            '--runtime', 'kata',
            '-m', memory,
            '--cpus', str(cpus),
            '--detach',
            image,
            'sleep', 'infinity'  # Keep container running
        ]

        cmd_with_disk_limit = list(base_cmd)
        if disk_max:
            cmd_with_disk_limit = [
                'run',
                '--name', f'sndbx-{sandbox_id}',
                '--runtime', 'kata',
                '-m', memory,
                '--cpus', str(cpus),
                '--detach',
                '--storage-opt', f'size={disk_max}',
                image,
                'sleep', 'infinity'  # Keep container running
            ]

        success, output = self._run_docker_cmd(cmd_with_disk_limit)
        if not success and disk_max and self._is_storage_opt_unsupported(output):
            logger.warning(
                "Disk limit '%s' is not supported by current Docker storage driver. "
                "Retrying sandbox '%s' without disk limit.",
                disk_max,
                sandbox_id,
            )
            success, output = self._run_docker_cmd(base_cmd)

        if success:
            logger.info(f"Created sandbox {sandbox_id}")
        else:
            logger.error(f"Failed to create sandbox {sandbox_id}: {output}")

        return success, output

    def start_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Start an existing sandbox container"""
        success, output = self._run_docker_cmd(['start', f'sndbx-{sandbox_id}'])
        if success:
            logger.info(f"Started sandbox {sandbox_id}")
        return success, output

    def stop_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Stop a running sandbox container"""
        success, output = self._run_docker_cmd(['stop', f'sndbx-{sandbox_id}'])
        if success:
            logger.info(f"Stopped sandbox {sandbox_id}")
        return success, output

    def execute_command(self, sandbox_id: str, command: str) -> tuple[bool, str]:
        """Execute a command inside the sandbox"""
        success, output = self._run_docker_cmd([
            'exec',
            f'sndbx-{sandbox_id}',
            'bash', '-c', command
        ])
        return success, output

    def restart_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Restart a running sandbox container."""
        success, output = self._run_docker_cmd(['restart', f'sndbx-{sandbox_id}'])
        if success:
            logger.info(f"Restarted sandbox {sandbox_id}")
        return success, output

    def list_sandboxes(self) -> tuple[bool, List[Dict[str, Any]]]:
        """List managed sandbox containers and their docker-level status."""
        success, output = self._run_docker_cmd([
            'ps',
            '-a',
            '--filter', 'name=sndbx-',
            '--format', '{{json .}}',
        ])
        if not success:
            return False, []

        items: List[Dict[str, Any]] = []
        for line in output.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue

            name = row.get('Names', '')
            sandbox_id = name.replace('sndbx-', '', 1) if name.startswith('sndbx-') else name
            items.append({
                'sandbox_id': sandbox_id,
                'container_name': name,
                'container_id': row.get('ID', ''),
                'image': row.get('Image', ''),
                'status': row.get('Status', ''),
                'ports': row.get('Ports', ''),
            })

        return True, items
