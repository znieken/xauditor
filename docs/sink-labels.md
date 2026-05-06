# Sink labels

Functions that represent dangerous I/O — process spawn, code
eval, deserialization, outbound HTTP, SQL execution, file
operations, template rendering — are tagged with
`is_well_known_sink: true` and a `sink_kind` string at
graph-build time. The labels gate `SinkAuditUnit` and
`BoundaryAuditUnit` enumeration when
`audit.experimental.units: true`.

The labelling layer lives in
`src/xauditor/graph/sink_labelling.py`; the runtime overlay
that merges operator overrides with the default table is
`resolve_sink_set(well_known, custom)`.

## The sink_kind taxonomy

| `sink_kind` | What it represents | Vuln class anchor |
|---|---|---|
| `subprocess` | OS process spawn | shell command injection |
| `command` | `eval` / `exec` / `compile` | code injection |
| `deserializer` | pickle / yaml / marshal-style decoders | deserialization |
| `http_client` | outbound HTTP request | SSRF |
| `sql` | SQL execution | SQL injection |
| `filesystem` | file write / unlink | path traversal |
| `rendering` | template render | XSS / template injection |

The closed set is exported as `SINK_KINDS` from
`sink_labelling.py`. Two of the seven (`http_client` and
`subprocess`) additionally drive `BoundaryAuditUnit` emission
since calls into them cross out of the in-process trust zone
— see [Audit units](audit-units.md#boundaryauditunit).

## Default sink table

The cross-language default table maps fully-qualified names
(FQNs) to `sink_kind` values. Coverage is heaviest on Python
because that's xauditor's primary audit target; other
languages carry the most-common offenders only. Add to
`audit.sinks.custom` for codebase-specific sinks the default
table doesn't know about.

### Python

| FQN | `sink_kind` |
|---|---|
| `subprocess.run`, `subprocess.Popen`, `subprocess.call`, `subprocess.check_call`, `subprocess.check_output`, `subprocess.getoutput`, `subprocess.getstatusoutput` | `subprocess` |
| `os.system`, `os.popen`, `os.execv`, `os.execve`, `os.execvp`, `os.spawnl`, `os.spawnv` | `subprocess` |
| `builtins.eval`, `builtins.exec`, `builtins.compile`, `eval`, `exec`, `compile` | `command` |
| `pickle.loads`, `pickle.load`, `marshal.loads`, `marshal.load`, `yaml.load`, `yaml.unsafe_load`, `shelve.open`, `dill.loads`, `dill.load` | `deserializer` |
| `requests.{get,post,put,delete,patch,head,request}` | `http_client` |
| `urllib.request.urlopen`, `urllib.request.Request` | `http_client` |
| `httpx.{get,post,put,delete,patch,request}` | `http_client` |
| `aiohttp.ClientSession.{get,post}` | `http_client` |
| `jinja2.Template.render`, `jinja2.Environment.from_string`, `django.template.Template.render` | `rendering` |

### Go

| FQN | `sink_kind` |
|---|---|
| `os/exec.Command`, `os/exec.CommandContext` | `subprocess` |
| `database/sql.DB.{Query,QueryContext,Exec,ExecContext}` | `sql` |
| `net/http.{Get,Post,NewRequest}`, `net/http.Client.Do` | `http_client` |

### Java

| FQN | `sink_kind` |
|---|---|
| `java.lang.Runtime.exec`, `java.lang.ProcessBuilder.start` | `subprocess` |
| `java.sql.Statement.{execute,executeQuery,executeUpdate}` | `sql` |
| `java.sql.PreparedStatement.{execute,executeQuery,executeUpdate}` | `sql` |
| `java.io.ObjectInputStream.readObject` | `deserializer` |
| `java.net.http.HttpClient.{send,sendAsync}` | `http_client` |

### JavaScript / TypeScript

| FQN | `sink_kind` |
|---|---|
| `child_process.{exec,execSync,spawn,spawnSync,execFile,execFileSync}` | `subprocess` |
| `globalThis.eval`, `Function` (constructor) | `command` |
| `globalThis.fetch`, `axios.{get,post,put,delete,request}` | `http_client` |

### C / C++

| FQN | `sink_kind` |
|---|---|
| `system`, `popen`, `execve`, `execvp`, `execl`, `execlp`, `execle` | `subprocess` |

(NB: `system` collides with Python's identical name. Both are
labelled the same kind, and the graph builder disambiguates by
file language at parse time.)

### Rust

| FQN | `sink_kind` |
|---|---|
| `std::process::Command`, `tokio::process::Command` | `subprocess` |

## Operator overrides — `audit.sinks.*`

```yaml
audit:
  sinks:
    well_known: []        # OPTIONAL allow-list pruning the default
                          # table. Empty = use the full table
                          # (default). Non-empty = only the listed
                          # FQNs are eligible.
    custom:               # Additive operator-defined sinks.
      - myapp.run_shell
      - myapp.danger.exec
```

Resolution semantics, implemented in
`resolve_sink_set(well_known, custom)`:

1. Start from the full `_DEFAULT_SINK_TABLE`.
2. If `well_known` is non-empty, intersect: keep only the
   listed FQNs (drops the rest from scope — useful for
   pruning noise from sinks the codebase provably doesn't
   call).
3. Add every FQN in `custom`. Custom entries land with
   `sink_kind=""` (uncategorized) since the config shape
   doesn't carry per-FQN sink_kind; the graph still tags them
   `is_well_known_sink: true`, just without a kind.

Custom FQNs that collide with default-table entries are
silently no-ops (they'd have no `sink_kind` to assign over the
default). A future change may upgrade `custom` to a richer
dict-shape (`{fqn: sink_kind}`) if operators ask for it.

### Env-var binding

| Env var | Maps to |
|---|---|
| `XAUDITOR_AUDIT_SINKS_WELL_KNOWN` | `audit.sinks.well_known` (comma-separated) |
| `XAUDITOR_AUDIT_SINKS_CUSTOM` | `audit.sinks.custom` (comma-separated) |

## How the labels flow through the pipeline

1. **Parse** — the language parser produces
   `_ParsedFunction` records for in-repo functions and
   `_ParsedCall` records for every call site (including
   stdlib / third-party).
2. **Canonical** — `canonical.py`'s
   `_synthesize_sink_stubs(...)` walks calls to FQNs that
   match the resolved sink set, and creates one stub
   `FunctionRecord` per distinct external FQN. Stubs carry
   `is_external=True, is_well_known_sink=True,
   sink_kind=<kind>` and `source=""`.
3. **Graph write** — both parsed and stub Function records
   land as `:Function` nodes; the sink properties are
   persisted alongside.
4. **Audit planner** —
   `_enumerate_sink_units(source, plan)` walks Function
   records with `is_well_known_sink: true` and emits one
   `SinkAuditUnit` per match (with inbound paths attached).
   `_enumerate_boundary_units(source, plan)` filters further
   to `sink_kind ∈ {http_client, subprocess}`.

## See also

- [Audit units](audit-units.md) — `SinkAuditUnit`,
  `BoundaryAuditUnit`, the `audit.experimental.units` flag
- [Configuration reference](configuration.md) —
  `audit.sinks.*` config block
- [Framework heuristics](framework-heuristics.md) —
  decorator → framework / intent table for `EntryAuditUnit`
