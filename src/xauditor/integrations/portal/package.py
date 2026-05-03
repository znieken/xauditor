from __future__ import annotations

from dataclasses import dataclass
from importlib import metadata
from pathlib import Path

from xauditor.errors import XAuditorError


class PortalPackageMissing(XAuditorError):
    """Raised when a portal operation requires `xauditor-portal` but it is not installed."""


@dataclass(frozen=True)
class PortalPackageInfo:
    """Locations of the portal package's Docker build contexts on disk."""

    distribution_name: str
    distribution_version: str
    backend_context: Path
    backend_dockerfile: Path
    frontend_context: Path
    frontend_dockerfile: Path


INSTALL_HINT = (
    "The `xauditor-portal` package is required for this command. "
    "Install it with `pip install xauditor-portal` (or the matching uv / poetry verb)."
)


def resolve_portal_package() -> PortalPackageInfo:
    """Locate the installed `xauditor-portal` package and its Dockerfile layout.

    Raises `PortalPackageMissing` with a user-facing install hint when the
    package is not installed or when the build contexts cannot be located.

    The Dockerfiles and frontend tree live **inside** the Python package:

      xauditor_portal/
        docker/
          backend.Dockerfile
          requirements.txt
        frontend/
          Dockerfile
          ...
        .dockerignore        (build-context root for the backend image)

    That layout means both editable installs and wheel installs preserve
    the build contexts on disk, so `xauditor portal init` can always
    find them when it auto-builds missing images.
    """

    try:
        dist = metadata.distribution("xauditor-portal")
    except metadata.PackageNotFoundError as exc:
        raise PortalPackageMissing(INSTALL_HINT) from exc

    try:
        import xauditor_portal  # noqa: PLC0415 - optional package, deferred
    except ImportError as exc:
        raise PortalPackageMissing(INSTALL_HINT) from exc

    if not xauditor_portal.__file__:
        raise PortalPackageMissing(
            f"{INSTALL_HINT} (xauditor_portal has no __file__ attribute)"
        )
    package_root = Path(xauditor_portal.__file__).resolve().parent

    backend_dockerfile = package_root / "docker" / "backend.Dockerfile"
    frontend_dir = package_root / "frontend"
    frontend_dockerfile = frontend_dir / "Dockerfile"
    for label, path in (
        ("docker/backend.Dockerfile", backend_dockerfile),
        ("frontend/Dockerfile", frontend_dockerfile),
        ("docker/requirements.txt", package_root / "docker" / "requirements.txt"),
    ):
        if not path.exists():
            raise PortalPackageMissing(
                f"{INSTALL_HINT} (resolved package is missing `{label}` at {path})"
            )

    return PortalPackageInfo(
        distribution_name=dist.metadata["Name"] or "xauditor-portal",
        distribution_version=dist.version,
        backend_context=package_root,
        backend_dockerfile=backend_dockerfile,
        frontend_context=frontend_dir,
        frontend_dockerfile=frontend_dockerfile,
    )
