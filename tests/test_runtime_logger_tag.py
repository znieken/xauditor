"""``RuntimeLogger`` ``tag`` field tests.

Subprocess audit workers (``LocalSubprocessPool``) build their own
``RuntimeLogger`` tagged with the worker_id (e.g. ``audit-worker-2``)
so multi-worker output can be told apart in shared stderr / log_file.
This module covers the ``tag`` plumbing in isolation; the wired-up
worker test lives in ``tests/audit/test_worker_pool.py``.
"""

from __future__ import annotations

import io
import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.runtime_logging import RuntimeLogger


class RuntimeLoggerTagTests(unittest.TestCase):
    def test_default_tag_empty_preserves_legacy_format(self) -> None:
        # No tag → format identical to pre-tag releases:
        #   "INFO [<module>]: <msg>"
        stream = io.StringIO()
        logger = RuntimeLogger(level="info", stream=stream)
        logger.info("hello")
        line = stream.getvalue()
        self.assertIn("INFO [", line)
        self.assertNotIn("audit-worker", line)
        self.assertIn(": hello", line)

    def test_tag_appears_inside_bracket_segment(self) -> None:
        # With a tag → "INFO [<tag>/<module>]: <msg>" so a single
        # grep on "audit-worker-2" pulls every line that worker
        # emitted.
        stream = io.StringIO()
        logger = RuntimeLogger(
            level="info", stream=stream, tag="audit-worker-2"
        )
        logger.info("path 7 done")
        line = stream.getvalue()
        self.assertIn("audit-worker-2", line)
        self.assertIn(": path 7 done", line)
        # Tag and module joined with ``/`` — operator's mental
        # parser is "INFO [<tag-or-module-or-tag/module>]:"
        self.assertRegex(line, r"INFO \[audit-worker-2/[^\]]+\]: ")

    def test_tag_only_when_module_unknown(self) -> None:
        # Edge case: if ``_caller_module`` returns "" we still
        # emit the tag, just without the trailing ``/<module>``.
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, tag="w0")
        # Bypass _emit's caller-frame walk by stubbing it directly.
        logger._caller_module = lambda: ""  # type: ignore[assignment]
        logger.info("anonymous")
        line = stream.getvalue()
        self.assertIn("INFO [w0]:", line)

    def test_tag_propagates_through_from_config(self) -> None:
        # ``RuntimeLogger.from_config`` accepts ``tag=`` and threads
        # it through. Smoke-tests on a frozen-config-like stub.
        class _StubLLM:
            api_key = "sk-secret"
            def api_keys(self): return ("sk-secret",)
        class _StubLogging:
            level = "debug"
            file = None
        class _StubGraphdb:
            password = ""
        class _StubConfig:
            llm = _StubLLM()
            logging = _StubLogging()
            graphdb = _StubGraphdb()
            coder = None

        stream = io.StringIO()
        logger = RuntimeLogger.from_config(
            _StubConfig(), stream=stream, tag="audit-worker-7"
        )
        self.assertEqual(logger.tag, "audit-worker-7")
        logger.info("ready")
        self.assertIn("audit-worker-7", stream.getvalue())


class RuntimeLoggerLogFileConcurrencyTests(unittest.TestCase):
    """N concurrent emits to a shared ``log_file`` must not tear
    individual lines. POSIX ``O_APPEND`` is atomic per write(2)
    when payload < PIPE_BUF (typically 4096B); a tagged audit
    log line is well under that. This test verifies lines stay
    intact under concurrent emits from many threads — the
    multi-process case relies on the same OS-level guarantee."""

    def test_no_torn_lines_under_concurrent_emit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_file = Path(tmp) / "audit.log"
            stream = io.StringIO()
            loggers = [
                RuntimeLogger(
                    level="info",
                    stream=stream,
                    log_file=log_file,
                    tag=f"audit-worker-{i}",
                )
                for i in range(4)
            ]

            barrier = threading.Barrier(len(loggers))

            def _spam(logger: RuntimeLogger, count: int) -> None:
                barrier.wait()
                for k in range(count):
                    logger.info(f"event {k} from {logger.tag}")

            threads = [
                threading.Thread(target=_spam, args=(logger, 25))
                for logger in loggers
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=5.0)

            content = log_file.read_text(encoding="utf-8")
            lines = content.splitlines()
            self.assertEqual(len(lines), 4 * 25)
            # Every line begins with "INFO [" — no torn fragments.
            for line in lines:
                self.assertTrue(
                    line.startswith("INFO ["),
                    f"torn line detected: {line!r}",
                )
            # Each line's bracket prefix carries exactly one tag —
            # no "two tags mashed together" indicating an
            # interleave inside one line. Extract the bracket
            # prefix (between the first ``[`` and the matching
            # ``]``) and count tags there.
            import re
            for line in lines:
                m = re.match(r"INFO \[([^\]]+)\]: ", line)
                self.assertIsNotNone(m, f"unparsable line: {line!r}")
                bracket = m.group(1)
                tag_hits = [
                    f"audit-worker-{i}" for i in range(4)
                    if f"audit-worker-{i}" in bracket
                ]
                self.assertEqual(
                    len(tag_hits), 1,
                    f"bracket prefix carries multiple tags: "
                    f"{bracket!r} (line: {line!r})",
                )


class RuntimeLoggerFlushTests(unittest.TestCase):
    """Each emit must flush the stream so subprocess audit workers
    don't leave debug output stranded in stderr buffer when stderr
    is redirected to a file/pipe (default Python behaviour is full
    buffering, 8 KB)."""

    def test_emit_flushes_stream_after_each_write(self) -> None:
        class _RecordingStream:
            def __init__(self) -> None:
                self.events: list[str] = []
            def write(self, data: str) -> int:
                self.events.append(f"write:{data}")
                return len(data)
            def flush(self) -> None:
                self.events.append("flush")

        stream = _RecordingStream()
        logger = RuntimeLogger(level="debug", stream=stream)
        logger.info("first")
        logger.info("second")

        # Each emit pair: one write followed by one flush, in
        # that order, no batching.
        flush_indices = [
            i for i, e in enumerate(stream.events) if e == "flush"
        ]
        write_indices = [
            i for i, e in enumerate(stream.events) if e.startswith("write:")
        ]
        self.assertEqual(len(write_indices), 2)
        self.assertEqual(len(flush_indices), 2)
        # write[0] < flush[0] < write[1] < flush[1] — no
        # write-write-flush-flush batching.
        self.assertLess(write_indices[0], flush_indices[0])
        self.assertLess(flush_indices[0], write_indices[1])
        self.assertLess(write_indices[1], flush_indices[1])

    def test_emit_tolerates_streams_without_flush(self) -> None:
        # Some custom stream wrappers don't expose ``flush``; the
        # logger must not blow up. ``io.StringIO`` does have flush
        # so we use a hand-rolled stub.
        class _NoFlushStream:
            def __init__(self) -> None:
                self.buf = ""
            def write(self, data: str) -> int:
                self.buf += data
                return len(data)

        stream = _NoFlushStream()
        logger = RuntimeLogger(level="info", stream=stream)
        logger.info("ok")  # must not raise
        self.assertIn("INFO", stream.buf)

    def test_emit_tolerates_flush_raising(self) -> None:
        # Flush failure must not propagate — losing one log flush
        # is preferable to crashing the audit.
        class _FlushErrStream:
            def __init__(self) -> None:
                self.buf = ""
            def write(self, data: str) -> int:
                self.buf += data
                return len(data)
            def flush(self) -> None:
                raise OSError("simulated flush failure")

        stream = _FlushErrStream()
        logger = RuntimeLogger(level="info", stream=stream)
        logger.info("ok")  # must not raise
        self.assertIn("INFO", stream.buf)


if __name__ == "__main__":
    unittest.main()
