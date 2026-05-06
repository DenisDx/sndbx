"""
Docker-based sandbox manager
Manages lifecycle of Docker containers with Kata runtime
"""

import subprocess
import json
import logging
import os
from pathlib import Path
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
        root = str(config.get('root') or '').strip() or os.getcwd()
        self.root_dir = Path(root).resolve()
        self.images_dir = self.root_dir / 'images'
    
    def _run_docker_cmd(self, cmd: List[str], timeout: int = 30) -> tuple[bool, str]:
        """Run docker command and return (success, output)"""
        try:
            result = subprocess.run(
                ['docker'] + cmd,
                capture_output=True,
                text=True,
                timeout=timeout
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

    def _local_image_id_for_ref(self, image_ref: str) -> Optional[str]:
        """Resolve local image directory id for an image ref if present in images/.

        input: image reference from sandbox config
        output: local image id when images/<id> exists, otherwise None
        """
        ref = str(image_ref or '').strip()
        if not ref:
            return None
        local_id = ref.split(':', 1)[0]
        image_dir = self.images_dir / local_id
        return local_id if image_dir.is_dir() else None

    def _docker_image_exists(self, image_ref: str) -> bool:
        """Check if image exists in local Docker image store."""
        ok, _ = self._run_docker_cmd(['image', 'inspect', image_ref], timeout=20)
        return ok

    def _build_local_image(self, local_id: str, image_ref: str, no_cache: bool = False) -> tuple[bool, str]:
        """Build image_ref from images/<local_id>/Dockerfile."""
        image_dir = self.images_dir / local_id
        dockerfile = image_dir / 'Dockerfile'
        if not dockerfile.is_file():
            return False, f"Dockerfile not found: {dockerfile}"

        cmd = ['build', '-t', image_ref]
        if no_cache:
            cmd.append('--no-cache')
        cmd.extend(['--build-arg', f'APT_MIRROR={self.APT_MIRROR}'])
        cmd.append(str(image_dir))
        return self._run_docker_cmd(cmd, timeout=1800)

    def _ensure_image_ready(self, image_ref: str) -> tuple[bool, str]:
        """Ensure image is available, auto-building local images/<id> when needed."""
        if self._docker_image_exists(image_ref):
            return True, "image already present"

        local_id = self._local_image_id_for_ref(image_ref)
        if not local_id:
            return False, f"Docker image not found locally: {image_ref}"

        logger.info("Building local image '%s' from images/%s", image_ref, local_id)
        return self._build_local_image(local_id, image_ref, no_cache=False)

    def _shared_mount_args(self, sandbox_id: str, sandbox_cfg: Dict[str, Any]) -> List[str]:
        """Build docker -v args from shared_directories and create host paths.

        input: sandbox id and sandbox config
        output: flat docker run argument list
        """
        args: List[str] = []
        rows = sandbox_cfg.get('shared_directories', [])
        if not isinstance(rows, list):
            return args

        for row in rows:
            if not isinstance(row, dict):
                continue
            host_path = str(row.get('host_path', '')).strip()
            guest_path = str(row.get('guest_path', '')).strip()
            mount_type = str(row.get('mount_type', '')).strip().lower()
            permission = str(row.get('permission', 'rw')).strip().lower()
            host_mode = str(row.get('host_mode', '')).strip()
            mode = 'ro' if permission == 'ro' else 'rw'

            if not host_path or not guest_path:
                continue

            host = Path(host_path)
            if not host.is_absolute():
                host = (self.root_dir / host).resolve()

            # If mount target looks like a file, ensure parent + file; otherwise ensure dir.
            guest_name = Path(guest_path).name
            # Prefer explicit mount_type from config. Fallback to conservative suffix-based heuristic.
            if mount_type == 'file':
                is_file_mount = True
            elif mount_type == 'dir':
                is_file_mount = False
            else:
                is_file_mount = bool(Path(guest_name).suffix and not guest_name.startswith('.'))
            try:
                if is_file_mount:
                    host.parent.mkdir(parents=True, exist_ok=True)
                    host.touch(exist_ok=True)
                else:
                    if host.exists() and host.is_file():
                        logger.warning(
                            "Mount path '%s' is a file but directory is required; replacing with directory",
                            host,
                        )
                        host.unlink()
                    host.mkdir(parents=True, exist_ok=True)
                    # Apply host_mode permissions if specified
                    if host_mode:
                        try:
                            import os
                            os.chmod(host, int(host_mode, 8))
                        except (ValueError, OSError) as exc:
                            logger.warning("Could not set mode %s on %s: %s", host_mode, host, exc)
            except Exception as exc:
                logger.warning("Could not prepare host mount '%s' for sandbox '%s': %s", host, sandbox_id, exc)
                continue

            args.extend(['-v', f'{host}:{guest_path}:{mode}'])

        return args

    def _port_binding_args(self, sandbox_id: str, sandbox_cfg: Dict[str, Any]) -> List[str]:
        """Build docker -p args from sandbox port_bindings config.

        input: sandbox id and sandbox config
        output: flat docker run argument list
        """
        args: List[str] = []
        rows = sandbox_cfg.get('port_bindings', [])
        if not isinstance(rows, list):
            return args

        for row in rows:
            if not isinstance(row, dict):
                continue

            bind_host = str(row.get('bind_host', '127.0.0.1')).strip() or '127.0.0.1'
            vm_port = row.get('vm_port')
            publish_port = row.get('publish_port')

            try:
                vm_port_i = int(vm_port)
                publish_port_i = int(publish_port)
            except (TypeError, ValueError):
                logger.warning(
                    "Invalid port binding in sandbox '%s': vm_port=%r publish_port=%r",
                    sandbox_id,
                    vm_port,
                    publish_port,
                )
                continue

            if not (1 <= vm_port_i <= 65535 and 1 <= publish_port_i <= 65535):
                logger.warning(
                    "Out-of-range port binding in sandbox '%s': vm_port=%s publish_port=%s",
                    sandbox_id,
                    vm_port_i,
                    publish_port_i,
                )
                continue

            args.extend(['-p', f'{bind_host}:{publish_port_i}:{vm_port_i}'])

        return args

    def _run_image_hook(self, sandbox_id: str, sandbox_cfg: Dict[str, Any], hook_name: str = 'on_system_start') -> tuple[bool, str]:
        """Run standardized image hook from /opt/sndbx-image/app.py inside container.

        input: sandbox id, sandbox config, hook name
        output: (ok, message)
        """
        image_ref = str(sandbox_cfg.get('image', '')).strip()
        local_id = self._local_image_id_for_ref(image_ref)
        if not local_id:
            return True, 'not a local image'

        host_app = self.images_dir / local_id / 'app.py'
        if not host_app.is_file():
            return True, 'no app.py hook file'

        ctx = {
            'sandbox_id': sandbox_id,
            'sandbox_cfg': sandbox_cfg,
        }
        ctx_json = json.dumps(ctx, ensure_ascii=True)

        ok, out = self._run_docker_cmd([
            'exec',
            '-e', f'SNDBX_HOOK={hook_name}',
            '-e', f'SNDBX_CONTEXT_JSON={ctx_json}',
            f'sndbx-{sandbox_id}',
            'python3', '/opt/sndbx-image/app.py',
        ], timeout=60)
        if not ok:
            return False, out.strip() or 'hook failed'
        return True, out.strip() or 'hook completed'

    def list_local_images(self) -> List[Dict[str, Any]]:
        """List configured images from sandboxes with local build metadata."""
        rows: Dict[str, Dict[str, Any]] = {}
        for sandbox_id, sandbox_cfg in self.sandbox_configs.items():
            image_ref = str(sandbox_cfg.get('image', '')).strip()
            if not image_ref:
                continue
            local_id = self._local_image_id_for_ref(image_ref)
            if not local_id:
                continue
            rec = rows.get(image_ref)
            if not rec:
                rec = {
                    'image': image_ref,
                    'local_id': local_id,
                    'path': str(self.images_dir / local_id) if local_id else '',
                    'has_dockerfile': bool(local_id and (self.images_dir / local_id / 'Dockerfile').is_file()),
                    'has_app_py': bool(local_id and (self.images_dir / local_id / 'app.py').is_file()),
                    'built': self._docker_image_exists(image_ref),
                    'sandboxes': [],
                }
                rows[image_ref] = rec
            rec['sandboxes'].append(sandbox_id)
        return sorted(rows.values(), key=lambda x: x['image'])

    def build_configured_image(self, image_ref: str, no_cache: bool = False) -> tuple[bool, str]:
        """Build configured local image by ref if images/<id> exists."""
        ref = str(image_ref or '').strip()
        if not ref:
            return False, 'image is required'
        local_id = self._local_image_id_for_ref(ref)
        if not local_id:
            return False, f"Local image folder not found for '{ref}'"
        return self._build_local_image(local_id, ref, no_cache=no_cache)

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
        """Return container IP address for a sandbox, or None when unavailable.

        Docker may keep the effective address in NetworkSettings.Networks.<name>.IPAddress
        while NetworkSettings.IPAddress remains empty.
        """
        ok, out = self._run_docker_cmd(['inspect', f'sndbx-{sandbox_id}'])
        if not ok:
            return None

        try:
            data = json.loads(out)
            if not data:
                return None
            net = data[0].get('NetworkSettings', {}) or {}

            # Legacy/bridge path (often empty on modern Docker).
            ip = str(net.get('IPAddress', '')).strip()
            if ip and ip not in ("<no value>", "0.0.0.0"):
                return ip

            # Preferred modern path: first non-empty network IP.
            networks = net.get('Networks', {}) or {}
            if isinstance(networks, dict):
                for row in networks.values():
                    if not isinstance(row, dict):
                        continue
                    nip = str(row.get('IPAddress', '')).strip()
                    if nip and nip not in ("<no value>", "0.0.0.0"):
                        return nip
        except (json.JSONDecodeError, IndexError, TypeError, ValueError):
            return None

        return None

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
                    ip=self.get_container_ip(sandbox_id),
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

        image_ok, image_msg = self._ensure_image_ready(image)
        if not image_ok:
            logger.error("Failed to prepare image '%s' for sandbox '%s': %s", image, sandbox_id, image_msg)
            return False, image_msg
        
        # Mount apt directories as tmpfs to avoid virtiofs overhead for small-file writes.
        # The sandbox rootfs is virtiofs-backed (host overlay → virtiofsd → guest), so
        # apt update writing hundreds of index files is very slow without this.
        apt_tmpfs_args = [
            '--tmpfs', '/var/lib/apt/lists:rw,exec',
            '--tmpfs', '/var/cache/apt:rw,exec',
        ]
        shared_mount_args = self._shared_mount_args(sandbox_id, sandbox_cfg)
        port_binding_args = self._port_binding_args(sandbox_id, sandbox_cfg)

        base_cmd = [
            'run',
            '--name', f'sndbx-{sandbox_id}',
            '--runtime', 'kata',
            '-m', memory,
            '--cpus', str(cpus),
            '--detach',
            *apt_tmpfs_args,
            *shared_mount_args,
            *port_binding_args,
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
                *shared_mount_args,
                *port_binding_args,
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

            hook_ok, hook_msg = self._run_image_hook(sandbox_id, sandbox_cfg, hook_name='on_system_start')
            if not hook_ok:
                logger.warning("image hook failed for sandbox '%s': %s", sandbox_id, hook_msg)

        return success, output

    def start_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Start an existing sandbox container"""
        success, output = self._run_docker_cmd(['start', f'sndbx-{sandbox_id}'])
        if success:
            logger.info(f"Started sandbox {sandbox_id}")
            sandbox_cfg = self.sandbox_configs.get(sandbox_id, {})
            hook_ok, hook_msg = self._run_image_hook(sandbox_id, sandbox_cfg, hook_name='on_system_start')
            if not hook_ok:
                logger.warning("image hook failed on start for sandbox '%s': %s", sandbox_id, hook_msg)
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
