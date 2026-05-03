from __future__ import annotations

import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.runtime_logging import RuntimeLogger


class RuntimeLoggingTests(unittest.TestCase):
    def test_debug_kv_formats_details_truncates_and_redacts(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-token",))

        logger.debug_kv(
            "Validator verdict",
            status="Valid",
            functions=("handler", "helper", "run_ls", "entrypoint", "adapter", "sink"),
            analysis=("reachable sink " * 20) + "secret-token",
            ignored=None,
        )

        rendered = stream.getvalue()
        self.assertIn("Validator verdict", rendered)
        self.assertRegex(rendered, r"^DEBUG \[[^\]]+\]: Validator verdict")
        self.assertIn("status=Valid", rendered)
        self.assertIn("functions=handler, helper, run_ls, entrypoint, adapter, ...", rendered)
        self.assertRegex(rendered, r"analysis=.*\.\.\.")
        self.assertNotIn("ignored=", rendered)
        self.assertNotIn("secret-token", rendered)

    def test_error_kv_emits_error_level_with_context_and_redaction(self) -> None:
        stream = io.StringIO()
        logger = RuntimeLogger(level="debug", stream=stream, redactions=("secret-token",))

        logger.error_kv(
            "Neo4j write failed",
            operation="neo4j.persist_graph",
            fingerprint="build-123",
            attempts=3,
            cause="dial tcp secret-token timeout",
        )

        rendered = stream.getvalue()
        self.assertRegex(rendered, r"^ERROR \[[^\]]+\]: Neo4j write failed")
        self.assertIn("operation=neo4j.persist_graph", rendered)
        self.assertIn("fingerprint=build-123", rendered)
        self.assertIn("attempts=3", rendered)
        self.assertNotIn("secret-token", rendered)


    def test_log_file_receives_redacted_output_when_configured(self) -> None:
        import tempfile

        stream = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "nested" / "xauditor.log"
            logger = RuntimeLogger(
                level="debug",
                stream=stream,
                redactions=("secret-token",),
                log_file=log_path,
            )

            logger.warning("PythonParser skipped foo.py: TabError (line 3, offset 8) secret-token")

            file_contents = log_path.read_text(encoding="utf-8")

        self.assertTrue(file_contents, "log file must receive runtime log output")
        self.assertIn("PythonParser skipped foo.py", file_contents)
        self.assertNotIn("secret-token", file_contents)
        self.assertIn("PythonParser skipped foo.py", stream.getvalue())
        self.assertNotIn("secret-token", stream.getvalue())

    def test_log_file_respects_level_filter(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "xauditor.log"
            logger = RuntimeLogger(level="warning", stream=io.StringIO(), log_file=log_path)
            logger.debug("debug detail")
            logger.warning("warned")

            contents = log_path.read_text(encoding="utf-8")

        self.assertIn("warned", contents)
        self.assertNotIn("debug detail", contents)


if __name__ == "__main__":
    unittest.main()
