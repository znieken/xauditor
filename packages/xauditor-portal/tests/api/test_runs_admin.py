"""Tests for the admin run-management endpoints (cancel, complete, delete).

These exercise the role-gate, status-transition idempotency, and audit-
log row insertion using ``TestClient`` with ``current_user`` and
``get_session`` overridden by mocks.
"""

from __future__ import annotations

import unittest
import uuid
from datetime import datetime, timezone
from typing import Any
from unittest.mock import AsyncMock, MagicMock

from fastapi.testclient import TestClient

from xauditor_portal.app import create_app
from xauditor_portal.auth import current_user, get_session
from xauditor_portal.auth.roles import Role
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.report import AuditRun, AuditRunAdminAction


def _user(role: str) -> User:
    user = User(
        id=uuid.uuid4(),
        username="actor",
        password_hash="x",
        must_change_password=False,
        role=role,
    )
    user.created_at = datetime.now(timezone.utc)
    user.updated_at = user.created_at
    return user


def _run(*, status: str = "in_progress") -> AuditRun:
    run = AuditRun(
        id=uuid.uuid4(),
        repo_root="/tmp/repo",
        project_name="repo",
        build_fingerprint="bf-1",
        mode="single",
        status=status,
        progress_percent=50,
        total_candidates=0,
        valid_findings=0,
        false_positives=0,
        unlabeled_findings=0,
        duplicate_findings=0,
        llm_providers_used={},
        started_at=datetime.now(timezone.utc),
        completed_at=None if status == "in_progress" else datetime.now(timezone.utc),
    )
    return run


def _fake_session(*, run: AuditRun | None = None) -> MagicMock:
    session = MagicMock()
    session.execute = AsyncMock()
    session.scalar = AsyncMock(return_value=None)
    session.get = AsyncMock(return_value=run)
    session.add = MagicMock()
    session.commit = AsyncMock()
    session.refresh = AsyncMock()
    session.rollback = AsyncMock()
    session.delete = AsyncMock()
    # _run_metrics calls session.execute with multiple selects; returning
    # an empty result keeps the metrics computation a zero pivot.
    empty_result = MagicMock()
    empty_result.first.return_value = None
    empty_result.all.return_value = []
    empty_result.scalar.return_value = 0
    empty_result.scalar_one.return_value = 0
    session.execute = AsyncMock(return_value=empty_result)
    return session


def _build_client(
    *, role: str, session: MagicMock | None = None
) -> TestClient:
    app = create_app()
    actor = _user(role)
    app.dependency_overrides[current_user] = lambda: actor
    fake_session = session or _fake_session()

    async def _provider() -> Any:
        yield fake_session

    app.dependency_overrides[get_session] = _provider
    client = TestClient(app)
    client._actor = actor  # type: ignore[attr-defined]
    client._session = fake_session  # type: ignore[attr-defined]
    return client


class RunAdminRoleGateTests(unittest.TestCase):
    """auditor and viewer SHALL receive HTTP 403 from every admin run
    endpoint; admin proceeds."""

    def test_auditor_cancel_is_403(self) -> None:
        with _build_client(role=Role.AUDITOR.value) as client:
            response = client.post(f"/api/runs/{uuid.uuid4()}/cancel")
        self.assertEqual(response.status_code, 403)

    def test_auditor_complete_is_403(self) -> None:
        with _build_client(role=Role.AUDITOR.value) as client:
            response = client.post(f"/api/runs/{uuid.uuid4()}/complete")
        self.assertEqual(response.status_code, 403)

    def test_auditor_delete_is_403(self) -> None:
        with _build_client(role=Role.AUDITOR.value) as client:
            response = client.delete(f"/api/runs/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 403)

    def test_viewer_is_403(self) -> None:
        with _build_client(role=Role.VIEWER.value) as client:
            response = client.post(f"/api/runs/{uuid.uuid4()}/cancel")
        self.assertEqual(response.status_code, 403)


class CancelRunTests(unittest.TestCase):
    def test_unknown_run_is_404(self) -> None:
        # _fake_session with run=None makes session.get return None,
        # which the handler converts into a 404.
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.post(f"/api/runs/{uuid.uuid4()}/cancel")
        self.assertEqual(response.status_code, 404)

    def test_cancel_in_progress_run_sets_cancelled(self) -> None:
        run = _run(status="in_progress")
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(
                f"/api/runs/{run.id}/cancel",
                json={"reason": "worker stuck"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.status, "cancelled")
        self.assertIsNotNone(run.completed_at)
        # An audit-log row was added before commit.
        adds = session.add.call_args_list
        action_rows = [
            call.args[0]
            for call in adds
            if isinstance(call.args[0], AuditRunAdminAction)
        ]
        self.assertEqual(len(action_rows), 1)
        self.assertEqual(action_rows[0].action, "cancel")
        self.assertEqual(action_rows[0].previous_status, "in_progress")
        self.assertEqual(action_rows[0].new_status, "cancelled")
        self.assertEqual(action_rows[0].reason, "worker stuck")

    def test_cancel_already_cancelled_run_is_idempotent(self) -> None:
        run = _run(status="cancelled")
        original_completed_at = run.completed_at
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/runs/{run.id}/cancel")
        self.assertEqual(response.status_code, 200)
        # No row mutation, no audit-log insert, no commit.
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(run.completed_at, original_completed_at)
        session.commit.assert_not_awaited()
        adds = [c for c in session.add.call_args_list]
        for call in adds:
            self.assertNotIsInstance(call.args[0], AuditRunAdminAction)

    def test_cancel_failed_run_overrides_to_cancelled(self) -> None:
        # Cross-terminal override is allowed by design.
        run = _run(status="failed")
        original_completed_at = run.completed_at
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/runs/{run.id}/cancel")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.status, "cancelled")
        # completed_at was already set on the failed transition; the
        # cancel does not advance it.
        self.assertEqual(run.completed_at, original_completed_at)


class CompleteRunTests(unittest.TestCase):
    def test_complete_in_progress_run_sets_completed(self) -> None:
        run = _run(status="in_progress")
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(
                f"/api/runs/{run.id}/complete",
                json={"reason": "worker confirmed done"},
            )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.status, "completed")
        self.assertIsNotNone(run.completed_at)
        adds = session.add.call_args_list
        action_rows = [
            call.args[0]
            for call in adds
            if isinstance(call.args[0], AuditRunAdminAction)
        ]
        self.assertEqual(len(action_rows), 1)
        self.assertEqual(action_rows[0].action, "complete")
        self.assertEqual(action_rows[0].new_status, "completed")

    def test_complete_already_completed_is_idempotent(self) -> None:
        run = _run(status="completed")
        original_completed_at = run.completed_at
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.post(f"/api/runs/{run.id}/complete")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(run.completed_at, original_completed_at)
        session.commit.assert_not_awaited()


class DeleteRunTests(unittest.TestCase):
    def test_unknown_run_is_404(self) -> None:
        with _build_client(role=Role.ADMIN.value) as client:
            response = client.delete(f"/api/runs/{uuid.uuid4()}")
        self.assertEqual(response.status_code, 404)

    def test_delete_run_emits_audit_log_and_204(self) -> None:
        run = _run(status="completed")
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.delete(f"/api/runs/{run.id}")
        self.assertEqual(response.status_code, 204)
        # Audit-log row was added BEFORE the delete + commit.
        adds = session.add.call_args_list
        action_rows = [
            call.args[0]
            for call in adds
            if isinstance(call.args[0], AuditRunAdminAction)
        ]
        self.assertEqual(len(action_rows), 1)
        self.assertEqual(action_rows[0].action, "delete")
        self.assertEqual(action_rows[0].new_status, "deleted")
        self.assertEqual(action_rows[0].previous_status, "completed")
        session.delete.assert_awaited_once()
        session.commit.assert_awaited_once()

    def test_delete_run_pre_deletes_finding_annotations(self) -> None:
        # The handler MUST issue an explicit DELETE on
        # report.finding_annotations BEFORE the audit_runs delete to
        # defuse the CASCADE-vs-SET-NULL race against the
        # ck_finding_annotations_duplicate_pointer CHECK constraint.
        # If this regresses, full-run delete will 500 in production
        # whenever the run contains duplicate-labeled findings whose
        # canonical target is also in the same run.
        from sqlalchemy import Delete

        run = _run(status="completed")
        session = _fake_session(run=run)
        with _build_client(role=Role.ADMIN.value, session=session) as client:
            response = client.delete(f"/api/runs/{run.id}")
        self.assertEqual(response.status_code, 204)

        delete_stmts = [
            call.args[0]
            for call in session.execute.call_args_list
            if isinstance(call.args[0], Delete)
        ]
        self.assertTrue(
            delete_stmts,
            "handler must issue at least one explicit DELETE statement; "
            "got call_args=%r" % (session.execute.call_args_list,),
        )
        annotation_deletes = [
            stmt
            for stmt in delete_stmts
            if "finding_annotations" in str(stmt.compile()).lower()
        ]
        self.assertEqual(
            len(annotation_deletes),
            1,
            "expected exactly one DELETE on finding_annotations; "
            "got %d (all statements: %r)" % (
                len(annotation_deletes),
                [str(s.compile()) for s in delete_stmts],
            ),
        )


if __name__ == "__main__":
    unittest.main()
