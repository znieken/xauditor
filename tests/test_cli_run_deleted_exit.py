"""CLI ``os._exit(75)`` behaviour on ``RunDeletedExternallyError``.

When the audit worker detects the run was deleted externally and raises
``RunDeletedExternallyError``, the CLI must:

1. Print the user-facing message to stderr.
2. Flush stderr (because ``os._exit`` skips Python I/O finalisation).
3. Call ``os._exit(75)``, NOT ``return 75``.

The ``os._exit`` bypasses the Python interpreter's atexit chain — most
importantly, ``concurrent.futures.thread._python_exit`` which would
``executor.shutdown(wait=True)`` the coder dispatcher and block on
in-flight Claude CLI subprocesses. Without ``os._exit``, the audit
process appears stuck for the duration of the longest in-flight LLM
call after the workflow has cleanly raised.

These tests patch ``os._exit`` so the test process doesn't actually
terminate; they assert the call shape.
"""

from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.cli import main
from xauditor.errors import ConfigError

from xauditor_portal.sinks import RunDeletedExternallyError


def _make_services_raising(exc: Exception) -> MagicMock:
    """Build a minimal ``ApplicationServices`` mock that raises ``exc``
    when ``run_audit`` is called. The CLI's ``audit run`` path also
    calls ``_check_neo4j_reachable_or_die`` before ``run_audit``; we
    make that a no-op via the ``neo4j`` mock."""

    services = MagicMock()
    services.config = MagicMock()
    services.config.report_logging = MagicMock()
    services.config.report_logging.level = "INFO"
    services.config.report_logging.file = None
    services.neo4j = MagicMock()
    services.neo4j.ping = MagicMock(return_value=None)
    services.neo4j.set_logger = MagicMock(return_value=None)
    services.run_audit = MagicMock(side_effect=exc)
    return services


class CliOsExitOnRunDeletedTests(unittest.TestCase):
    def test_run_deleted_externally_calls_os_exit_75(self) -> None:
        run_id = "test-run-id-deadbeef"
        services = _make_services_raising(RunDeletedExternallyError(run_id))
        stderr = io.StringIO()

        with patch("os._exit") as mock_exit:
            # ``main`` calls ``os._exit(75)`` on this branch — patched so
            # the test process keeps running. The function returns ``None``
            # because ``os._exit`` was mocked away.
            main(
                ["audit", "run", "--build", "fp-1"],
                services=services,
                stderr=stderr,
            )

        mock_exit.assert_called_once_with(75)
        self.assertIn(run_id, stderr.getvalue())
        self.assertIn("deleted from the portal", stderr.getvalue())

    def test_other_exceptions_do_not_call_os_exit(self) -> None:
        # Non-``RunDeletedExternallyError`` paths SHALL preserve the
        # normal Python finalisation chain. ``ConfigError`` is a
        # convenient mid-stack raise that the existing handler tuple
        # already catches and returns 1 from.
        services = _make_services_raising(ConfigError("bad config"))
        stderr = io.StringIO()

        with patch("os._exit") as mock_exit:
            result = main(
                ["audit", "run", "--build", "fp-1"],
                services=services,
                stderr=stderr,
            )

        mock_exit.assert_not_called()
        self.assertEqual(result, 1)
        self.assertIn("bad config", stderr.getvalue())

    def test_generic_exception_propagates_does_not_call_os_exit(self) -> None:
        # A bare ``Exception`` outside the friendly-error tuple AND not
        # ``RunDeletedExternallyError`` SHALL propagate (re-raise),
        # NOT call ``os._exit``. The CLI's ``except Exception`` block
        # only catches ``RunDeletedExternallyError`` for the
        # ``os._exit(75)`` branch; everything else re-raises so the
        # uncaught path produces a real traceback for the operator.
        services = _make_services_raising(RuntimeError("unexpected"))
        stderr = io.StringIO()

        with patch("os._exit") as mock_exit:
            with self.assertRaises(RuntimeError):
                main(
                    ["audit", "run", "--build", "fp-1"],
                    services=services,
                    stderr=stderr,
                )

        mock_exit.assert_not_called()


if __name__ == "__main__":
    unittest.main()
