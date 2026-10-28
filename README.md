# Online-first task router for OpenCode

Run your router locally. Prefer free online inference for implementation, architecture, and CI/CD; use Ollama in WSL for routine unit tests and as the final fallback when online quotas/capacity are exhausted.

```text
OpenCode → self-hosted task router
              ├─ routine unit tests / private work → LiteLLM → Ollama (WSL or Docker)
              └─ other coding / architecture / CI/CD
                    → Gemini free tier
                    → Groq free tier (OpenAI gpt-oss models)
                    → OpenRouter verified :free models
                    → LiteLLM → Ollama
```

Candidate order is task-specific: see `online-pool.yaml`. For coding/architecture, the initial order is Gemini 3.8 Flash → Groq gpt-oss-120b → Gemini 3.5 Flash-Lite → Groq gpt-oss-20b → OpenRouter's task-specific free pool → Ollama. Missing keys and unconfirmed free-tier accounts are skipped. Model assignments are starting preferences, not benchmark claims that online inference always beats your local hardware.

## Configure free online providers

```sh
cp .env.example .env
# Edit .env with your provider keys and WSL Ollama endpoint.
```

You can configure any subset of the following; no OpenRouter account is required for direct Gemini/Groq calls.

| Provider | Required settings | Account requirements |
| --- | --- | --- |
| Gemini direct | `GEMINI_API_KEY`, `GEMINI_FREE_TIER_CONFIRMED=true` | Google AI Studio project on its free tier; do not enable paid billing |
| Groq direct | `GROQ_API_KEY`, `GROQ_FREE_TIER_CONFIRMED=true` | Groq Free plan; do not upgrade to Developer |
| OpenRouter, optional | `OPENROUTER_API_KEY` | Exact zero-priced `:free` models only; keep account unfunded, auto top-up off |

Gemini and Groq confirmation flags default to false, so a key alone cannot silently enable an account that might be on a paid plan. **Their inference APIs cannot prove that your account is free or force a free billing tier.** The confirmation is your statement that you checked the provider dashboard. If billing is later enabled externally, the router cannot detect that change reliably. Leave a provider unconfirmed if you cannot establish its free-plan status. An absolute provider-independent billing guarantee is possible only with local inference; use `LOCAL_ONLY=true` for that mode.

OpenAI gpt-oss models on Groq are open-weight models served under Groq's plan, not OpenAI's paid GPT API. No paid OpenAI API route is configured.

**Upgrading from the previous local-only version:** set `LOCAL_ONLY=false` in your existing `.env`, add the provider settings above, and rebuild. An old explicit `LOCAL_ONLY=true` remains respected. Do not overwrite existing keys/settings blindly. Without a usable online provider the router continues through local inference.

## Start with Ollama running in WSL

Pull your local fallback models in WSL:

```sh
ollama pull qwen2.5-coder:7b
ollama pull qwen3:8b
curl http://127.0.0.1:11434/api/tags
```

Set `OLLAMA_API_BASE` in `.env` to an address reachable by the **LiteLLM container**. With Docker running in the same WSL distro, `http://host.docker.internal:11434` can reach the host through the supplied host-gateway mapping if Ollama listens on that interface. Docker Desktop, WSL NAT and mirrored networking differ: use a reachable WSL address if needed. Container `localhost` points to that container, not WSL.

If necessary, configure the WSL Ollama service with `OLLAMA_HOST=0.0.0.0:11434` and restart it, or set it when starting `ollama serve` manually. Restrict access to the trusted host/container network: Ollama has no authentication configured here and its port must not be exposed publicly.

```sh
docker compose up -d --build
# Verify container-to-WSL connectivity without requesting inference:
docker compose exec litellm python -c 'import os, urllib.request; print(urllib.request.urlopen(os.environ["OLLAMA_API_BASE"] + "/api/tags", timeout=10).status)'
opencode
```

OpenCode reads the project `opencode.json`. Merge its provider/model settings into your global config if needed. Connectivity checks do not prove generation or tool-call quality: test a small real task on each selected backend.

If you prefer bundled Ollama instead, keep `OLLAMA_API_BASE=http://ollama:11434` and use:

```sh
docker compose --profile bundled up -d --build
docker compose exec ollama ollama pull qwen2.5-coder:7b
docker compose exec ollama ollama pull qwen3:8b
```

When switching to WSL Ollama, stop any previously started bundled instance with `docker compose --profile bundled stop ollama`. The starter container uses CPU inference; GPU configuration depends on your host. Local model size, quantization, context and concurrency determine RAM/VRAM needs. Change the three local model settings in `.env` and pull the corresponding Ollama tags (without `ollama_chat/`) to use other local models.

## Local dashboard

Open **http://localhost:8000/** in your browser after starting the router. This read-only dashboard is served by the gateway itself; there is no separate UI process, CDN, or hosted website.

- View every routing rule in execution order, inspect its regex, and filter rules.
- Inspect task-specific direct-provider and OpenRouter fallback order, including skipped/disabled models and the final Ollama fallback.
- See provider configuration, local daily/minute safety budgets, reported request/token headroom, observation timestamps, and cooldowns. Missing provider quota remains **Unknown**, not unlimited.
- Preview a task or explicit alias to see the matching rule and first eligible candidate without sending inference or reserving quota.
- View configured local models and the most recent routing decision. Prompts and API keys are not included.

Automatic refresh reads saved observations every 15 seconds and can be paused. **Refresh quota** also requests OpenRouter's account counter (bounded to eight seconds); Gemini/Groq quota observations come from normal inference response headers. Provider status means configured/eligible, not a live model or Ollama health check. Opening the dashboard, filtering rules, and previewing tasks do not call models.

The same Docker image includes the UI. When updating, run `docker compose up -d --build` (add `--profile bundled` if using the bundled Ollama service). Keep port 8000 on loopback/a trusted host; the dashboard has the same local access boundary as the API. Editing rules still happens in the YAML files followed by a router restart.

## Task rules

| Activity | Initial route | Local fallback / override |
| --- | --- | --- |
| Routine unit tests, mocks, test coverage | Ollama coder | `/tests` or `/local` |
| Implementation, refactoring, ordinary bug fixes | Free online pool | Ollama coder; `/code`, `/online`, `/cloud` |
| Architecture, design tradeoffs, hard debugging, security review | Free online pool | Ollama reasoning; `/architect`, `/reason` |
| CI/CD, Dockerfiles, Terraform, deployment pipelines | Free online pool | Ollama coder; `/cicd`, `/deploy` |
| Integration/end-to-end tests, contract tests, test strategy | Free online pool | Ollama coder |
| Summaries, translation, documentation | Ollama general | Explicit online alias if more context is needed |
| Private work | Ollama coder | `/private` or `/local` |
| Unmatched requests | Free online code pool | Ollama coder |

Unit tests still require understanding the target behavior. Local routing is a resource-saving default for routine tests, not a guarantee that every test task is easy. For difficult tests, select `free-tests` in OpenCode or use `/online`; for mixed implementation/testing requests use `/code` to prefer online reasoning.

Rules are deterministic keywords over the latest user message. Explicit OpenCode model selection wins, followed by prefixes, then architecture → CI/CD → integration testing → routine unit tests → implementation → prose → default. Prefixes remain in the prompt. Edit `rules.yaml`, direct-provider order/safety caps in `online-pool.yaml`, and OpenRouter order in `free-pool.yaml`; restart the router after changes. Direct model IDs are restricted to the reviewed allowlist in `router/online_pool.py`; adding a new ID requires checking its free-tier eligibility first.

A local prefix applies to that request only. Any later hosted selection receives the full conversation, including previous code/tool results. Select a local alias for the entire session, or set `LOCAL_ONLY=true`, when the conversation must remain private. Hosted free tiers may use content for improvement; check each provider's terms before sending code.

## Quota, failover, and zero-spend controls

```sh
curl -s http://localhost:8000/quota | python -m json.tool
watch -n 15 'curl -s http://localhost:8000/quota | python -m json.tool'
```

`/quota` reports each provider separately:

- **Gemini:** persisted local attempts per model, minute/day safety budgets, cooldowns. Daily windows use America/Los_Angeles. Remaining provider request/token allowance is unknown when the API does not return it; inspect active quota in AI Studio.
- **Groq:** the same local budgets plus observed remaining request/token headers, limits, reset durations and observation time. Groq documents request headers as daily and token headers as per-minute. These snapshots can become stale, particularly if another client uses the account.
- **OpenRouter:** live account `free_model_daily_requests` when `/key` supplies it, separate local estimates and model cooldowns. Its free models share an account-wide allowance; switching models does not multiply that allowance.

Direct-provider budgets in YAML are deliberately conservative local safety caps, not promised provider quotas. Raising them does not buy quota. They do not measure every provider token constraint; provider 429 responses remain authoritative. Missing remaining quota is null/unknown, never a fabricated precise number. Dollar credit balances are not treated as request allowance.

Hosted attempts, including failures, are counted before sending and persist in the `quota` Docker volume. Direct-provider observations, auth/payment blocks, and model cooldowns also persist there. Without Compose, set `QUOTA_DB` to a durable file; the default is in-memory. Run one gateway worker/instance: this is not distributed quota reservation. Other programs and keys can share account/project limits, so local counters cannot give exact account-wide headroom.

On a model quota error, skip it until Retry-After/reset (60 seconds if absent) and try the next eligible model/provider. Auth/payment errors block that direct provider for five minutes, without buying credits or upgrading. Already exhausted local budgets are skipped. At most four direct and three OpenRouter inference attempts occur per request. Server/network errors and missing/unusable providers can also cause fallback so the user can keep working. There are no hidden inference retries. After the first successful response starts streaming, errors propagate without replay to another model, preventing duplicate text/tool actions.

Safety controls reject provider overrides, arbitrary addons/plugins, server-side builtin tools and hosted media processing. Only client-defined function tools and approved chat fields are forwarded. OpenRouter additionally requires a fresh zero-price catalog entry (at most 60 seconds old), a `:free` suffix, tools support, and zero-price provider filters. All local requests stay local; local failures are never escalated to online models.

In normal hybrid mode `/health` reports `hybrid`; in `LOCAL_ONLY=true` mode `/quota` makes no hosted request and all automatic rules resolve locally. Explicit hosted aliases return 403 in that mode. Invalid LOCAL_ONLY values fail closed to local-only.

Response headers `x-task-model`, `x-task-rule`, `x-upstream-model` (provider/model on online success), and `x-fallback-reason` identify decisions. No prompt logging is added by the gateway. These rules choose an LLM; they do not grant cloud deployment credentials or modify OpenCode permissions.

## Provider references

Free-tier eligibility/model IDs were checked on 2026-10-28. Free offers can change; account entitlement and availability require your own checks.

- [Gemini pricing/free models](https://ai.google.dev/gemini-api/docs/pricing), [rate limits](https://ai.google.dev/gemini-api/docs/rate-limits), [OpenAI-compatible tool/streaming API](https://ai.google.dev/gemini-api/docs/openai).
- [Groq free-plan limits and quota headers](https://console.groq.com/docs/rate-limits), [models](https://console.groq.com/docs/models), [billing](https://console.groq.com/docs/billing-faqs).
- [OpenRouter free model catalog](https://openrouter.ai/collections/free-models), [shared quota](https://openrouter.ai/docs/api-reference/limits). Additional OpenRouter implementation notes are in [HOSTED.md](HOSTED.md).

## Validation and operations

```sh
curl http://localhost:8000/health
curl http://localhost:8000/v1/models
curl -i http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"/tests Write unit tests for this function"}]}'
docker compose logs router litellm
# In WSL:
ollama ps
```

The gateway port binds to loopback without authentication; keep it on a trusted machine. `/health` checks the gateway process, not model readiness. Dependencies and images use bounded ranges/moving tags; pin tested versions/digests for reproducibility.

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

Tests mock provider inference and consume no real quota. Live Gemini/Groq/OpenRouter calls and WSL inference have not been verified here because no model credentials or WSL host are connected. Hardware, electricity and rented infrastructure costs are separate from hosted API fees.
