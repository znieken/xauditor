# Reports and exports

xauditor 0.5.0+ persists every audit run into PostgreSQL. The
canonical artefact is the `audit_runs` row plus its child rows
(`findings`, `coverage_modules` / `_files` / `_functions`,
`validator_debates`, per-stage `*_subagent_records`, and
`coder_findings` when [coder verification](coder.md) ran). The
[portal](portal.md) reads from that schema directly.

To export a portable artefact, run `xauditor audit export <run_id>`
against any persisted run.

## JSON envelope (canonical)

```bash
xauditor audit export 20260429-010203                                 # JSON envelope to stdout
xauditor audit export 20260429-010203 --include-debug                 # add cli_exit_code / cli_stderr fields
```

The JSON envelope is versioned (`format_version: "1"`) so downstream
tooling can pin against a stable contract. JSON is the recommended
format for downstream automation (CI scripts, RL post-training
datasets, custom dashboards).

## Markdown bundle

```bash
xauditor audit export 20260429-010203 --format markdown \
    --output-dir bundle/
```

The Markdown bundle writes:

| File | Content |
|---|---|
| `findings.md` | Findings with confidence, source evidence, exploitation guidance, validation output, and coder verdicts |
| `false-positives.md` | Findings the validator marked False Positive |
| `coverage-report.md` | Audited versus unaudited modules, files, functions, and percentage coverage |
| `coder-results.md` | Per-finding coder verdicts (when [coder verification](coder.md) ran) |

## Per-stage Markdown (deferred)

Per-stage Markdown files (`analyzer-results.md` / `validator-results.md`
/ `exploitation-results.md` / `no-findings.md` / per-stage subagent
transcripts / debate files) are deferred to a follow-up — the
underlying data is persisted in `path_raw_outputs.raw_markdown` but
the export-side reconstruction is the missing piece.

## See also

- [Portal](portal.md) — UI for triaging findings + human feedback capture
- [Coder verification](coder.md) — `coder_findings` schema details
- [CLI reference](cli.md) — `audit export` flags and forms
