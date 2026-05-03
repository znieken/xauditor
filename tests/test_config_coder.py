from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import CoderConfig, ConfigError, load_config


_BASE_LLM_BLOCK = """
llm:
  default_provider: p1
  providers:
    p1:
      base_url: mock://p1
      api_key: k
      model_name: m
"""


def _write_project_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


class CoderConfigDefaultsTests(unittest.TestCase):
    def test_defaults_when_coder_block_absent(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.coder, CoderConfig())
            self.assertFalse(config.coder.enabled)
            self.assertEqual(config.coder.cli_command, ("claude",))
            self.assertEqual(config.coder.concurrency, 2)
            self.assertEqual(config.coder.request_timeout_seconds, 1800)
            self.assertIsNone(config.coder.thinking_effort)
            self.assertIsNone(config.coder.model_url)
            self.assertIsNone(config.coder.model_name)
            self.assertEqual(config.coder.model_api_key, "")
            self.assertIsNone(config.coder.working_directory)


class CoderConfigSecretRedactionTests(unittest.TestCase):
    def test_repr_redacts_model_api_key(self) -> None:
        cfg = CoderConfig(enabled=True, model_api_key="sk-ant-supersecret")
        rendered = repr(cfg)
        self.assertNotIn("sk-ant-supersecret", rendered)
        self.assertIn("'***'", rendered)

    def test_repr_shows_empty_string_when_no_key(self) -> None:
        cfg = CoderConfig(enabled=True)
        rendered = repr(cfg)
        self.assertIn("model_api_key=''", rendered)


class CoderConfigYamlTests(unittest.TestCase):
    def test_string_cli_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      enabled: true
                      cli_command: claude
                      concurrency: 4
                      thinking_effort: high
                      model_url: https://my-host/v1
                      model_name: claude-sonnet-X
                      model_api_key: sk-ant-yaml-secret
                      request_timeout_seconds: 600
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertTrue(config.coder.enabled)
            self.assertEqual(config.coder.cli_command, ("claude",))
            self.assertEqual(config.coder.concurrency, 4)
            self.assertEqual(config.coder.thinking_effort, "high")
            self.assertEqual(config.coder.model_url, "https://my-host/v1")
            self.assertEqual(config.coder.model_name, "claude-sonnet-X")
            self.assertEqual(config.coder.model_api_key, "sk-ant-yaml-secret")
            self.assertEqual(config.coder.request_timeout_seconds, 600)

    def test_list_cli_command(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      enabled: true
                      cli_command: [/usr/bin/wrapper, claude, --auto]
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.coder.cli_command,
                ("/usr/bin/wrapper", "claude", "--auto"),
            )

    def test_thinking_effort_case_insensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      thinking_effort: MEDIUM
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.coder.thinking_effort, "medium")

    def test_thinking_effort_xhigh_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      thinking_effort: xhigh
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.coder.thinking_effort, "xhigh")

    def test_thinking_effort_max_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      thinking_effort: MAX
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.coder.thinking_effort, "max")


class CoderConfigValidationTests(unittest.TestCase):
    def _expect_error(self, body: str, fragment: str) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK + textwrap.dedent(body))
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn(fragment, str(ctx.exception))

    def test_empty_list_cli_command_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              cli_command: []
            """,
            "coder.cli_command",
        )

    def test_shell_metacharacters_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              cli_command: [claude, ";rm -rf /"]
            """,
            "coder.cli_command[1]",
        )

    def test_concurrency_below_one_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              concurrency: 0
            """,
            "coder.concurrency",
        )

    def test_request_timeout_below_one_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              request_timeout_seconds: 0
            """,
            "coder.request_timeout_seconds",
        )

    def test_unknown_thinking_effort_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              thinking_effort: extreme
            """,
            "coder.thinking_effort",
        )

    def test_empty_string_cli_command_rejected(self) -> None:
        self._expect_error(
            """
            coder:
              cli_command: ""
            """,
            "coder.cli_command",
        )

    def test_non_mapping_block_rejected(self) -> None:
        self._expect_error(
            """
            coder: 42
            """,
            "coder",
        )


class CoderConfigEnvTests(unittest.TestCase):
    def test_enabled_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {"HOME": tmp, "XAUDITOR_CODER_ENABLED": "true"}
            config = load_config(repo_root=repo_root, env=env)
            self.assertTrue(config.coder.enabled)

    def test_cli_command_string_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_CLI_COMMAND": "/usr/local/bin/claude",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.cli_command, ("/usr/local/bin/claude",))

    def test_cli_command_json_array_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_CLI_COMMAND": '["claude", "--auto"]',
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.cli_command, ("claude", "--auto"))

    def test_empty_string_env_treated_as_unset(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      thinking_effort: high
                    """
                ),
            )
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_THINKING_EFFORT": "",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.thinking_effort, "high")

    def test_concurrency_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {"HOME": tmp, "XAUDITOR_CODER_CONCURRENCY": "5"}
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.concurrency, 5)

    def test_model_api_key_via_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {"HOME": tmp, "XAUDITOR_CODER_MODEL_API_KEY": "sk-ant-env"}
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.model_api_key, "sk-ant-env")
            # repr stays redacted even when sourced from env.
            self.assertNotIn("sk-ant-env", repr(config.coder))

    def test_env_overrides_yaml_model_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      model_api_key: sk-ant-yaml
                    """
                ),
            )
            env = {"HOME": tmp, "XAUDITOR_CODER_MODEL_API_KEY": "sk-ant-env"}
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.model_api_key, "sk-ant-env")

    def test_invalid_json_array_env_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_CLI_COMMAND": "[not json",
            }
            with self.assertRaises(ConfigError):
                load_config(repo_root=repo_root, env=env)


class CoderConfigHttpTransportTests(unittest.TestCase):
    def test_http_transport_smart_defaults_to_runtime_socket(self) -> None:
        # When transport: http and endpoint is unset, xauditor SHALL
        # default to ``unix://<runtime.root_dir>/coder.sock`` so the
        # operator's minimum HTTP-transport config is just two lines.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            expected_socket = f"{repo_root}/.xauditor/coder.sock"
            self.assertEqual(config.coder.endpoint, f"unix://{expected_socket}")
            self.assertEqual(config.coder.runtime_socket_path, expected_socket)

    def test_explicit_endpoint_overrides_smart_default(self) -> None:
        # An explicit endpoint MUST win over the smart default, including
        # remote URLs.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: https://coder-pool.internal/
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(
                config.coder.endpoint, "https://coder-pool.internal/"
            )
            # No smart default for runtime_socket_path on a non-unix endpoint
            self.assertEqual(config.coder.runtime_socket_path, "")

    def test_subprocess_transport_does_not_default_endpoint(self) -> None:
        # The smart default is gated on transport: http — subprocess mode
        # leaves endpoint empty.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: subprocess
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertEqual(config.coder.endpoint, "")

    def test_unknown_transport_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: rpc
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("coder.transport", str(ctx.exception))

    def test_enable_auth_without_token_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: http://127.0.0.1:8090
                      enable_auth: true
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            message = str(ctx.exception)
            self.assertIn("coder.endpoint_token", message)
            self.assertIn("coder.enable_auth", message)

    def test_enable_auth_with_token_accepted(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: https://coder-pool.internal/
                      enable_auth: true
                      endpoint_token: secret-abc
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertTrue(config.coder.enable_auth)
            self.assertEqual(config.coder.endpoint_token, "secret-abc")
            self.assertNotIn("secret-abc", repr(config.coder))

    def test_enable_auth_false_with_token_accepted(self) -> None:
        # auth disabled + token configured: no error, just leftover from a
        # previous config revision. Token still treated as a secret in repr.
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: http://127.0.0.1:8090
                      enable_auth: false
                      endpoint_token: leftover-token
                    """
                ),
            )
            config = load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertFalse(config.coder.enable_auth)
            self.assertEqual(config.coder.endpoint_token, "leftover-token")
            self.assertNotIn("leftover-token", repr(config.coder))

    def test_unknown_endpoint_scheme_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: "tcp://10.0.0.5:8090"
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("coder.endpoint", str(ctx.exception))
            self.assertIn("scheme", str(ctx.exception))

    def test_unix_endpoint_must_be_absolute(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: "unix://relative/path.sock"
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("absolute", str(ctx.exception))

    def test_poll_interval_below_minimum_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(
                repo_root,
                _BASE_LLM_BLOCK
                + textwrap.dedent(
                    """
                    coder:
                      transport: http
                      endpoint: http://127.0.0.1:8090
                      poll_interval_seconds: 0.01
                    """
                ),
            )
            with self.assertRaises(ConfigError) as ctx:
                load_config(repo_root=repo_root, env={"HOME": tmp})
            self.assertIn("coder.poll_interval_seconds", str(ctx.exception))

    def test_env_var_transport_override(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_TRANSPORT": "http",
                "XAUDITOR_CODER_ENDPOINT": "http://127.0.0.1:8090",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertEqual(config.coder.transport, "http")
            self.assertEqual(config.coder.endpoint, "http://127.0.0.1:8090")

    def test_env_var_enable_auth_redacts_token_in_repr(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_project_yaml(repo_root, _BASE_LLM_BLOCK)
            env = {
                "HOME": tmp,
                "XAUDITOR_CODER_TRANSPORT": "http",
                "XAUDITOR_CODER_ENDPOINT": "https://x.example/",
                "XAUDITOR_CODER_ENABLE_AUTH": "true",
                "XAUDITOR_CODER_ENDPOINT_TOKEN": "env-secret",
            }
            config = load_config(repo_root=repo_root, env=env)
            self.assertTrue(config.coder.enable_auth)
            self.assertEqual(config.coder.endpoint_token, "env-secret")
            self.assertNotIn("env-secret", repr(config.coder))


if __name__ == "__main__":
    unittest.main()
