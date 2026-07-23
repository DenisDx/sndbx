"""Tests for per-sandbox APT mirror behavior."""

import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from sandbox import DockerSandboxManager


class AptMirrorConfigurationTests(unittest.TestCase):
    """Verify APT mirror configuration during sandbox creation."""

    def _create_manager(self, sandbox_config: dict) -> DockerSandboxManager:
        """Return a manager with Docker operations replaced by mocks."""
        manager = DockerSandboxManager({
            "root": ".",
            "sandboxes": {"items": {"test": sandbox_config}},
        })
        manager._ensure_image_ready = Mock(return_value=(True, "ready"))
        manager._run_docker_cmd = Mock(return_value=(True, "created"))
        manager._shared_mount_args = Mock(return_value=[])
        manager._port_binding_args = Mock(return_value=[])
        manager.configure_apt_mirror = Mock(return_value=(True, "configured"))
        manager._run_image_hook = Mock(return_value=(True, "no hook"))
        return manager

    def test_mirror_configuration_remains_enabled_by_default(self) -> None:
        """Keep existing mirror behavior when the option is absent."""
        manager = self._create_manager({"image": "test-image"})

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        manager.configure_apt_mirror.assert_called_once_with("test")

    def test_mirror_configuration_can_be_disabled(self) -> None:
        """Preserve image-native package sources when explicitly disabled."""
        manager = self._create_manager({
            "image": "test-image",
            "configure_apt_mirror": False,
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        manager.configure_apt_mirror.assert_not_called()

    def test_runtime_contract_uses_the_image_default_command(self) -> None:
        """Allow a declared runtime image to run its entrypoint."""
        manager = self._create_manager({
            "image": "test-image",
            "runtime_contract": {"version": 1},
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertNotIn("sleep", create_command)

    def test_start_recreates_a_missing_sandbox_container(self) -> None:
        """Recover a persistent sandbox whose failed container was removed."""
        manager = self._create_manager({"image": "test-image"})
        manager._run_docker_cmd.return_value = (False, "No such container: sndbx-test")
        manager.create_sandbox = Mock(return_value=(True, "created"))

        success, output = manager.start_sandbox("test")

        self.assertTrue(success)
        self.assertEqual(output, "created")
        manager.create_sandbox.assert_called_once_with("test")

    def test_start_accepts_an_already_running_sandbox(self) -> None:
        """Treat a running persistent VM as a successful idempotent start."""
        manager = self._create_manager({"image": "test-image"})
        manager.get_status = Mock(return_value=SimpleNamespace(running=True))

        success, output = manager.start_sandbox("test")

        self.assertTrue(success)
        self.assertEqual(output, "already running")
        manager._run_docker_cmd.assert_not_called()

    def test_managed_volume_never_uses_a_host_path(self) -> None:
        """Mount runtime storage as a Docker volume rather than a host bind."""
        manager = self._create_manager({"image": "test-image"})

        args = manager._managed_volume_args(
            "test", {"managed_volumes": [{"name": "langvm-runtime", "guest_path": "/var/lib/langvm"}]}
        )

        self.assertEqual(args, ["-v", "langvm-runtime:/var/lib/langvm:rw"])
        manager._run_docker_cmd.assert_called_once_with(["volume", "inspect", "langvm-runtime"])

    def test_missing_optional_file_is_omitted_without_creation(self) -> None:
        """Omit a missing optional file instead of creating an empty mount source."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = DockerSandboxManager({"root": str(root), "sandboxes": {"items": {}}})
            optional_file = root / ".env"

            ok, args, resolved, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(optional_file),
                    "guest_path": "/opt/app/.env",
                    "source_type": "file",
                    "permission": "ro",
                    "required": False,
                }],
            })

            self.assertTrue(ok, error)
            self.assertEqual(args, [])
            self.assertEqual(resolved, [])
            self.assertFalse(optional_file.exists())

    def test_missing_required_mount_blocks_creation(self) -> None:
        """Reject a required source before Docker container creation."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = DockerSandboxManager({"root": str(root), "sandboxes": {"items": {}}})

            ok, _, _, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(root / "missing-config.json5"),
                    "guest_path": "/opt/app/config.json5",
                    "source_type": "file",
                    "permission": "ro",
                }],
            })

            self.assertFalse(ok)
            self.assertIn("required mount source is missing", error)

    def test_writable_directory_can_be_created_explicitly(self) -> None:
        """Create only an explicitly declared writable directory source."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = DockerSandboxManager({"root": str(root), "sandboxes": {"items": {}}})
            data_directory = root / "data"

            ok, args, resolved, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(data_directory),
                    "guest_path": "/var/lib/app",
                    "source_type": "directory",
                    "permission": "rw",
                    "create_if_missing": True,
                }],
            })

            self.assertTrue(ok, error)
            self.assertTrue(data_directory.is_dir())
            self.assertEqual(args, ["-v", f"{data_directory}:/var/lib/app:rw"])
            self.assertEqual(resolved[0]["guest_path"], "/var/lib/app")

    def test_mount_type_mismatch_fails_without_replacing_source(self) -> None:
        """Fail closed when a mount source has the wrong configured type."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            manager = DockerSandboxManager({"root": str(root), "sandboxes": {"items": {}}})
            file_source = root / "data"
            file_source.write_text("not a directory", encoding="utf-8")

            ok, _, _, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(file_source),
                    "guest_path": "/var/lib/app",
                    "source_type": "directory",
                    "permission": "rw",
                }],
            })

            self.assertFalse(ok)
            self.assertIn("type mismatch", error)
            self.assertTrue(file_source.is_file())

    def test_runtime_contract_injects_companion_environment(self) -> None:
        """Inject a declared companion endpoint without gateway discovery."""
        manager = DockerSandboxManager({"root": ".", "sandboxes": {"items": {}}})
        contract = {
            "runtime_contract": {
                "version": 1,
                "environment": {"SNDBX_PROVIDES_POSTGRES": "false"},
                "companion_services": [{
                    "name": "postgres",
                    "host": "host.docker.internal",
                    "port": 5432,
                    "inject": {"host_env": "DATABASE_HOST", "port_env": "DATABASE_PORT"},
                    "readiness": {"type": "tcp", "host": "127.0.0.1", "port": 5432},
                }],
            },
        }

        ok, environment_args, host_args, probes, error = manager._runtime_environment_args(contract)

        self.assertTrue(ok, error)
        self.assertEqual(host_args, ["--add-host", "host.docker.internal:host-gateway"])
        self.assertIn("SNDBX_PROVIDES_POSTGRES=false", environment_args)
        self.assertIn("DATABASE_HOST=host.docker.internal", environment_args)
        self.assertIn("DATABASE_PORT=5432", environment_args)
        self.assertEqual(probes, [{"type": "tcp", "host": "127.0.0.1", "port": 5432}])

    def test_guest_owned_postgres_suppresses_companion_injection(self) -> None:
        """Do not inject an external PostgreSQL endpoint when the guest owns it."""
        manager = DockerSandboxManager({"root": ".", "sandboxes": {"items": {}}})
        contract = {
            "runtime_contract": {
                "version": 1,
                "environment": {"SNDBX_PROVIDES_POSTGRES": "true"},
                "companion_services": [{
                    "name": "postgres",
                    "host": "host.docker.internal",
                    "port": 5432,
                    "inject": {"host_env": "DATABASE_HOST", "port_env": "DATABASE_PORT"},
                }],
            },
        }

        ok, environment_args, host_args, probes, error = manager._runtime_environment_args(contract)

        self.assertTrue(ok, error)
        self.assertEqual(environment_args, ["-e", "SNDBX_PROVIDES_POSTGRES=true"])
        self.assertEqual(host_args, [])
        self.assertEqual(probes, [])

    def test_runtime_contract_requires_matching_capability_and_readiness(self) -> None:
        """Fail closed when the declared capability hook does not match the contract."""
        manager = DockerSandboxManager({"root": ".", "sandboxes": {"items": {}}})
        contract = {
            "runtime_contract": {
                "version": 1,
                "environment": {"SNDBX_PROVIDES_POSTGRES": "true"},
                "capability_hook": True,
                "readiness": [{"type": "command", "command": "true"}],
            },
        }

        ok, error = manager._validate_runtime_start("test", contract, '{"provides_postgres": false}')

        self.assertFalse(ok)
        self.assertIn("does not match", error)

    def test_capability_hook_receives_runtime_contract_environment(self) -> None:
        """Expose declared capabilities to the image hook environment."""
        with tempfile.TemporaryDirectory() as temporary:
            manager = DockerSandboxManager({"root": temporary, "sandboxes": {"items": {}}})
            manager._local_image_id_for_ref = Mock(return_value="langvm")
            manager.images_dir = Path(temporary)
            image_dir = manager.images_dir / "langvm"
            image_dir.mkdir()
            (image_dir / "app.py").touch()
            manager._run_docker_cmd = Mock(return_value=(True, '{"provides_postgres": true}'))
            config = {
                "image": "langvm:latest",
                "runtime_contract": {
                    "version": 1,
                    "environment": {"SNDBX_PROVIDES_POSTGRES": "true"},
                },
            }

            success, _ = manager._run_image_hook("test", config)

        self.assertTrue(success)
        command = manager._run_docker_cmd.call_args.args[0]
        self.assertIn("SNDBX_PROVIDES_POSTGRES=true", command)


if __name__ == "__main__":
    unittest.main()