# Audit parallelism

`xauditor audit run` parallelises path execution through a pluggable
`WorkerPool`. There is one knob — `audit.worker_count` — and two pool
kinds picked automatically by its value. Per-path agents (analyzer →
exploitation → validator) remain strictly sequential within a single
path; concurrency exists between paths only.

## Configuration

```yaml
audit:
  worker_count: 1        # 1 (default) = inline; >= 2 = subprocess pool (1..16)
```

Environment override: `XAUDITOR_AUDIT_WORKER_COUNT`. Out-of-range
values raise `ConfigError` at parse time naming the offending
dot-path.

> The pre-0.10.0 `XAUDITOR_AUDIT_PATH_CONCURRENCY` env var is no
> longer recognised — stale values produce a one-line WARN at startup
> and are otherwise ignored.

## Pool kinds

### `worker_count: 1` (default) — `InlineExecutor`

Paths run synchronously on master's thread, one at a time. No
subprocess spawn, no thread pool, no IPC. Best fit for unit tests,
dev runs, and any audit whose total wall-clock is dominated by a
single path's LLM time. Host-wide in-flight = 1.

### `worker_count: N >= 2` — `LocalSubprocessPool`

Master spawns N subprocess workers via `multiprocessing.spawn` once
at pool construction (spawn cost is paid once per audit run, not per
path). Each subprocess loops over a shared queue and processes one
path at a time. Workers reconstruct a `Neo4jAuditGraphSource` from a
~200-byte connection descriptor, so worker-side memory is bounded by
Bolt session buffers (NOT graph size).

The master is a pure orchestrator: it submits paths, aggregates
`PathResult`s, and dispatches the [coder stage](coder.md) centrally
so there's exactly one `CoderDispatcher` regardless of worker count.
Host-wide in-flight = `worker_count`.

## Memory model

Every audit reads from Neo4j over Bolt. Total host RAM is:

```
Neo4j daemon JVM heap + (worker_count + 1) × ~80 MB Bolt clients
```

With `worker_count: 8` and a 600 MB graph, expect:

```
~512 MB (Neo4j JVM) + 9 × ~80 MB (Bolt clients) ≈ 1.2 GB
```

## Determinism

Findings get plan-order `finding_id`s (`F-0001`, `F-0002`, …)
regardless of pool kind — two runs against the same graph build with
different `worker_count` values produce the same finding identifiers
in the same order.

## Resilience model

- **Per-path LLM failure** is isolated as of 0.5.4 — a single failed
  path lands in `failed_paths` with `checkpoint_status:
  failed_llm_error`; siblings continue. A subsequent `xauditor audit
  resume` re-runs only failed and skipped paths.
- **Subprocess worker crash** (SIGSEGV, OOM kill) under
  `LocalSubprocessPool` is detected by the master's monitor thread;
  the at-most-one in-flight path on the dead worker resolves as
  `PathOutcome.WORKER_CRASHED` so the master never hangs on
  `future.result()`. Workers push a `("claim", index, worker_id)`
  message to the result queue before invoking `_process_one_path`,
  so when one worker dies mid-flight the master pops the crashed
  worker's claim entry by key in O(1). The remaining workers continue.
- **Neo4j network failure** (`ServiceUnavailable` / `SessionExpired`)
  inside a subprocess worker is classified as
  `PathOutcome.LLM_FAILED` with `reason: "neo4j_unavailable"` —
  `xauditor audit resume` re-runs those paths after the network
  recovers, mirroring the LLM-error UX from 0.5.4.
- **`KeyboardInterrupt` (Ctrl+C)** drives a cooperative shutdown
  budgeted by `audit.shutdown_timeout_seconds` (default `30`, range
  `[1, 600]`). The first SIGINT sets the workflow's `_cancelling` flag,
  calls `pool.cancel_all()` (a no-op for `InlineExecutor`; SIGTERMs
  in-flight subprocess workers under `LocalSubprocessPool`), drains the
  coder dispatcher inside its own `audit.coder.shutdown_timeout_seconds`
  inner cap, persists a partial-run snapshot, and raises
  `UserCancelledError`. In-flight subprocess workers escalate to
  SIGKILL when the per-worker grace window collapses (the `cancel`
  event short-circuits the per-worker grace from the configured value
  down to ~0.5 s once cancellation is observed). A second SIGINT
  within the budget force-exits without further cleanup. The shutdown
  emits structured `audit.shutdown.start` / `_done` / `audit.shutdown
  .complete` events per stage; if a stage's drain exceeds its inner
  cap, an `audit.shutdown.timeout` event names the stalled subsystem
  in its payload. See [Configuration → Audit cancellation](configuration.md#audit-cancellation)
  for the full key matrix.

## See also

- [CLI reference](cli.md) — `audit run` and `audit resume` commands
- [Coder verification](coder.md) — central dispatch independent of
  worker count
