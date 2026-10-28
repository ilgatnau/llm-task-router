> Historical optional-hosted guide. The current default is local-only; see README.md. To exercise this legacy mode set LOCAL_ONLY=false and explicitly request a free-* alias. Current automatic rules select local models. For WSL Ollama set OLLAMA_API_BASE and omit the bundled profile. Free model IDs are configured in free-pool.yaml; FREE_* environment variables are obsolete.

# OpenCode task router: free hosted models + local Ollama

This setup is intended to spend **zero dollars on hosted inference**. OpenCode sends requests to the task gateway. The gateway uses a verified free-only OpenRouter pool for selected tasks and LiteLLM → Ollama for local work and fallback. Hosted traffic goes directly to OpenRouter so the gateway can enforce the exact model ID, zero-price provider selection, inspect rate-limit headers, and query account quota. LiteLLM has only local models configured.

## Start

Requires Docker Compose and OpenCode. From this directory:

```sh
cp .env.example .env
# Add OPENROUTER_API_KEY. Do not purchase credits or enable auto top-up.
docker compose --profile bundled up -d --build
docker compose exec ollama ollama pull qwen2.5-coder:7b
docker compose exec ollama ollama pull qwen3:8b
opencode
```

If you already have `.env`, keep its OpenRouter key and local settings; the paid-provider keys and FREE_* model variables from earlier versions are no longer used. `free-pool.yaml` is now the single source of hosted model IDs. Rebuild with the command above to apply the free-only change.

OpenCode reads project `opencode.json`. Merge provider/model settings into your existing global configuration if desired. Choose local models appropriate to your hardware and pull their Ollama tags without the `ollama_chat/` prefix. Compose uses CPU inference; GPU support is host dependent. Local inference consumes hardware and electricity, but no hosted API fees. Without an OpenRouter key, free-task requests fall back locally.

## Task selection and free model switching

| Task / prefix | Ordered free pool |
| --- | --- |
| Unit tests, pytest, coverage; `/tests` | North Mini Code → Inkling Small → Nemotron 3 Ultra |
| Architecture, tradeoffs, hard debugging; `/architect` or `/reason` | Nemotron 3 Ultra → Inkling Small → North Mini Code |
| Implementation, refactoring; `/code` or `/cloud` | Inkling Small → North Mini Code → Nemotron 3 Ultra |
| CI/CD, Terraform, pipelines; `/cicd` or `/deploy` | North Mini Code → Inkling Small → Nemotron 3 Ultra |
| `/local` or `/private`, routine default | Local coder; never sent to hosted models |
| Summaries, translation, documentation | Local general model |

These are starting task recommendations, not workload-specific benchmark results. IDs were verified in the [OpenRouter catalog](https://openrouter.ai/api/v1/models) on 2026-10-28:

- [Cohere North Mini Code](https://openrouter.ai/cohere/north-mini-code:free): `cohere/north-mini-code:free`; coding, terminal tasks, tool use.
- [Thinking Machines Inkling Small](https://openrouter.ai/thinkingmachines/inkling-small:free): `thinkingmachines/inkling-small:free`; coding and agent workflows. Its free endpoint is limited to agent harnesses such as OpenCode and logs prompts/outputs for improvement. It prohibits confidential/personal data.
- [NVIDIA Nemotron 3 Ultra](https://openrouter.ai/nvidia/nemotron-3-ultra-550b-a55b:free): `nvidia/nemotron-3-ultra-550b-a55b:free`; reasoning, planning, long context. Its free endpoint also discloses logging and prohibits confidential/personal data.

Edit keywords and task priority in `rules.yaml`; candidate order and IDs in `free-pool.yaml`. Restart the router to reload. Explicit OpenCode model selection overrides keyword rules, but free task aliases still use their ordered fallback pool. Message prefixes override keywords. Keyword priority is architecture → CI/CD → tests → implementation → writing → local default. Prefixes remain in the model prompt.

A model-specific 429 puts that model into a cooldown (Retry-After seconds, otherwise 60 seconds) and tries the next verified free candidate. Temporary server/network failures or a removed model can also advance the pool. There are at most three hosted attempts per request and no hidden inference retries. Shared rate-limit headers stop switching immediately. Authentication/credit errors stop hosted attempts. Exhausted or unavailable pools fall back to the local coder. The local path never falls back to cloud. If Ollama is unavailable too, the request returns an error.

After streaming output begins, the gateway does not replay the request to another model: that could duplicate generated text or tool calls. Mid-stream errors propagate to the client.

## Zero-spend controls

- Paid aliases, provider overrides, paid plugins, and arbitrary request extras are rejected. The old OpenAI and Anthropic routes are removed.
- Only explicit IDs ending in `:free` can be configured in the hosted pool.
- The live catalog must list zero in every reported pricing field and advertise tool support. Pricing verification is cached for at most 60 seconds. If verification fails, use local inference.
- Every hosted request sets provider maximum prompt and completion prices to zero. No model auto-router, paid fallback, builtin server tools, media processing, or plugins are enabled. Hosted message parts must be text.
- Keep the provider account unfunded and auto top-up disabled as an additional account-level safeguard. The router does not change account billing settings or buy credits.

Pricing and service terms are controlled by the provider; this is enforcement of the configured free endpoints and zero-price filters, not a guarantee against bugs or provider billing changes. Check model eligibility/terms before sending code. For private work choose the local model explicitly for the entire session: a `/private` prefix applies to the current request only. Later hosted selections receive the conversation history, including code and tool results.

## Observe quota

```sh
curl -s http://127.0.0.1:8000/quota | python -m json.tool
# Optional, on hosts with watch:
watch -n 15 'curl -s http://127.0.0.1:8000/quota | python -m json.tool'
```

The JSON includes:

| Field | Meaning |
| --- | --- |
| `provider_reported_daily` | Provider free-request `used`, `limit`, `remaining`, plus reset information only if supplied; null if unavailable |
| `provider_observed_at` | Unix timestamp of the successful quota observation |
| `local_daily_estimate` | All hosted attempts through this gateway, including failures, counted in a UTC day; conservative cap of 50 |
| `local_minute_estimate` | Rolling 60-second local attempt count; cap of 20 |
| `conservative_remaining_daily` | Minimum of locally remaining allowance and provider remaining, if supplied |
| `models` | Model IDs, local attempts today, cooldowns; per-model quota is null because it is not reported |
| `last_event` | Last selected model or failure reason; no prompt or key content |

The gateway refreshes the account counter before each hosted attempt and when `/quota` is requested. Its local count is reserved before the network call, so failed/uncertain attempts count. Quota counters and model cooldowns persist in the Compose `quota` volume. Shared Retry-After cooldown is process-local; after restart the provider counter is checked again. Do not delete the volume to continue a daily allowance. Outside Compose, `QUOTA_DB` defaults to in-memory: set it to a durable SQLite file if running manually.

[OpenRouter limits documentation](https://openrouter.ai/docs/api-reference/limits) distinguishes account-wide free daily quota from model-specific capacity. The free tier currently has 20 requests/minute and 50 requests/day without buying credits. **All free models share that daily allowance**; switching models or keys does not create extra quota. The router never recommends purchasing a higher allowance and retains a conservative local 50/day cap even on a funded account.

The `/key` endpoint can report `free_model_daily_requests`; dollar fields such as `limit_remaining` are not request quota and are not used. If a response lacks a free-request counter, `/quota` clearly labels the remaining count as a local estimate. Other apps consuming the same account, provider accounting delays, or policy changes mean an estimate cannot be exact. Shared limits remain enforced by the provider. The local limiter assumes one router process; do not run multiple workers/replicas against the same account and expect a distributed reservation system.

When the shared quota is exhausted, use Ollama until it resets. Switching among independent providers would require separate free-tier adapters and accounts; this setup does not yet implement those. It does not evade provider limits.

Response headers `x-task-model`, `x-task-rule`, `x-upstream-model` (hosted success), and `x-fallback-reason` (local fallback) explain each decision. These rules select the model only: they do not grant deployment credentials or change OpenCode tool permissions.

## Verification and operations

```sh
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
curl -i http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"/local Write a Python greeting"}]}'
docker compose logs router litellm
```

Health checks the gateway process, not model readiness. Validate real tool calling on your chosen models. Only the gateway port is published, bound to loopback; it has no authentication and is intended for a trusted local machine. No prompt logging is added by this gateway. Provider logging policies are separate.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

Tests use mocked providers; no inference quota or paid requests are consumed. Python dependency ranges and container tags track upstream releases; pin tested versions/digests for reproducibility. Live inference requires your key and downloaded local models and has not been verified in this workspace.
