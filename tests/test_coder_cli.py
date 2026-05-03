"""CLI dispatch tests for ``xauditor coder {build,init,start,stop,reset,status}``.

We don't shell out to docker — the CLI handler routes to
``ApplicationServices.coder_*``, which we replace with a fake to record
the dispatch + verify the flag plumbing.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.cli import _handle_coder, _render_coder_status, build_parser
from xauditor.integrations.coder import CoderRuntimeStatus


class _FakeServices:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def coder_build(self, *, push, logger):
        self.calls.append(("build", push))
        return "tag:built"

    def coder_init(self, *, logger):
        self.calls.append(("init",))
        return "ready"

    def coder_start(self, *, logger):
        self.calls.append(("start",))
        return "started"

    def coder_stop(self, *, logger):
        self.calls.append(("stop",))
        return "stopped"

    def coder_reset(self, *, confirmed, logger):
        self.calls.append(("reset", confirmed))
        return ["x", "y"] if confirmed else []

    def coder_status(self, *, logger):
        self.calls.append(("status",))
        return CoderRuntimeStatus(
            transport="http",
            endpoint_kind="loopback",
            endpoint="http://127.0.0.1:8090",
            container_name="cs",
            image="img:tag",
            image_present=True,
            container_exists=True,
            container_running=True,
            health_status=200,
            claude_cli_version="claude X",
            in_flight=2,
            error=None,
        )


class _NullLogger:
    def info(self, _msg): pass
    def warning(self, _msg): pass
    def error(self, _msg): pass


def _run(*verb_args: str) -> tuple[int, str, _FakeServices]:
    parser = build_parser()
    args = parser.parse_args(["coder", *verb_args])
    services = _FakeServices()
    stdout = io.StringIO()
    rc = _handle_coder(args, services, stdout, _NullLogger())  # type: ignore[arg-type]
    return rc, stdout.getvalue(), services


class CoderCliDispatchTests(unittest.TestCase):
    def test_build_no_push(self) -> None:
        rc, out, services = _run("build")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("build", False)])
        self.assertIn("tag:built", out)

    def test_build_push(self) -> None:
        rc, out, services = _run("build", "--push")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("build", True)])

    def test_init(self) -> None:
        rc, out, services = _run("init")
        self.assertEqual(rc, 0)
        self.assertIn("ready", out)
        self.assertEqual(services.calls, [("init",)])

    def test_start(self) -> None:
        rc, _out, services = _run("start")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("start",)])

    def test_stop(self) -> None:
        rc, _out, services = _run("stop")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("stop",)])

    def test_reset_without_yes(self) -> None:
        rc, out, services = _run("reset")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("reset", False)])
        self.assertIn("no managed", out.lower())

    def test_reset_with_yes(self) -> None:
        rc, out, services = _run("reset", "--yes")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("reset", True)])
        self.assertIn("Deleted x, y", out)

    def test_status(self) -> None:
        rc, out, services = _run("status")
        self.assertEqual(rc, 0)
        self.assertEqual(services.calls, [("status",)])
        self.assertIn("Coder transport: http", out)
        self.assertIn("running", out)
        self.assertIn("HTTP 200", out)
        self.assertIn("claude X", out)


class RenderCoderStatusTests(unittest.TestCase):
    def test_remote_endpoint_replaces_image_container_lines(self) -> None:
        status = CoderRuntimeStatus(
            transport="http",
            endpoint_kind="remote",
            endpoint="https://coder-pool.internal/",
            container_name="cs",
            image="img:tag",
            image_present=False,
            container_exists=False,
            container_running=False,
            health_status=200,
            claude_cli_version="claude X",
            in_flight=5,
            error=None,
        )
        lines = _render_coder_status(status)
        rendered = "\n".join(lines)
        self.assertIn("https://coder-pool.internal/", rendered)
        self.assertIn("remote endpoint; container lifecycle managed externally", rendered)
        self.assertIn("HTTP 200", rendered)

    def test_unreachable_remote_surfaces_error(self) -> None:
        status = CoderRuntimeStatus(
            transport="http",
            endpoint_kind="remote",
            endpoint="https://x/",
            container_name="(remote)",
            image="(remote)",
            image_present=False,
            container_exists=False,
            container_running=False,
            health_status=None,
            claude_cli_version="",
            in_flight=None,
            error="ConnectError: nope",
        )
        lines = _render_coder_status(status)
        rendered = "\n".join(lines)
        self.assertIn("unreachable", rendered)
        self.assertIn("ConnectError", rendered)


if __name__ == "__main__":
    unittest.main()
