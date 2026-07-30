"""
Docker-based sandbox manager
Manages lifecycle of Docker containers with Kata runtime
"""

import subprocess
import json
import re
import os
import socket
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, Any, Optional, List
from dataclasses import dataclass

from logging_utils import get_logger

logger = get_logger("sandbox_manager")


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

    def _preflight_shared_mounts(
        self, sandbox_id: str, sandbox_cfg: Dict[str, Any]
    ) -> tuple[bool, List[str], List[Dict[str, Any]], str]:
        """Validate shared mounts and return Docker arguments with redacted metadata."""
        args: List[str] = []
        resolved: List[Dict[str, Any]] = []
        rows = sandbox_cfg.get('shared_directories', [])
        if not isinstance(rows, list):
            return False, args, resolved, "shared_directories must be a list"

        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                return False, args, resolved, f"shared_directories[{index}] must be an object"
            host_path = str(row.get('host_path', '')).strip()
            guest_path = str(row.get('guest_path', '')).strip()
            source_type = str(row.get('source_type') or row.get('mount_type') or '').strip().lower()
            permission = str(row.get('permission', 'rw')).strip().lower()
            host_mode = str(row.get('host_mode', '')).strip()
            required = bool(row.get('required', True))
            create_if_missing = bool(row.get('create_if_missing', False))
            mode = 'ro' if permission == 'ro' else 'rw'

            if not host_path or not guest_path:
                return False, args, resolved, f"shared_directories[{index}] requires host_path and guest_path"
            if not guest_path.startswith('/'):
                return False, args, resolved, f"shared_directories[{index}] guest_path must be absolute"
            if permission not in {'ro', 'rw'}:
                return False, args, resolved, f"shared_directories[{index}] permission must be ro or rw"
            if source_type == 'dir':
                source_type = 'directory'
            if source_type not in {'file', 'directory'}:
                return False, args, resolved, (
                    f"shared_directories[{index}] source_type must be file or directory"
                )
            if create_if_missing and (source_type != 'directory' or permission != 'rw'):
                return False, args, resolved, (
                    f"shared_directories[{index}] create_if_missing requires a writable directory"
                )

            host = Path(host_path)
            if not host.is_absolute():
                host = (self.root_dir / host).resolve()

            try:
                if not host.exists() and create_if_missing:
                    host.mkdir(parents=True, exist_ok=True)
                if not host.exists():
                    if required:
                        return False, args, resolved, f"required mount source is missing: {host}"
                    continue
                matches_type = host.is_file() if source_type == 'file' else host.is_dir()
                if not matches_type:
                    return False, args, resolved, (
                        f"mount source type mismatch for {host}: expected {source_type}"
                    )
                if host_mode:
                    os.chmod(host, int(host_mode, 8))
            except Exception as exc:
                return False, args, resolved, (
                    f"could not prepare mount source for sandbox {sandbox_id}: {host}: {exc}"
                )

            args.extend(['-v', f'{host}:{guest_path}:{mode}'])
            resolved.append({
                'guest_path': guest_path,
                'source_type': source_type,
                'permission': permission,
                'required': required,
            })

        return True, args, resolved, ""

    def _managed_volume_args(self, sandbox_id: str, sandbox_cfg: Dict[str, Any]) -> List[str]:
        """Build Docker-managed volume mounts that never expose host paths.

        Args:
            sandbox_id: configured sandbox identifier used for diagnostics.
            sandbox_cfg: sandbox configuration containing ``managed_volumes``.

        Returns:
            Flat Docker ``-v`` arguments for validated named volumes.
        """
        args: List[str] = []
        rows = sandbox_cfg.get('managed_volumes', [])
        if not isinstance(rows, list):
            return args
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = str(row.get('name', '')).strip()
            guest_path = str(row.get('guest_path', '')).strip()
            if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or not guest_path.startswith('/'):
                logger.warning("Invalid managed volume for sandbox '%s'", sandbox_id)
                continue
            exists, _ = self._run_docker_cmd(['volume', 'inspect', name])
            if not exists:
                created, output = self._run_docker_cmd(['volume', 'create', name])
                if not created:
                    logger.warning("Could not create managed volume '%s' for sandbox '%s': %s", name, sandbox_id, output)
                    continue
            args.extend(['-v', f'{name}:{guest_path}:rw'])
        return args

    def _tmpfs_args(self, sandbox_cfg: Dict[str, Any]) -> List[str]:
        """Build Docker tmpfs arguments from absolute configured mount paths."""
        rows = sandbox_cfg.get('tmpfs', [])
        if not isinstance(rows, list):
            return []
        args: List[str] = []
        for path in rows:
            mount = str(path).strip()
            if not mount.startswith('/') or '\x00' in mount:
                logger.warning("Ignoring invalid tmpfs mount path: %r", path)
                continue
            args.extend(['--tmpfs', mount])
        return args

    def _shm_size_args(self, sandbox_cfg: Dict[str, Any]) -> List[str]:
        """Build a validated Docker shared-memory size argument."""
        size = str(sandbox_cfg.get('shm_size', '')).strip()
        if not size:
            return []
        if not re.fullmatch(r'\d+(?:[kKmMgG](?:[bB])?)?', size):
            logger.warning("Ignoring invalid shared-memory size: %r", size)
            return []
        return ['--shm-size', size]

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

    def _runtime_contract(self, sandbox_cfg: Dict[str, Any]) -> tuple[bool, Dict[str, Any], str]:
        """Return the validated optional runtime contract for one sandbox."""
        contract = sandbox_cfg.get('runtime_contract')
        if contract is None:
            return True, {}, ""
        if not isinstance(contract, dict):
            return False, {}, "runtime_contract must be an object"
        if contract.get('version') != 1:
            return False, {}, "runtime_contract.version must be 1"
        return True, contract, ""

    def _as_bool(self, value: Any) -> bool:
        """Convert supported configuration values to a boolean result."""
        return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

    def _runtime_environment_args(
        self, sandbox_cfg: Dict[str, Any]
    ) -> tuple[bool, List[str], List[str], List[Dict[str, Any]], str]:
        """Build runtime environment, host aliases, and active companion probes."""
        contract_ok, contract, contract_error = self._runtime_contract(sandbox_cfg)
        if not contract_ok:
            return False, [], [], [], contract_error

        environment = contract.get('environment', {})
        if not isinstance(environment, dict):
            return False, [], [], [], "runtime_contract.environment must be an object"
        values: Dict[str, str] = {}
        for name, value in environment.items():
            if not isinstance(name, str) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', name):
                return False, [], [], [], "runtime_contract.environment has an invalid variable name"
            values[name] = str(value)

        companions = contract.get('companion_services', [])
        if not isinstance(companions, list):
            return False, [], [], [], "runtime_contract.companion_services must be a list"

        guest_provides_postgres = self._as_bool(values.get('SNDBX_PROVIDES_POSTGRES', False))
        host_aliases: List[str] = []
        active_readiness: List[Dict[str, Any]] = []
        for index, companion in enumerate(companions):
            if not isinstance(companion, dict):
                return False, [], [], [], f"companion_services[{index}] must be an object"
            name = str(companion.get('name', '')).strip()
            host = str(companion.get('host', '')).strip()
            try:
                port = int(companion.get('port'))
            except (TypeError, ValueError):
                return False, [], [], [], f"companion_services[{index}] port must be an integer"
            inject = companion.get('inject', {})
            if not re.fullmatch(r'[a-z][a-z0-9_-]*', name) or not host or not (1 <= port <= 65535):
                return False, [], [], [], f"companion_services[{index}] has invalid name, host, or port"
            if name == 'postgres' and guest_provides_postgres:
                continue
            if not isinstance(inject, dict):
                return False, [], [], [], f"companion_services[{index}].inject must be an object"
            host_env = str(inject.get('host_env', '')).strip()
            port_env = str(inject.get('port_env', '')).strip()
            if not re.fullmatch(r'[A-Z_][A-Z0-9_]*', host_env) or not re.fullmatch(r'[A-Z_][A-Z0-9_]*', port_env):
                return False, [], [], [], f"companion_services[{index}].inject requires host_env and port_env"
            if host_env in values or port_env in values:
                return False, [], [], [], f"companion_services[{index}] conflicts with runtime environment"
            values[host_env] = host
            values[port_env] = str(port)
            if host == 'host.docker.internal' and host not in host_aliases:
                host_aliases.extend(['--add-host', 'host.docker.internal:host-gateway'])
            readiness = companion.get('readiness')
            if readiness is not None:
                if not isinstance(readiness, dict):
                    return False, [], [], [], f"companion_services[{index}].readiness must be an object"
                active_readiness.append(readiness)

        environment_args: List[str] = []
        for name, value in values.items():
            environment_args.extend(['-e', f'{name}={value}'])
        return True, environment_args, host_aliases, active_readiness, ""

    def _validate_readiness_probe(self, probe: Dict[str, Any]) -> tuple[bool, str]:
        """Validate the shape of one HTTP, TCP, or command readiness probe."""
        probe_type = str(probe.get('type', '')).strip().lower()
        if probe_type == 'http':
            if not str(probe.get('url', '')).startswith(('http://', 'https://')):
                return False, "HTTP readiness probe requires an http(s) URL"
        elif probe_type == 'tcp':
            try:
                port = int(probe.get('port'))
            except (TypeError, ValueError):
                return False, "TCP readiness probe requires a port"
            if not str(probe.get('host', '')).strip() or not (1 <= port <= 65535):
                return False, "TCP readiness probe has invalid host or port"
        elif probe_type == 'command':
            if not str(probe.get('command', '')).strip():
                return False, "command readiness probe requires a command"
        else:
            return False, "readiness probe type must be http, tcp, or command"
        return True, ""

    def _wait_for_readiness_probe(self, sandbox_id: str, probe: Dict[str, Any]) -> tuple[bool, str]:
        """Wait for one bounded readiness probe to succeed."""
        probe_ok, probe_error = self._validate_readiness_probe(probe)
        if not probe_ok:
            return False, probe_error
        attempts = probe.get('attempts', 1)
        interval_seconds = probe.get('interval_seconds', 1)
        try:
            attempts = max(1, int(attempts))
            interval_seconds = max(0, float(interval_seconds))
        except (TypeError, ValueError):
            return False, "readiness probe attempts and interval_seconds must be numeric"

        probe_type = str(probe['type']).lower()
        last_error = "readiness probe did not succeed"
        for attempt in range(attempts):
            try:
                if probe_type == 'http':
                    with urllib.request.urlopen(str(probe['url']), timeout=2):
                        return True, ""
                elif probe_type == 'tcp':
                    with socket.create_connection((str(probe['host']), int(probe['port'])), timeout=2):
                        return True, ""
                else:
                    ok, output = self._run_docker_cmd([
                        'exec', f'sndbx-{sandbox_id}', 'sh', '-c', str(probe['command'])
                    ], timeout=10)
                    if ok:
                        return True, ""
                    last_error = output.strip() or last_error
            except (OSError, urllib.error.URLError, ValueError) as exc:
                last_error = str(exc)
            if attempt + 1 < attempts:
                time.sleep(interval_seconds)
        return False, last_error

    def _validate_runtime_start(
        self, sandbox_id: str, sandbox_cfg: Dict[str, Any], hook_message: str
    ) -> tuple[bool, str]:
        """Verify hook capabilities and readiness for an optional runtime contract."""
        contract_ok, contract, contract_error = self._runtime_contract(sandbox_cfg)
        if not contract_ok:
            return False, contract_error
        if not contract:
            return True, ""

        environment = contract.get('environment', {})
        if contract.get('capability_hook', False):
            try:
                capabilities = json.loads(hook_message)
            except json.JSONDecodeError:
                return False, "runtime capability hook did not return JSON"
            expected = self._as_bool(environment.get('SNDBX_PROVIDES_POSTGRES', False))
            if not isinstance(capabilities, dict) or capabilities.get('provides_postgres') is not expected:
                return False, "runtime capability hook does not match SNDBX_PROVIDES_POSTGRES"

        environment_ok, _, _, companion_probes, environment_error = self._runtime_environment_args(sandbox_cfg)
        if not environment_ok:
            return False, environment_error
        probes = contract.get('readiness', [])
        if not isinstance(probes, list):
            return False, "runtime_contract.readiness must be a list"
        for probe in [*companion_probes, *probes]:
            if not isinstance(probe, dict):
                return False, "runtime readiness probes must be objects"
            ready, readiness_error = self._wait_for_readiness_probe(sandbox_id, probe)
            if not ready:
                return False, f"readiness failed: {readiness_error}"
        return True, ""

    def _run_image_hook(
        self,
        sandbox_id: str,
        sandbox_cfg: Dict[str, Any],
        hook_name: str = 'on_system_start',
        resolved_mounts: Optional[List[Dict[str, Any]]] = None,
    ) -> tuple[bool, str]:
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
            'image_ref': image_ref,
            'resolved_mounts': resolved_mounts or [],
            'ssh_keys': list(dict.fromkeys(
                key.strip()
                for key in sandbox_cfg.get('ssh_keys', [])
                if isinstance(key, str) and key.strip()
            )),
        }
        ctx_json = json.dumps(ctx, ensure_ascii=True)

        runtime_ok, runtime_environment_args, _, _, runtime_error = self._runtime_environment_args(sandbox_cfg)
        if not runtime_ok:
            return False, runtime_error

        ok, out = self._run_docker_cmd([
            'exec',
            '--user', 'root',
            *runtime_environment_args,
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
        mounts_ok, shared_mount_args, resolved_mounts, mount_error = self._preflight_shared_mounts(
            sandbox_id, sandbox_cfg
        )
        if not mounts_ok:
            logger.error("Mount preflight failed for sandbox '%s': %s", sandbox_id, mount_error)
            return False, mount_error
        managed_volume_args = self._managed_volume_args(sandbox_id, sandbox_cfg)
        tmpfs_args = self._tmpfs_args(sandbox_cfg)
        shm_size_args = self._shm_size_args(sandbox_cfg)
        port_binding_args = self._port_binding_args(sandbox_id, sandbox_cfg)
        runtime_ok, runtime_environment_args, runtime_host_args, _, runtime_error = self._runtime_environment_args(
            sandbox_cfg
        )
        if not runtime_ok:
            logger.error("Runtime contract failed for sandbox '%s': %s", sandbox_id, runtime_error)
            return False, runtime_error
        runtime_command = [] if sandbox_cfg.get('runtime_contract') else ['sleep', 'infinity']
        rootfs_args = ['--read-only'] if self._as_bool(sandbox_cfg.get('read_only_rootfs', False)) else []

        base_cmd = [
            'run',
            '--name', f'sndbx-{sandbox_id}',
            '--runtime', 'kata',
            '-m', memory,
            '--cpus', str(cpus),
            '--detach',
            *rootfs_args,
            *apt_tmpfs_args,
            *tmpfs_args,
            *shm_size_args,
            *shared_mount_args,
            *managed_volume_args,
            *port_binding_args,
            *runtime_host_args,
            *runtime_environment_args,
            image,
            *runtime_command,
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
                *rootfs_args,
                *apt_tmpfs_args,
                *tmpfs_args,
                *shm_size_args,
                *shared_mount_args,
                *managed_volume_args,
                *port_binding_args,
                *runtime_host_args,
                *runtime_environment_args,
                image,
                *runtime_command,
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
            if sandbox_cfg.get('configure_apt_mirror', True):
                mirror_ok, mirror_msg = self.configure_apt_mirror(sandbox_id)
                if not mirror_ok:
                    logger.warning("apt mirror config failed for sandbox '%s': %s", sandbox_id, mirror_msg)

            hook_ok, hook_msg = self._run_image_hook(
                sandbox_id, sandbox_cfg, hook_name='on_system_start', resolved_mounts=resolved_mounts
            )
            if not hook_ok:
                if sandbox_cfg.get('runtime_contract'):
                    self._run_docker_cmd(['rm', '-f', f'sndbx-{sandbox_id}'])
                    return False, f"image hook failed: {hook_msg}"
                logger.warning("image hook failed for sandbox '%s': %s", sandbox_id, hook_msg)
            runtime_ok, runtime_error = self._validate_runtime_start(sandbox_id, sandbox_cfg, hook_msg)
            if not runtime_ok:
                self._run_docker_cmd(['rm', '-f', f'sndbx-{sandbox_id}'])
                return False, runtime_error

        return success, output

    def start_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Start a sandbox container, creating it when no container exists."""
        status = self.get_status(sandbox_id)
        if status.running:
            logger.info("Sandbox '%s' is already running", sandbox_id)
            return True, "already running"
        success, output = self._run_docker_cmd(['start', f'sndbx-{sandbox_id}'])
        if not success and 'No such container' in output:
            logger.info("Sandbox '%s' is absent; creating it before start", sandbox_id)
            return self.create_sandbox(sandbox_id)
        if success:
            logger.info(f"Started sandbox {sandbox_id}")
            sandbox_cfg = self.sandbox_configs.get(sandbox_id, {})
            hook_ok, hook_msg = self._run_image_hook(sandbox_id, sandbox_cfg, hook_name='on_system_start')
            if not hook_ok:
                if sandbox_cfg.get('runtime_contract'):
                    self._run_docker_cmd(['stop', f'sndbx-{sandbox_id}'])
                    return False, f"image hook failed: {hook_msg}"
                logger.warning("image hook failed on start for sandbox '%s': %s", sandbox_id, hook_msg)
            runtime_ok, runtime_error = self._validate_runtime_start(sandbox_id, sandbox_cfg, hook_msg)
            if not runtime_ok:
                self._run_docker_cmd(['stop', f'sndbx-{sandbox_id}'])
                return False, runtime_error
        return success, output

    def stop_sandbox(self, sandbox_id: str) -> tuple[bool, str]:
        """Stop a running sandbox container"""
        success, output = self._run_docker_cmd(['stop', f'sndbx-{sandbox_id}'])
        if success:
            logger.info(f"Stopped sandbox {sandbox_id}")
        return success, output

    def execute_command(self, sandbox_id: str, command: str) -> tuple[bool, str]:
        """Execute a command inside the sandbox"""
        logger.info("Executing command in sandbox '%s': %s", sandbox_id, command)
        success, output = self._run_docker_cmd([
            'exec',
            f'sndbx-{sandbox_id}',
            'bash', '-c', command
        ])
        logger.info("Command finished in sandbox '%s': success=%s", sandbox_id, success)
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
