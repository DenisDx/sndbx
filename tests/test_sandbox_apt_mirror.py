"""Tests for per-sandbox APT mirror behavior."""

import json
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

    def test_failed_host_preparation_prevents_sandbox_creation(self) -> None:
        """Stop before image preparation when required host ACL setup fails."""
        manager = self._create_manager({
            "image": "test-image",
            "host_prepare_script": "prepare_host_permissions.sh",
        })
        manager._run_host_prepare_script = Mock(return_value=(False, "sudo denied"))

        success, output = manager.create_sandbox("test")

        self.assertFalse(success)
        self.assertEqual(output, "host preparation failed: sudo denied")
        manager._ensure_image_ready.assert_not_called()

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

    def test_read_only_rootfs_adds_docker_read_only_flag(self) -> None:
        """Apply the configured read-only root filesystem contract."""
        manager = self._create_manager({
            "image": "test-image",
            "read_only_rootfs": True,
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertIn("--read-only", create_command)

    def test_declared_tmpfs_paths_are_added_to_docker_command(self) -> None:
        """Mount only configured absolute tmpfs directories."""
        manager = self._create_manager({
            "image": "test-image",
            "tmpfs": ["/tmp:rw,noexec,nosuid,size=64m", "/run:rw,noexec,nosuid,size=16m"],
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertIn("/tmp:rw,noexec,nosuid,size=64m", create_command)
        self.assertIn("/run:rw,noexec,nosuid,size=16m", create_command)

    def test_declared_shm_size_is_added_to_docker_command(self) -> None:
        """Apply a validated shared-memory size for browser workloads."""
        manager = self._create_manager({
            "image": "test-image",
            "shm_size": "1g",
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertEqual(create_command[create_command.index("--shm-size") + 1], "1g")

    def test_posix_acl_annotation_is_limited_to_kata_virtiofs(self) -> None:
        """Pass only the supported POSIX ACL VirtioFS annotation to Kata."""
        manager = self._create_manager({
            "image": "test-image",
            "kata_annotations": {
                "io.katacontainers.config.hypervisor.virtio_fs_extra_args": [
                    "--thread-pool-size=1",
                    "--announce-submounts",
                    "--posix-acl",
                ],
            },
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        annotation_index = create_command.index("--annotation")
        self.assertEqual(
            create_command[annotation_index + 1],
            "io.katacontainers.config.hypervisor.virtio_fs_extra_args="
            "[\"--thread-pool-size=1\", \"--announce-submounts\", \"--posix-acl\"]",
        )

    def test_acl_compatibility_profile_uses_runtime_rs_annotation_format(self) -> None:
        """Pass the fixed CSV annotation required by the Kata 4.1 runtime-rs shim."""
        manager = self._create_manager({
            "image": "test-image",
            "virtiofs_profile": "posix-acl-compat",
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        annotation_index = create_command.index("--annotation")
        self.assertEqual(
            create_command[annotation_index + 1],
            "io.katacontainers.config.hypervisor.virtio_fs_extra_args="
            "--thread-pool-size=1,--no-announce-submounts,--posix-acl=always",
        )

    def test_unknown_virtiofs_profile_blocks_sandbox_creation(self) -> None:
        """Reject an unreviewed VirtioFS profile before Docker creation."""
        manager = self._create_manager({
            "image": "test-image",
            "virtiofs_profile": "untrusted-profile",
        })

        success, output = manager.create_sandbox("test")

        self.assertFalse(success)
        self.assertIn("unsupported virtiofs_profile", output)
        manager._run_docker_cmd.assert_not_called()

    def test_acl_compatibility_profile_rejects_nested_mounts(self) -> None:
        """Reject nested host mount points hidden by the ACL compatibility profile."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            source = Path(temporary_directory) / "source"
            source.mkdir()
            manager = DockerSandboxManager({"root": temporary_directory, "sandboxes": {"items": {}}})
            manager._nested_mount_points = Mock(return_value=[source / "nested-mount"])

            ok, _, _, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(source),
                    "guest_path": "/mnt/source",
                    "source_type": "directory",
                    "permission": "ro",
                }],
            }, reject_nested_mounts=True)

        self.assertFalse(ok)
        self.assertIn("does not support nested mount source", error)

    def test_kata_acl_runtime_is_selected_only_when_configured(self) -> None:
        """Use the isolated Kata ACL runtime only for an opted-in sandbox."""
        manager = self._create_manager({
            "image": "test-image",
            "kata_runtime": "kata-acl",
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertEqual(create_command[create_command.index("--runtime") + 1], "kata-acl")

    def test_unsupported_kata_runtime_falls_back_to_default(self) -> None:
        """Keep an invalid Kata runtime from reaching Docker."""
        manager = self._create_manager({
            "image": "test-image",
            "kata_runtime": "untrusted-runtime",
        })

        success, _ = manager.create_sandbox("test")

        self.assertTrue(success)
        create_command = manager._run_docker_cmd.call_args_list[0].args[0]
        self.assertEqual(create_command[create_command.index("--runtime") + 1], "kata")

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

    def test_image_hook_receives_normalized_ssh_keys_as_root(self) -> None:
        """Pass public key configuration to an image hook without full sandbox config."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            app_path = root / "images" / "test-image" / "app.py"
            app_path.parent.mkdir(parents=True)
            app_path.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
            sandbox_config = {
                "image": "test-image:latest",
                "ssh_keys": [" ssh-ed25519 key-one ", "", "ssh-ed25519 key-one", 42],
            }
            manager = DockerSandboxManager({
                "root": str(root),
                "sandboxes": {"items": {"test": sandbox_config}},
            })
            manager._runtime_environment_args = Mock(return_value=(True, [], [], [], ""))
            manager._run_docker_cmd = Mock(return_value=(True, "hook completed"))

            success, _ = manager._run_image_hook("test", sandbox_config)

            self.assertTrue(success)
            command = manager._run_docker_cmd.call_args.args[0]
            self.assertEqual(command[:3], ["exec", "--user", "0"])
            context_arg = next(item for item in command if item.startswith("SNDBX_CONTEXT_JSON="))
            context = json.loads(context_arg.removeprefix("SNDBX_CONTEXT_JSON="))
            self.assertEqual(context["ssh_keys"], ["ssh-ed25519 key-one"])

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

    def test_legacy_mount_without_source_type_defaults_to_directory(self) -> None:
        """Keep existing directory shares valid when source_type is absent."""
        with tempfile.TemporaryDirectory() as temporary_directory:
            root = Path(temporary_directory)
            source = root / "shared"
            source.mkdir()
            manager = DockerSandboxManager({"root": str(root), "sandboxes": {"items": {}}})

            ok, args, _, error = manager._preflight_shared_mounts("test", {
                "shared_directories": [{
                    "host_path": str(source),
                    "guest_path": "/mnt/shared",
                    "permission": "rw",
                }],
            })

        self.assertTrue(ok, error)
        self.assertEqual(args, ["-v", f"{source}:/mnt/shared:rw"])

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

    def test_local_image_id_maps_a_docker_tag_to_its_source_directory(self) -> None:
        """Use local_image_id when a Docker tag differs from the image folder."""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / "images" / "denis_obsidian_assistant").mkdir(parents=True)
            manager = DockerSandboxManager({
                "root": str(root),
                "sandboxes": {"items": {
                    "assistant": {
                        "image": "denis-obsidian-assistant:latest",
                        "local_image_id": "denis_obsidian_assistant",
                    },
                }},
            })

            local_image_id = manager._local_image_id_for_ref("denis-obsidian-assistant:latest")

        self.assertEqual(local_image_id, "denis_obsidian_assistant")


if __name__ == "__main__":
    unittest.main()