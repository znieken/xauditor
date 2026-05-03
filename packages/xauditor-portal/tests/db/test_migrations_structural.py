"""Static checks on the migration chain — no DB required.

Each migration file must be present and chain correctly via its
``down_revision`` pointer so ``alembic upgrade head`` applies them in the
expected order.
"""

from __future__ import annotations

import unittest
from pathlib import Path


VERSIONS_DIR = (
    Path(__file__).resolve().parents[2]
    / "src"
    / "xauditor_portal"
    / "db"
    / "migrations"
    / "versions"
)


class MigrationRevisionPointersTests(unittest.TestCase):
    """Validate every migration declares the expected revision pair.

    We parse the file contents for ``revision`` and ``down_revision`` rather
    than importing to avoid side effects from alembic's ``op`` context.
    """

    def _revision_pair(self, path: Path) -> tuple[str, str | None]:
        text = path.read_text(encoding="utf-8")
        revision = self._pick(text, "revision: str")
        down = self._pick(text, "down_revision: str | None")
        return revision, down if down != "None" else None

    @staticmethod
    def _pick(text: str, prefix: str) -> str:
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith(prefix):
                _, _, value = stripped.partition("=")
                return value.strip().strip('"').strip("'")
        raise AssertionError(f"Did not find `{prefix}` in migration text")

    def test_0001_initial_has_no_parent(self) -> None:
        revision, down = self._revision_pair(VERSIONS_DIR / "0001_initial.py")
        self.assertEqual(revision, "0001_initial")
        self.assertIsNone(down)

    def test_0002_project_index_follows_initial(self) -> None:
        revision, down = self._revision_pair(
            VERSIONS_DIR / "0002_project_index.py"
        )
        self.assertEqual(revision, "0002_project_index")
        self.assertEqual(down, "0001_initial")

    def test_0003_coverage_status_indexes_follows_0002(self) -> None:
        path = VERSIONS_DIR / "0003_coverage_status_indexes.py"
        self.assertTrue(path.exists(), "0003 migration must ship in the package")
        revision, down = self._revision_pair(path)
        self.assertEqual(revision, "0003_coverage_status_indexes")
        self.assertEqual(down, "0002_project_index")

    def test_0004_findings_path_fingerprint_follows_0003(self) -> None:
        path = VERSIONS_DIR / "0004_findings_path_fingerprint.py"
        self.assertTrue(path.exists(), "0004 migration must ship in the package")
        revision, down = self._revision_pair(path)
        self.assertEqual(revision, "0004_findings_path_fingerprint")
        self.assertEqual(down, "0003_coverage_status_indexes")

    def test_0005_coder_findings_follows_0004(self) -> None:
        path = VERSIONS_DIR / "0005_coder_findings.py"
        self.assertTrue(path.exists(), "0005 migration must ship in the package")
        revision, down = self._revision_pair(path)
        self.assertEqual(revision, "0005_coder_findings")
        self.assertEqual(down, "0004_findings_path_fingerprint")

    def test_0006_audit_runs_resume_state_follows_0005(self) -> None:
        path = VERSIONS_DIR / "0006_audit_runs_resume_state.py"
        self.assertTrue(path.exists(), "0006 migration must ship in the package")
        revision, down = self._revision_pair(path)
        self.assertEqual(revision, "0006_audit_runs_resume_state")
        self.assertEqual(down, "0005_coder_findings")


if __name__ == "__main__":
    unittest.main()
