from __future__ import annotations

import os
from typing import Mapping, Protocol

from xauditor.errors import SecretNotFoundError


class _KeyringBackend(Protocol):
    def get_password(self, service: str, username: str) -> str | None: ...


def _load_keyring() -> _KeyringBackend | None:
    try:
        import keyring  # type: ignore[import-not-found]
    except ImportError:
        return None
    return keyring


_KEYRING_SERVICE = "xauditor"


class SecretProvider:
    """Resolves secrets from explicit args, environment variables, or OS keyring.

    Values flow through this class so that plaintext keys never need to be
    persisted on :class:`LLMConfig` or dumped via ``__repr__``.
    """

    def __init__(
        self,
        *,
        env: Mapping[str, str] | None = None,
        keyring_backend: _KeyringBackend | None | object = ...,
    ) -> None:
        self._env = dict(env if env is not None else os.environ)
        if keyring_backend is ...:
            self._keyring = _load_keyring()
        else:
            self._keyring = keyring_backend  # type: ignore[assignment]
        self._resolved: set[str] = set()

    def get(self, name: str, *, explicit: str | None = None) -> str:
        if explicit:
            value = explicit.strip()
            if value:
                self._resolved.add(value)
                return value

        env_key = self._env_key_for(name)
        env_value = self._env.get(env_key, "").strip()
        if env_value:
            self._resolved.add(env_value)
            return env_value

        if self._keyring is not None:
            keyring_value = self._keyring.get_password(_KEYRING_SERVICE, name)
            if keyring_value:
                stripped = keyring_value.strip()
                if stripped:
                    self._resolved.add(stripped)
                    return stripped

        raise SecretNotFoundError(
            f"Could not resolve secret `{name}` from environment or keyring.",
            name=name,
        )

    def try_get(self, name: str, *, explicit: str | None = None) -> str:
        try:
            return self.get(name, explicit=explicit)
        except SecretNotFoundError:
            return ""

    def resolved_values(self) -> tuple[str, ...]:
        return tuple(sorted(self._resolved))

    @staticmethod
    def _env_key_for(name: str) -> str:
        normalized = name.replace(".", "_").replace("-", "_").upper()
        if not normalized.startswith("XAUDITOR_"):
            normalized = f"XAUDITOR_{normalized}"
        return normalized


__all__ = ["SecretProvider"]
