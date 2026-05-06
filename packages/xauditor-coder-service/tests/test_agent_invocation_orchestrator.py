"""Direct tests of run_agent_invocation against a stub claude binary.

The stub binary is a small Python script that reads JSON from stdin
and writes one of several scripted responses to stdout. This exercises
the full subprocess + JSON parsing + schema validation path without
depending on the real ``claude`` binary.

Endpoint-level tests live in ``test_agent_invocations_endpoint.py``;
they bypass the orchestrator entirely.
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor_coder_service.agent_invocation import (
    AgentInvocationConfig,
    AgentInvocationRequest,
    run_agent_invocation,
)


def _write_stub_binary(tmpdir: Path, *, body: str) -> str:
    """Write an executable Python stub at tmpdir/claude_stub and return its path.

    ``body`` is the function body of a Python script that will be
    invoked as the claude binary. Available locals at body time:
    ``stdin_text`` (the payload piped in), ``argv`` (the full argv
    list including flags).
    """

    preamble = (
        f"#!{sys.executable}\n"
        "import json, sys, os\n"
        "argv = sys.argv\n"
        "stdin_text = sys.stdin.read()\n"
    )
    stub_path = tmpdir / "claude_stub"
    stub_path.write_text(preamble + body + "\n")
    stub_path.chmod(0o755)
    return str(stub_path)


def _make_request(**overrides) -> AgentInvocationRequest:
    fields = {
        "system_prompt": "You are an analyzer.",
        "user_payload": {"key": "value"},
        "response_schema": {
            "type": "object",
            "properties": {"verdict": {"type": "string"}},
            "required": ["verdict"],
        },
        "timeout_seconds": 30,
        "project": None,
    }
    fields.update(overrides)
    return AgentInvocationRequest(**fields)


def _run(coro):
    return asyncio.run(coro)


class HappyPathTests(unittest.TestCase):
    def test_envelope_with_result_key_is_extracted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body='print(json.dumps({"result": {"verdict": "Valid"}}))',
            )
            req = _make_request()
            resp = _run(
                run_agent_invocation(
                    req,
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertFalse(resp.fell_back)
        self.assertIsNone(resp.fallback_reason)
        self.assertEqual(resp.final_answer, {"verdict": "Valid"})
        self.assertGreaterEqual(resp.elapsed_seconds, 0.0)

    def test_structured_output_preferred_over_result_text(self) -> None:
        # Claude Code 2.1.x with `--json-schema` puts the schema-conforming
        # payload under `structured_output` and the conversational summary
        # under `result` (a string). Older versions inlined the dict at
        # `result`. The extractor must prefer `structured_output` so the
        # newer envelope shape doesn't trip schema validation.
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    'print(json.dumps({'
                    '"result": "Done.",'
                    ' "structured_output": {"verdict": "Valid"}'
                    '}))'
                ),
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertFalse(resp.fell_back)
        self.assertIsNone(resp.fallback_reason)
        self.assertEqual(resp.final_answer, {"verdict": "Valid"})

    def test_envelope_top_level_dict_treated_as_final_answer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body='print(json.dumps({"verdict": "Valid"}))',
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertFalse(resp.fell_back)
        self.assertEqual(resp.final_answer, {"verdict": "Valid"})

    def test_transcript_extracted_from_messages_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    'print(json.dumps({'
                    '"result": {"verdict": "Valid"}, '
                    '"messages": ['
                    '{"tool": "Read", "input": {"path": "x.py"}, "output": "ok"}'
                    "]"
                    "}))"
                ),
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertEqual(len(resp.transcript), 1)
        self.assertEqual(resp.transcript[0]["tool"], "Read")


class FallbackTests(unittest.TestCase):
    def test_subprocess_exit_nonzero_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body='import sys; sys.stderr.write("boom"); sys.exit(7)',
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertTrue(resp.fallback_reason.startswith("subprocess_exit_7"))
        self.assertIsNone(resp.final_answer)

    def test_empty_stdout_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(tmpdir, body="pass")  # no output
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertEqual(resp.fallback_reason, "empty_stdout")

    def test_malformed_json_falls_back(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir, body='print("not valid json {{{")',
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertTrue(resp.fallback_reason.startswith("malformed_response"))

    def test_schema_violating_response_falls_back_but_preserves_final_answer(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    'print(json.dumps({"result": {"wrong_key": "value"}}))'
                ),
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertTrue(
            resp.fallback_reason.startswith("schema_validation_failed")
        )
        # final_answer is preserved so the caller can inspect what claude
        # actually returned.
        self.assertEqual(resp.final_answer, {"wrong_key": "value"})

    def test_invalid_response_schema_short_circuits_before_subprocess(
        self,
    ) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Stub that would fail the test if invoked (records via marker
            # file existence).
            marker = tmpdir / "stub_was_called"
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    f'open({str(marker)!r}, "w").close(); '
                    'print(json.dumps({"verdict": "Valid"}))'
                ),
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(
                        response_schema={"type": "object", "properties": "not-a-dict"}
                    ),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertTrue(
            resp.fallback_reason.startswith("invalid_response_schema")
        )
        self.assertFalse(
            marker.exists(),
            "stub binary should not have been invoked when schema is invalid",
        )

    def test_missing_binary_falls_back_with_spawn_failed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(
                        cli_path=str(Path(tmp) / "does_not_exist")
                    ),
                    project_dir=tmp,
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertIn("subprocess_spawn_failed", resp.fallback_reason)


class TimeoutTests(unittest.TestCase):
    def test_subprocess_hang_triggers_timeout_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            cli = _write_stub_binary(
                tmpdir,
                body="import time; time.sleep(60); print('{}')",
            )
            # timeout_seconds has a min of 10 (Pydantic Field constraint),
            # so we test the timeout path with the smallest allowed value.
            # The stub will sleep through it.
            resp = _run(
                run_agent_invocation(
                    _make_request(timeout_seconds=10),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
        self.assertTrue(resp.fell_back)
        self.assertTrue(resp.fallback_reason.startswith("timeout"))


class CancellationTests(unittest.TestCase):
    def test_cancel_event_triggers_cancellation_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            # Stub sleeps 5s; we set the cancel event after 0.5s.
            cli = _write_stub_binary(
                tmpdir,
                body="import time; time.sleep(5); print('{}')",
            )
            cancel_event = asyncio.Event()

            async def _scenario():
                async def _delayed_cancel():
                    await asyncio.sleep(0.5)
                    cancel_event.set()

                cancel_task = asyncio.create_task(_delayed_cancel())
                resp = await run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                    cancel_event=cancel_event,
                )
                cancel_task.cancel()
                try:
                    await cancel_task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
                return resp

            resp = _run(_scenario())
        self.assertTrue(resp.fell_back)
        self.assertTrue(resp.fallback_reason.startswith("cancelled"))


class CliInvocationTests(unittest.TestCase):
    """Verify the orchestrator builds the real ``claude`` argv shape."""

    def test_argv_uses_real_claude_flags(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            argv_dump = tmpdir / "argv_dump.json"
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    f'open({str(argv_dump)!r}, "w").write(json.dumps(argv));'
                    'print(json.dumps({"verdict": "Valid"}))'
                ),
            )
            resp = _run(
                run_agent_invocation(
                    _make_request(),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
            self.assertFalse(resp.fell_back)
            import json

            argv = json.loads(argv_dump.read_text())
        self.assertEqual(argv[1], "-p")
        self.assertIn("--output-format", argv)
        self.assertIn("json", argv)
        self.assertIn("--json-schema", argv)
        self.assertIn("--append-system-prompt", argv)
        self.assertIn("--add-dir", argv)
        # No --tool-grants, --max-tool-calls, --max-budget-usd, --timeout
        # — the design explicitly drops these.
        for forbidden in (
            "--tool-grants",
            "--max-tool-calls",
            "--max-budget-usd",
            "--timeout",
            "--allowed-tools",
        ):
            self.assertNotIn(
                forbidden,
                argv,
                f"argv should not contain {forbidden}: {argv}",
            )

    def test_user_payload_piped_via_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            stdin_dump = tmpdir / "stdin_dump.txt"
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    f'open({str(stdin_dump)!r}, "w").write(stdin_text);'
                    'print(json.dumps({"verdict": "Valid"}))'
                ),
            )
            payload = {"path_fingerprint": "f00", "data": [1, 2, 3]}
            resp = _run(
                run_agent_invocation(
                    _make_request(user_payload=payload),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=str(tmpdir),
                )
            )
            self.assertFalse(resp.fell_back)
            import json

            piped = json.loads(stdin_dump.read_text())
            self.assertEqual(piped, payload)


class ProjectRoutingTests(unittest.TestCase):
    """The orchestrator's argv reflects the supplied project_dir."""

    def test_project_dir_lands_in_add_dir_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            argv_dump = tmpdir / "argv_dump.json"
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    f'open({str(argv_dump)!r}, "w").write(json.dumps(argv));'
                    'print(json.dumps({"verdict": "Valid"}))'
                ),
            )
            project_dir = str(tmpdir)
            resp = _run(
                run_agent_invocation(
                    _make_request(project="alpha"),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir=project_dir,
                )
            )
            self.assertFalse(resp.fell_back)
            import json

            argv = json.loads(argv_dump.read_text())
            # --add-dir comes immediately after the flag in argv.
            i = argv.index("--add-dir")
            self.assertEqual(argv[i + 1], project_dir)

    def test_empty_project_dir_omits_add_dir_flag(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            tmpdir = Path(tmp)
            argv_dump = tmpdir / "argv_dump.json"
            cli = _write_stub_binary(
                tmpdir,
                body=(
                    f'open({str(argv_dump)!r}, "w").write(json.dumps(argv));'
                    'print(json.dumps({"verdict": "Valid"}))'
                ),
            )
            # Empty project_dir routes to the legacy single-repo
            # container — orchestrator skips --add-dir entirely.
            resp = _run(
                run_agent_invocation(
                    _make_request(project=None),
                    config=AgentInvocationConfig(cli_path=cli),
                    project_dir="",
                )
            )
            # Stub still runs because cwd defaults to the test process's
            # cwd; the file open uses absolute path so it works regardless.
            self.assertFalse(resp.fell_back)
            import json

            argv = json.loads(argv_dump.read_text())
            self.assertNotIn("--add-dir", argv)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
