# Local task router for OpenCode

Run routing and model inference on your own machine:

```text
OpenCode → local task gateway → local LiteLLM → local Ollama models
```

No OpenRouter account, API key, credit purchase, hosted quota, or internet inference is needed. This implements the routing function locally; it does not install the proprietary OpenRouter service. Downloads of containers, packages, and model weights require internet access initially. After setup, the routing/inference path uses your machine. OpenCode tools and other optional integrations can still access the network separately.

## Start

Requires Docker Compose and OpenCode:

```sh
cp .env.example .env
docker compose --profile bundled up -d --build
docker compose exec ollama ollama pull qwen2.5-coder:7b
docker compose exec ollama ollama pull qwen3:8b
opencode
```

When upgrading an existing `.env`, set `LOCAL_ONLY=true` and add `LOCAL_REASONING_MODEL=ollama_chat/qwen3:8b`. Compose supplies defaults for all three local model settings. Do not overwrite existing settings blindly. Rebuild with the command above to apply changes.

OpenCode reads `opencode.json` in this directory. Merge its provider settings into your global OpenCode config if you want it available in other projects.

## Use Ollama already running in WSL

The bundled container is optional. If Ollama runs directly in WSL, pull models there:

```sh
ollama pull qwen2.5-coder:7b
ollama pull qwen3:8b
curl http://127.0.0.1:11434/api/tags
```

Set `OLLAMA_API_BASE` in `.env` to the address that the **LiteLLM container** can reach. For Docker running in the same WSL distro, `http://host.docker.internal:11434` can reach the Docker host via the provided host-gateway mapping, provided Ollama listens on that interface. Docker Desktop, WSL NAT, and mirrored networking can differ; use the reachable WSL IP if necessary. Container `localhost` refers to the container itself.

Ollama normally binds to loopback. When container access requires it, configure the WSL Ollama service with `OLLAMA_HOST=0.0.0.0:11434` and restart it (or set it when starting `ollama serve` manually). Ollama has no built-in authentication here: restrict access to the trusted host/container network with firewall rules. Do not expose its port publicly.

Start only the router and LiteLLM by omitting the bundled profile:

```sh
docker compose up -d --build
```

If a bundled Ollama container is already running from the previous setup, stop it with `docker compose --profile bundled stop ollama`. Test container-to-WSL reachability without requesting inference:

```sh
docker compose exec litellm python -c 'import os, urllib.request; print(urllib.request.urlopen(os.environ["OLLAMA_API_BASE"] + "/api/tags", timeout=10).status)'
```

A successful response confirms connectivity and lists downloaded model availability, not actual generation/tool-call readiness. When inspecting WSL resources, run `ollama ps` in WSL instead of the bundled-container command below. You can also run LiteLLM and the gateway directly in the same WSL distro; then Ollama can stay on `http://127.0.0.1:11434`.

## Task rules

| Activity | Local model | Alias / prefix |
| --- | --- | --- |
| Unit tests, test coverage | Qwen2.5 Coder 7B | local-code / `/tests` |
| Code implementation and refactoring | Qwen2.5 Coder 7B | local-code / `/code` |
| CI/CD, Dockerfiles, Terraform, pipelines | Qwen2.5 Coder 7B | local-code / `/cicd`, `/deploy` |
| Architecture, design tradeoffs, hard debugging | Qwen3 8B | local-reasoning / `/architect`, `/reason` |
| Summaries, translation, documentation | Qwen3 8B | local-general |
| Private requests and routine default | Qwen2.5 Coder 7B | local-code / `/local`, `/private` |

These are starter sizes, not quality guarantees. Architecture and reliable agentic tool use benefit from larger models if your hardware supports them. Change the model environment variables in `.env` and pull the corresponding Ollama tags; model values include the `ollama_chat/` prefix but pull commands do not. RAM/VRAM requirements depend on model size, quantization, context, and concurrency. CPU inference is configured by default; GPU enablement depends on your host.

Edit `rules.yaml` to change task keywords or priorities; restart the router to reload. Explicit OpenCode model selection overrides rules. Prefixes override keyword matches. Keyword order is architecture → CI/CD → tests → implementation → writing → default. Routing uses the latest user message and forwards the full conversation. Prefix text remains in the prompt. Tool calls and streamed output are passed through.

## Local-only enforcement and status

`LOCAL_ONLY` defaults to true. Only the exact value `false` opts into the previous hosted capability. In local-only mode:

- Every automatic rule selects a local alias.
- OpenCode and `/v1/models` list local models only.
- Explicit requests for a hosted alias are rejected with HTTP 403.
- The hosted pool and its quota database are not initialized; `/quota` never queries OpenRouter.
- Local model errors return to the client without cloud fallback.

```sh
curl http://localhost:8000/health
curl http://localhost:8000/quota
curl http://localhost:8000/v1/models
curl -i http://localhost:8000/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{"model":"auto","messages":[{"role":"user","content":"/tests Write unit tests for this function"}]}'
docker compose exec ollama ollama ps
docker stats
```

There is no provider request allowance to exhaust: constraints are hardware utilization, available memory, context size, and waiting time. `/quota` reports local-only mode and no provider quota; `ollama ps` and `docker stats` show loaded models and resource consumption. `/health` checks the gateway process, not model readiness. Routing response headers identify the selected local alias and matched rule.

No hosted inference fees are incurred by this configuration. Hardware, electricity, and any infrastructure you choose to rent have their own costs. Generated CI/CD files do not grant deployment credentials or change OpenCode permissions. Validate generated code and tool behavior on your actual workloads.

The gateway is bound to loopback and has no authentication; use it on a trusted machine. Model images/dependencies use moving tags and bounded package ranges; pin tested versions for reproducibility. Hosted inference is retained as an optional legacy feature documented in [HOSTED.md](HOSTED.md); those instructions are subordinate to the local-only defaults here. Enabling that feature alone does not change the local rules; a free task alias must be selected explicitly or configured in the rules.

## Test

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt pytest
.venv/bin/python -m pytest -q
```

Tests mock inference; local model downloads and live hardware inference have not been verified in this workspace.
