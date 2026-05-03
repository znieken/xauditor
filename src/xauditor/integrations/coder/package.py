"""Locate the coder-service Dockerfile inside the installed package.

Mirrors :mod:`xauditor.integrations.portal.package` — the Dockerfile and
its requirements file ship inside the ``xauditor-coder-service`` Python
package itself, so:

- editable installs and wheel installs both work, without any
  operator-side staging step;
- ``xauditor coder build`` only requires that ``xauditor-coder-service``
  is on the same Python path that runs ``xauditor`` (typically a
  ``pip install xauditor-coder-service`` away);
- the docker build context is the installed package directory; the
  Dockerfile copies that tree in via ``COPY .`` and installs deps from
  the shipped ``docker/requirements.txt``.

Pre-0.3.3 xauditor located the Dockerfile under
``<repo_root>/deploy/coder-service/Dockerfile`` and required two wheel
files staged into a tempdir build context. That layout is gone.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from xauditor.errors import XAuditorError


class CoderPackageMissing(XAuditorError):
    """Raised when ``xauditor-coder-service`` cannot be located."""


@dataclass(frozen=True)
class CoderPackageInfo:
    """Locations of the coder-service docker build context on disk."""

    distribution_name: str
    distribution_version: str
    dockerfile: Path
    context: Path

    def is_complete(self) -> bool:
        return self.dockerfile.is_file() and self.context.is_dir()


INSTALL_HINT = (
    "The `xauditor-coder-service` package is required for `xauditor coder build`. "
    "Install it with `pip install xauditor-coder-service` (or the matching uv / "
    "poetry verb) and re-run `xauditor coder init`."
)


def resolve_coder_package(repo_root: Path | None = None) -> CoderPackageInfo:
    """Locate the installed ``xauditor-coder-service`` package.

    The ``repo_root`` argument is accepted for API compatibility with
    callers that still pass it (notably ``CoderRuntimeManager``) but is
    NOT consulted — resolution always goes through the installed package
    metadata, mirroring how :func:`xauditor.integrations.portal.package.resolve_portal_package`
    locates its own build context.

    Raises :class:`CoderPackageMissing` with a user-facing install hint
    when ``xauditor-coder-service`` is not installed or when the build
    context cannot be located inside the resolved package directory.
    """

    del repo_root  # accepted for back-compat; not used

    try:
        dist = metadata.distribution("xauditor-coder-service")
    except metadata.PackageNotFoundError as exc:
        raise CoderPackageMissing(INSTALL_HINT) from exc

    try:
        import xauditor_coder_service  # noqa: PLC0415 - optional package, deferred
    except ImportError as exc:
        raise CoderPackageMissing(INSTALL_HINT) from exc

    if not getattr(xauditor_coder_service, "__file__", None):
        raise CoderPackageMissing(
            f"{INSTALL_HINT} (xauditor_coder_service has no __file__ attribute)"
        )
    package_root = Path(xauditor_coder_service.__file__).resolve().parent

    dockerfile = package_root / "docker" / "Dockerfile"
    requirements = package_root / "docker" / "requirements.txt"
    for label, path in (
        ("docker/Dockerfile", dockerfile),
        ("docker/requirements.txt", requirements),
    ):
        if not path.is_file():
            raise CoderPackageMissing(
                f"{INSTALL_HINT} (resolved package is missing `{label}` at {path})"
            )

    return CoderPackageInfo(
        distribution_name=dist.metadata["Name"] or "xauditor-coder-service",
        distribution_version=dist.version,
        dockerfile=dockerfile,
        context=package_root,
    )


__all__ = [
    "CoderPackageInfo",
    "CoderPackageMissing",
    "INSTALL_HINT",
    "resolve_coder_package",
]
