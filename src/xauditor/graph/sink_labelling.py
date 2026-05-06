"""Sink labelling — match Function nodes against a known-sink table.

`capture-decorators-and-registrations` Phase 1.4. Labels Function
nodes that match a documented well-known sink list with
`(is_well_known_sink: true, sink_kind: <kind>)` so SinkAuditUnit
enumeration can later anchor on these nodes.

Foundation pass (`capture-decorators-and-registrations` Commit 1):
this module ships the algorithmic core (default sink table +
`label_sinks(...)` function) without yet wiring it into the graph
build pipeline. The graph builder integration + Neo4j writer
extension + planner emission ship in Commit 2.

Multi-language note: the FQN strings in `_DEFAULT_SINK_TABLE` are
language-tagged where the same conceptual sink has different
import paths across languages (e.g., Python `subprocess.run` vs
Go `os/exec.Command`). The matching is exact-string against
`FunctionRecord.qualified_name`, so per-language coverage depends
on each parser emitting consistent FQNs. Operators extend via
`audit.sinks.custom` for codebase-specific sinks not in the
default table.
"""

from __future__ import annotations

from typing import Iterable

from xauditor.models import FunctionRecord


# ---------------------------------------------------------------------------
# Default sink table — FQN → sink_kind
# ---------------------------------------------------------------------------


SINK_KINDS: tuple[str, ...] = (
    "subprocess",     # OS process spawn (shell command injection class)
    "command",        # eval / exec — code injection class
    "deserializer",   # pickle / yaml — deserialization class
    "http_client",    # outbound HTTP — SSRF class
    "sql",            # SQL execution — injection class
    "filesystem",     # file write / unlink — path traversal class
    "rendering",      # template render — XSS / template injection class
)


_DEFAULT_SINK_TABLE: dict[str, str] = {
    # ----- Python: subprocess -----
    "subprocess.run": "subprocess",
    "subprocess.Popen": "subprocess",
    "subprocess.call": "subprocess",
    "subprocess.check_call": "subprocess",
    "subprocess.check_output": "subprocess",
    "subprocess.getoutput": "subprocess",
    "subprocess.getstatusoutput": "subprocess",
    "os.system": "subprocess",
    "os.popen": "subprocess",
    "os.execv": "subprocess",
    "os.execve": "subprocess",
    "os.execvp": "subprocess",
    "os.spawnl": "subprocess",
    "os.spawnv": "subprocess",
    # ----- Python: command (eval / exec) -----
    "builtins.eval": "command",
    "builtins.exec": "command",
    "builtins.compile": "command",
    "eval": "command",
    "exec": "command",
    "compile": "command",
    # ----- Python: deserialization -----
    "pickle.loads": "deserializer",
    "pickle.load": "deserializer",
    "marshal.loads": "deserializer",
    "marshal.load": "deserializer",
    "yaml.load": "deserializer",
    "yaml.unsafe_load": "deserializer",
    "shelve.open": "deserializer",
    "dill.loads": "deserializer",
    "dill.load": "deserializer",
    # ----- Python: http_client (outbound) -----
    "requests.get": "http_client",
    "requests.post": "http_client",
    "requests.put": "http_client",
    "requests.delete": "http_client",
    "requests.patch": "http_client",
    "requests.head": "http_client",
    "requests.request": "http_client",
    "urllib.request.urlopen": "http_client",
    "urllib.request.Request": "http_client",
    "httpx.get": "http_client",
    "httpx.post": "http_client",
    "httpx.put": "http_client",
    "httpx.delete": "http_client",
    "httpx.patch": "http_client",
    "httpx.request": "http_client",
    "aiohttp.ClientSession.get": "http_client",
    "aiohttp.ClientSession.post": "http_client",
    # ----- Python: rendering -----
    "jinja2.Template.render": "rendering",
    "jinja2.Environment.from_string": "rendering",
    "django.template.Template.render": "rendering",
    # ----- Go: subprocess (os/exec) -----
    "os/exec.Command": "subprocess",
    "os/exec.CommandContext": "subprocess",
    # ----- Go: sql -----
    "database/sql.DB.Query": "sql",
    "database/sql.DB.QueryContext": "sql",
    "database/sql.DB.Exec": "sql",
    "database/sql.DB.ExecContext": "sql",
    # ----- Go: http_client -----
    "net/http.Get": "http_client",
    "net/http.Post": "http_client",
    "net/http.NewRequest": "http_client",
    "net/http.Client.Do": "http_client",
    # ----- Java: subprocess -----
    "java.lang.Runtime.exec": "subprocess",
    "java.lang.ProcessBuilder.start": "subprocess",
    # ----- Java: sql -----
    "java.sql.Statement.execute": "sql",
    "java.sql.Statement.executeQuery": "sql",
    "java.sql.Statement.executeUpdate": "sql",
    "java.sql.PreparedStatement.execute": "sql",
    "java.sql.PreparedStatement.executeQuery": "sql",
    "java.sql.PreparedStatement.executeUpdate": "sql",
    # ----- Java: deserialization -----
    "java.io.ObjectInputStream.readObject": "deserializer",
    # ----- Java: http_client -----
    "java.net.http.HttpClient.send": "http_client",
    "java.net.http.HttpClient.sendAsync": "http_client",
    # ----- JavaScript / TypeScript: subprocess -----
    "child_process.exec": "subprocess",
    "child_process.execSync": "subprocess",
    "child_process.spawn": "subprocess",
    "child_process.spawnSync": "subprocess",
    "child_process.execFile": "subprocess",
    "child_process.execFileSync": "subprocess",
    # ----- JavaScript / TypeScript: command -----
    "globalThis.eval": "command",
    "Function": "command",  # `new Function(...)` constructor
    # ----- JavaScript / TypeScript: http_client -----
    "globalThis.fetch": "http_client",
    "axios.get": "http_client",
    "axios.post": "http_client",
    "axios.put": "http_client",
    "axios.delete": "http_client",
    "axios.request": "http_client",
    # ----- C / C++: subprocess -----
    "system": "subprocess",  # NB: also matches Python's identical name
    "popen": "subprocess",
    "execve": "subprocess",
    "execvp": "subprocess",
    "execl": "subprocess",
    "execlp": "subprocess",
    "execle": "subprocess",
    # ----- Rust: subprocess -----
    "std::process::Command": "subprocess",
    "tokio::process::Command": "subprocess",
}


# ---------------------------------------------------------------------------
# Resolver — apply operator config on top of the default table
# ---------------------------------------------------------------------------


def resolve_sink_set(
    *,
    well_known: Iterable[str] = (),
    custom: Iterable[str] = (),
) -> dict[str, str]:
    """Return the effective `{fqn: sink_kind}` table for an audit run.

    `well_known` is an OPTIONAL allow-list. When empty (default),
    the full `_DEFAULT_SINK_TABLE` is in scope. When non-empty,
    only the listed FQNs are eligible (operator pruned the default
    list to suppress noise from sinks they know aren't relevant).
    `custom` adds operator-defined FQNs (sink_kind="" since the
    config shape doesn't carry per-FQN sink_kind today).

    `well_known` entries that don't appear in the default table
    are silently dropped (they'd have no sink_kind to assign);
    operators should add such FQNs via `custom` instead.
    """

    well_known_tuple = tuple(well_known)
    if not well_known_tuple:
        # Empty `well_known` → full default table is in scope.
        result = dict(_DEFAULT_SINK_TABLE)
    else:
        # Restrict to listed FQNs. Skip entries that don't have a
        # sink_kind in the default table.
        result = {
            fqn: kind
            for fqn, kind in _DEFAULT_SINK_TABLE.items()
            if fqn in well_known_tuple
        }
    for fqn in custom:
        if not fqn:
            continue
        # Operator-added FQNs land with sink_kind="" (uncategorized).
        # Don't overwrite a more-specific kind from the default table.
        result.setdefault(fqn, "")
    return result


# ---------------------------------------------------------------------------
# Labelling pass
# ---------------------------------------------------------------------------


def label_sinks(
    functions: Iterable[FunctionRecord],
    sink_set: dict[str, str],
) -> dict[str, str]:
    """Label each Function whose `qualified_name` matches the sink_set.

    Returns a dict `{function_id: sink_kind}` containing only the
    matched functions. Callers (graph builder) feed this into the
    Neo4j writer to set the `is_well_known_sink: true` +
    `sink_kind: <kind>` properties on the corresponding Function
    nodes.

    Functions not in the sink_set are simply absent from the
    return value (caller treats absence as
    `is_well_known_sink: false`).
    """

    if not sink_set:
        return {}
    labels: dict[str, str] = {}
    for fn in functions:
        kind = sink_set.get(fn.qualified_name)
        if kind is None:
            continue
        labels[fn.function_id] = kind
    return labels


__all__ = [
    "SINK_KINDS",
    "label_sinks",
    "resolve_sink_set",
]
