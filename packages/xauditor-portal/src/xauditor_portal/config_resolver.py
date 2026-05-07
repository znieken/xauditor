"""env/yml-over-DB configuration resolver.

Loads the effective xauditor yml config, layers the latest DB snapshot
under it, and tags each field with its source (`env`, `yml`, `db`,
`default`). Writes `.xauditor/portal/effective-config.yml` on every save.

The set of keys editable from the portal UI is intentionally narrow:
- `logging.level`, `logging.file`
- `repository.excludes`
- `graph.build.*` (enable_llm_enrichment, max_file_bytes, paths_max_depth,
  paths_max_count, neo4j_chunk_size)
- `audit.worker_count`
- `audit.mode`, `audit.replication.{analyzer,validator,exploiter}`,
  `audit.validator.debate.{enabled,max_rounds,halt_on_consensus}`,
  `audit.{analyzer,validator,exploiter}.provider_list`,
  `audit.max_findings_per_unit`
- Legacy `teaming.enabled`, `teaming.analyzer.*`,
  `teaming.validator.*`, `teaming.exploiter.*` — still
  allow-listed for one minor release for backward compatibility;
  the ``_migrate_legacy_teaming`` shim in ``xauditor.config``
  translates them to the canonical ``audit.*`` shape on read,
  including ``teaming.<stage>.provider_list`` →
  ``audit.<stage>.provider_list`` (Phase 1B,
  ``migrate-provider-list-to-audit-namespace``)
- `coder.*` operational + infrastructure knobs (deprecated
  `coder.repo_mount_path` not exposed)
- `llm.default_provider`, `llm.providers.<name>.{base_url,model_name,
  kind,thinking_enabled,thinking_effort,request_timeout_seconds,
  max_tokens,thinking_budget_tokens,temperature,top_p,top_k,
  repetition_penalty}`
- `agents.<name>.llm.{provider,thinking_enabled,
  request_timeout_seconds,temperature,top_p,top_k,repetition_penalty}`

Secret-shaped keys are excluded from UI writes and redacted from read
responses. A key is secret-shaped when its dotted path ends in any of
``FORBIDDEN_UI_KEY_SUFFIXES`` (``.api_key``, ``.password``,
``.model_api_key``, ``.endpoint_token``) OR matches a glob in
``SECRET_VALUE_KEY_PATTERNS`` (currently ``*.remote.url``, which can
carry inline credentials). The ``reportdb.remote.*`` and
``graph.db.remote.*`` prefixes additionally forbid the entire remote
config block from UI writes.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import yaml


SOURCE_ENV = "env"
SOURCE_YML = "yml"
SOURCE_DB = "db"
SOURCE_DEFAULT = "default"


FORBIDDEN_UI_KEY_PREFIXES: tuple[str, ...] = (
    "reportdb.remote.",
    "graph.db.remote.",
)

FORBIDDEN_UI_KEY_SUFFIXES: tuple[str, ...] = (
    ".api_key",
    ".password",
    ".model_api_key",
    ".endpoint_token",
)

# Glob patterns matched with ``fnmatch`` over the dotted key path. Used
# for keys whose value is sensitive even when the key name itself does
# not end in a secret-shaped suffix — the canonical case is
# ``*.remote.url`` for database connection URLs that can embed
# credentials inline (``postgresql://user:pw@host/db``).
SECRET_VALUE_KEY_PATTERNS: tuple[str, ...] = (
    "*.remote.url",
)

UI_EDITABLE_KEY_PREFIXES: tuple[str, ...] = (
    "logging.",
    "repository.",
    "graph.build.",
    "teaming.",
    "audit.",
    "coder.",
    "llm.default_provider",
    "llm.providers.",
    "agents.",
)


@dataclass(frozen=True)
class FieldResolution:
    key: str
    value: Any
    source: str
    overridden_by_yml: bool


@dataclass(frozen=True)
class EffectiveConfig:
    values: dict[str, Any]
    per_field: dict[str, FieldResolution]

    @property
    def overridden_keys(self) -> list[str]:
        return [k for k, r in self.per_field.items() if r.overridden_by_yml]


def flatten(data: Mapping[str, Any], prefix: str = "") -> dict[str, Any]:
    """Flatten nested dict into dot-notation keys."""

    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, Mapping):
            out.update(flatten(value, path))
        else:
            out[path] = value
    return out


def unflatten(flat: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for dotted, value in flat.items():
        cursor = out
        parts = dotted.split(".")
        for segment in parts[:-1]:
            cursor = cursor.setdefault(segment, {})
            if not isinstance(cursor, dict):
                # Replace leaf with a dict when branching; rare but possible
                # if DB and yml disagree on leaf/branch.
                cursor = {}
        cursor[parts[-1]] = value
    return out


def is_ui_editable(key: str) -> bool:
    if any(key.startswith(prefix) for prefix in FORBIDDEN_UI_KEY_PREFIXES):
        return False
    if any(key.endswith(suffix) for suffix in FORBIDDEN_UI_KEY_SUFFIXES):
        return False
    return any(key.startswith(prefix) for prefix in UI_EDITABLE_KEY_PREFIXES)


def is_secret_key(key: str) -> bool:
    """Return True iff ``key`` carries a secret value that must not leave
    the server unredacted.

    A key is secret-shaped when its dotted path ends in one of
    ``FORBIDDEN_UI_KEY_SUFFIXES`` OR matches a glob in
    ``SECRET_VALUE_KEY_PATTERNS``.
    """

    if any(key.endswith(suffix) for suffix in FORBIDDEN_UI_KEY_SUFFIXES):
        return True
    if any(fnmatch.fnmatchcase(key, pattern) for pattern in SECRET_VALUE_KEY_PATTERNS):
        return True
    return False


def forbidden_keys_in_payload(payload: Mapping[str, Any]) -> list[str]:
    """Return keys in a UI submission that are not allowed to be written."""

    forbidden: list[str] = []
    for key in flatten(payload).keys():
        if any(key.startswith(prefix) for prefix in FORBIDDEN_UI_KEY_PREFIXES):
            forbidden.append(key)
            continue
        if is_secret_key(key):
            forbidden.append(key)
    return forbidden


def redact_values(
    values: Mapping[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """Strip every secret-shaped key from ``values`` and report presence.

    Returns ``(redacted_values, redacted_keys_map)`` where:
    - ``redacted_values`` is a fresh dict with every secret-shaped key's
      value replaced by ``None`` and every non-secret value carried
      through unchanged.
    - ``redacted_keys_map`` maps each secret-shaped key seen in
      ``values`` to ``{"present": bool}``. ``present`` is True iff the
      original value is a non-empty truthy scalar.

    Callers that also want a per-key ``source`` field should attach it
    after the fact — ``redact_values`` stays oblivious to source info so
    it can be reused by any caller (effective config, audit logs, etc.).
    """

    redacted_values: dict[str, Any] = {}
    redacted_keys_map: dict[str, dict[str, Any]] = {}
    for key, value in values.items():
        if is_secret_key(key):
            redacted_values[key] = None
            redacted_keys_map[key] = {"present": _is_secret_present(value)}
        else:
            redacted_values[key] = value
    return redacted_values, redacted_keys_map


def _is_secret_present(value: Any) -> bool:
    """A secret is *present* iff it has a meaningful non-empty value."""

    if value is None:
        return False
    if isinstance(value, str):
        return value != ""
    return bool(value)


def resolve_effective(
    *,
    yml: Mapping[str, Any],
    db_snapshot: Mapping[str, Any] | None,
    defaults: Mapping[str, Any] | None = None,
    env_overrides: Mapping[str, Any] | None = None,
) -> EffectiveConfig:
    """Merge env over yml over DB over defaults and tag each field with its source.

    Resolution order (highest precedence first):
    1. ``env_overrides`` — already-flattened dotted-key dict from the
       process environment. When omitted (the default) the legacy
       three-source resolution is preserved.
    2. ``yml`` — nested dict from the parsed yaml file.
    3. ``db_snapshot`` — nested dict from the latest DB snapshot row.
    4. ``defaults`` — nested dict from compiled-in defaults.

    Only UI-editable keys are included in ``per_field``; other resolved
    content (notably remote DB endpoints) is carried in ``values`` but
    not surfaced to the editor.

    ``overridden_by_yml`` on each ``FieldResolution`` retains its legacy
    meaning ("the key is set in both yml and DB"). New consumers should
    gate read-only behaviour off ``source`` instead — env- and yml-sourced
    fields are read-only regardless of whether a DB snapshot also names
    the key.
    """

    flat_env = dict(env_overrides) if env_overrides else {}
    flat_yml = flatten(yml)
    flat_db = flatten(db_snapshot or {})
    flat_def = flatten(defaults or {})

    values: dict[str, Any] = {}
    per_field: dict[str, FieldResolution] = {}

    keys: set[str] = set(flat_env) | set(flat_yml) | set(flat_db) | set(flat_def)
    for key in sorted(keys):
        if key in flat_env:
            value = flat_env[key]
            source = SOURCE_ENV
        elif key in flat_yml:
            value = flat_yml[key]
            source = SOURCE_YML
        elif key in flat_db:
            value = flat_db[key]
            source = SOURCE_DB
        else:
            value = flat_def[key]
            source = SOURCE_DEFAULT
        values[key] = value
        if is_ui_editable(key):
            per_field[key] = FieldResolution(
                key=key,
                value=value,
                source=source,
                overridden_by_yml=(key in flat_yml and key in flat_db),
            )
    return EffectiveConfig(values=values, per_field=per_field)


def write_effective_config_yml(
    effective: EffectiveConfig, *, target: Path
) -> Path:
    """Write the merged config to `.xauditor/portal/effective-config.yml`.

    The output is a full nested yml dump that a human operator can diff
    against `xauditor.yml`. Remote endpoints are included when present in
    the effective values so the file faithfully reflects what the backend
    is actually using, but they come exclusively from yml.
    """

    target.parent.mkdir(parents=True, exist_ok=True)
    nested = unflatten(effective.values)
    target.write_text(
        yaml.safe_dump(nested, sort_keys=True, default_flow_style=False),
        encoding="utf-8",
    )
    return target


__all__ = [
    "EffectiveConfig",
    "FieldResolution",
    "FORBIDDEN_UI_KEY_PREFIXES",
    "FORBIDDEN_UI_KEY_SUFFIXES",
    "SECRET_VALUE_KEY_PATTERNS",
    "SOURCE_DB",
    "SOURCE_DEFAULT",
    "SOURCE_ENV",
    "SOURCE_YML",
    "UI_EDITABLE_KEY_PREFIXES",
    "flatten",
    "forbidden_keys_in_payload",
    "is_secret_key",
    "is_ui_editable",
    "redact_values",
    "resolve_effective",
    "unflatten",
    "write_effective_config_yml",
]
