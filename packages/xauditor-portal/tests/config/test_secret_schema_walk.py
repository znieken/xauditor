"""Guard against silently introducing a new secret-shaped config field.

Walks every ``@dataclass`` reachable from ``xauditor.config.XAuditorConfig``
and asserts that any leaf field whose name *contains* a secret-shaped
token (``api_key``, ``password``, ``model_api_key``, ``endpoint_token``)
is matched by ``is_secret_key`` for its synthesised dot-path. If a future
config refactor introduces a new such field without extending
``FORBIDDEN_UI_KEY_SUFFIXES`` / ``SECRET_VALUE_KEY_PATTERNS``, this test
fails with a message naming the field and pointing at the redaction list.
"""

from __future__ import annotations

import dataclasses
import unittest
from typing import Any

from xauditor.config import XAuditorConfig
from xauditor_portal.config_resolver import is_secret_key


_SECRET_TOKENS: tuple[str, ...] = (
    "api_key",
    "password",
    "model_api_key",
    "endpoint_token",
)

# Keys whose name carries a secret token but whose value is by design a
# *non-secret* identifier (e.g. a public username field next to a
# password). Document each entry inline so future readers know why it's
# allowlisted.
_NON_SECRET_NAMES_BY_DESIGN: frozenset[str] = frozenset(
    {
        # No exceptions today — kept for documentation purposes.
    }
)


def _walk_dataclass(
    cls: type,
    prefix: str,
    out: list[tuple[str, str]],
    seen: set[type],
) -> None:
    """Append ``(dot_path, field_name)`` for every leaf field reachable
    from ``cls``. Sub-dataclasses recurse with their attribute name as
    the next path segment.
    """

    if cls in seen:
        return
    seen.add(cls)
    for fld in dataclasses.fields(cls):
        dot_path = f"{prefix}.{fld.name}" if prefix else fld.name
        field_type: Any = fld.type
        # Resolve forward refs / typing wrappers — for our config the
        # nested types are concrete dataclasses, so getattr is enough.
        if isinstance(field_type, str):
            # Skip stringified types we can't resolve without a full
            # ``typing.get_type_hints`` call; the schema walks the
            # concrete class graph below regardless.
            out.append((dot_path, fld.name))
            continue
        if dataclasses.is_dataclass(field_type) and isinstance(field_type, type):
            _walk_dataclass(field_type, dot_path, out, seen)
        else:
            out.append((dot_path, fld.name))


def _walk_schema(cls: type) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    _walk_dataclass(cls, prefix="", out=out, seen=set())
    return out


class SchemaWalkSecretGuardTests(unittest.TestCase):
    def test_every_secret_named_field_is_redacted(self) -> None:
        offenders: list[str] = []
        for dot_path, field_name in _walk_schema(XAuditorConfig):
            if field_name in _NON_SECRET_NAMES_BY_DESIGN:
                continue
            if not any(token in field_name for token in _SECRET_TOKENS):
                continue
            # Some leaf paths refer to dict-typed fields (e.g.
            # ``llm.providers``) where the actual leaf key only exists at
            # runtime under a user-chosen provider name. Synthesise a
            # representative path under ``llm.providers.<name>`` for the
            # ``api_key`` case so we still cover the secret rule.
            candidate_paths = [dot_path]
            if dot_path == "llm.providers":
                candidate_paths = [f"llm.providers.<name>.{field_name}"]
            for candidate in candidate_paths:
                if not is_secret_key(candidate):
                    offenders.append(f"{candidate} (field name: {field_name})")
        self.assertFalse(
            offenders,
            msg=(
                "Found secret-named config field(s) not matched by "
                "is_secret_key. Update FORBIDDEN_UI_KEY_SUFFIXES or "
                "SECRET_VALUE_KEY_PATTERNS in config_resolver.py:\n  "
                + "\n  ".join(offenders)
            ),
        )

    def test_known_secret_paths_are_redacted_smoke(self) -> None:
        # Sanity check — these are the secret keys we know about today.
        for path in (
            "llm.providers.shared.api_key",
            "coder.model_api_key",
            "coder.endpoint_token",
            "graph.db.password",
            "reportdb.password",
            "graph.db.remote.url",
            "reportdb.remote.url",
        ):
            self.assertTrue(is_secret_key(path), msg=path)


if __name__ == "__main__":
    unittest.main()
