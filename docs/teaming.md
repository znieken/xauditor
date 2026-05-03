# Teaming mode

Teaming mode runs the audit with three cooperating teams per path —
`Analyzer → Validator → Exploiter` — where each team fans out to
multiple subagents (each subagent can use a distinct provider). It is
opt-in; single-agent behavior is unchanged when the `teaming` block
is absent or `teaming.enabled` is false.

## Configuration

```yaml
teaming:
  enabled: true
  analyzer:
    subagent_count: 3
    provider_list: [provider_a, provider_b]
  validator:
    subagent_count: 3
    provider_list: [provider_a, provider_b, provider_c]
    debate_rounds: 5
  exploiter:
    subagent_count: 2
    provider_list: [provider_a, provider_b]
```

The matching environment overrides:

| Env var | Maps to |
|---|---|
| `XAUDITOR_TEAMING_ENABLED` | `teaming.enabled` |
| `XAUDITOR_TEAMING_ANALYZER_SUBAGENT_COUNT` | `teaming.analyzer.subagent_count` |
| `XAUDITOR_TEAMING_VALIDATOR_SUBAGENT_COUNT` | `teaming.validator.subagent_count` |
| `XAUDITOR_TEAMING_EXPLOITER_SUBAGENT_COUNT` | `teaming.exploiter.subagent_count` |
| `XAUDITOR_TEAMING_ANALYZER_PROVIDER_LIST` | `teaming.analyzer.provider_list` (comma-separated) |
| `XAUDITOR_TEAMING_VALIDATOR_PROVIDER_LIST` | `teaming.validator.provider_list` (comma-separated) |
| `XAUDITOR_TEAMING_EXPLOITER_PROVIDER_LIST` | `teaming.exploiter.provider_list` (comma-separated) |
| `XAUDITOR_TEAMING_VALIDATOR_DEBATE_ROUNDS` | `teaming.validator.debate_rounds` |

## Provider list semantics

Provider lists cycle with modulo, so a 3-subagent team backed by
`[a, b]` yields `[a, b, a]`. Every provider listed MUST also be
defined under `llm.providers` (see [Providers](providers.md) for the
full provider declaration shape).

## Cost model

Cost scales roughly with `sum(subagent_count)` per path, plus debate
rounds when validator subagents disagree. Measure your provider cost
budget before raising subagent counts.

## Output layout

When teaming mode is on, each run directory contains
`analyzer-subagents/`, `validator-subagents/`, `exploiter-subagents/`,
and `validator-debates/` subdirectories alongside the consolidated
stage Markdown files. The portal renders the same structure inside
each finding card.

## See also

- [Coder verification](coder.md) — pairs naturally with teaming for a
  4th, repo-global verdict per finding
- [Providers](providers.md) — provider declaration syntax
- [Configuration reference](configuration.md) — full xauditor.yml schema
