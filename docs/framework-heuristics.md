# Framework heuristics

When the parser captures a decorator on a function, the
graph-build pipeline runs the decorator's source-text through
`classify_decorator(expression)` to produce a
`(framework, intent)` pair. The `intent` value drives:

- `EntryAuditUnit` enumeration — functions with
  `intent ∈ {route, task_handler, cli_entry}` are entries.
- `GraphSlice.entry_classification` — a deterministic
  `EntryKind` derived from the decorator chain when any
  decorator carries an entry-class intent.
- The portal's per-finding decorator-chain panel — annotates
  each decorator with its framework + intent.

The classifier lives in
`src/xauditor/graph/framework_heuristics.py`.

## The `intent` closed set

| `intent` | What it represents | Used for entry detection |
|---|---|---|
| `route` | HTTP request handler binding | yes |
| `task_handler` | async task / celery worker | yes |
| `cli_entry` | CLI command entry | yes |
| `auth_required` | auth check / permission gate | no — wrapper only |
| `csrf_exempt` | explicit CSRF bypass | no — wrapper only |
| `staticmethod` | Python `@staticmethod` | no |
| `classmethod` | Python `@classmethod` | no |
| `property` | Python `@property` | no |
| `cached_property` | Python `@cached_property` | no |
| `dataclass` | Python `@dataclass` | no |
| `""` (empty) | unrecognised / not-load-bearing | no |

The closed set is exported as `INTENT_VALUES` from the
heuristics module. Adding a new intent is a code change; the
table is fixed today (no operator config knob).

## Resolution algorithm

`classify_decorator(expression)` does substring matching over
the decorator's source-text in declaration order:

1. Strip whitespace from the expression.
2. Walk the rule table top-to-bottom; return on the first
   substring hit.
3. If nothing matches, return `("", "")` — the decorator is
   captured in the graph but doesn't influence entry
   classification or registration intent.

Order matters in the rule table: more-specific patterns come
first (`.route(` precedes the bare-keyword stdlib matchers).
The classifier never tries to resolve the decorator's import
chain — substring matching against the source-text expression
is enough for the typical `from flask import Flask; app =
Flask(__name__); @app.route(...)` shape, where the decorator
expression is literally `app.route('/users')`.

## The default rule table

### HTTP request routes

| Substring | `framework` | `intent` |
|---|---|---|
| `.route(` | `flask_or_fastapi` | `route` |
| `.get(` | `fastapi` | `route` |
| `.post(` | `fastapi` | `route` |
| `.put(` | `fastapi` | `route` |
| `.patch(` | `fastapi` | `route` |
| `.delete(` | `fastapi` | `route` |
| `.head(` | `fastapi` | `route` |
| `.options(` | `fastapi` | `route` |
| `.websocket(` | `starlette` | `route` |
| `.on_event(` | `starlette` | `route` |

The `flask_or_fastapi` framework label reflects the ambiguity
of `.route(` — both Flask (`@app.route('/x')`) and FastAPI's
older API decorators use it. The `fastapi` label on the verb
forms (`.get(`, `.post(`, etc.) is biased toward FastAPI but
also matches `@router.get(...)` etc. across Starlette and
similar frameworks. Downstream consumers treat `framework` as
an annotation, not a contract.

### Async / task handlers

| Substring | `framework` | `intent` |
|---|---|---|
| `celery.task` | `celery` | `task_handler` |
| `celery.shared_task` | `celery` | `task_handler` |
| `shared_task` | `celery` | `task_handler` |
| `.task(` | `celery_or_dramatiq` | `task_handler` |
| `dramatiq.actor` | `dramatiq` | `task_handler` |
| `@actor` | `dramatiq` | `task_handler` |

### CLI entries

| Substring | `framework` | `intent` |
|---|---|---|
| `click.command` | `click` | `cli_entry` |
| `click.group` | `click` | `cli_entry` |
| `.command(` | `click_or_typer` | `cli_entry` |
| `typer.callback` | `typer` | `cli_entry` |

### Auth / permission gates

| Substring | `framework` | `intent` |
|---|---|---|
| `login_required` | `auth` | `auth_required` |
| `require_admin` | `auth` | `auth_required` |
| `require_authenticated_user` | `auth` | `auth_required` |
| `require_authenticated` | `auth` | `auth_required` |
| `permission_required` | `auth` | `auth_required` |
| `user_passes_test` | `django.auth` | `auth_required` |
| `staff_member_required` | `django.auth` | `auth_required` |

These match wrapper decorators only; they don't make a
function an entry on their own. They give the analyzer
enough context to reason about authn / authz boundaries
when a route decorator is also present on the same chain.

### CSRF

| Substring | `framework` | `intent` |
|---|---|---|
| `csrf_exempt` | `django.csrf` | `csrf_exempt` |

### Python builtins / stdlib

| Substring | `framework` | `intent` |
|---|---|---|
| `staticmethod` | `python` | `staticmethod` |
| `classmethod` | `python` | `classmethod` |
| `cached_property` | `python` | `cached_property` |
| `property` | `python` | `property` |
| `dataclass` | `python` | `dataclass` |

These appear last in the rule table so framework matchers
take precedence (a hypothetical `@my.staticmethod` would
match the framework rule first if one existed).

## Fallback for unrecognised decorators

When `classify_decorator` returns `("", "")` the decorator is
still captured in the graph as a `Decorator` node with
`framework=""` and `intent=""`. It contributes to the
`decorator_chain` field of `GraphSlice` but doesn't influence
entry classification. Operators who want their codebase's
custom decorators recognised will need to extend the rule
table — today via a code change; a future config knob may
expose this if demand justifies it.

The bias here is intentional: false-positive intent labels
("yes, this looks like a route") would propagate into entry
enumeration and false-positive findings. Returning `("", "")`
on uncertainty is the safer default.

## Graph-build flow

1. **Parse** — `PythonParser._extract_decorators(node)` walks
   `decorator_list` in reverse order (so position 0 = closest
   to the function definition / applied first at runtime) and
   captures each decorator's source-text expression.
2. **Canonical** — `_build_decorator_records(functions)` runs
   `classify_decorator` on each captured expression, builds
   one `DecoratorRecord` + one `FunctionDecoratorEdge` per
   decorator, and emits MERGE keys derived from
   `(file_path, line_number, expression)`.
3. **Graph write** — Decorator nodes carry `framework` and
   `intent` properties; the edge carries `position`.
4. **Audit planner** —
   `_enumerate_entry_units(source)` reads decorators per
   function and marks the function as an entry when any
   decorator's `intent ∈ {route, task_handler, cli_entry}`.

## See also

- [Audit units](audit-units.md) — `EntryAuditUnit` emission
  driven by entry intents
- [Audit modes](audit-modes.md) — `GraphSlice.decorator_chain`
  + `entry_classification` fields
- [Sink labels](sink-labels.md) — the `sink_kind` taxonomy
  for the dual-use case of decorators on functions that also
  call sinks
- [Configuration reference](configuration.md) —
  `audit.experimental.{units,graph_slice}` flags
