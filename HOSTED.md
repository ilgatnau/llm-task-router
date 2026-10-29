# OpenRouter pool details

OpenRouter is an optional third online pool after direct Gemini/Groq. It is not required to use the direct free tiers. See [README.md](README.md) for the current online-first setup, WSL endpoint, task rules, quota monitoring, and account-tier safety requirements.

Set `OPENROUTER_API_KEY` in `.env`, keep `LOCAL_ONLY=false`, and rebuild. Only the exact `:free` model IDs in `free-pool.yaml` are eligible. The router verifies live catalog pricing and tool support and sends provider zero-price filters; no paid fallback is configured.

| Task alias | Ordered OpenRouter pool |
| --- | --- |
| free-code | Inkling Small → Xing4.0 → North Mini Code → Nemotron 3 Ultra |
| free-architecture | Xing4.0 → Nemotron 3 Ultra → Inkling Small → North Mini Code |
| free-cicd | North Mini Code → Xing4.0 → Inkling Small → Nemotron 3 Ultra |
| free-tests (explicit opt-in for hard tests) | North Mini Code → Inkling Small → Xing4.0 → Nemotron 3 Ultra |

The models' current free endpoint terms apply. Inkling's free endpoint is for agent harnesses and prohibits confidential/personal data; Nemotron also prohibits confidential/personal data. Both disclose logging. Use local inference for private sessions.

`GET /quota` exposes this pool under `providers.openrouter`. `/key` can report `free_model_daily_requests` (used, limit, remaining). If absent, the provider counter is null and local estimates are labelled as estimates. Dollar fields are not interpreted as request quota. The local conservative limit remains 50/day and 20/minute, irrespective of credit purchases. Each candidate attempt, including errors, consumes local budget. Other apps using the account are visible only through the provider counter.

OpenRouter's daily cap is shared across its free models: rotating models/keys cannot extend it. Platform limit headers stop this pool; a model-specific 429 puts that model into cooldown. Other independent providers are governed by their own account/project limits. When all online pools are unavailable, the task falls back locally. No streaming replay occurs once a successful response begins.

Counts and model cooldowns persist in the quota volume. The shared OpenRouter Retry-After cooldown remains process-local; after restart, the account quota is checked again before requests. Do not delete quota data to reset an allowance.

References: [free models](https://openrouter.ai/collections/free-models), [limits](https://openrouter.ai/docs/api-reference/limits), [models API](https://openrouter.ai/api/v1/models).

## Chinese model fallback

Reviewed against the live [OpenRouter models API](https://openrouter.ai/api/v1/models) on 2026-10-28: `xingchen-agi/xing4.0-29b-a4b:free` advertises zero prompt/completion prices and function tools. OpenRouter identifies Xing4.0 as China Telecom TeleAI's open-weight model. It is the first OpenRouter architecture/reasoning candidate and a secondary implementation/CI/CD candidate. These are initial routing preferences, not measured quality or latency claims.

No Qwen or DeepSeek `:free` endpoints were present in that catalog snapshot, so none are configured. Their paid IDs are never substituted. Xing is skipped automatically if it disappears, gains a nonzero catalog price or loses tool support. The same zero-price provider filter applies on every request.

Use an unfunded OpenRouter account with automatic top-up disabled and set `OPENROUTER_API_KEY` in your ignored environment file. Restart the router; no additional OpenCode model selection is needed with `task-router/auto`. The dashboard automatically includes Xing in the routing chains and OpenRouter model observations. Its remaining model quota is unknown; the free account allowance is shared with the other OpenRouter candidates. No additional quota is created by switching to Xing. No direct paid China Telecom, Alibaba or DeepSeek API is enabled.
