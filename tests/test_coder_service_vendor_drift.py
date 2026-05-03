"""Drift detector for vendored helpers in xauditor-coder-service.

The microservice vendors a handful of constants + functions from
``xauditor.audit.coder`` and ``xauditor.models`` into
``xauditor_coder_service._vendored`` so the container has zero
runtime dep on ``xauditor`` (see ``align-coder-service-with-portal``).

The trade-off: two copies of the same logic. This test suite is the
guardrail. Every behaviour-relevant constant, dict, and function is
compared between the upstream and the vendored copy. If you change
one side, you MUST change the other in the same PR or this test will
fail.

Skipped automatically when ``xauditor_coder_service`` is not installed
(e.g. running the xauditor test suite in isolation in CI).
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

try:
    from xauditor_coder_service import _vendored as vendored
except ImportError:  # pragma: no cover - drift test only runs when service is installed
    vendored = None  # type: ignore[assignment]

from xauditor.audit.coder import (
    ENV_ALLOWLIST,
    ENV_ALLOWLIST_PREFIXES,
    build_cli_argv,
    parse_coder_response,
    scrub_environment,
)
from xauditor.integrations.coder.projects_walk import walk_projects
from xauditor.models import (
    CODER_STATUS_INCONCLUSIVE,
    CODER_STATUS_NOT_VERIFIED,
    CODER_STATUS_PENDING,
    CODER_STATUS_SKIPPED,
    CODER_STATUS_VERIFIED,
    CODER_STATUSES,
    normalize_coder_status,
)


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class StatusConstantsDriftTests(unittest.TestCase):
    def test_status_strings_match(self) -> None:
        self.assertEqual(vendored.CODER_STATUS_VERIFIED, CODER_STATUS_VERIFIED)
        self.assertEqual(vendored.CODER_STATUS_NOT_VERIFIED, CODER_STATUS_NOT_VERIFIED)
        self.assertEqual(vendored.CODER_STATUS_INCONCLUSIVE, CODER_STATUS_INCONCLUSIVE)
        self.assertEqual(vendored.CODER_STATUS_SKIPPED, CODER_STATUS_SKIPPED)
        self.assertEqual(vendored.CODER_STATUS_PENDING, CODER_STATUS_PENDING)
        self.assertEqual(tuple(vendored.CODER_STATUSES), tuple(CODER_STATUSES))


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class EnvAllowlistDriftTests(unittest.TestCase):
    def test_allowlist_matches(self) -> None:
        self.assertEqual(tuple(vendored.ENV_ALLOWLIST), tuple(ENV_ALLOWLIST))
        self.assertEqual(
            tuple(vendored.ENV_ALLOWLIST_PREFIXES), tuple(ENV_ALLOWLIST_PREFIXES)
        )

    def test_scrub_environment_behaviour_matches(self) -> None:
        for env in (
            {"PATH": "/usr/bin", "FOO": "ignored", "ANTHROPIC_API_KEY": "sk-..."},
            {"CLAUDE_CONFIG_HOME": "/x", "OTHER": "drop"},
            {},
            {"path": "lower", "PATH": "upper"},  # case-sensitive
        ):
            with self.subTest(env=env):
                self.assertEqual(vendored.scrub_environment(env), scrub_environment(env))


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class BuildCliArgvDriftTests(unittest.TestCase):
    def test_argv_matches_for_common_inputs(self) -> None:
        # Claude Code 2.x reads model / endpoint / API key from env
        # vars (ANTHROPIC_MODEL, ANTHROPIC_BASE_URL, ANTHROPIC_API_KEY)
        # — none of them go through ``build_cli_argv``. Only
        # ``thinking_effort`` (→ ``--effort`` flag) remains as a
        # build_cli_argv kwarg.
        cases = [
            (("claude",), {}),
            (("claude",), {"thinking_effort": "low"}),
            (("claude",), {"thinking_effort": "high"}),
            (("claude",), {"thinking_effort": "max"}),
            (("wrapper", "--my-flag", "v", "claude"), {}),
        ]
        for cli, kwargs in cases:
            with self.subTest(cli=cli, kwargs=kwargs):
                self.assertEqual(
                    vendored.build_cli_argv(cli, **kwargs),
                    build_cli_argv(cli, **kwargs),
                )


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class NormalizeStatusDriftTests(unittest.TestCase):
    def test_synonyms_normalise_identically(self) -> None:
        for raw in (
            None,
            "",
            "Verified",
            "verified",
            "true_positive",
            "false positive",
            "Inconclusive",
            "uncertain",
            "skipped",
            "in progress",
            "garbage",
            42,
            "  Verified  ",
        ):
            with self.subTest(raw=raw):
                self.assertEqual(
                    vendored.normalize_coder_status(raw),
                    normalize_coder_status(raw),
                )


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class ParseCoderResponseDriftTests(unittest.TestCase):
    """Parser equivalence: same status / reason / evidence shape on both sides.

    The vendored parser does NOT take a ``logger`` argument (the
    service has no RuntimeLogger). The xauditor side accepts one but
    we always call it WITHOUT one for this comparison so the redaction
    branch is identical (no-op).
    """

    def _assert_equiv(self, **kwargs) -> None:
        upstream = parse_coder_response(**kwargs)
        ours = vendored.parse_coder_response(**kwargs)
        self.assertEqual(ours.status, upstream.status)
        self.assertEqual(ours.reason, upstream.reason)
        self.assertEqual(ours.cli_exit_code, upstream.cli_exit_code)
        self.assertEqual(ours.cli_stderr, upstream.cli_stderr)
        self.assertEqual(ours.duration_ms, upstream.duration_ms)
        # Evidence is a tuple of frozen dataclasses; compare field-by-field.
        self.assertEqual(len(ours.evidence), len(upstream.evidence))
        for o_ev, u_ev in zip(ours.evidence, upstream.evidence):
            self.assertEqual(o_ev.file_path, u_ev.file_path)
            self.assertEqual(o_ev.function_name, u_ev.function_name)
            self.assertEqual(o_ev.snippet, u_ev.snippet)
            self.assertEqual(o_ev.language, u_ev.language)
            self.assertEqual(o_ev.role, u_ev.role)
        # Analysis: vendored doesn't run the redactor, upstream-without-logger
        # also doesn't, so the strings should match byte-for-byte.
        self.assertEqual(ours.analysis, upstream.analysis)

    def test_non_zero_exit_yields_inconclusive(self) -> None:
        self._assert_equiv(stdout="boom", stderr="oops", exit_code=1, duration_ms=5)

    def test_empty_stdout_yields_inconclusive(self) -> None:
        self._assert_equiv(stdout="   \n", stderr="", exit_code=0, duration_ms=5)

    def test_invalid_json_yields_inconclusive(self) -> None:
        self._assert_equiv(
            stdout="this is not JSON", stderr="", exit_code=0, duration_ms=5,
        )

    def test_non_object_json_yields_inconclusive(self) -> None:
        self._assert_equiv(stdout='"justastring"', stderr="", exit_code=0, duration_ms=5)

    def test_well_formed_response_parses(self) -> None:
        payload = (
            '{"status": "Verified", '
            '"analysis": "checked sanitizer in module X", '
            '"reason": "input validated upstream", '
            '"call_chain_evidence": ['
            '{"file_path": "a/b.py", "function_name": "f", '
            '"snippet": "validate(x)", "language": "python", "role": "sanitizer"}'
            ']}'
        )
        self._assert_equiv(stdout=payload, stderr="", exit_code=0, duration_ms=42)

    def test_evidence_with_missing_optional_fields(self) -> None:
        payload = (
            '{"status": "Not Verified", '
            '"call_chain_evidence": [{"file_path": "x.py", "snippet": ""}]}'
        )
        self._assert_equiv(stdout=payload, stderr="", exit_code=0, duration_ms=1)

    def test_evidence_with_invalid_entries_dropped(self) -> None:
        payload = (
            '{"status": "Inconclusive", '
            '"call_chain_evidence": ['
            '{"file_path": ""}, '
            '"not-a-dict", '
            '{"file_path": "ok.py", "snippet": "code"}'
            ']}'
        )
        self._assert_equiv(stdout=payload, stderr="", exit_code=0, duration_ms=1)


@unittest.skipIf(vendored is None, "xauditor-coder-service not installed")
class WalkProjectsDriftTests(unittest.TestCase):
    """walk_projects must produce the same results upstream and vendored."""

    def _run_layout(self, layout: dict[str, list[str]]):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for rel, markers in layout.items():
                sub = Path(tmp) / rel
                sub.mkdir(parents=True, exist_ok=True)
                for marker in markers:
                    (sub / marker).touch()
            upstream = walk_projects(tmp)
            ours = vendored.walk_projects(tmp)
            return upstream, ours

    def test_flat_layout_matches(self) -> None:
        upstream, ours = self._run_layout(
            {"alpha": [], "beta": [], "gamma": []}
        )
        self.assertEqual(ours[0], upstream[0])
        self.assertEqual(ours[1]["truncated"], upstream[1]["truncated"])

    def test_nested_marker_layout_matches(self) -> None:
        upstream, ours = self._run_layout(
            {
                "flat": [],
                "team/repo": [".git"],
                "team/yml-only": ["xauditor.yml"],
                "team/no-marker": [],
                "deeper/a/b/c": [".xauditor"],
            }
        )
        self.assertEqual(ours[0], upstream[0])

    def test_dotfile_filter_matches(self) -> None:
        upstream, ours = self._run_layout(
            {"real": [], ".cache": [], "__pycache__": []}
        )
        self.assertEqual(ours[0], upstream[0])

    def test_entry_budget_matches(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            for i in range(20):
                (Path(tmp) / f"d{i:02d}").mkdir()
            upstream = walk_projects(tmp, entry_budget=5)
            ours = vendored.walk_projects(tmp, entry_budget=5)
        self.assertEqual(len(ours[0]), len(upstream[0]))
        self.assertEqual(ours[1]["truncated"], upstream[1]["truncated"])
        self.assertEqual(ours[1]["entries_visited"], upstream[1]["entries_visited"])


if __name__ == "__main__":
    unittest.main()
