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

**Setup:** [Free providers](#configure-free-online-providers) · [Local development](#local-development-without-docker) · [Docker + WSL](#docker-option-ollama-running-in-wsl)

**Usage:** [OpenCode setup](#configure-and-use-opencode) · [Routing configuration](#configure-routing) · [Dashboard](#local-dashboard) · [Quota and failover](#quota-failover-and-zero-spend-controls)

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

## Local development without Docker

Requires **Python 3.12+** and Ollama running on your machine or in WSL. The UI is already bundled static HTML/CSS/JS, so Node/npm and a frontend build are not required.

Run in the same WSL distro as Ollama for the simplest networking:

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements-dev.txt
# Keep existing settings; copy only missing files:
cp -n .env.example .env
cp -n .env.local.example .env.local
# Put provider keys/free-tier confirmations in .env; WSL endpoint overrides in .env.local.
ollama pull llama3.2:3b
python dev.py
```

Open **http://localhost:8000/** for the dashboard. OpenCode uses the same `/v1` endpoint as the Docker setup. On native Windows use `py -3.12 -m venv .venv` and `.\.venv\Scripts\Activate.ps1` in PowerShell; specify an Ollama address reachable from Windows if Ollama is in a different WSL network context.

The launcher starts LiteLLM on loopback port 4000 and the router/UI on loopback port 8000, then stops both on Ctrl+C. It does not install/download models or start Ollama for you. If an owned service fails, the other is stopped. Port conflicts fail with an explanatory message; stop Docker services first if they already publish port 8000, or select a different port.

Configuration precedence is defaults → `.env` → `.env.local` → shell environment; explicit launcher flags override their corresponding settings. Values are treated literally (no `${VAR}` expansion). `.env.local` is ignored by Git and Docker, so `OLLAMA_API_BASE=http://127.0.0.1:11434` can be used for development without changing Docker's `http://ollama:11434` setting. Same-distro WSL Ollama can stay bound to loopback. A launcher running outside that distro may need the reachable WSL IP, depending on networking mode.

Quota state persists in the ignored `.data/quota.dev.sqlite3` file by default; it is separate from Docker's quota volume. Use the same state store rather than alternating runtimes to extend a provider allowance; the provider's limits apply regardless of runtime. Hosted inference remains online-first with the same free-tier confirmations, local test routing and private/local overrides.

Python, routing YAML, and UI HTML/CSS/JS changes reload the router during development. Refresh the browser to load changed UI assets. Environment changes and LiteLLM model/proxy configuration changes require restarting `dev.py`. The UI and routing API are the same implementation in both runtimes.

```sh
python dev.py --no-reload                 # No automatic reload
python dev.py --local-model llama3.2:3b    # Override old local tags for this run
python dev.py --port 8001 --proxy-port 4001
python dev.py --env-file /path/to/dev.env
python dev.py --no-proxy                  # Existing LiteLLM via LITELLM_URL
```

If changing the router port, update OpenCode's provider `baseURL` accordingly. `--no-proxy` only manages the router/UI; it does not stop an external LiteLLM service. The launcher does not print provider keys. It generates an ephemeral LiteLLM master key if one is not configured and shares it with the router in child-process environments only; it is not saved to disk. An existing proxy can be authenticated with `LITELLM_API_KEY` (or `LITELLM_MASTER_KEY`) in `.env.local`.

## Docker option: Ollama running in WSL

Pull your local fallback models in WSL:

```sh
ollama pull llama3.2:3b
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
docker compose exec ollama ollama pull llama3.2:3b
```

When switching to WSL Ollama, stop any previously started bundled instance with `docker compose --profile bundled stop ollama`. The starter container uses CPU inference; GPU configuration depends on your host. Local model size, quantization, context and concurrency determine RAM/VRAM needs. Change the three local model settings in `.env` and pull the corresponding Ollama tags (without `ollama_chat/`) to use other local models.

### Small local defaults and slow-response checks

All three local aliases now default to **`llama3.2:3b`**, a smaller non-thinking model for CPU/WSL development. The `local-reasoning` alias remains separately configurable, but no longer loads an 8B model by default. Online-first task rules are unchanged. Small general-purpose models may be less reliable at code generation, tool calls and long contexts than larger coding models.

Existing `.env`/`.env.local` files are not overwritten by `git pull`. To switch an existing setup, set all three values in `.env.local` for development, or `.env` for Docker:

```dotenv
LOCAL_CODE_MODEL=ollama_chat/llama3.2:3b
LOCAL_GENERAL_MODEL=ollama_chat/llama3.2:3b
LOCAL_REASONING_MODEL=ollama_chat/llama3.2:3b
```

For development you can instead override existing tags for the current run with `python dev.py --local-model llama3.2:3b`. This changes all three local aliases without editing `.env` and leaves online-first routing unchanged.

Then pull the model in WSL and check it directly before testing a large OpenCode session:

```sh
ollama pull llama3.2:3b
ollama run llama3.2:3b "Reply with hello"
ollama ps
```

For even lower memory use, pull `llama3.2:1b` and replace the three tags with `ollama_chat/llama3.2:1b`; expect weaker coding/tool accuracy. See [Ollama's Llama 3.2 model sizes](https://ollama.com/library/llama3.2). Restart `dev.py` after changing tags, or recreate Docker services. Long prompts, large tool schemas, CPU-only inference and model loading can still delay the first response even with a small model.

## Configure and use OpenCode

### Install the terminal client

In WSL/Linux, use one of the installation methods in the [official OpenCode introduction](https://opencode.ai/docs/):

```sh
# If Node/npm is available:
npm install -g opencode-ai
opencode --version
```

Alternatively, the official installer is `curl -fsSL https://opencode.ai/install | bash`. These installation methods currently target **OpenCode 1**, matching the checked-in provider configuration. OpenCode 2 also uses the `opencode` command but has a separate distribution/migration workflow; check your existing installation before replacing it. See the official introduction for the current version-specific instructions. Node/npm is needed only if you choose the npm installation method, not for running this repository's Python router or dashboard.

### Connect OpenCode to this router

Start the router first, either with `python dev.py` or Docker as described above. In another terminal:

```sh
cd /path/to/llm-task-router
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/v1/models
opencode --model task-router/auto
```

The repository's `opencode.json` is already configured. Its provider ID is `task-router`, and its default model is `task-router/auto`. The essential connection settings are:

```json
{
  "$schema": "https://opencode.ai/config.json",
  "model": "task-router/auto",
  "provider": {
    "task-router": {
      "npm": "@ai-sdk/openai-compatible",
      "name": "Task Router",
      "options": {
        "baseURL": "http://127.0.0.1:8000/v1",
        "apiKey": "local-only"
      },
      "models": {
        "auto": { "name": "Automatic task routing", "tool_call": true }
      }
    }
  }
}
```

This is a minimal example; the checked-in file additionally registers every explicit local/online task alias. Keep those entries if you want to select them in OpenCode. The compatibility package is resolved by OpenCode; there is no separate frontend package installation in this repository.

Point OpenCode at the **gateway's `/v1` URL**, not at LiteLLM port 4000, Ollama port 11434, or a hosted provider directly: those endpoints bypass the task router. If you start `dev.py --port 8001`, change `baseURL` to `http://127.0.0.1:8001/v1`.

The `apiKey: "local-only"` value is a placeholder for this loopback gateway, not a provider credential and not a routing mode. `LOCAL_ONLY` in the router's environment controls that mode. Put real Gemini/Groq/OpenRouter keys and free-tier confirmations in `.env` (or your shell), not in `opencode.json`. You do not need to connect those providers separately inside OpenCode to use this gateway. The router's private LiteLLM key is also separate and is handled by `dev.py` automatically.

### Use the router in another project

Keep the router running from this checkout, then open OpenCode in the project you actually want it to work on. You can merge the repository's `provider.task-router` block and `model` setting into that project's existing `opencode.json`/`opencode.jsonc`, or into `~/.config/opencode/opencode.json`. Preserve existing settings rather than overwriting the file.

For a one-command setup without copying the provider configuration:

```sh
cd /path/to/your-application
OPENCODE_CONFIG=/absolute/path/to/llm-task-router/opencode.json opencode --model task-router/auto
```

`OPENCODE_CONFIG` selects an additional config file; it does not change OpenCode's working directory to the router checkout. OpenCode merges its configuration sources, and a project's settings can override conflicting custom/global keys. If the provider is missing or the URL is wrong, inspect the effective configuration in your project and merge the block there. See [OpenCode configuration locations and precedence](https://opencode.ai/docs/config/).

### Pick automatic routing or a specific task pool

In the terminal UI, use OpenCode's `/models` picker and select a model under **Task Router**. Or select it at launch with `--model`:

| OpenCode model ID | Behavior |
| --- | --- |
| `task-router/auto` | Match the latest user request against `rules.yaml`; recommended default |
| `task-router/free-code` | Online-first implementation pool, then local coder |
| `task-router/free-architecture` | Online-first architecture pool, then local reasoning |
| `task-router/free-cicd` | Online-first CI/CD pool, then local coder |
| `task-router/free-tests` | Explicit online testing pool for harder tests, then local coder |
| `task-router/local-code` | Always use the local coder; no hosted escalation |
| `task-router/local-general` | Always use the local general model |
| `task-router/local-reasoning` | Always use the local reasoning model |

The `free-*` choices are task pools, not a promise that one specific hosted model is available. They still apply account eligibility, quota/cooldown checks, ordered failover, and local fallback. Explicit model selection overrides keyword rules; choose a local alias for an entire private session. With `LOCAL_ONLY=true`, automatic routing resolves locally and explicit `free-*` requests are rejected.

When using `auto`, ordinary prompts are enough:

```text
Write unit tests for the login function.       → local coder
Implement pagination for the users API.       → online code pool
Review the service architecture.              → online architecture pool
Create a GitHub Actions deployment pipeline.  → online CI/CD pool
```

Noninteractive examples, run from the application project with the provider configuration available:

```sh
opencode run --model task-router/auto "Write unit tests for the login function"
opencode run --model task-router/free-architecture "Review the service architecture"
opencode run --model task-router/local-code "Explain this function without using hosted models"
```

These commands perform real inference and can consume the selected provider's free quota. To check routing without inference, use the dashboard's **Where would this go?** panel, or:

```sh
curl http://127.0.0.1:8000/api/route-preview \
  -H 'Content-Type: application/json' \
  -d '{"task":"Implement pagination for the users API","model":"auto"}'
```

The preview shows the matching rule, candidate chain, and first eligible candidate. Real eligibility may change later. The dashboard's latest-request panel shows the actual route after a real request reaches an upstream backend.

Router prefixes such as `/tests`, `/code`, `/architect`, and `/private` are **message text recognized by the gateway**, not built-in OpenCode slash commands. OpenCode's TUI may intercept unknown slash commands before sending a request. In the TUI, use natural-language prompts or the model picker; use the following optional custom commands if you want slash shortcuts.

### Optional OpenCode slash shortcuts

Merge a `command` block like this into your OpenCode configuration, alongside the existing provider settings:

```json
{
  "command": {
    "tests": {
      "description": "Routine unit tests with local Ollama",
      "template": "Write routine unit tests for: $ARGUMENTS",
      "model": "task-router/local-code"
    },
    "architect": {
      "description": "Architecture using the free online pool",
      "template": "Review the architecture and tradeoffs for: $ARGUMENTS",
      "model": "task-router/free-architecture"
    },
    "code": {
      "description": "Implementation using the free online pool",
      "template": "Implement: $ARGUMENTS",
      "model": "task-router/free-code"
    },
    "cicd": {
      "description": "CI/CD using the free online pool",
      "template": "Create or update the CI/CD configuration for: $ARGUMENTS",
      "model": "task-router/free-cicd"
    }
  }
}
```

Then `/tests login validation` runs the configured local testing command, while `/architect the authentication service` uses the online architecture pool. These command model choices intentionally override keyword routing. Check your current `/models` selection when returning to ordinary prompts. See [OpenCode custom commands](https://opencode.ai/docs/commands/) and [CLI options](https://opencode.ai/docs/cli/).

The router's zero-spend restrictions apply only to requests sent through the `task-router` provider. Choosing a separately configured paid provider in OpenCode bypasses those restrictions. OpenCode tool permissions are independent of model routing; review proposed file edits and deployment actions under your normal project permissions.

### Show streamed thinking in OpenCode

In OpenCode's terminal UI run **`/thinking`** to show reasoning blocks. This is a display toggle, not a command that makes every model reason. Use `Ctrl+T` to cycle available model variants. See [OpenCode thinking display](https://opencode.ai/docs/tui/#thinking).

The fast default **Llama 3.2 3B does not emit an Ollama thinking channel**. Its answer text streams normally once generation starts. For an optional small thinking model, keep the fast defaults for routine work and override only `local-reasoning`:

```sh
ollama pull qwen3:1.7b
python dev.py --local-model llama3.2:3b --reasoning-model qwen3:1.7b
```

In another terminal, launch OpenCode with `opencode --model task-router/local-reasoning`, then enter `/thinking`. Select the `thinking` variant with `Ctrl+T` if thinking needs to be explicitly enabled; `direct` requests no thinking where the model supports turning it off. The checked-in `local-reasoning` definition declares reasoning support and these two variants. This metadata does not add thinking support to a non-thinking model. Small models can still be unreliable with code/tool calls, and thinking can increase total latency.

For a persistent configuration (including Docker), set `LOCAL_REASONING_MODEL=ollama_chat/qwen3:1.7b` in the relevant environment file and restart. `--reasoning-model` takes precedence over `--local-model` for that one alias. Other local models and online-first task rules are unchanged; explicit `task-router/local-reasoning` guarantees that the selected local thinking model is used rather than an online architecture pool.

The streaming path is Ollama `message.thinking` → LiteLLM `delta.reasoning_content` → the router's unchanged SSE bytes → OpenCode reasoning blocks. The dashboard identifies reasoning/answer/tool-call phases and counts observed reasoning/answer chunks, without retaining their text. Chunk counts are not token counts. Routing logs indicate the phases only; emitted reasoning content goes to OpenCode, not the shared activity log.

Neither OpenCode nor the router can show reasoning before the model emits it. Model loading, prompt prefill and large tool contexts can still produce an initial pause. The dashboard's waiting/elapsed/byte status remains useful during that period. Do not inject fabricated thinking text or progress messages into model responses. [Ollama thinking controls](https://docs.ollama.com/capabilities/thinking) explains how to inspect `/api/show` for supported settings; [Qwen3 1.7B](https://ollama.com/library/qwen3:1.7b) is the optional model used here.

## Configure routing

### Choose the right configuration file

| File | What to configure |
| --- | --- |
| `opencode.json` | OpenCode gateway URL, default alias, visible model/task aliases, optional shortcuts |
| `.env` | Shared provider keys, free-tier confirmations, local model tags and runtime mode |
| `.env.local` | Development-only overrides, especially WSL Ollama URL and local quota database |
| `rules.yaml` | Ordered task keyword/prefix matches and default route |
| `online-pool.yaml` | Direct Gemini/Groq candidate order and per-model local request safety caps |
| `free-pool.yaml` | Optional OpenRouter candidate order and exact `:free` IDs |
| `litellm.yaml` | Local aliases and their Ollama adapter settings |

OpenCode selects the task alias. The gateway selects the first eligible model in that task's pool. LiteLLM handles the final local Ollama path. Editing OpenCode's `model` setting alone does not reorder the gateway's provider pools.

### Add or reorder a rule

Each request using `auto` checks `rules.yaml` top to bottom, and the first matching Python regex wins. If no rule matches, `default` is used. For example, to keep formatting/lint work local, insert this rule **after explicit prefix rules and before broader keyword rules**, rather than replacing the whole file:

```yaml
- name: local-formatting
  pattern: '(?i)\b(lint|format code|prettier|black)\b'
  model: local-code
```

`(?i)` makes the pattern case-insensitive; `\b` matches word boundaries. The `model` must be one of the configured task/local aliases. Moving this rule above architecture or implementation matches gives it priority for mixed requests; moving it below them lets those rules win. Keep `/private` and other explicit prefixes near the top so generic keywords do not intercept them.

Change `default: free-code` to another existing alias if desired. For a complete local-only session/system, prefer selecting a local model or setting `LOCAL_ONLY=true`; a local default alone does not override other rules that still select online pools.

### Change the online model order or safety budgets

Inside `online-pool.yaml`, the `tasks` lists define direct-provider order. This example tries Groq first for implementation; it does not replace your other task lists:

```yaml
tasks:
  free-code: [groq-reasoning, gemini-flash, groq-fast, gemini-lite]
```

Each list entry references an existing entry under `models`. After direct candidates are exhausted, the corresponding `free-code` list in `free-pool.yaml` is tried, followed by the local coder. Architecture's final fallback is the local reasoning model.

`daily_safety_cap` and `minute_safety_cap` in `online-pool.yaml` limit this router's attempts. They are local request budgets, not guaranteed provider allowances; increasing them does not increase your account's quota. Gemini/Groq token constraints can still cause 429 responses before a request budget runs out. Exact model IDs must remain on the reviewed allowlist in `router/online_pool.py`; do not add paid models to avoid quota errors.

### Change local models and apply updates

Set the desired Ollama tags in `.env`/`.env.local`:

```dotenv
LOCAL_CODE_MODEL=ollama_chat/llama3.2:3b
LOCAL_GENERAL_MODEL=ollama_chat/llama3.2:3b
LOCAL_REASONING_MODEL=ollama_chat/llama3.2:3b
```

Pull matching tags where Ollama runs, without the `ollama_chat/` prefix. Use an `OLLAMA_API_BASE` reachable from the LiteLLM process: native same-distro WSL usually uses `http://127.0.0.1:11434`; Docker may need a host/WSL address instead.

| Change | How to apply it |
| --- | --- |
| Router Python/routing YAML in development | `dev.py` auto-reloads by default; restart manually with `--no-reload` |
| UI assets in development | Refresh the browser; the same Python server serves them |
| Environment, local model tags, LiteLLM config in development | Stop and restart `python dev.py` |
| Rules/pool YAML in Docker | `docker compose restart router` (mounted configuration) |
| Environment or application/UI code in Docker | `docker compose up -d --build`; add `--profile bundled` if used |
| OpenCode configuration | Restart OpenCode and explicitly select `task-router/auto` or the intended alias |

Finally, open **http://localhost:8000/**, inspect the rule/pool order, and preview representative tasks. Then run a small real OpenCode task to check tool calling and confirm the actual backend in the latest-routing panel.

### Troubleshooting

The gateway accepts standard OpenAI-compatible streaming options (`stream_options.include_usage`), reasoning effort, and either token-limit field (`max_completion_tokens` is normalized to `max_tokens`). Client bookkeeping fields such as `user`, `metadata`, `store: false`, and `verbosity` are accepted but not forwarded. Server-side storage, paid addons, and provider overrides remain disabled. Rejection errors name unsupported field names without returning their values.

| Symptom | Check |
| --- | --- |
| First prompt returns `Unsupported parameters` | Pull the compatibility fix and restart `dev.py` (or rebuild Docker). If it persists, report only the names in `unsupported_parameters`, not request bodies or keys |
| Task Router missing in OpenCode | Run from the configured project or set `OPENCODE_CONFIG`; preserve all model entries; check project config overrides and OpenCode version |
| Connection refused | Start `dev.py`/Docker; check `/health` and the gateway port in `baseURL` |
| Every online task falls back locally | Check `.env` keys, free-tier confirmation flags, `LOCAL_ONLY`, safety budgets, and provider cooldowns in the dashboard |
| Explicit online alias gets 403 | Router is in local-only mode; use a local alias or deliberately set `LOCAL_ONLY=false` and restart |
| Ollama fallback fails | Verify the endpoint from the LiteLLM process's network context and that the chosen tags are downloaded |
| A task routes unexpectedly | Check explicit model selection first, then rule priority using the dashboard preview; mixed tasks may need a specific task alias |
| A slash shortcut does not work | Router prefixes are not automatically OpenCode commands; add the optional `command` entries or use the model picker |
| Port already in use | Stop the other runtime or change dev ports; update OpenCode's `baseURL` to match |

## Local dashboard

Open **http://localhost:8000/** in your browser after starting the router. This read-only dashboard is served by the gateway itself; there is no separate UI process, CDN, or hosted website.

- View every routing rule in execution order, inspect its regex, and filter rules.
- Inspect task-specific direct-provider and OpenRouter fallback order, including skipped/disabled models and the final Ollama fallback.
- See provider configuration, local daily/minute safety budgets, reported request/token headroom, observation timestamps, and cooldowns. Missing provider quota remains **Unknown**, not unlimited.
- Preview a task or explicit alias to see the matching rule and first eligible candidate without sending inference or reserving quota.
- View configured local models and the most recent routing decision. Prompts and API keys are not included.

Automatic refresh reads saved observations every 15 seconds and can be paused. **Refresh quota** also requests OpenRouter's account counter (bounded to eight seconds); Gemini/Groq quota observations come from normal inference response headers. Provider status means configured/eligible, not a live model or Ollama health check. Opening the dashboard, filtering rules, and previewing tasks do not call models.

The same Python server serves the UI in development and Docker. For Docker updates, run `docker compose up -d --build` (add `--profile bundled` if using the bundled Ollama service). Keep port 8000 on loopback/a trusted host; the dashboard has the same local access boundary as the API. Editing rules still happens in the YAML files followed by a router restart.

### Current rule and Python routing logs

The dashboard's **Current / latest request** panel shows the active request immediately, including the first matching rule and its regex/order, why it applies, the route/model, HTTP status, elapsed time, response-byte count and time since the last data. The matching rule is highlighted in the rules table. When no request is active, the latest finished request stays visible. Concurrent requests are tracked separately, with the most recently started active request shown first.

The **Request lifecycle** panel shows rule selection, online/local selection, fallback, response start, first data, periodic waiting updates and the final completed/failed/cancelled result. Activity polls every two seconds through `/api/routing`; the auto-refresh checkbox pauses it along with quota polling. These status reads do not send inference or consume model quota.

The Python app emits the same routing events at INFO level in the `task_router.routing` logger. `python dev.py` shows them in the terminal; Docker users can run `docker compose logs -f router`. Every event includes a generated request ID and rule name so the lifecycle can be followed across terminal lines. Responses include the same ID as `x-route-request-id`.

While waiting, a progress line is emitted every ten seconds. No bytes yet can mean LiteLLM/Ollama is still loading a model or processing context. Received bytes can include reasoning or keep-alives and do not prove that a final answer exists. HTTP 200 alone is not completion: streaming requests remain active until the stream actually ends. Stream errors, disconnections and backend timeouts are recorded as failures/cancellations without automatically replaying a partially delivered response.

These are **per model API request**, not a whole OpenCode agent session: one prompt can generate several tool/model requests. Only metadata is retained—no prompts, model output, tool content, API keys or raw provider exception messages. The in-memory activity history holds the last 30 finished requests and resets on router restart/reload. Quota accounting remains in its existing persistent database. Routing logs describe what this gateway can observe; they do not inspect Ollama CPU/GPU utilization or prove the model is healthy.

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

Hosted attempts, including failures, are counted before sending and persist in the `quota` Docker volume. Direct-provider observations, auth/payment blocks, and model cooldowns also persist there. The development launcher uses `.data/quota.dev.sqlite3`; raw Uvicorn runs default to in-memory unless `QUOTA_DB` is set to a durable file. Run one gateway worker/instance: this is not distributed quota reservation. Other programs and keys can share account/project limits, so local counters cannot give exact account-wide headroom.

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
.venv/bin/pip install -r requirements-dev.txt
.venv/bin/python -m pytest -q
```

Tests mock provider inference and consume no real quota. Live Gemini/Groq/OpenRouter calls and WSL inference have not been verified here because no model credentials or WSL host are connected. Hardware, electricity and rented infrastructure costs are separate from hosted API fees.
