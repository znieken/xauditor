"""Hermetic unit tests for the coverage-state → DB-status mapping.

Avoids the live-DB scaffolding used by ``test_postgres_sink_integration``;
asserts the pure-function behavior of the translation layer so it stays
in lockstep with ``CoverageState`` enum changes.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

# Ensure both the portal package and the xauditor source tree are
# importable without requiring an editable install. The portal tests are
# typically run with PYTHONPATH preconfigured; this fallback keeps the
# test self-contained when invoked directly.
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[4]
sys.path.insert(0, str(_REPO_ROOT / "src"))
sys.path.insert(
    0, str(_REPO_ROOT / "packages" / "xauditor-portal" / "src")
)

from xauditor.models import CoverageState  # noqa: E402
from xauditor_portal.sinks.postgres_sink import (  # noqa: E402
    _COVERAGE_STATE_TO_STATUS,
    _coverage_status,
)


class CoverageStateToDbStatusTests(unittest.TestCase):
    def test_failed_state_maps_to_failed_string(self) -> None:
        self.assertEqual(_coverage_status(CoverageState.FAILED), "failed")

    def test_failed_state_present_in_lookup_map(self) -> None:
        self.assertIn("failed", _COVERAGE_STATE_TO_STATUS)
        self.assertEqual(_COVERAGE_STATE_TO_STATUS["failed"], "failed")

    def test_existing_states_unchanged(self) -> None:
        self.assertEqual(_coverage_status(CoverageState.AUDITED), "audited")
        self.assertEqual(_coverage_status(CoverageState.NOT_AUDITED), "unaudited")
        self.assertEqual(_coverage_status(CoverageState.EXCLUDED), "excluded")
        self.assertEqual(_coverage_status(CoverageState.INTERRUPTED), "skipped")

    def test_unknown_string_state_falls_back_unchanged(self) -> None:
        # Defensive: a forward-compatible status value should pass
        # through rather than crash, mirroring existing behavior.
        self.assertEqual(_coverage_status("brand_new_state"), "brand_new_state")


if __name__ == "__main__":
    unittest.main()
