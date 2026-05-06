"""Drift guard between coder-service `pyproject.toml` and the
`docker/requirements.txt` mirror that gets baked into the
container image.

The Dockerfile installs from the requirements.txt (so the
build context can be the bare package source tree, no
pyproject in the build context). The two files must agree, or
container-side `import` statements blow up at startup with
`ModuleNotFoundError` — which is what triggered this guard
being added.

If you add a runtime dependency to coder-service:

  packages/xauditor-coder-service/pyproject.toml
    [project] dependencies = [...]

you MUST also add it to:

  packages/xauditor-coder-service/src/xauditor_coder_service/docker/requirements.txt

This test fails when those two files disagree.
"""

from __future__ import annotations

import re
import sys
import tomllib
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))


_REPO_ROOT = Path(__file__).resolve().parents[1]
_PYPROJECT = (
    _REPO_ROOT / "packages" / "xauditor-coder-service" / "pyproject.toml"
)
_REQUIREMENTS = (
    _REPO_ROOT
    / "packages"
    / "xauditor-coder-service"
    / "src"
    / "xauditor_coder_service"
    / "docker"
    / "requirements.txt"
)


def _normalize(spec: str) -> str:
    """Strip comments + extras + version specifier whitespace."""

    spec = spec.split("#", 1)[0].strip()
    # Drop whitespace inside extras ("uvicorn[standard]>=0.27").
    return re.sub(r"\s+", "", spec)


def _read_pyproject_deps() -> set[str]:
    with _PYPROJECT.open("rb") as handle:
        data = tomllib.load(handle)
    raw = data.get("project", {}).get("dependencies", [])
    return {_normalize(spec) for spec in raw if _normalize(spec)}


def _read_requirements() -> set[str]:
    text = _REQUIREMENTS.read_text(encoding="utf-8")
    deps: set[str] = set()
    for line in text.splitlines():
        normalized = _normalize(line)
        if normalized:
            deps.add(normalized)
    return deps


class CoderServiceRequirementsDriftTests(unittest.TestCase):
    def test_pyproject_and_dockerfile_requirements_match(self) -> None:
        pyproject_deps = _read_pyproject_deps()
        requirements_deps = _read_requirements()
        missing_from_requirements = pyproject_deps - requirements_deps
        extra_in_requirements = requirements_deps - pyproject_deps
        self.assertEqual(
            (missing_from_requirements, extra_in_requirements),
            (set(), set()),
            (
                "coder-service pyproject.toml dependencies and "
                "docker/requirements.txt are out of sync.\n"
                f"Missing from requirements.txt: {sorted(missing_from_requirements)}\n"
                f"Extra in requirements.txt: {sorted(extra_in_requirements)}\n"
                "Update both files together so the container image picks "
                "up new deps."
            ),
        )


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
