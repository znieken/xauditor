"""Network bootstrap behavior in ``ContainerLifecycleManager``.

Phase: ``move-network-to-bootstrap`` change. The lifecycle manager is
responsible for ensuring its configured docker network exists before
``docker create`` runs, appending ``--network <name>`` to the create
argv, and re-attaching an already-existing container that drifted off
the network (the upgrade path from xauditor 0.5.1).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from xauditor.config import ReportDBConfig
from xauditor.errors import ReportDBError
from xauditor.integrations.docker import CommandResult
from xauditor.integrations.reportdb import PostgresContainerManager


class _ScriptedRunner:
    def __init__(self, responses):
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args, *, timeout=None):
        key = tuple(args)
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"Unexpected docker command: {key}")
        return self.responses[key]


def _ok() -> CommandResult:
    return CommandResult(returncode=0, stdout="", stderr="")


def _err(stderr: str = "boom") -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr=stderr)


def _result(stdout: str) -> CommandResult:
    return CommandResult(returncode=0, stdout=stdout, stderr="")


class NetworkBootstrapTests(unittest.TestCase):
    def _probe_ok(self) -> None:
        return None

    def test_init_ensures_network_when_missing_and_passes_network_to_create(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "network", "inspect", config.network_name): _err(
                    "no network"
                ),
                (
                    "docker",
                    "network",
                    "create",
                    "--label",
                    "com.xauditor.managed=true",
                    config.network_name,
                ): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _err(
                    "missing"
                ),
                (
                    "docker",
                    "create",
                    "--name",
                    config.container_name,
                    "--label",
                    "com.xauditor.managed=true",
                    "--network",
                    config.network_name,
                    "-e",
                    f"POSTGRES_USER={config.username}",
                    "-e",
                    f"POSTGRES_PASSWORD={config.password}",
                    "-e",
                    f"POSTGRES_DB={config.database}",
                    "-p",
                    f"{config.port}:5432",
                    "-v",
                    f"{config.volume_name}:/var/lib/postgresql/data",
                    config.image,
                ): _ok(),
                ("docker", "start", config.container_name): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config, command_runner=runner, readiness_probe=self._probe_ok
        )
        manager.init_runtime()
        # Network create SHALL fire BEFORE container create.
        cmds = runner.calls
        net_create_idx = cmds.index(
            (
                "docker",
                "network",
                "create",
                "--label",
                "com.xauditor.managed=true",
                config.network_name,
            )
        )
        container_create_idx = next(
            i for i, c in enumerate(cmds) if c[:2] == ("docker", "create")
        )
        self.assertLess(net_create_idx, container_create_idx)

    def test_init_skips_network_create_when_inspect_succeeds(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "network", "inspect", config.network_name): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _err(
                    "missing"
                ),
                (
                    "docker",
                    "create",
                    "--name",
                    config.container_name,
                    "--label",
                    "com.xauditor.managed=true",
                    "--network",
                    config.network_name,
                    "-e",
                    f"POSTGRES_USER={config.username}",
                    "-e",
                    f"POSTGRES_PASSWORD={config.password}",
                    "-e",
                    f"POSTGRES_DB={config.database}",
                    "-p",
                    f"{config.port}:5432",
                    "-v",
                    f"{config.volume_name}:/var/lib/postgresql/data",
                    config.image,
                ): _ok(),
                ("docker", "start", config.container_name): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config, command_runner=runner, readiness_probe=self._probe_ok
        )
        manager.init_runtime()
        # ``docker network create`` SHALL NOT be invoked when the
        # network already exists.
        for cmd in runner.calls:
            self.assertNotEqual(cmd[:3], ("docker", "network", "create"))


class UpgradeReattachTests(unittest.TestCase):
    """Existing 0.5.1 container on bridge gets re-attached at next init."""

    def _probe_ok(self) -> None:
        return None

    def test_existing_container_off_network_gets_attached(self) -> None:
        config = ReportDBConfig()
        # The container exists, network exists, but inspect shows the
        # container is only on ``bridge`` — not the configured network.
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "network", "inspect", config.network_name): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _ok(),
                (
                    "docker",
                    "inspect",
                    "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                    config.container_name,
                ): _result("bridge "),
                (
                    "docker",
                    "network",
                    "connect",
                    config.network_name,
                    config.container_name,
                ): _ok(),
                ("docker", "start", config.container_name): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config, command_runner=runner, readiness_probe=self._probe_ok
        )
        manager.init_runtime()
        # ``docker network connect`` SHALL fire exactly once.
        connects = [
            cmd
            for cmd in runner.calls
            if cmd[:3] == ("docker", "network", "connect")
        ]
        self.assertEqual(len(connects), 1)

    def test_existing_container_already_on_network_skips_attach(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "network", "inspect", config.network_name): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _ok(),
                (
                    "docker",
                    "inspect",
                    "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                    config.container_name,
                ): _result(f"bridge {config.network_name} "),
                ("docker", "start", config.container_name): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config, command_runner=runner, readiness_probe=self._probe_ok
        )
        manager.init_runtime()
        for cmd in runner.calls:
            self.assertNotEqual(cmd[:3], ("docker", "network", "connect"))

    def test_attach_failure_is_hard_error_not_swallowed(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "network", "inspect", config.network_name): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _ok(),
                (
                    "docker",
                    "inspect",
                    "-f",
                    "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}",
                    config.container_name,
                ): _result("bridge "),
                (
                    "docker",
                    "network",
                    "connect",
                    config.network_name,
                    config.container_name,
                ): _err("network not found"),
            }
        )
        manager = PostgresContainerManager(
            config, command_runner=runner, readiness_probe=self._probe_ok
        )
        with self.assertRaises(ReportDBError) as ctx:
            manager.init_runtime()
        self.assertIn(config.network_name, str(ctx.exception))
        self.assertIn(config.container_name, str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
