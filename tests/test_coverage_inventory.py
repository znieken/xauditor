from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.models import CoverageInventory, CoverageRecord, CoverageState


class CoverageInventoryPercentageTests(unittest.TestCase):
    def _inventory(self, *records: CoverageRecord) -> CoverageInventory:
        inv = CoverageInventory()
        for record in records:
            inv.add(record)
        return inv

    def test_failed_paths_counted_in_denominator_not_numerator(self) -> None:
        inv = self._inventory(
            CoverageRecord(category="path", identifier="fp-A", state=CoverageState.AUDITED),
            CoverageRecord(category="path", identifier="fp-B", state=CoverageState.INTERRUPTED),
            CoverageRecord(category="path", identifier="fp-C", state=CoverageState.FAILED),
        )
        self.assertAlmostEqual(inv.percentage("path"), 100.0 / 3.0, places=4)

    def test_excluded_paths_stay_out_of_denominator(self) -> None:
        inv = self._inventory(
            CoverageRecord(category="path", identifier="fp-A", state=CoverageState.AUDITED),
            CoverageRecord(category="path", identifier="fp-B", state=CoverageState.EXCLUDED),
            CoverageRecord(category="path", identifier="fp-C", state=CoverageState.FAILED),
        )
        self.assertEqual(inv.percentage("path"), 50.0)

    def test_all_failed_yields_zero_percent(self) -> None:
        inv = self._inventory(
            CoverageRecord(category="path", identifier="fp-A", state=CoverageState.FAILED),
            CoverageRecord(category="path", identifier="fp-B", state=CoverageState.FAILED),
        )
        self.assertEqual(inv.percentage("path"), 0.0)

    def test_empty_category_returns_full_coverage(self) -> None:
        inv = CoverageInventory()
        self.assertEqual(inv.percentage("path"), 100.0)


if __name__ == "__main__":
    unittest.main()
