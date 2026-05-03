"""Runtime settings for the portal backend, loaded from env vars.

The portal is deliberately narrow in what it reads from environment so that
yml / DB config can't leak credentials in from the UI. Remote DB URLs and
the JWT secret live here only.
"""

from __future__ import annotations

import os
import secrets
from functools import lru_cache
from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


JWT_SECRET_FILE_DEFAULT = Path(".xauditor/portal/jwt.secret")


class PortalSettings(BaseSettings):
    """Environment-driven settings.

    The database URL is populated by the main xauditor package when it
    imports the portal; a bare portal process can also rely on the
    environment variables documented below.
    """

    model_config = SettingsConfigDict(env_prefix="XAUDITOR_PORTAL_", extra="ignore")

    # Full SQLAlchemy async URL; overrides the main-package ReportDBConfig
    # when set. Used by the FastAPI app to build its async engine.
    database_url: str | None = Field(default=None)

    # JWT signing secret. When unset the app reads or creates
    # `.xauditor/portal/jwt.secret` with 0600 perms at first boot.
    jwt_secret: str | None = Field(default=None)
    jwt_secret_file: Path = Field(default=JWT_SECRET_FILE_DEFAULT)

    # JWT lifetime (seconds). Default 12 hours.
    jwt_ttl_seconds: int = Field(default=12 * 60 * 60)

    # Minimum password policy
    password_min_length: int = Field(default=12)


@lru_cache(maxsize=1)
def get_settings() -> PortalSettings:
    return PortalSettings()


def resolve_jwt_secret(settings: PortalSettings | None = None) -> str:
    """Return the JWT signing secret.

    Order of precedence:
    1. ``XAUDITOR_PORTAL_JWT_SECRET`` env var (via `PortalSettings.jwt_secret`)
    2. ``.xauditor/portal/jwt.secret`` on disk (creates one if missing, 0600)
    """

    settings = settings or get_settings()
    if settings.jwt_secret:
        return settings.jwt_secret
    path = settings.jwt_secret_file
    if not path.is_absolute():
        path = Path.cwd() / path
    if path.exists():
        return path.read_text(encoding="utf-8").strip()
    path.parent.mkdir(parents=True, exist_ok=True)
    secret = secrets.token_urlsafe(64)
    path.write_text(secret + "\n", encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return secret


__all__ = ["PortalSettings", "get_settings", "resolve_jwt_secret"]
