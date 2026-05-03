from __future__ import annotations

import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.audit.preflight import check_coder_cli
from xauditor.config import CoderConfig
from xauditor.errors import PreflightError


def _write_python_script(path: Path, body: str) -> None:
    path.write_text(
        f"#!{sys.executable}\n" + textwrap.dedent(body).lstrip(),
        encoding="utf-8",
    )
    mode = path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH
    path.chmod(mode)


class CoderPreflightDisabledTests(unittest.TestCase):
    def test_disabled_coder_returns_none(self) -> None:
        cfg = CoderConfig(enabled=False)
        self.assertIsNone(check_coder_cli(cfg, env={"PATH": "/usr/bin"}))


class CoderPreflightMissingBinaryTests(unittest.TestCase):
    def test_relative_command_not_on_path_raises(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = CoderConfig(
                enabled=True,
                cli_command=("definitely-not-installed-claude",),
            )
            env = {"PATH": tmp}  # empty bin dir
            with self.assertRaises(PreflightError) as ctx:
                check_coder_cli(cfg, env=env)
            message = str(ctx.exception)
            self.assertIn("definitely-not-installed-claude", message)
            self.assertIn("coder.cli_command", message)
            self.assertIn("coder.enabled: false", message)

    def test_absolute_path_must_exist_and_be_executable(self) -> None:
        cfg = CoderConfig(
            enabled=True,
            cli_command=("/no/such/path/claude",),
        )
        with self.assertRaises(PreflightError):
            check_coder_cli(cfg, env={"PATH": "/usr/bin"})

    def test_existing_but_non_executable_path_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            non_exec = Path(tmp) / "claude"
            non_exec.write_text("#!/bin/sh\necho hi\n", encoding="utf-8")
            cfg = CoderConfig(
                enabled=True,
                cli_command=(str(non_exec),),
            )
            with self.assertRaises(PreflightError):
                check_coder_cli(cfg, env={"PATH": "/usr/bin"})


class CoderPreflightSuccessTests(unittest.TestCase):
    def test_resolved_path_and_version_captured(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake-claude"
            _write_python_script(
                cli,
                """
                import sys
                if "--version" in sys.argv:
                    sys.stdout.write("claude 0.5.7\\n")
                    sys.exit(0)
                # If the CLI is invoked normally, exit 1.
                sys.exit(1)
                """,
            )
            cfg = CoderConfig(enabled=True, cli_command=(str(cli),))
            result = check_coder_cli(cfg, env={"PATH": "/usr/bin"})
            assert result is not None  # for type checker
            self.assertEqual(result.cli_command, (str(cli),))
            self.assertEqual(result.resolved_path, str(cli))
            self.assertIn("claude 0.5.7", result.version)

    def test_relative_path_resolved_via_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake-claude"
            _write_python_script(
                cli,
                """
                import sys
                sys.stdout.write("claude 1.2.3\\n")
                sys.exit(0)
                """,
            )
            cfg = CoderConfig(enabled=True, cli_command=("fake-claude",))
            env = {"PATH": tmp}
            result = check_coder_cli(cfg, env=env)
            assert result is not None
            self.assertEqual(result.resolved_path, str(cli))
            self.assertIn("1.2.3", result.version)

    def test_version_probe_failure_is_silent(self) -> None:
        # The CLI is executable but its --version probe exits non-zero. We
        # should still return success, with an empty version string.
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake-claude"
            _write_python_script(
                cli,
                """
                import sys
                sys.exit(2)
                """,
            )
            cfg = CoderConfig(enabled=True, cli_command=(str(cli),))
            result = check_coder_cli(cfg, env={"PATH": "/usr/bin"})
            assert result is not None
            self.assertEqual(result.version, "")

    def test_provider_manifest_shape(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "fake-claude"
            _write_python_script(
                cli,
                """
                import sys
                sys.stdout.write("claude 9.9.9\\n")
                sys.exit(0)
                """,
            )
            cfg = CoderConfig(enabled=True, cli_command=(str(cli),))
            result = check_coder_cli(cfg, env={"PATH": "/usr/bin"})
            assert result is not None
            manifest = result.as_provider_manifest()
            self.assertEqual(manifest["cli_command"], [str(cli)])
            self.assertEqual(manifest["resolved_path"], str(cli))
            self.assertIn("9.9.9", manifest["version"])

    def test_as_provider_summary_returns_flat_string(self) -> None:
        """``RunMeta.llm_providers_used["coder"]`` is now a string
        with optional model_name suffix. The portal renders it via
        ``String(v)`` and a dict turns into ``[object Object]``."""

        from xauditor.audit.preflight import CoderPreflightResult
        # ``claude --version`` outputs ``"2.1.122 (Claude Code)"``;
        # the parenthetical SHALL be stripped because "claude-code"
        # is already our prefix.
        result = CoderPreflightResult(
            cli_command=("http",), resolved_path="http://x:8090",
            version="2.1.122 (Claude Code)",
        )
        self.assertEqual(result.as_provider_summary(), "claude-code 2.1.122")
        self.assertIsInstance(result.as_provider_summary(), str)

    def test_as_provider_summary_includes_model_name(self) -> None:
        """When ``coder.model_name`` is configured, the summary SHALL
        append it after a comma so operators see both runtime and
        target model in the portal's "LLM providers" header."""

        from xauditor.audit.preflight import CoderPreflightResult
        result = CoderPreflightResult(
            cli_command=("http",), resolved_path="http://x:8090",
            version="2.1.123 (Claude Code)",
        )
        self.assertEqual(
            result.as_provider_summary(model_name="fortiai180-multimodal"),
            "claude-code 2.1.123, fortiai180-multimodal",
        )

    def test_as_provider_summary_handles_partial_inputs(self) -> None:
        """Falls back gracefully on missing version / model_name."""

        from xauditor.audit.preflight import CoderPreflightResult
        no_version = CoderPreflightResult(
            cli_command=("http",), resolved_path="http://x:8090", version="",
        )
        self.assertEqual(no_version.as_provider_summary(), "claude-code")
        self.assertEqual(
            no_version.as_provider_summary(model_name="claude-sonnet-4-7"),
            "claude-code, claude-sonnet-4-7",
        )

        with_version = CoderPreflightResult(
            cli_command=("http",), resolved_path="http://x:8090",
            version="2.1.0 (Claude Code)",
        )
        self.assertEqual(
            with_version.as_provider_summary(model_name=""),
            "claude-code 2.1.0",
        )
        self.assertEqual(
            with_version.as_provider_summary(model_name=None),
            "claude-code 2.1.0",
        )


if __name__ == "__main__":
    unittest.main()
