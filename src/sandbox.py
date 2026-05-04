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

    def _is_kata_runtime_unavailable(self, output: str) -> bool:
        """Detect missing/unknown kata runtime in Docker daemon."""
        text = (output or "").lower()
        hints = [
            "unknown or invalid runtime name",
            "unknown runtime specified",
            "runtime name: kata",
            "runtime kata",
        ]
        return any(hint in text for hint in hints)

    def _is_name_conflict(self, output: str) -> bool:
        """Detect container name conflict on create."""
        text = (output or "").lower()
        return "conflict" in text and "container name" in text and "already in use" in text

    def _is_kata_config_missing(self, output: str) -> bool:
        """Detect missing kata configuration files from runtime output."""
        text = (output or "").lower()
        return "configuration.toml" in text and "does not exist" in text

    # Aliyun mirror is fast from this host (~2.8 MB/s vs ~10 KB/s from archive.ubuntu.com).
    # Applied once at container creation; keeps indices in tmpfs so writes are in-memory.
    APT_MIRROR = "http://mirrors.aliyun.com/ubuntu"
    APT_SECURITY_MIRROR = "http://mirrors.aliyun.com/ubuntu"

    def configure_apt_mirror(self, sandbox_id: str) -> tuple[bool, str]:
        """Replace /etc/apt/sources.list with the fast Aliyun mirror."""
        sources = (
            f"deb {self.APT_MIRROR} jammy main restricted universe multiverse\n"
            f"deb {self.APT_MIRROR} jammy-updates main restricted universe multiverse\n"
            f"deb {self.APT_MIRROR} jammy-backports main restricted universe multiverse\n"
            f"deb {self.APT_SECURITY_MIRROR} jammy-security main restricted universe multiverse\n"
        )
        script = f"cat > /etc/apt/sources.list << 'SNDBX_SOURCES'\n{sources}SNDBX_SOURCES\n"
        try:
            result = subprocess.run(
                ['docker', 'exec', f'sndbx-{sandbox_id}', 'bash', '-c', script],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                logger.info("Configured apt mirror in sandbox '%s'", sandbox_id)
                return True, "mirror configured"
            return False, (result.stdout + result.stderr).strip()
        except Exception as exc:
            return False, str(exc)

    def get_container_ip(self, sandbox_id: str) -> Optional[str]:
        """Return Docker bridge IP for a running sandbox container, or None."""
        ok, out = self._run_docker_cmd([
            'inspect', '--format', '{{.NetworkSettings.IPAddress}}',
            f'sndbx-{sandbox_id}',
        ])
        ip = out.strip() if ok else ""
        return ip if ip and ip not in ("<no value>", "0.0.0.0", "") else None

    def exec_ssh_setup(self, sandbox_id: str, authorized_keys: List[str]) -> tuple[bool, str]:
        """Install sshd and configure authorized_keys inside a running container.

        Installs openssh-server when absent, writes authorized_keys, enables
        root login with key-only auth and (re)starts the sshd daemon.
        Returns (ok, message).
        """
        keys_block = "\n".join(authorized_keys) if authorized_keys else ""
        # Inline setup script executed via docker exec.
        setup_script = r"""
set -e
if ! command -v sshd >/dev/null 2>&1; then
    apt-get update -qq
    DEBIAN_FRONTEND=noninteractive apt-get install -y -qq openssh-server
fi
mkdir -p /root/.ssh /run/sshd
chmod 700 /root/.ssh
cat > /root/.ssh/authorized_keys << 'SNDBX_KEYS'
""" + keys_block + r"""
SNDBX_KEYS
chmod 600 /root/.ssh/authorized_keys
sed -i 's/^#\?PermitRootLogin.*/PermitRootLogin prohibit-password/' /etc/ssh/sshd_config
sed -i 's/^#\?PasswordAuthentication.*/PasswordAuthentication no/' /etc/ssh/sshd_config
pkill -x sshd || true
/usr/sbin/sshd
"""
        try:
            result = subprocess.run(
                ['docker', 'exec', f'sndbx-{sandbox_id}', 'bash', '-c', setup_script],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode == 0:
                logger.info("SSH daemon configured in sandbox '%s'", sandbox_id)
                return True, "sshd started"
            msg = (result.stdout + result.stderr).strip()
            logger.warning("SSH setup failed in sandbox '%s': %s", sandbox_id, msg)
            return False, msg
        except subprocess.TimeoutExpired:
            return False, "ssh setup timed out (120 s)"
        except Exception as exc:
            return False, str(exc)

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
        
        # Mount apt directories as tmpfs to avoid virtiofs overhead for small-file writes.
        # The sandbox rootfs is virtiofs-backed (host overlay → virtiofsd → guest), so
        # apt update writing hundreds of index files is very slow without this.
        apt_tmpfs_args = [
            '--tmpfs', '/var/lib/apt/lists:rw,exec',
            '--tmpfs', '/var/cache/apt:rw,exec',
        ]

        base_cmd = [
            'run',
            '--name', f'sndbx-{sandbox_id}',
            '--runtime', 'kata',
            '-m', memory,
            '--cpus', str(cpus),
            '--detach',
            *apt_tmpfs_args,
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
                *apt_tmpfs_args,
                image,
                'sleep', 'infinity'  # Keep container running
            ]

        launch_cmd = cmd_with_disk_limit
        success, output = self._run_docker_cmd(launch_cmd)
        if not success and disk_max and self._is_storage_opt_unsupported(output):
            logger.warning(
                "Disk limit '%s' is not supported by current Docker storage driver. "
                "Retrying sandbox '%s' without disk limit.",
                disk_max,
                sandbox_id,
            )
            launch_cmd = base_cmd
            success, output = self._run_docker_cmd(launch_cmd)

        if not success and self._is_name_conflict(output):
            logger.warning(
                "Container name conflict for sandbox '%s'. Removing stale container and retrying once.",
                sandbox_id,
            )
            rm_ok, rm_out = self._run_docker_cmd(['rm', '-f', f'sndbx-{sandbox_id}'])
            if rm_ok:
                success, output = self._run_docker_cmd(launch_cmd)
            else:
                output = f"{output}\nCleanup failed: {rm_out}"

        if success:
            logger.info(f"Created sandbox {sandbox_id}")
        else:
            needs_cleanup = self._is_kata_runtime_unavailable(output) or self._is_kata_config_missing(output)
            if needs_cleanup:
                rm_ok, rm_out = self._run_docker_cmd(['rm', '-f', f'sndbx-{sandbox_id}'])
                if rm_ok:
                    logger.warning("Removed failed container shell for sandbox '%s' after create error", sandbox_id)
                else:
                    logger.warning("Could not clean up failed container '%s': %s", sandbox_id, rm_out)

            if self._is_kata_runtime_unavailable(output):
                output = (
                    f"{output}\n"
                    "Hint: Docker runtime 'kata' is not registered. "
                    "Run ./install_prerequisites.sh to configure /etc/docker/daemon.json and restart docker."
                )
            if self._is_kata_config_missing(output):
                output = (
                    f"{output}\n"
                    "Hint: Kata configuration is missing. Run 'Repair Kata Runtime' in Web UI, "
                    "or install prerequisites again to restore configuration.toml."
                )
            logger.error(f"Failed to create sandbox {sandbox_id}: {output}")

        if success:
            mirror_ok, mirror_msg = self.configure_apt_mirror(sandbox_id)
            if not mirror_ok:
                logger.warning("apt mirror config failed for sandbox '%s': %s", sandbox_id, mirror_msg)

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
