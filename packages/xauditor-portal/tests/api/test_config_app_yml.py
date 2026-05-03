"""Regression tests for ``_app_yml`` — the helper that the
``/api/config/effective`` endpoint uses to source the live yml config.

The historical bug: when no ``app.state.load_yml_config`` callable was
installed, ``_app_yml`` called ``load_config(repo_root=cwd)`` without
``env``. ``load_config`` defaults missing env to ``{}``, which made
``_resolve_home_dir`` return ``None`` and silently dropped
``~/.xauditor/xauditor.yml``. Operators saw a Settings page where every
yml-set field rendered as if no yml were loaded.
"""

from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import mock

from xauditor_portal.api.config import _app_yml


def _fake_request_without_loader() -> SimpleNamespace:
    """Build a minimal stand-in for ``fastapi.Request`` whose
    ``app.state`` has no ``load_yml_config`` attribute, forcing the
    fallback path that hits ``xauditor.config.load_config``."""

    state = SimpleNamespace()  # No `load_yml_config` set.
    app = SimpleNamespace(state=state)
    return SimpleNamespace(app=app)


class AppYmlHomeConfigTests(unittest.TestCase):
    def test_fallback_loads_home_xauditor_yml(self) -> None:
        """`~/.xauditor/xauditor.yml` SHALL contribute to the resolved
        yml even when only HOME (not cwd) carries the file."""

        with TemporaryDirectory() as home, TemporaryDirectory() as cwd:
            home_xauditor_dir = Path(home) / ".xauditor"
            home_xauditor_dir.mkdir()
            (home_xauditor_dir / "xauditor.yml").write_text(
                "logging:\n  level: debug\n",
                encoding="utf-8",
            )
            # cwd has no project-level yml — only HOME-level yml is set.
            request = _fake_request_without_loader()
            with (
                mock.patch.dict(os.environ, {"HOME": home}, clear=False),
                mock.patch("pathlib.Path.cwd", return_value=Path(cwd)),
            ):
                result = _app_yml(request)
            self.assertEqual(
                result.get("logging", {}).get("level"),
                "debug",
                f"~/.xauditor/xauditor.yml not loaded; got: {result.get('logging')!r}",
            )

    def test_loader_on_app_state_takes_precedence(self) -> None:
        """If a host installed ``app.state.load_yml_config``, the
        fallback path SHALL NOT run."""

        state = SimpleNamespace(load_yml_config=lambda: {"logging": {"level": "warning"}})
        request = SimpleNamespace(app=SimpleNamespace(state=state))
        result = _app_yml(request)
        self.assertEqual(result, {"logging": {"level": "warning"}})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
