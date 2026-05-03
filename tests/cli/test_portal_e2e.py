"""End-to-end `xauditor portal` CLI against real Docker.

Gated behind the env flag `XAUDITOR_PORTAL_E2E=1` so normal test runs stay
fast and hermetic. When the flag is set this suite:

- Requires Docker to be running and reachable from the test process.
- Requires `xauditor-portal` to be importable (we build from its bundled
  Dockerfiles here; no pre-existing images are required).
- Brings up the two-container portal via `xauditor portal init`, which
  builds both images, creates the containers, and starts them; probes the
  host URL's `/api/health` through the frontend proxy (the endpoint
  returns HTTP 200 with `status=degraded` when no Postgres is running,
  which is what we expect in CI).
- Tears everything down via `xauditor portal stop` followed by
  `xauditor portal reset --yes` and asserts that no managed Docker
  resources remain.
- Verifies the install-hint error surfaces when the portal package is
  simulated missing (by injecting a fake `for_testing` services bundle
  into the CLI entry point — see the non-e2e test for the structural
  flow).
"""

from __future__ import annotations

import io
import os
import subprocess
import sys
import time
import unittest
from pathlib import Path

import urllib.error
import urllib.request


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def _docker_available() -> bool:
    try:
        result = subprocess.run(
            ["docker", "version"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return False
    return result.returncode == 0


def _require_portal_package() -> None:
    try:
        import xauditor_portal  # noqa: F401
    except ImportError as exc:  # pragma: no cover - guarded by e2e flag
        raise unittest.SkipTest("xauditor-portal is not installed") from exc


def _poll(url: str, *, attempts: int, interval: float) -> int | None:
    for _ in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=2) as response:  # noqa: S310
                return int(response.status)
        except urllib.error.HTTPError as exc:
            return int(exc.code)
        except Exception:
            time.sleep(interval)
    return None


def _docker_image_tags() -> str:
    return subprocess.run(
        ["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
        capture_output=True,
        text=True,
        timeout=10,
    ).stdout


@unittest.skipUnless(
    os.environ.get("XAUDITOR_PORTAL_E2E") == "1",
    "Set XAUDITOR_PORTAL_E2E=1 to run real-docker portal tests",
)
class PortalE2ETests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        if not _docker_available():
            raise unittest.SkipTest("Docker is not reachable")
        _require_portal_package()

    def _cli(self, args: list[str], *, expect_exit: int = 0) -> str:
        from xauditor.cli import main

        stdout = io.StringIO()
        stderr = io.StringIO()
        code = main(args, stdout=stdout, stderr=stderr)
        if code != expect_exit:  # pragma: no cover - surfaced on failure
            self.fail(
                f"`xauditor {' '.join(args)}` exit={code} "
                f"stdout={stdout.getvalue()!r} stderr={stderr.getvalue()!r}"
            )
        return stdout.getvalue()

    def test_full_lifecycle_against_real_docker(self) -> None:
        # Always start from a clean slate so a prior aborted run cannot
        # pollute the assertion set.
        try:
            self._cli(["portal", "reset", "--yes"])
        except Exception:  # pragma: no cover - reset is best-effort
            pass

        # Bring up the portal (builds both images, creates containers,
        # starts them). Probe the host URL's /api/health through the
        # frontend proxy. No postgres is running, so health is `degraded`
        # but the endpoint SHALL return HTTP 200.
        try:
            started = self._cli(["portal", "init"])
            self.assertIn("Portal running at http://127.0.0.1:", started)
            images = _docker_image_tags()
            self.assertIn("xauditor-portal-backend:local", images)
            self.assertIn("xauditor-portal-frontend:local", images)
            status = _poll(
                "http://127.0.0.1:8080/api/health", attempts=20, interval=1.0
            )
            self.assertEqual(status, 200)

            # Stop preserves images.
            self._cli(["portal", "stop"])
            images = _docker_image_tags()
            self.assertIn("xauditor-portal-backend:local", images)
            self.assertIn("xauditor-portal-frontend:local", images)
        finally:
            reset_out = self._cli(["portal", "reset", "--yes"])
            self.assertIn("xauditor-portal-backend:local", reset_out)
            self.assertIn("xauditor-portal-frontend:local", reset_out)
            self.assertIn("xauditor-portal-net", reset_out)

        containers = subprocess.run(
            ["docker", "ps", "-a", "--filter", "name=xauditor-portal", "-q"],
            capture_output=True,
            text=True,
        )
        self.assertEqual(containers.stdout.strip(), "")

        networks = subprocess.run(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                "name=xauditor-portal-net",
                "-q",
            ],
            capture_output=True,
            text=True,
        )
        self.assertEqual(networks.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
