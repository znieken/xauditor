from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "src"))

from xauditor.config import Neo4jConfig, RemoteConnectionConfig, ReportDBConfig
from xauditor.errors import ReportDBError
from xauditor.integrations.docker import (
    CommandResult,
    DockerManager,
)
from xauditor.integrations.reportdb import PostgresContainerManager, postgres_spec


class _ScriptedRunner:
    """Records invocations and returns a scripted reply per command."""

    def __init__(self, responses: dict[tuple[str, ...], CommandResult]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, ...]] = []

    def __call__(self, args: list[str], *, timeout: int | None = None) -> CommandResult:
        key = tuple(args)
        self.calls.append(key)
        if key not in self.responses:
            raise AssertionError(f"Unexpected docker command: {key}")
        return self.responses[key]


def _ok() -> CommandResult:
    return CommandResult(returncode=0, stdout="", stderr="")


def _err(stderr: str = "boom") -> CommandResult:
    return CommandResult(returncode=1, stdout="", stderr=stderr)


class PostgresContainerManagerLifecycleTests(unittest.TestCase):
    def _probe_always_ok(self) -> None:
        return None

    def test_init_pulls_creates_and_starts_when_nothing_exists(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                # Network bootstrap (xauditor 0.5.2): the lifecycle
                # manager ensures the docker network exists before
                # ``docker create`` runs the container. ``inspect``
                # returns missing → manager creates the network with
                # the managed label.
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
                ("docker", "image", "inspect", config.image): _err("no image"),
                ("docker", "pull", config.image): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _err("no volume"),
                ("docker", "volume", "create", config.volume_name): _ok(),
                ("docker", "container", "inspect", config.container_name): _err(
                    "no container"
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
            config,
            command_runner=runner,
            readiness_probe=self._probe_always_ok,
        )
        result = manager.init_runtime()
        self.assertEqual(result, "reportdb ready")

    def test_start_requires_init(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "container", "inspect", config.container_name): _err(
                    "no container"
                ),
            }
        )
        manager = PostgresContainerManager(
            config,
            command_runner=runner,
            readiness_probe=self._probe_always_ok,
        )
        with self.assertRaises(ReportDBError) as ctx:
            manager.start_runtime()
        self.assertIn("xauditor reportdb init", str(ctx.exception))

    def test_start_existing_stopped_container(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "container", "inspect", config.container_name): _ok(),
                (
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    config.container_name,
                ): CommandResult(returncode=0, stdout="false\n", stderr=""),
                ("docker", "start", config.container_name): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config,
            command_runner=runner,
            readiness_probe=self._probe_always_ok,
        )
        self.assertEqual(
            manager.start_runtime(), "Managed PostgreSQL container started"
        )

    def test_stop_noop_when_no_container(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "container", "inspect", config.container_name): _err(
                    "no container"
                ),
            }
        )
        manager = PostgresContainerManager(
            config,
            command_runner=runner,
            readiness_probe=self._probe_always_ok,
        )
        self.assertEqual(manager.stop_runtime(), "No managed container was running")

    def test_reset_requires_confirmation(self) -> None:
        config = ReportDBConfig()
        manager = PostgresContainerManager(
            config,
            command_runner=lambda args, **_: _ok(),
            readiness_probe=self._probe_always_ok,
        )
        with self.assertRaises(ReportDBError) as ctx:
            manager.reset_runtime(confirmed=False)
        self.assertIn("--yes", str(ctx.exception))

    def test_reset_removes_container_volume_and_image(self) -> None:
        config = ReportDBConfig()
        runner = _ScriptedRunner(
            {
                ("docker", "version"): _ok(),
                ("docker", "container", "inspect", config.container_name): _ok(),
                (
                    "docker",
                    "inspect",
                    "-f",
                    "{{.State.Running}}",
                    config.container_name,
                ): CommandResult(returncode=0, stdout="true\n", stderr=""),
                ("docker", "stop", config.container_name): _ok(),
                ("docker", "rm", config.container_name): _ok(),
                ("docker", "volume", "inspect", config.volume_name): _ok(),
                ("docker", "volume", "rm", config.volume_name): _ok(),
                ("docker", "image", "inspect", config.image): _ok(),
                ("docker", "image", "rm", config.image): _ok(),
            }
        )
        manager = PostgresContainerManager(
            config,
            command_runner=runner,
            readiness_probe=self._probe_always_ok,
        )
        deleted = manager.reset_runtime(confirmed=True)
        self.assertEqual(
            deleted,
            [config.container_name, config.volume_name, config.image],
        )


class PostgresSpecTests(unittest.TestCase):
    def test_remote_config_sets_remote_configured_flag(self) -> None:
        remote = RemoteConnectionConfig(url="postgresql://user:pass@h:5432/db")
        config = ReportDBConfig(remote=remote)
        spec = postgres_spec(config)
        self.assertTrue(spec.remote_configured)

    def test_postgres_spec_uses_correct_mount_path_and_port(self) -> None:
        config = ReportDBConfig(port=55432)
        spec = postgres_spec(config)
        self.assertEqual(spec.volume_mount_path, "/var/lib/postgresql/data")
        self.assertEqual(len(spec.ports), 1)
        self.assertEqual(spec.ports[0].host, 55432)
        self.assertEqual(spec.ports[0].container, 5432)


class RemoteBypassTests(unittest.TestCase):
    def _runner_that_fails(self, args: list[str], **_: object) -> CommandResult:
        raise AssertionError(
            f"Unexpected docker invocation in remote-bypass mode: {args}"
        )

    def test_postgres_remote_bypass_all_verbs(self) -> None:
        config = ReportDBConfig(
            remote=RemoteConnectionConfig(url="postgresql://u:p@h:5432/db")
        )
        manager = PostgresContainerManager(
            config,
            command_runner=self._runner_that_fails,
            readiness_probe=lambda: None,
        )
        message = manager.init_runtime()
        self.assertIn("remote endpoint is configured", message)
        self.assertEqual(manager.init_runtime(), manager.start_runtime())
        self.assertEqual(manager.stop_runtime(), manager.init_runtime())
        # reset is a no-op returning an empty deletion list
        self.assertEqual(manager.reset_runtime(confirmed=False), [])
        self.assertEqual(manager.reset_runtime(confirmed=True), [])

    def test_neo4j_remote_bypass_via_docker_manager(self) -> None:
        config = Neo4jConfig(remote=RemoteConnectionConfig(url="bolt://h:7687"))
        manager = DockerManager(
            config,
            command_runner=self._runner_that_fails,
            readiness_probe=lambda: None,
        )
        init_msg = manager.init_runtime()
        self.assertIn("remote endpoint is configured", init_msg)
        self.assertEqual(manager.start_runtime(), init_msg)
        self.assertEqual(manager.stop_runtime(), init_msg)
        self.assertEqual(manager.reset_runtime(confirmed=True), [])


if __name__ == "__main__":
    unittest.main()
