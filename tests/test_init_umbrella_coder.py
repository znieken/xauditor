"""Tests for the `xauditor init` umbrella's coder-init branch.

Ensures the umbrella:
- runs `coder_init` when coder is enabled + http transport + local endpoint
- skips with a clear reason when any of those gates is false
"""

from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from xauditor.config import load_config
from xauditor.services import ApplicationServices


_BASE_LLM = """
llm:
  default_provider: p1
  providers:
    p1:
      base_url: mock://p1
      api_key: k
      model_name: m
"""


def _write_yaml(repo_root: Path, body: str) -> None:
    (repo_root / "xauditor.yml").write_text(
        textwrap.dedent(body).strip() + "\n", encoding="utf-8"
    )


def _make_services(repo_root: Path, env_extra: dict[str, str]) -> ApplicationServices:
    env = {
        "HOME": str(repo_root),
        "XAUDITOR_LLM_BASE_URL": "mock://offline",
        "XAUDITOR_LLM_API_KEY": "k",
        "XAUDITOR_LLM_MODEL_NAME": "m",
    }
    env.update(env_extra)
    config = load_config(repo_root=repo_root, env=env, require_llm=True)
    runtime = config.runtime.layout.ensure()
    services = ApplicationServices.for_testing(repo_root=repo_root, env=env)
    services.config = config
    services.runtime = runtime
    return services


class _RecordingLogger:
    def __init__(self) -> None:
        self.infos: list[str] = []
    def info(self, m): self.infos.append(m)
    def warning(self, m): pass
    def error(self, m): pass


class InitUmbrellaCoderGateTests(unittest.TestCase):
    def test_skips_when_coder_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(repo_root, _BASE_LLM)
            services = _make_services(repo_root, {})
            logger = _RecordingLogger()
            results = services.init_all(logger=logger)
        # graphdb + reportdb + coder
        # graphdb + reportdb + portal + coder (xauditor 0.5.0 added portal
        # to the umbrella init).
        self.assertEqual(len(results), 4)
        self.assertTrue(any("coder.enabled is false" in m.lower() for m in logger.infos))

    def test_skips_when_subprocess_transport(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    coder:
                      enabled: true
                      transport: subprocess
                    """
                ),
            )
            services = _make_services(repo_root, {})
            logger = _RecordingLogger()
            results = services.init_all(logger=logger)
        # graphdb + reportdb + portal + coder (xauditor 0.5.0 added portal
        # to the umbrella init).
        self.assertEqual(len(results), 4)
        self.assertTrue(
            any("transport is `subprocess`" in m for m in logger.infos),
            f"infos={logger.infos}",
        )

    def test_skips_when_endpoint_is_remote(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    coder:
                      enabled: true
                      transport: http
                      endpoint: https://coder-pool.internal/
                    """
                ),
            )
            services = _make_services(repo_root, {})
            logger = _RecordingLogger()
            results = services.init_all(logger=logger)
        # graphdb + reportdb + portal + coder (xauditor 0.5.0 added portal
        # to the umbrella init).
        self.assertEqual(len(results), 4)
        self.assertTrue(
            any("is remote" in m for m in logger.infos),
            f"infos={logger.infos}",
        )

    def test_runs_coder_init_when_local_http(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            _write_yaml(
                repo_root,
                _BASE_LLM
                + textwrap.dedent(
                    """
                    coder:
                      enabled: true
                      transport: http
                      endpoint: http://127.0.0.1:8090
                    """
                ),
            )
            services = _make_services(repo_root, {})
            results = services.init_all(logger=_RecordingLogger())
        # The InMemoryCoderRuntimeManager init returns "Coder runtime ready: ..."
        # graphdb + reportdb + portal + coder (xauditor 0.5.0 added portal
        # to the umbrella init).
        self.assertEqual(len(results), 4)
        self.assertTrue(
            any("Coder runtime ready" in m for m in results),
            f"results={results}",
        )
        self.assertEqual(services.coder_runtime.container_state, "running")


if __name__ == "__main__":
    unittest.main()
