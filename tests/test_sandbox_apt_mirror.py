"""Tests for per-sandbox APT mirror behavior."""

import unittest
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


if __name__ == "__main__":
    unittest.main()