# LLM providers

Each provider declares its wire protocol via `kind`:

| `kind` | wire protocol | backend | when to use |
|---|---|---|---|
| `openai` (default) | OpenAI-compat REST | `langchain_openai.ChatOpenAI` | OpenAI itself, vLLM, Together, Ollama-compat, Anthropic's OpenAI-compat shim, internal OpenAI-style fronts |
| `anthropic` | Anthropic native API | `langchain_anthropic.ChatAnthropic` | `api.anthropic.com`, Claude-API-compatible internal endpoints (FortiAI, fwb-aiserver, Bedrock-style proxies) |

Existing yaml without a `kind` field continues to load as `kind:
openai` — zero migration burden.

## OpenAI-compatible (default)

Any OpenAI-compatible endpoint, vLLM-style sampling params first-class:

```yaml
llm:
  default_provider: shared
  providers:
    shared:
      base_url: https://api.openai.com/v1
      api_key: sk-...
      model_name: gpt-4o
      temperature: 0
      top_p: 1
```

For vLLM-style sampling fields (`top_k`, `repetition_penalty`), see
[Configuration → Sampling parameters](configuration.md#sampling-parameters).

## Anthropic native

Native Anthropic API via `langchain-anthropic`, including
extended-thinking mode. Use this for `api.anthropic.com` or any
Claude-API-compatible internal endpoint:

```yaml
llm:
  default_provider: anthropic
  providers:
    anthropic:
      kind: anthropic
      base_url: https://api.anthropic.com    # or your internal Claude-API-compatible endpoint
      api_key: sk-ant-...
      model_name: claude-sonnet-4-7
      thinking_effort: high                   # → ChatAnthropic(effort="high")
                                              #   one of: low | medium | high | xhigh | max
      temperature: 0
      top_k: 64
      top_p: 0.95
```

### `thinking_effort` is preferred

`thinking_effort` is the **preferred** extended-thinking dial on
Anthropic providers. Operators on the legacy `thinking_enabled: true`
shape (which routed to `thinking={"type":"enabled","budget_tokens":N}`)
keep working but see a one-time WARNING per run recommending the
migration — Anthropic is deprecating that older parameter shape.

## Sampling-field mapping by `kind`

| sampling field | `kind: openai` | `kind: anthropic` |
|---|---|---|
| `temperature`, `top_p` | top-level | top-level |
| `top_k` | `extra_body["top_k"]` | top-level (Anthropic accepts it natively) |
| `repetition_penalty` | `extra_body["repetition_penalty"]` | dropped + once-per-run WARNING (no Anthropic equivalent) |

## Mixing providers per agent

Different agents can use different providers — for example, a
deterministic validator on `kind: anthropic` and a creative analyzer
on `kind: openai`:

```yaml
llm:
  default_provider: shared
  providers:
    shared:
      kind: openai
      base_url: https://api.openai.com/v1
      api_key: sk-...
      model_name: gpt-4o
      temperature: 0.2
    validator_provider:
      kind: anthropic
      base_url: https://api.anthropic.com
      api_key: sk-ant-...
      model_name: claude-sonnet-4-7
      thinking_effort: high
      temperature: 0
agents:
  auditor:
    llm:
      provider: shared
  validator:
    llm:
      provider: validator_provider
```

For the per-agent override mechanics, see
[Configuration → Per-role overrides](configuration.md#per-role-overrides).

## See also

- [Configuration reference](configuration.md) — full sampling-field
  semantics, env vars, workflow contract
- [Teaming mode](teaming.md) — fan out across multiple providers per stage
