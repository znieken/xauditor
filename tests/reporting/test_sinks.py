from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from xauditor.reporting.sinks import (
    ProgressEvent,
    ReportSink,
    ReportSinkBus,
    RunMeta,
)


class _RecordingSink:
    def __init__(self, *, fail_on: set[str] | None = None) -> None:
        self.fail_on = fail_on or set()
        self.calls: list[tuple[str, object]] = []

    def _maybe_fail(self, op: str) -> None:
        if op in self.fail_on:
            raise RuntimeError(f"sink rejected {op}")

    def open_run(self, meta: RunMeta) -> None:
        self.calls.append(("open_run", meta))
        self._maybe_fail("open_run")

    def emit_snapshot(self, snapshot) -> None:
        self.calls.append(("emit_snapshot", snapshot))
        self._maybe_fail("emit_snapshot")

    def upsert_finding(self, run_id: str, finding) -> None:
        self.calls.append(("upsert_finding", (run_id, finding)))
        self._maybe_fail("upsert_finding")

    def write_progress(self, event: ProgressEvent) -> None:
        self.calls.append(("write_progress", event))
        self._maybe_fail("write_progress")

    def write_resume_state(self, payload) -> None:
        self.calls.append(("write_resume_state", dict(payload)))
        self._maybe_fail("write_resume_state")

    def close_run(self, final) -> None:
        self.calls.append(("close_run", final))
        self._maybe_fail("close_run")

    def fail_run(self, *, status: str, error: str) -> None:
        self.calls.append(("fail_run", (status, error)))
        self._maybe_fail("fail_run")


def _meta(tmp: Path) -> RunMeta:
    return RunMeta(
        repo_root=str(tmp),
        project_name="proj",
        build_fingerprint="fp-123",
        mode="single",
        run_label=tmp.name or "20260429-010203",
        started_at=datetime.now(timezone.utc),
    )


class ReportSinkProtocolTests(unittest.TestCase):
    def test_recording_sink_satisfies_protocol(self) -> None:
        sink = _RecordingSink()
        self.assertIsInstance(sink, ReportSink)


class ReportSinkBusTests(unittest.TestCase):
    """Phase 2 ``ReportSinkBus`` is a single-sink wrapper.

    The 0.4.x primary/secondary fan-out is gone — there is only the
    Postgres sink. The bus keeps a ``threading.Lock`` so concurrent
    coder worker callbacks serialise into the single sink.
    """

    def test_every_method_delegates_to_the_wrapped_sink(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            sink = _RecordingSink()
            bus = ReportSinkBus(sink)
            bus.open_run(_meta(Path(tmp)))
            bus.emit_snapshot("snap")
            bus.upsert_finding("RUN-1", "finding")
            bus.write_progress(
                ProgressEvent(stage="analyzer", heartbeat_kind="progress", message="x")
            )
            bus.write_resume_state({"schema_version": 1, "shared_state": {}})
            bus.close_run("final")
            ops = [c[0] for c in sink.calls]
            self.assertEqual(
                ops,
                [
                    "open_run",
                    "emit_snapshot",
                    "upsert_finding",
                    "write_progress",
                    "write_resume_state",
                    "close_run",
                ],
            )

    def test_emit_snapshot_exception_propagates(self) -> None:
        sink = _RecordingSink(fail_on={"emit_snapshot"})
        bus = ReportSinkBus(sink)
        with self.assertRaises(RuntimeError):
            bus.emit_snapshot("snap")

    def test_upsert_finding_exception_propagates(self) -> None:
        sink = _RecordingSink(fail_on={"upsert_finding"})
        bus = ReportSinkBus(sink)
        with self.assertRaises(RuntimeError):
            bus.upsert_finding("RUN-1", "fake")

    def test_write_progress_exception_is_swallowed_and_logged(self) -> None:
        sink = _RecordingSink(fail_on={"write_progress"})
        bus = ReportSinkBus(sink)
        with self.assertLogs("xauditor.reporting.sinks", level="WARNING") as cm:
            bus.write_progress(
                ProgressEvent(stage="reporting", heartbeat_kind="started")
            )
        self.assertIn("write progress", "\n".join(cm.output).lower())

    def test_write_resume_state_exception_is_swallowed_and_logged(self) -> None:
        sink = _RecordingSink(fail_on={"write_resume_state"})
        bus = ReportSinkBus(sink)
        with self.assertLogs("xauditor.reporting.sinks", level="WARNING") as cm:
            bus.write_resume_state({"schema_version": 1, "shared_state": {}})
        self.assertIn("resume state", "\n".join(cm.output).lower())

    def test_fail_run_exception_is_swallowed_and_logged(self) -> None:
        sink = _RecordingSink(fail_on={"fail_run"})
        bus = ReportSinkBus(sink)
        with self.assertLogs("xauditor.reporting.sinks", level="WARNING") as cm:
            bus.fail_run(status="failed", error="boom")
        self.assertIn("fail_run", "\n".join(cm.output))


class RateLimitedHeartbeatTests(unittest.TestCase):
    def test_progress_is_rate_limited(self) -> None:
        from xauditor.services import _rate_limited_heartbeat

        captured: list[ProgressEvent] = []
        clock = [100.0]

        def _progress(index: int) -> ProgressEvent:
            return ProgressEvent(
                stage="analyzer", heartbeat_kind="progress", current_path_index=index
            )

        def _stage_completed() -> ProgressEvent:
            return ProgressEvent(stage="analyzer", heartbeat_kind="stage_completed")

        # Monkey-patch ``time.monotonic`` so the test is deterministic.
        import time as time_module

        original_monotonic = time_module.monotonic
        try:
            time_module.monotonic = lambda: clock[0]  # type: ignore[assignment]
            cb = _rate_limited_heartbeat(captured.append, interval_seconds=2.0)
            cb(_progress(1))  # fires — first event
            cb(_progress(2))  # dropped — under 2s
            clock[0] += 1.0
            cb(_progress(3))  # dropped — still under 2s
            clock[0] += 1.5
            cb(_progress(4))  # fires — 2.5s after last fire
            cb(_stage_completed())  # always fires regardless of interval
            cb(_progress(5))  # dropped — immediately after
        finally:
            time_module.monotonic = original_monotonic

        kinds = [(e.heartbeat_kind, e.current_path_index) for e in captured]
        self.assertEqual(
            kinds,
            [
                ("progress", 1),
                ("progress", 4),
                ("stage_completed", None),
            ],
        )


if __name__ == "__main__":
    unittest.main()
