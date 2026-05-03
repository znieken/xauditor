from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import Neo4jConfig
from xauditor.errors import GraphdbError
from xauditor.integrations.docker import CommandResult, DockerManager


class DockerIntegrationTests(unittest.TestCase):
    def test_wait_ready_retries_until_readiness_probe_succeeds(self) -> None:
        probe_calls = {"count": 0}

        def probe() -> None:
            probe_calls["count"] += 1
            if probe_calls["count"] < 2:
                raise GraphdbError("not ready yet")

        manager = DockerManager(Neo4jConfig(), readiness_probe=probe)

        with patch("xauditor.integrations.docker.time.sleep", return_value=None):
            manager.wait_ready(timeout_seconds=2)

        self.assertEqual(probe_calls["count"], 2)

    def test_start_runtime_uses_configured_ready_timeout_for_slow_startups(self) -> None:
        clock = {"now": 0.0}
        probe_state = {"ready": False}

        def probe() -> None:
            if not probe_state["ready"]:
                raise GraphdbError("database unavailable")

        def runner(args: list[str], *, timeout: int | None = None) -> CommandResult:
            if args == ["docker", "version"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if args == ["docker", "container", "inspect", "xauditor-neo4j"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            if args == ["docker", "inspect", "-f", "{{.State.Running}}", "xauditor-neo4j"]:
                return CommandResult(returncode=0, stdout="false\n", stderr="")
            if args == ["docker", "start", "xauditor-neo4j"]:
                return CommandResult(returncode=0, stdout="", stderr="")
            raise AssertionError(f"Unexpected command: {args}")

        manager = DockerManager(
            Neo4jConfig(ready_timeout_seconds=45),
            command_runner=runner,
            readiness_probe=probe,
        )

        def fake_sleep(seconds: float) -> None:
            clock["now"] += seconds
            if clock["now"] >= 31:
                probe_state["ready"] = True

        with patch("xauditor.integrations.docker.time.monotonic", side_effect=lambda: clock["now"]):
            with patch("xauditor.integrations.docker.time.sleep", side_effect=fake_sleep):
                result = manager.start_runtime()

        self.assertEqual(result, "Managed Neo4j container started")
        self.assertGreaterEqual(clock["now"], 31)

    def test_wait_ready_reports_last_probe_error_when_timeout_expires(self) -> None:
        clock = {"now": 0.0}

        def probe() -> None:
            raise GraphdbError("The client is unauthorized due to authentication failure.")

        manager = DockerManager(Neo4jConfig(), readiness_probe=probe)

        with patch("xauditor.integrations.docker.time.monotonic", side_effect=lambda: clock["now"]):
            with patch(
                "xauditor.integrations.docker.time.sleep",
                side_effect=lambda seconds: clock.__setitem__("now", clock["now"] + seconds),
            ):
                with self.assertRaisesRegex(GraphdbError, "authentication failure") as ctx:
                    manager.wait_ready(timeout_seconds=2)

        self.assertIn("Timed out while waiting for the managed Neo4j container to become ready.", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
