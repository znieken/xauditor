"""`/api/config/*` endpoints: effective config + UI-edited snapshots."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from xauditor_portal.auth import (
    forbid_must_change_password,
    get_session,
    require_admin,
)
from xauditor_portal.config_resolver import (
    FORBIDDEN_UI_KEY_PREFIXES,
    FORBIDDEN_UI_KEY_SUFFIXES,
    EffectiveConfig,
    flatten,
    forbidden_keys_in_payload,
    redact_values,
    resolve_effective,
    write_effective_config_yml,
)
from xauditor_portal.db.models.auth import User
from xauditor_portal.db.models.config import ConfigSnapshot


_AGENTS = ("graph_builder", "auditor", "exploitation", "validator")


@dataclass(frozen=True)
class _NumericRule:
    """A range rule for a UI-editable numeric config key.

    Used for both client-side hint generation and server-side payload
    validation. ``minimum`` / ``maximum`` may be ``None`` for unbounded
    ends. ``kind`` is ``"int"`` or ``"float"`` and gates the type check.
    Inclusivity flags default to True (closed interval).
    """

    minimum: float | None
    maximum: float | None
    kind: str = "float"
    min_inclusive: bool = True
    max_inclusive: bool = True

    def describe(self) -> str:
        left = "[" if self.min_inclusive else "("
        right = "]" if self.max_inclusive else ")"
        lo = self.minimum if self.minimum is not None else "-∞"
        hi = self.maximum if self.maximum is not None else "∞"
        return f"{left}{lo}, {hi}{right}"

    def check(self, key: str, value: Any) -> str | None:
        if value is None:
            return None
        if self.kind == "int":
            if isinstance(value, bool) or not isinstance(value, int):
                return f"{key}: expected an integer in {self.describe()}"
        else:  # float — accept ints AND floats
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                return f"{key}: expected a number in {self.describe()}"
        if self.minimum is not None:
            if self.min_inclusive:
                if value < self.minimum:
                    return f"{key}: must be in {self.describe()}"
            else:
                if value <= self.minimum:
                    return f"{key}: must be in {self.describe()}"
        if self.maximum is not None:
            if self.max_inclusive:
                if value > self.maximum:
                    return f"{key}: must be in {self.describe()}"
            else:
                if value >= self.maximum:
                    return f"{key}: must be in {self.describe()}"
        return None


# Range rules indexed by either the full dot-path (e.g.
# ``audit.worker_count``) or the trailing segment (e.g. ``temperature``,
# ``request_timeout_seconds``). Full-path entries win — see
# ``_lookup_numeric_rule``.
NUMERIC_RANGE_REGISTRY: dict[str, _NumericRule] = {
    # Full-path rules (specific to one section).
    "audit.worker_count": _NumericRule(1, 16, kind="int"),
    "graph.build.max_file_bytes": _NumericRule(1, None, kind="int"),
    "graph.build.paths_max_depth": _NumericRule(1, None, kind="int"),
    "graph.build.paths_max_count": _NumericRule(1, None, kind="int"),
    "graph.build.neo4j_chunk_size": _NumericRule(100, 50_000, kind="int"),
    "coder.concurrency": _NumericRule(1, 64, kind="int"),
    "coder.request_timeout_seconds": _NumericRule(1, None, kind="int"),
    "coder.poll_interval_seconds": _NumericRule(
        0, None, kind="float", min_inclusive=False
    ),
    "coder.preflight_timeout_seconds": _NumericRule(1, None, kind="int"),
    # Trailing-segment rules (apply wherever the segment appears).
    "temperature": _NumericRule(0, None, kind="float"),
    "top_p": _NumericRule(0, 1, kind="float", min_inclusive=False),
    "top_k": _NumericRule(1, None, kind="int"),
    "repetition_penalty": _NumericRule(
        0, None, kind="float", min_inclusive=False
    ),
    "request_timeout_seconds": _NumericRule(
        0, None, kind="float", min_inclusive=False
    ),
    "max_tokens": _NumericRule(1, None, kind="int"),
    "thinking_budget_tokens": _NumericRule(1, None, kind="int"),
}


def _lookup_numeric_rule(key: str) -> _NumericRule | None:
    """Resolve the rule for ``key``: full-path match wins; otherwise
    fall back to the trailing-segment registry entry (if any)."""

    if key in NUMERIC_RANGE_REGISTRY:
        return NUMERIC_RANGE_REGISTRY[key]
    last_segment = key.rsplit(".", 1)[-1]
    return NUMERIC_RANGE_REGISTRY.get(last_segment)


def _validate_numeric_field(key: str, value: Any) -> str | None:
    rule = _lookup_numeric_rule(key)
    if rule is None:
        return None
    return rule.check(key, value)


def _validate_numeric_payload(payload: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    for key, value in flatten(payload).items():
        message = _validate_numeric_field(key, value)
        if message is not None:
            errors.append(message)
    return errors


# Back-compat aliases — kept so older callers / tests that import the
# sampling-only validators keep working. New code should use
# ``_validate_numeric_field`` / ``_validate_numeric_payload``.
def _validate_sampling_field(key: str, value: Any) -> str | None:
    return _validate_numeric_field(key, value)


def _validate_sampling_payload(payload: Mapping[str, Any]) -> list[str]:
    return _validate_numeric_payload(payload)


def _rules_summary() -> dict[str, str]:
    """Render the registry as a flat ``{ key: human-readable range }``
    dict for inclusion in HTTP 400 responses."""

    return {key: rule.describe() for key, rule in NUMERIC_RANGE_REGISTRY.items()}


def _collect_env_overrides(env: Mapping[str, str]) -> dict[str, str]:
    """Walk ``env`` and return a flat dotted-key dict of recognised
    XAuditor env-var overrides.

    The portal config-loader callable installed on
    ``app.state.load_yml_config`` already merges env vars into the
    resolved yml as part of ``xauditor.config.load_config``. The portal
    needs to know *which* keys came from the environment so the Settings
    page can render them with ``source = "env"`` instead of pretending
    they are yml-sourced. We re-walk the environment here using the
    canonical ``ENV_KEY_MAP`` plus the ``XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE``
    opt-out path that lives outside that map.
    """

    overrides: dict[str, str] = {}
    try:
        from xauditor.config import ENV_KEY_MAP
    except Exception:  # noqa: BLE001 — be tolerant if the main package fails to import
        ENV_KEY_MAP = {}  # type: ignore[assignment]

    for env_key, dot_path in ENV_KEY_MAP.items():
        raw = env.get(env_key)
        if raw is not None and raw != "":
            overrides[dot_path] = raw

    chunk_size_raw = env.get("XAUDITOR_GRAPH_BUILD_NEO4J_CHUNK_SIZE")
    if chunk_size_raw is not None and chunk_size_raw != "":
        overrides["graph.build.neo4j_chunk_size"] = chunk_size_raw

    return overrides


router = APIRouter(tags=["config"], dependencies=[Depends(forbid_must_change_password)])


class ConfigFieldOut(BaseModel):
    value: Any
    source: str
    overridden_by_yml: bool


class RedactedKeyOut(BaseModel):
    """One entry in the secret-redaction map.

    ``present`` is ``True`` iff the underlying yml/env value is a
    non-empty string. ``source`` is the resolution source (``yml`` /
    ``env`` / ``default``); ``db`` is impossible because the snapshot
    endpoint refuses to write secret-shaped keys.
    """

    present: bool
    source: str


class EffectiveConfigOut(BaseModel):
    values: dict[str, Any]
    fields: dict[str, ConfigFieldOut]
    overridden_keys: list[str]
    # Map of secret-shaped key → presence + source. Editable controls
    # SHALL NOT be rendered for these keys; instead the UI shows a
    # read-only "configured" / "not configured" stub with the source
    # badge. The matching values are also stripped from ``values`` —
    # they appear there as ``None`` so the response shape stays stable.
    redacted_keys: dict[str, RedactedKeyOut] = {}


class SnapshotIn(BaseModel):
    body: dict[str, Any]
    note: str | None = None


class SnapshotOut(BaseModel):
    id: str
    version: int
    note: str | None
    created_at: datetime


def _app_yml(request: Request) -> Mapping[str, Any]:
    """Return the latest in-memory yml config set by the host process.

    The main xauditor package can install a callable on
    `app.state.load_yml_config` that returns the current yml. The default
    path tries `xauditor.config.load_config` with cwd. If neither works we
    fall back to an empty dict.
    """

    loader = getattr(request.app.state, "load_yml_config", None)
    if loader is not None:
        try:
            return loader()
        except Exception:  # noqa: BLE001 - fall back silently
            return {}
    try:
        from pathlib import Path as _Path

        from xauditor.config import load_config

        # ``env=os.environ`` is required so ``load_config`` can resolve
        # ``~/.xauditor/xauditor.yml`` via ``HOME`` and apply env-var
        # overrides; without it the home-level yml silently disappears.
        config = load_config(repo_root=_Path.cwd(), env=os.environ)
        return _yml_like(config)
    except Exception:  # noqa: BLE001 - fall back
        return {}


def _yml_like(config) -> dict[str, Any]:
    """Convert an XAuditorConfig to the nested dict the resolver expects."""

    from dataclasses import asdict

    try:
        data = asdict(config)
    except TypeError:
        return {}
    # Drop non-yml fields the resolver shouldn't see.
    data.pop("repo_root", None)
    return data


async def _latest_snapshot(session: AsyncSession) -> ConfigSnapshot | None:
    return await session.scalar(
        select(ConfigSnapshot).order_by(ConfigSnapshot.version.desc()).limit(1)
    )


def build_effective_config_out(
    *,
    yml: Mapping[str, Any],
    db_body: Mapping[str, Any],
    env_overrides: Mapping[str, Any] | None,
) -> EffectiveConfigOut:
    """Pure composition: resolve sources, redact secrets, return the
    response model.

    Extracted from ``effective_config`` so unit tests can exercise the
    redaction + source-tagging logic without a live DB / FastAPI
    request. Callers are the async route handler (which injects the DB
    snapshot body and process env) and tests.
    """

    env_overrides = env_overrides or {}
    resolved: EffectiveConfig = resolve_effective(
        yml=yml, db_snapshot=db_body, env_overrides=env_overrides
    )
    redacted_values, redacted_keys_map = redact_values(resolved.values)

    flat_yml = flatten(yml)
    redacted_keys: dict[str, RedactedKeyOut] = {}
    for key, info in redacted_keys_map.items():
        if key in resolved.per_field:
            source = resolved.per_field[key].source
        elif key in env_overrides:
            source = "env"
        elif key in flat_yml:
            source = "yml"
        else:
            source = "default"
        redacted_keys[key] = RedactedKeyOut(present=info["present"], source=source)

    fields = {
        key: ConfigFieldOut(
            value=resolution.value,
            source=resolution.source,
            overridden_by_yml=resolution.overridden_by_yml,
        )
        for key, resolution in resolved.per_field.items()
    }
    return EffectiveConfigOut(
        values=redacted_values,
        fields=fields,
        overridden_keys=resolved.overridden_keys,
        redacted_keys=redacted_keys,
    )


@router.get("/effective", response_model=EffectiveConfigOut)
async def effective_config(
    request: Request, session: AsyncSession = Depends(get_session)
) -> EffectiveConfigOut:
    yml = _app_yml(request)
    env_overrides = _collect_env_overrides(os.environ)
    snapshot = await _latest_snapshot(session)
    db_body = snapshot.body if snapshot else {}
    return build_effective_config_out(
        yml=yml, db_body=db_body, env_overrides=env_overrides
    )


@router.put("/snapshot", response_model=SnapshotOut)
async def save_snapshot(
    body: SnapshotIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
    user: User = Depends(require_admin),
) -> SnapshotOut:
    forbidden = forbidden_keys_in_payload(body.body)
    if forbidden:
        api_key_hits = [k for k in forbidden if k.endswith(".api_key")]
        secret_hits = [
            k
            for k in forbidden
            if any(k.endswith(s) for s in FORBIDDEN_UI_KEY_SUFFIXES)
        ]
        if api_key_hits:
            detail_message = "api_key is managed in xauditor.yml only"
        elif secret_hits:
            detail_message = (
                "Secret-shaped keys (api_key / password / model_api_key / "
                "endpoint_token) are managed in xauditor.yml only"
            )
        else:
            detail_message = "Remote DB endpoints cannot be written from the UI."
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": detail_message,
                "forbidden_keys": forbidden,
                "forbidden_prefixes": list(FORBIDDEN_UI_KEY_PREFIXES),
                "forbidden_suffixes": list(FORBIDDEN_UI_KEY_SUFFIXES),
            },
        )
    numeric_errors = _validate_numeric_payload(body.body)
    if numeric_errors:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Numeric configuration value out of range",
                "errors": numeric_errors,
                "rules": _rules_summary(),
            },
        )
    next_version = (
        await session.scalar(select(func.coalesce(func.max(ConfigSnapshot.version), 0)))
        or 0
    ) + 1
    snapshot = ConfigSnapshot(
        version=next_version,
        body=body.body,
        created_by_user_id=user.id,
        note=body.note,
    )
    session.add(snapshot)
    await session.commit()

    # Write effective-config.yml next to other portal artifacts.
    yml = _app_yml(request)
    resolved = resolve_effective(yml=yml, db_snapshot=body.body)
    out_dir = getattr(request.app.state, "portal_artifacts_dir", None)
    target_path = (
        Path(out_dir) if out_dir else Path.cwd() / ".xauditor" / "portal"
    ) / "effective-config.yml"
    try:
        write_effective_config_yml(resolved, target=target_path)
    except OSError:
        # Non-fatal — the DB snapshot is authoritative; the file is a convenience.
        pass

    return SnapshotOut(
        id=str(snapshot.id),
        version=snapshot.version,
        note=snapshot.note,
        created_at=snapshot.created_at,
    )


class LLMAgentEffectiveRow(BaseModel):
    agent: str
    provider: str | None
    model_name: str | None
    temperature: float | None
    top_p: float | None
    top_k: int | None
    repetition_penalty: float | None
    thinking_enabled: bool | None
    sources: dict[str, str]


class LLMEffectiveConfigOut(BaseModel):
    agents: list[LLMAgentEffectiveRow]
    providers: list[str]
    default_provider: str | None


@router.get("/effective/llm", response_model=LLMEffectiveConfigOut)
async def effective_llm_config(
    request: Request, session: AsyncSession = Depends(get_session)
) -> LLMEffectiveConfigOut:
    yml = _app_yml(request)
    snapshot = await _latest_snapshot(session)
    db_body = snapshot.body if snapshot else {}
    resolved = resolve_effective(yml=yml, db_snapshot=db_body)
    values = resolved.values  # dotted-key -> value
    per_field = resolved.per_field

    provider_names = sorted(
        {
            key.split(".")[2]
            for key in values
            if key.startswith("llm.providers.") and len(key.split(".")) >= 4
        }
    )
    default_provider = values.get("llm.default_provider")

    rows: list[LLMAgentEffectiveRow] = []
    for agent in _AGENTS:
        agent_provider_key = f"agents.{agent}.llm.provider"
        provider = values.get(agent_provider_key) or default_provider
        sources: dict[str, str] = {
            "provider": _source_of(values, per_field, agent_provider_key, "llm.default_provider"),
        }
        row_values: dict[str, Any] = {"provider": provider}
        for field in ("model_name", "temperature", "top_p", "top_k", "repetition_penalty", "thinking_enabled"):
            agent_key = f"agents.{agent}.llm.{field}"
            provider_key = (
                f"llm.providers.{provider}.{field}" if provider else None
            )
            if agent_key in values:
                row_values[field] = values[agent_key]
                sources[field] = per_field.get(agent_key).source if agent_key in per_field else "default"
            elif provider_key and provider_key in values:
                row_values[field] = values[provider_key]
                sources[field] = per_field.get(provider_key).source if provider_key in per_field else "default"
            else:
                row_values[field] = None
                sources[field] = "default"
        rows.append(
            LLMAgentEffectiveRow(
                agent=agent,
                provider=row_values["provider"],
                model_name=row_values.get("model_name"),
                temperature=_coerce_float(row_values.get("temperature")),
                top_p=_coerce_float(row_values.get("top_p")),
                top_k=_coerce_int(row_values.get("top_k")),
                repetition_penalty=_coerce_float(row_values.get("repetition_penalty")),
                thinking_enabled=_coerce_bool(row_values.get("thinking_enabled")),
                sources=sources,
            )
        )
    return LLMEffectiveConfigOut(
        agents=rows,
        providers=provider_names,
        default_provider=default_provider,
    )


def _source_of(
    values: Mapping[str, Any],
    per_field: Mapping[str, Any],
    primary: str,
    fallback: str,
) -> str:
    if primary in per_field:
        return per_field[primary].source
    if fallback in per_field:
        return per_field[fallback].source
    return "default"


def _coerce_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


@router.get("/snapshots", response_model=list[SnapshotOut])
async def list_snapshots(
    session: AsyncSession = Depends(get_session),
    limit: int = 50,
) -> list[SnapshotOut]:
    rows = (
        await session.execute(
            select(ConfigSnapshot)
            .order_by(ConfigSnapshot.version.desc())
            .limit(limit)
        )
    ).scalars().all()
    return [
        SnapshotOut(
            id=str(r.id), version=r.version, note=r.note, created_at=r.created_at
        )
        for r in rows
    ]
