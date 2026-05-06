"""Pattern-match decorator expressions to (framework, intent) labels.

`capture-decorators-and-registrations` Phase 1.2.2.

The classifier operates on the decorator's source-text expression
(e.g. `'app.route("/users")'`, `'login_required'`,
`'celery.task'`). Substring + suffix matching keeps the rules
language-and-framework-agnostic without needing a full FQN
resolver — a typical Python codebase imports `from flask import
Flask` and uses `app = Flask(__name__)` then `@app.route(...)`,
which the classifier matches via the `.route(` substring.

The (framework, intent) pair is consumed by:
- GraphSlice's `entry_classification` field (deterministic when
  decorator chain has any `intent="route"` etc.).
- The portal's per-finding decorator-chain panel (annotates each
  decorator with its framework + intent).

Operators extending the table for codebase-specific decorators
do so via a future config knob; today the table is fixed in
this module.
"""

from __future__ import annotations


# Entries are checked in order — more specific patterns first.
#
# Each rule: (substring_to_match, framework, intent).
# `framework` is short and lowercase ("flask", "fastapi",
# "celery", "django", "click", "typer", "auth", "python", "...").
# `intent` is one of:
#   - "route"            — HTTP request handler binding
#   - "task_handler"     — async task / celery worker
#   - "cli_entry"        — CLI command entry
#   - "auth_required"    — auth check / permission gate
#   - "csrf_exempt"      — explicit CSRF bypass
#   - "staticmethod"     — Python @staticmethod
#   - "classmethod"      — Python @classmethod
#   - "property"         — Python @property
#   - "cached_property"  — Python @cached_property
#   - "dataclass"        — Python @dataclass
#   - ""                 — unrecognised / not-load-bearing

INTENT_VALUES: tuple[str, ...] = (
    "route",
    "task_handler",
    "cli_entry",
    "auth_required",
    "csrf_exempt",
    "staticmethod",
    "classmethod",
    "property",
    "cached_property",
    "dataclass",
)


_PATTERNS: tuple[tuple[str, str, str], ...] = (
    # ----- Web framework routes (HTTP entry binding) -----
    # Flask: @app.route("/path"), @blueprint.route(...)
    (".route(", "flask_or_fastapi", "route"),
    # FastAPI / Starlette: @app.get("/path"), @router.post(...)
    (".get(", "fastapi", "route"),
    (".post(", "fastapi", "route"),
    (".put(", "fastapi", "route"),
    (".patch(", "fastapi", "route"),
    (".delete(", "fastapi", "route"),
    (".head(", "fastapi", "route"),
    (".options(", "fastapi", "route"),
    # Starlette event handlers (websocket / on_event)
    (".websocket(", "starlette", "route"),
    (".on_event(", "starlette", "route"),
    # ----- Async tasks -----
    # Celery: @celery.task, @celery.shared_task, @app.task
    ("celery.task", "celery", "task_handler"),
    ("celery.shared_task", "celery", "task_handler"),
    ("shared_task", "celery", "task_handler"),
    (".task(", "celery_or_dramatiq", "task_handler"),
    # Dramatiq: @dramatiq.actor
    ("dramatiq.actor", "dramatiq", "task_handler"),
    ("@actor", "dramatiq", "task_handler"),
    # ----- CLI -----
    # Click: @click.command, @click.group, @cli.command
    ("click.command", "click", "cli_entry"),
    ("click.group", "click", "cli_entry"),
    (".command(", "click_or_typer", "cli_entry"),
    # Typer: @typer.callback
    ("typer.callback", "typer", "cli_entry"),
    # ----- Auth / permission gates -----
    ("login_required", "auth", "auth_required"),
    ("require_admin", "auth", "auth_required"),
    ("require_authenticated_user", "auth", "auth_required"),
    ("require_authenticated", "auth", "auth_required"),
    ("permission_required", "auth", "auth_required"),
    ("user_passes_test", "django.auth", "auth_required"),
    ("staff_member_required", "django.auth", "auth_required"),
    # ----- CSRF -----
    ("csrf_exempt", "django.csrf", "csrf_exempt"),
    # ----- Python builtin / stdlib -----
    # Order matters here — `staticmethod(...)` has no leading dot
    # so we match the bare keyword. Place AFTER framework
    # matchers in case operators write `@my.staticmethod`
    # (unlikely, but defensive).
    ("staticmethod", "python", "staticmethod"),
    ("classmethod", "python", "classmethod"),
    ("cached_property", "python", "cached_property"),
    ("property", "python", "property"),
    ("dataclass", "python", "dataclass"),
)


def classify_decorator(expression: str) -> tuple[str, str]:
    """Return `(framework, intent)` for a decorator's source-text
    expression. Returns `("", "")` when no pattern matches —
    operators / framework-heuristics extension can layer on top
    in a future change.

    Examples:
        >>> classify_decorator("app.route('/users')")
        ('flask_or_fastapi', 'route')
        >>> classify_decorator("router.post('/items')")
        ('fastapi', 'route')
        >>> classify_decorator("login_required")
        ('auth', 'auth_required')
        >>> classify_decorator("staticmethod")
        ('python', 'staticmethod')
        >>> classify_decorator("my_company.special_decorator()")
        ('', '')
    """

    if not expression:
        return "", ""
    expr = expression.strip()
    if not expr:
        return "", ""
    for pattern, framework, intent in _PATTERNS:
        if pattern in expr:
            return framework, intent
    return "", ""


__all__ = [
    "INTENT_VALUES",
    "classify_decorator",
]
