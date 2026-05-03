from __future__ import annotations

from typing import Callable

from xauditor.errors import XAuditorError
from xauditor.integrations.docker import (
    CommandResult,
    MANAGED_LABEL,
    default_command_runner,
)


class NetworkManager:
    """Manage a single named docker bridge network shared by the portal containers."""

    def __init__(
        self,
        network_name: str,
        *,
        error_cls: type[XAuditorError],
        command_runner: Callable[..., CommandResult] | None = None,
    ) -> None:
        self.network_name = network_name
        self.error_cls = error_cls
        self.command_runner = command_runner or default_command_runner

    def exists(self) -> bool:
        return (
            self.command_runner(
                ["docker", "network", "inspect", self.network_name]
            ).returncode
            == 0
        )

    def ensure(self) -> bool:
        """Create the network if it does not exist. Returns True if created, False if reused."""
        if self.exists():
            return False
        result = self.command_runner(
            [
                "docker",
                "network",
                "create",
                "--label",
                MANAGED_LABEL,
                self.network_name,
            ]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown docker error"
            raise self.error_cls(
                f"Failed to create managed docker network `{self.network_name}`: {detail}"
            )
        return True

    def remove(self) -> bool:
        """Remove the network if it exists. Returns True if removed, False if it did not exist."""
        if not self.exists():
            return False
        result = self.command_runner(
            ["docker", "network", "rm", self.network_name]
        )
        if result.returncode != 0:
            detail = result.stderr.strip() or result.stdout.strip() or "unknown docker error"
            raise self.error_cls(
                f"Failed to remove managed docker network `{self.network_name}`: {detail}"
            )
        return True
