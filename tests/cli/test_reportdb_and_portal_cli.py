from __future__ import annotations

import io
import sys
import tempfile
import textwrap
import unittest
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.cli import main
from xauditor.config import RemoteConnectionConfig
from xauditor.services import ApplicationServices


_ENV = {
    "XAUDITOR_LLM_BASE_URL": "mock://offline",
    "XAUDITOR_LLM_API_KEY": "secret",
    "XAUDITOR_LLM_MODEL_NAME": "mock-model",
    "XAUDITOR_LOGGING_LEVEL": "info",
}


def _write_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


class ReportDBCliTests(unittest.TestCase):
    def test_reportdb_init_start_stop_reset_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )

            stdout = io.StringIO()
            self.assertEqual(
                main(["reportdb", "start"], services=services, stdout=stdout, stderr=io.StringIO()),
                1,
            )  # start before init

            stdout = io.StringIO()
            self.assertEqual(
                main(["reportdb", "init"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("reportdb ready", stdout.getvalue())

            stdout = io.StringIO()
            self.assertEqual(
                main(["reportdb", "stop"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("stopped", stdout.getvalue())

            stderr = io.StringIO()
            self.assertEqual(
                main(["reportdb", "reset"], services=services, stdout=io.StringIO(), stderr=stderr),
                1,
            )  # --yes required
            self.assertIn("--yes", stderr.getvalue())

            stdout = io.StringIO()
            self.assertEqual(
                main(
                    ["reportdb", "reset", "--yes"],
                    services=services,
                    stdout=stdout,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertIn("Deleted", stdout.getvalue())

    def test_reportdb_commands_report_remote_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                """
                reportdb:
                  remote:
                    url: postgresql://u:p@host:5432/db
                """,
            )
            services = ApplicationServices.for_testing(
                repo_root=repo_root,
                env=_ENV,
            )
            stdout = io.StringIO()
            self.assertEqual(
                main(["reportdb", "init"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("remote endpoint is configured", stdout.getvalue())


class InitUmbrellaTests(unittest.TestCase):
    def test_init_runs_both_databases_in_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )
            stdout = io.StringIO()
            exit_code = main(
                ["init"], services=services, stdout=stdout, stderr=io.StringIO()
            )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("graphdb ready", output)
            self.assertIn("reportdb ready", output)
            self.assertLess(
                output.index("graphdb ready"),
                output.index("reportdb ready"),
            )

    def test_init_skips_graphdb_when_remote_configured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )
            # Swap in a remote graphdb config without reloading from yml
            services.config = replace(
                services.config,
                graphdb=replace(
                    services.config.graphdb,
                    remote=RemoteConnectionConfig(url="bolt://host:7687"),
                ),
            )
            # Re-create in-memory docker so it sees remote
            from xauditor.integrations.docker import InMemoryDockerManager
            services.docker = InMemoryDockerManager(services.config.graphdb)

            stdout = io.StringIO()
            exit_code = main(
                ["init"], services=services, stdout=stdout, stderr=io.StringIO()
            )
            self.assertEqual(exit_code, 0)
            output = stdout.getvalue()
            self.assertIn("Graph database: remote endpoint configured", output)
            self.assertIn("reportdb ready", output)
            self.assertNotIn("graphdb ready", output)


class PortalCliTests(unittest.TestCase):
    def test_portal_start_fails_when_portal_package_missing(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp),
                env=_ENV,
                portal_package_installed=False,
            )
            stdout = io.StringIO()
            stderr = io.StringIO()
            exit_code = main(
                ["portal", "start"], services=services, stdout=stdout, stderr=stderr
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("xauditor-portal", stderr.getvalue())

    def test_portal_full_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )

            # First-time setup goes through `init`, mirroring the coder /
            # graphdb / reportdb verbs. `init` builds the images, creates
            # the containers, starts them, and probes health.
            stdout = io.StringIO()
            self.assertEqual(
                main(["portal", "init"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("http://127.0.0.1:8080", stdout.getvalue())

            stdout = io.StringIO()
            self.assertEqual(
                main(["portal", "status"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            output = stdout.getvalue()
            self.assertIn("backend: running", output)
            self.assertIn("frontend: running", output)

            stdout = io.StringIO()
            self.assertEqual(
                main(["portal", "stop"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("Stopped managed portal containers", stdout.getvalue())

            # `start` works AFTER `init` because containers exist (just stopped).
            stdout = io.StringIO()
            self.assertEqual(
                main(["portal", "start"], services=services, stdout=stdout, stderr=io.StringIO()),
                0,
            )
            self.assertIn("http://127.0.0.1:8080", stdout.getvalue())

            stderr = io.StringIO()
            self.assertEqual(
                main(["portal", "reset"], services=services, stdout=io.StringIO(), stderr=stderr),
                1,
            )
            self.assertIn("--yes", stderr.getvalue())

    def test_portal_start_refuses_when_containers_missing(self) -> None:
        """Strict `portal start`: refuses when nothing was init'd, points at `portal init`."""

        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )
            stderr = io.StringIO()
            exit_code = main(
                ["portal", "start"],
                services=services,
                stdout=io.StringIO(),
                stderr=stderr,
            )
            self.assertEqual(exit_code, 1)
            self.assertIn("Portal container(s) missing", stderr.getvalue())
            self.assertIn("xauditor portal init", stderr.getvalue())

            stdout = io.StringIO()
            self.assertEqual(
                main(
                    ["portal", "reset", "--yes"],
                    services=services,
                    stdout=stdout,
                    stderr=io.StringIO(),
                ),
                0,
            )
            self.assertIn("Deleted", stdout.getvalue())

    def test_portal_reset_does_not_touch_graphdb_or_reportdb(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            services = ApplicationServices.for_testing(
                repo_root=Path(tmp), env=_ENV
            )
            # Bring everything up
            main(["init"], services=services, stdout=io.StringIO(), stderr=io.StringIO())
            main(["portal", "start"], services=services, stdout=io.StringIO(), stderr=io.StringIO())

            # Snapshot prior graphdb/reportdb state
            docker_initialized_before = services.docker.initialized
            reportdb_initialized_before = services.reportdb.initialized

            # Reset portal only
            main(
                ["portal", "reset", "--yes"],
                services=services,
                stdout=io.StringIO(),
                stderr=io.StringIO(),
            )

            # graphdb and reportdb should still be initialized
            self.assertEqual(services.docker.initialized, docker_initialized_before)
            self.assertTrue(services.docker.initialized)
            self.assertEqual(services.reportdb.initialized, reportdb_initialized_before)
            self.assertTrue(services.reportdb.initialized)

            # Portal should be fully torn down
            self.assertFalse(services.portal.backend_running)
            self.assertFalse(services.portal.frontend_running)
            self.assertFalse(services.portal.network_exists)


if __name__ == "__main__":
    unittest.main()
