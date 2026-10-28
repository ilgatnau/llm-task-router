# OpenRouter pool details

OpenRouter is an optional third online pool after direct Gemini/Groq. It is not required to use the direct free tiers. See [README.md](README.md) for the current online-first setup, WSL endpoint, task rules, quota monitoring, and account-tier safety requirements.

Set `OPENROUTER_API_KEY` in `.env`, keep `LOCAL_ONLY=false`, and rebuild. Only the exact `:free` model IDs in `free-pool.yaml` are eligible. The router verifies live catalog pricing and tool support and sends provider zero-price filters; no paid fallback is configured.

| Task alias | Ordered OpenRouter pool |
| --- | --- |
| free-code | Inkling Small → North Mini Code → Nemotron 3 Ultra |
| free-architecture | Nemotron 3 Ultra → Inkling Small → North Mini Code |
| free-cicd | North Mini Code → Inkling Small → Nemotron 3 Ultra |
| free-tests (explicit opt-in for hard tests) | North Mini Code → Inkling Small → Nemotron 3 Ultra |

The models' current free endpoint terms apply. Inkling's free endpoint is for agent harnesses and prohibits confidential/personal data; Nemotron also prohibits confidential/personal data. Both disclose logging. Use local inference for private sessions.

`GET /quota` exposes this pool under `providers.openrouter`. `/key` can report `free_model_daily_requests` (used, limit, remaining). If absent, the provider counter is null and local estimates are labelled as estimates. Dollar fields are not interpreted as request quota. The local conservative limit remains 50/day and 20/minute, irrespective of credit purchases. Each candidate attempt, including errors, consumes local budget. Other apps using the account are visible only through the provider counter.

OpenRouter's daily cap is shared across its free models: rotating models/keys cannot extend it. Platform limit headers stop this pool; a model-specific 429 puts that model into cooldown. Other independent providers are governed by their own account/project limits. When all online pools are unavailable, the task falls back locally. No streaming replay occurs once a successful response begins.

Counts and model cooldowns persist in the quota volume. The shared OpenRouter Retry-After cooldown remains process-local; after restart, the account quota is checked again before requests. Do not delete quota data to reset an allowance.

References: [free models](https://openrouter.ai/collections/free-models), [limits](https://openrouter.ai/docs/api-reference/limits), [models API](https://openrouter.ai/api/v1/models).
