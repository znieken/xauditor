from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.coder import (
    ClaudeCodeCliTransport,
    CoderAgent,
    CoderDispatcher,
    CoderResult,
    build_cli_argv,
    parse_coder_response,
    render_coder_payload,
    scrub_environment,
)
from xauditor.config import CoderConfig
from xauditor.models import (
    CODER_STATUS_FAIL,
    CODER_STATUS_INCONCLUSIVE,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_VERIFIED,
    CoderEvidence,
)


def _write_python_script(path: Path, body: str) -> None:
    """Write a Python script with the current interpreter's shebang.

    ``body`` is fed through ``textwrap.dedent`` so callers can use a plain
    triple-quoted string for the script content.
    """

    contents = f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip()
    path.write_text(contents, encoding="utf-8")
    mode = path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod(mode)


class BuildCliArgvTests(unittest.TestCase):
    def test_minimal(self) -> None:
        # ``-p`` is always present — the worker pipes stdin and reads
        # stdout, never the interactive UI.
        self.assertEqual(build_cli_argv(("claude",)), ["claude", "-p"])

    def test_with_effort(self) -> None:
        # Only ``--effort`` is a real flag in our build path. Model /
        # endpoint / API key are env-only (ANTHROPIC_MODEL,
        # ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY) — see ENV_ALLOWLIST.
        self.assertEqual(
            build_cli_argv(("claude",), thinking_effort="high"),
            ["claude", "-p", "--effort", "high"],
        )

    def test_does_not_accept_model_kwargs(self) -> None:
        """Regression: callers MUST inject claude's three env vars
        (ANTHROPIC_MODEL, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY) via
        the subprocess env. Passing them as ``build_cli_argv`` kwargs
        used to be how it worked but ALL THREE were going onto wrong
        flags claude 2.x doesn't accept (``--model-name`` /
        ``--model-url`` / ANTHROPIC_API_KEY-as-flag). The kwargs are
        removed; the test guards against accidental re-introduction."""

        for kwarg in ("model_name", "model_url", "model_api_key"):
            with self.subTest(kwarg=kwarg):
                with self.assertRaises(TypeError):
                    build_cli_argv(("claude",), **{kwarg: "x"})

    def test_list_form_prefix_preserved(self) -> None:
        argv = build_cli_argv(
            ("wrapper", "claude", "--auto"),
            thinking_effort="medium",
        )
        self.assertEqual(
            argv,
            ["wrapper", "claude", "--auto", "-p", "--effort", "medium"],
        )


class ScrubEnvironmentTests(unittest.TestCase):
    def test_allowlisted_vars_pass_through(self) -> None:
        parent = {
            "PATH": "/usr/bin",
            "HOME": "/home/me",
            "USER": "me",
            "LANG": "C",
            "TERM": "xterm",
        }
        self.assertEqual(scrub_environment(parent), parent)

    def test_xauditor_vars_dropped(self) -> None:
        parent = {
            "PATH": "/x",
            "XAUDITOR_LLM_PROVIDERS_DEFAULT_API_KEY": "leak",
            "XAUDITOR_GRAPHDB_PASSWORD": "leak",
        }
        scrubbed = scrub_environment(parent)
        self.assertEqual(scrubbed, {"PATH": "/x"})

    def test_claude_prefix_passes_through(self) -> None:
        parent = {
            "PATH": "/x",
            "CLAUDE_API_KEY": "claude-key",
            "CLAUDE_MODEL": "sonnet",
            "OTHER": "drop",
        }
        scrubbed = scrub_environment(parent)
        self.assertIn("CLAUDE_API_KEY", scrubbed)
        self.assertIn("CLAUDE_MODEL", scrubbed)
        self.assertNotIn("OTHER", scrubbed)


class ParseCoderResponseTests(unittest.TestCase):
    def test_happy_path_with_evidence(self) -> None:
        stdout = json.dumps(
            {
                "status": "verified",
                "analysis": "ok",
                "reason": "sanitizer present",
                "call_chain_evidence": [
                    {
                        "file_path": "src/foo.py",
                        "function_name": "sanitize",
                        "snippet": "def sanitize(): ...",
                        "language": "python",
                        "role": "sanitizer",
                    }
                ],
            }
        )
        result = parse_coder_response(stdout=stdout, stderr="", exit_code=0, duration_ms=12)
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertEqual(result.reason, "sanitizer present")
        self.assertEqual(result.duration_ms, 12)
        self.assertEqual(result.cli_exit_code, 0)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].role, "sanitizer")

    def test_no_json_in_stdout_yields_inconclusive(self) -> None:
        # Pure prose / refusal: no '{' anywhere → "no JSON object".
        result = parse_coder_response(
            stdout="I cannot help with this request.", stderr="", exit_code=0, duration_ms=1,
        )
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("no JSON object", result.reason)
        self.assertIn("cannot help", result.analysis)

    def test_markdown_fenced_json_is_extracted(self) -> None:
        """Regression: claude-code 2.x often wraps the JSON object
        in ```json ... ``` fences despite the system prompt saying
        not to. The lenient parser SHALL strip fences before json.loads."""

        stdout = (
            "```json\n"
            '{"status": "Verified", "analysis": "ok", "reason": "checked"}\n'
            "```\n"
        )
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=10,
        )
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertEqual(result.reason, "checked")
        self.assertEqual(result.analysis, "ok")

    def test_preamble_before_json_is_tolerated(self) -> None:
        """Claude sometimes prefaces with 'Here is the analysis:'.
        Brace-walking finds the JSON regardless."""

        stdout = (
            "Here is the analysis:\n\n"
            '{"status": "Not Verified", "reason": "input is sanitised upstream"}\n\n'
            "Let me know if you need clarification."
        )
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=10,
        )
        self.assertEqual(result.status, CODER_STATUS_NOT_VERIFIED)
        self.assertEqual(result.reason, "input is sanitised upstream")

    def test_nested_braces_in_strings_do_not_break_extraction(self) -> None:
        """The brace-walker must respect string literals so embedded
        '{' / '}' inside snippets don't unbalance the count."""

        # The "snippet" contains a '}' inside a string; the walker
        # must NOT treat it as a real closing brace.
        stdout = (
            '{"status": "Verified", "analysis": "found `if (x > 0) { return; }`",'
            ' "reason": "ok"}'
        )
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=10,
        )
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertIn("if (x > 0)", result.analysis)

    def test_malformed_json_after_extraction_yields_inconclusive(self) -> None:
        """Extracted candidate that still fails json.loads even after the
        typo-repair pass → distinct reason 'malformed JSON' (vs 'no JSON
        object' for refusals). Operators reading the portal can tell the
        cases apart."""

        stdout = '{"status": "Verified", "analysis": "double trailing comma",,}'
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=1,
        )
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("malformed JSON", result.reason)

    def test_missing_comma_between_keys_is_recovered(self) -> None:
        """Regression: claude-code emits a JSON object missing a comma
        between two key-value pairs. The structural-repair fallback
        injects the missing comma so the verdict survives instead of
        surfacing as 'transport error: malformed JSON'.

        Observed shape (real run, 2026-04-29) — missing comma after a
        ``"snippet"`` value in ``call_chain_evidence``:

            "snippet": "..."
                  "language": "cpp",
        """

        stdout = (
            '{"status": "Verified",\n'
            ' "analysis": "ok",\n'
            ' "reason": "checked",\n'
            ' "call_chain_evidence": [\n'
            '   {\n'
            '     "file_path": "src/foo.cc",\n'
            '     "snippet": "void f() {}"\n'  # <-- MISSING comma here
            '     "language": "cpp",\n'
            '     "role": "definition"\n'
            '   }\n'
            ' ]}\n'
        )
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=10,
        )
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertEqual(result.reason, "checked")
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].language, "cpp")

    def test_trailing_comma_before_close_is_recovered(self) -> None:
        """Single trailing comma before ``}`` / ``]`` is the second-most
        common LLM JSON typo. Repair pass drops it cleanly."""

        stdout = (
            '{"status": "Verified", "analysis": "ok", "reason": "checked",'
            ' "call_chain_evidence": [{"file_path": "a", "snippet": "b",}]}'
        )
        result = parse_coder_response(
            stdout=stdout, stderr="", exit_code=0, duration_ms=10,
        )
        self.assertEqual(result.status, CODER_STATUS_VERIFIED)
        self.assertEqual(result.reason, "checked")

    def test_non_zero_exit_yields_inconclusive(self) -> None:
        result = parse_coder_response(
            stdout="diagnostic output",
            stderr="boom",
            exit_code=2,
            duration_ms=5,
        )
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertEqual(result.reason, "transport error: exit 2")
        self.assertEqual(result.cli_exit_code, 2)
        self.assertIn("boom", result.cli_stderr or "")

    def test_empty_stdout_yields_inconclusive(self) -> None:
        result = parse_coder_response(stdout="", stderr="", exit_code=0, duration_ms=1)
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("empty stdout", result.reason)

    def test_status_synonyms_normalised(self) -> None:
        for raw, expected in [
            ("false_positive", CODER_STATUS_NOT_VERIFIED),
            ("uncertain", CODER_STATUS_INCONCLUSIVE),
            ("VERIFIED", CODER_STATUS_VERIFIED),
        ]:
            stdout = json.dumps({"status": raw, "analysis": "", "reason": ""})
            result = parse_coder_response(stdout=stdout, stderr="", exit_code=0, duration_ms=1)
            self.assertEqual(result.status, expected, raw)

    def test_evidence_without_file_path_dropped(self) -> None:
        stdout = json.dumps(
            {
                "status": "verified",
                "analysis": "",
                "reason": "",
                "call_chain_evidence": [
                    {"file_path": "src/a.py", "snippet": "a", "role": "definition"},
                    {"file_path": "", "snippet": "missing path"},
                    "not a dict",
                ],
            }
        )
        result = parse_coder_response(stdout=stdout, stderr="", exit_code=0, duration_ms=1)
        self.assertEqual(len(result.evidence), 1)
        self.assertEqual(result.evidence[0].file_path, "src/a.py")


class RenderCoderPayloadTests(unittest.TestCase):
    def test_includes_upstream_blocks(self) -> None:
        payload = render_coder_payload(
            finding={"finding_id": "F-1", "finding_name": "x"},
            path_context={
                "call_chain": ["A", "B"],
                "function_definitions": [{"name": "A"}],
                "referenced_symbols": [],
            },
            analyzer={"status": "candidate"},
            exploitation={"status": "uncertain"},
            validator={"status": "Valid"},
            validator_debate=None,
        )
        self.assertEqual(payload["finding"]["finding_id"], "F-1")
        self.assertEqual(payload["path"]["call_chain"], ["A", "B"])
        self.assertEqual(payload["upstream"]["analyzer"], {"status": "candidate"})
        self.assertIsNone(payload["upstream"]["validator_debate"])
        self.assertIn("system_prompt", payload)


class ClaudeCodeCliTransportTests(unittest.TestCase):
    def _config(self, cli_path: Path, *, timeout: int = 5, **extra: object) -> CoderConfig:
        defaults = {
            "enabled": True,
            "cli_command": (str(cli_path),),
            "concurrency": 1,
            "request_timeout_seconds": timeout,
        }
        defaults.update(extra)
        return CoderConfig(**defaults)  # type: ignore[arg-type]

    def test_happy_path_returns_verdict(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            _write_python_script(
                cli_path,
                """
                import json, sys
                _ = sys.stdin.read()
                json.dump(
                    {
                        "status": "verified",
                        "analysis": "all good",
                        "reason": "sanitizer at src/foo.py:42",
                        "call_chain_evidence": [],
                    },
                    sys.stdout,
                )
                """,
            )
            config = self._config(cli_path)
            transport = ClaudeCodeCliTransport(config, env={"PATH": "/usr/bin"})
            result = transport.invoke({"finding": {"finding_id": "F-1"}})
            self.assertEqual(result.status, CODER_STATUS_VERIFIED)
            self.assertEqual(result.cli_exit_code, 0)
            self.assertIsNotNone(result.duration_ms)

    def test_non_zero_exit_returns_inconclusive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            _write_python_script(
                cli_path,
                """
                import sys
                sys.stderr.write("backend down\\n")
                sys.exit(7)
                """,
            )
            config = self._config(cli_path)
            transport = ClaudeCodeCliTransport(config, env={"PATH": "/usr/bin"})
            result = transport.invoke({})
            self.assertEqual(result.status, CODER_STATUS_FAIL)
            self.assertEqual(result.cli_exit_code, 7)
            self.assertIn("backend down", result.cli_stderr or "")
            self.assertIn("exit 7", result.reason)

    def test_timeout_terminates_subprocess(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            _write_python_script(
                cli_path,
                """
                import time
                time.sleep(30)
                """,
            )
            config = self._config(cli_path, timeout=1)
            transport = ClaudeCodeCliTransport(config, env={"PATH": "/usr/bin"})
            start = time.monotonic()
            result = transport.invoke({})
            elapsed = time.monotonic() - start
            self.assertLess(elapsed, 6.0, "timeout should terminate well under 6s")
            self.assertEqual(result.status, CODER_STATUS_FAIL)
            self.assertIn("timed out", result.reason)

    def test_missing_binary_returns_inconclusive(self) -> None:
        config = CoderConfig(
            enabled=True,
            cli_command=("/no/such/path/claude",),
            concurrency=1,
            request_timeout_seconds=5,
        )
        transport = ClaudeCodeCliTransport(config, env={"PATH": "/usr/bin"})
        result = transport.invoke({})
        self.assertEqual(result.status, CODER_STATUS_FAIL)
        self.assertIn("cli not found", result.reason)

    def test_subprocess_environment_is_scrubbed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            env_dump_path = Path(tmp) / "env-dump.txt"
            _write_python_script(
                cli_path,
                f"""
                import json, os, sys
                _ = sys.stdin.read()
                env_keys = sorted(os.environ.keys())
                with open({str(env_dump_path)!r}, "w", encoding="utf-8") as fh:
                    fh.write("\\n".join(env_keys))
                json.dump({{"status": "verified", "analysis": "ok", "reason": "ok", "call_chain_evidence": []}}, sys.stdout)
                """,
            )
            parent_env = {
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "HOME": os.environ.get("HOME", "/tmp"),
                "XAUDITOR_LLM_PROVIDERS_DEFAULT_API_KEY": "leak-me",
                "CLAUDE_API_KEY": "claude-key",
                "RANDOM_LEAK": "no",
            }
            config = self._config(cli_path)
            transport = ClaudeCodeCliTransport(config, env=parent_env)
            result = transport.invoke({})
            self.assertEqual(result.status, CODER_STATUS_VERIFIED)
            captured_keys = env_dump_path.read_text(encoding="utf-8").splitlines()
            self.assertIn("PATH", captured_keys)
            self.assertIn("CLAUDE_API_KEY", captured_keys)
            self.assertNotIn("XAUDITOR_LLM_PROVIDERS_DEFAULT_API_KEY", captured_keys)
            self.assertNotIn("RANDOM_LEAK", captured_keys)

    def test_anthropic_api_key_passes_through_when_unset_in_config(self) -> None:
        """ANTHROPIC_API_KEY from the parent env reaches the CLI by default."""

        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            value_dump = Path(tmp) / "key.txt"
            _write_python_script(
                cli_path,
                f"""
                import json, os, sys
                _ = sys.stdin.read()
                with open({str(value_dump)!r}, "w", encoding="utf-8") as fh:
                    fh.write(os.environ.get("ANTHROPIC_API_KEY", "<unset>"))
                json.dump({{"status": "verified", "analysis": "", "reason": "", "call_chain_evidence": []}}, sys.stdout)
                """,
            )
            parent_env = {
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ANTHROPIC_API_KEY": "shell-key",
            }
            config = self._config(cli_path)
            transport = ClaudeCodeCliTransport(config, env=parent_env)
            result = transport.invoke({})
            self.assertEqual(result.status, CODER_STATUS_VERIFIED)
            self.assertEqual(value_dump.read_text(encoding="utf-8"), "shell-key")

    def test_configured_model_api_key_overrides_parent_env(self) -> None:
        """``coder.model_api_key`` takes precedence over an inherited value."""

        with tempfile.TemporaryDirectory() as tmp:
            cli_path = Path(tmp) / "fake-claude"
            value_dump = Path(tmp) / "key.txt"
            _write_python_script(
                cli_path,
                f"""
                import json, os, sys
                _ = sys.stdin.read()
                with open({str(value_dump)!r}, "w", encoding="utf-8") as fh:
                    fh.write(os.environ.get("ANTHROPIC_API_KEY", "<unset>"))
                json.dump({{"status": "verified", "analysis": "", "reason": "", "call_chain_evidence": []}}, sys.stdout)
                """,
            )
            parent_env = {
                "PATH": os.environ.get("PATH", "/usr/bin"),
                "ANTHROPIC_API_KEY": "shell-key",
            }
            config = self._config(cli_path, model_api_key="config-key")
            transport = ClaudeCodeCliTransport(config, env=parent_env)
            result = transport.invoke({})
            self.assertEqual(result.status, CODER_STATUS_VERIFIED)
            self.assertEqual(value_dump.read_text(encoding="utf-8"), "config-key")


class FakeTransport:
    """In-process transport for dispatcher tests."""

    def __init__(self, *, latency: float = 0.0, results: dict[str, CoderResult] | None = None) -> None:
        self.latency = latency
        self.results = results or {}
        self.invocations: list[dict] = []
        self.lock = threading.Lock()

    def invoke(self, payload):  # type: ignore[no-untyped-def]
        if self.latency:
            time.sleep(self.latency)
        finding_id = payload.get("finding", {}).get("finding_id", "")
        with self.lock:
            self.invocations.append(dict(payload))
        return self.results.get(
            finding_id,
            CoderResult(
                status=CODER_STATUS_VERIFIED,
                analysis="ok",
                reason="default",
                evidence=(CoderEvidence("a.py", None, "x", "python", "supporting"),),
                cli_exit_code=0,
                duration_ms=1,
            ),
        )


class CoderDispatcherTests(unittest.TestCase):
    def test_submit_and_drain_returns_results(self) -> None:
        transport = FakeTransport()
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=2)
        try:
            for fid in ("F-1", "F-2", "F-3"):
                dispatcher.submit(fid, {"finding": {"finding_id": fid}})
            settled = dispatcher.drain(timeout=5)
            self.assertEqual({fid for fid, _ in settled}, {"F-1", "F-2", "F-3"})
            for _fid, result in settled:
                self.assertEqual(result.status, CODER_STATUS_VERIFIED)
            self.assertFalse(dispatcher.has_pending())
        finally:
            dispatcher.shutdown(wait=True)

    def test_poll_completed_only_returns_done_tasks(self) -> None:
        transport = FakeTransport(latency=0.2)
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=2)
        try:
            for fid in ("F-1", "F-2"):
                dispatcher.submit(fid, {"finding": {"finding_id": fid}})
            time.sleep(0.05)
            # Likely still running.
            mid = dispatcher.poll_completed()
            self.assertEqual(mid, [])
            # Wait for them to settle.
            settled = dispatcher.drain(timeout=5)
            self.assertEqual({fid for fid, _ in settled}, {"F-1", "F-2"})
        finally:
            dispatcher.shutdown(wait=True)

    def test_duplicate_submit_is_ignored(self) -> None:
        transport = FakeTransport()
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=1)
        try:
            dispatcher.submit("F-1", {"finding": {"finding_id": "F-1"}})
            dispatcher.submit("F-1", {"finding": {"finding_id": "F-1"}})
            settled = dispatcher.drain(timeout=5)
            self.assertEqual(len(settled), 1)
        finally:
            dispatcher.shutdown(wait=True)

    def test_cancel_all_removes_unstarted_tasks(self) -> None:
        # With concurrency=1 and a slow task, the second submit can't start
        # until the first finishes — so cancelling immediately should drop
        # the queued task.
        transport = FakeTransport(latency=0.5)
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=1)
        try:
            dispatcher.submit("F-running", {"finding": {"finding_id": "F-running"}})
            dispatcher.submit("F-queued", {"finding": {"finding_id": "F-queued"}})
            time.sleep(0.05)
            cancelled = dispatcher.cancel_all()
            self.assertIn("F-queued", cancelled)
            # Drain the still-running task
            dispatcher.drain(timeout=5)
        finally:
            dispatcher.shutdown(wait=True)

    def test_drain_invokes_callback_per_settlement(self) -> None:
        transport = FakeTransport()
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=2)
        callbacks: list[str] = []
        try:
            for fid in ("F-1", "F-2"):
                dispatcher.submit(fid, {"finding": {"finding_id": fid}})
            dispatcher.drain(
                timeout=5,
                on_settled=lambda finding_id, _result: callbacks.append(finding_id),
            )
            self.assertEqual(set(callbacks), {"F-1", "F-2"})
        finally:
            dispatcher.shutdown(wait=True)

    def test_submit_on_settled_fires_without_drain(self) -> None:
        """Per-finding streaming: ``submit(on_settled=...)`` fires the
        callback the moment the future settles — no main-thread poll
        required. Whether the callback runs on the worker thread or
        synchronously in ``submit`` (stdlib behaviour when the future
        is already done) is an implementation detail; the contract we
        care about is "no drain needed"."""

        import threading
        transport = FakeTransport(latency=0.05)  # latency so worker isn't done before callback registration
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=2)
        observed: list[str] = []
        completed = threading.Event()

        def on_settled(finding_id: str, _result):
            observed.append(finding_id)
            if len(observed) == 2:
                completed.set()

        try:
            for fid in ("F-1", "F-2"):
                dispatcher.submit(
                    fid, {"finding": {"finding_id": fid}}, on_settled=on_settled,
                )
            # Callbacks fire on their own — we never call drain or
            # poll_completed from the test.
            self.assertTrue(
                completed.wait(timeout=5),
                "on_settled callbacks did not fire within 5s without drain",
            )
            self.assertEqual(set(observed), {"F-1", "F-2"})
            # Pending map is drained by `_fire_settled` itself.
            self.assertFalse(dispatcher.has_pending())
            # And `poll_completed` finds nothing left, because the
            # callback popped each task as it ran.
            self.assertEqual(dispatcher.poll_completed(), [])
        finally:
            dispatcher.shutdown(wait=True)

    def test_submit_on_settled_swallows_callback_exceptions(self) -> None:
        """Callback errors must not break the worker — log + drop."""

        transport = FakeTransport()
        agent = CoderAgent(transport=transport)
        dispatcher = CoderDispatcher(agent=agent, concurrency=1)
        observed: list[str] = []
        try:
            def on_settled_bad(finding_id, _result):
                observed.append(finding_id)
                raise RuntimeError("simulated sink failure")
            dispatcher.submit(
                "F-bad", {"finding": {"finding_id": "F-bad"}},
                on_settled=on_settled_bad,
            )
            # Sleep just long enough for the worker to fire the callback.
            for _ in range(50):
                if observed:
                    break
                time.sleep(0.05)
            self.assertEqual(observed, ["F-bad"])
            # Dispatcher recovered: a follow-up submit + drain still works.
            results: list[tuple[str, object]] = []
            dispatcher.submit(
                "F-good", {"finding": {"finding_id": "F-good"}},
                on_settled=lambda fid, r: results.append((fid, r)),
            )
            for _ in range(50):
                if results:
                    break
                time.sleep(0.05)
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0][0], "F-good")
        finally:
            dispatcher.shutdown(wait=True)


if __name__ == "__main__":
    unittest.main()
