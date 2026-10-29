"""Task selection gateway. LiteLLM owns provider protocol translation."""
import os
import re
import asyncio
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from router.activity import RoutingActivity, StreamObservation
from router.dashboard import fallback_model, snapshot
from router.chat import normalize_chat
from router.online_pool import OnlinePool

MODELS = ('local-code', 'local-general', 'local-reasoning', 'free-tests', 'free-architecture', 'free-code', 'free-cicd')
CONFIG = yaml.safe_load(Path(os.getenv('RULES_PATH', 'rules.yaml')).read_text())
if CONFIG['default'] not in MODELS:
    raise ValueError('Unknown default model')
for rule in CONFIG['rules']:
    if rule['model'] not in MODELS:
        raise ValueError(f"Unknown model in rule {rule['name']}")
    re.compile(rule['pattern'])


def latest_user_text(messages):
    for message in reversed(messages):
        if message.get('role') == 'user':
            content = message.get('content', '')
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                return '\n'.join(p.get('text', '') for p in content if isinstance(p, dict) and p.get('type') == 'text')
            return ''
    return ''


def select_model(payload):
    requested = payload.get('model', 'auto')
    if requested in MODELS:
        return requested, 'explicit-model'
    if requested != 'auto':
        raise HTTPException(400, 'Unknown model; use auto or a listed model alias')
    text = latest_user_text(payload.get('messages', []))
    for rule in CONFIG['rules']:
        if re.search(rule['pattern'], text):
            return rule['model'], rule['name']
    return CONFIG['default'], 'default'


def resolve_route(payload, local_only):
    model, rule = select_model(payload)
    reason = None
    if local_only and not model.startswith('local-'):
        if payload.get('model', 'auto') != 'auto':
            raise HTTPException(403, 'Hosted inference is disabled in local-only mode')
        model, reason = fallback_model(model), 'local-only-mode'
    return model, rule, reason


@asynccontextmanager
async def lifespan(app):
    app.state.local_only = os.getenv('LOCAL_ONLY', 'false').lower() != 'false'
    app.state.last_route = None
    app.state.activity = RoutingActivity()
    app.state.local_models = {
        'local-code': os.getenv('LOCAL_CODE_MODEL', 'ollama_chat/llama3.2:3b'),
        'local-general': os.getenv('LOCAL_GENERAL_MODEL', 'ollama_chat/llama3.2:3b'),
        'local-reasoning': os.getenv('LOCAL_REASONING_MODEL', 'ollama_chat/llama3.2:3b'),
    }
    app.state.online_config = yaml.safe_load(Path('online-pool.yaml').read_text())
    app.state.openrouter_config = yaml.safe_load(Path('free-pool.yaml').read_text())
    proxy_key = os.getenv('LITELLM_API_KEY') or os.getenv('LITELLM_MASTER_KEY', '')
    proxy_headers = {'Authorization': f'Bearer {proxy_key}'} if proxy_key else {}
    async with httpx.AsyncClient(base_url=os.getenv('LITELLM_URL', 'http://127.0.0.1:4000'), headers=proxy_headers, timeout=httpx.Timeout(300, connect=10)) as client:
        app.state.client = client
        async with httpx.AsyncClient(base_url='https://openrouter.ai/api/v1', timeout=httpx.Timeout(300, connect=10)) as public:
            app.state.pool = None if app.state.local_only else OnlinePool(public, os.getenv('OPENROUTER_API_KEY', ''), os.getenv('QUOTA_DB', ':memory:'))
            try:
                yield
            finally:
                for route in list(app.state.activity.active.values()):
                    app.state.activity.finish(route, 'cancelled', 'Router shutdown interrupted the request.')
                if app.state.pool is not None:
                    app.state.pool.db.close()


app = FastAPI(lifespan=lifespan)
STATIC_DIR = Path(__file__).parent / 'static'
app.mount('/assets', StaticFiles(directory=STATIC_DIR), name='assets')


@app.get('/health')
async def health():
    return {'status': 'ok', 'mode': 'local-only' if app.state.local_only else 'hybrid'}


@app.get('/v1/models')
async def models():
    visible = [m for m in MODELS if m.startswith('local-') or not app.state.local_only]
    return {'object': 'list', 'data': [{'id': m, 'object': 'model', 'owned_by': 'task-router'} for m in ('auto', *visible)]}


@app.post('/v1/chat/completions')
async def completions(request: Request):
    try:
        payload = await request.json()
    except ValueError:
        raise HTTPException(400, 'Expected a JSON object')
    if not isinstance(payload, dict) or not isinstance(payload.get('messages'), list) or any(not isinstance(m, dict) for m in payload['messages']):
        raise HTTPException(400, 'messages must be an array of objects')
    if 'tools' in payload and (not isinstance(payload['tools'], list) or any(not isinstance(t, dict) or t.get('type') != 'function' for t in payload['tools'])):
        raise HTTPException(400, 'Only function tool definitions are supported')
    payload = normalize_chat(payload)
    model, rule, local_reason = resolve_route(payload, app.state.local_only)
    activity = app.state.activity
    route = activity.start(model, rule, CONFIG, payload.get('model', 'auto'))
    app.state.last_route = route
    activity.watch(route)
    headers = {'x-task-model': model, 'x-task-rule': rule, 'x-route-request-id': route['request_id']}
    if local_reason:
        headers['x-fallback-reason'] = local_reason
        activity.event(route, 'local-only-override', 'Local-only mode maps the configured online route to a local model.', fallback_reason=local_reason)
    upstream = None
    local_candidate = None
    try:
        if model.startswith('free-'):
            activity.event(route, 'online-selection', 'Selecting an eligible free online model using the configured task pool.')
            upstream, selected = await app.state.pool.send(model, payload,
                on_event=lambda name, message, **fields: activity.event(route, name, message, **fields))
            if upstream is not None:
                headers['x-upstream-model'] = selected
                activity.event(route, 'upstream-selected', 'An online candidate accepted the request.', upstream_model=selected, status=upstream.status_code)
            else:
                model = fallback_model(model)
                headers.update({'x-task-model': model, 'x-fallback-reason': selected})
                activity.event(route, 'local-fallback', 'Online candidates are unavailable, blocked, or exhausted; using the local fallback.', model=model, fallback_reason=selected)
        if upstream is None:
            payload['model'] = model
            backend = 'ollama/' + app.state.local_models[model].removeprefix('ollama_chat/')
            local_candidate = {'provider': 'ollama', 'model': backend.removeprefix('ollama/'),
                               'state': 'trying', 'http_status': None,
                               'reason': 'local-fallback' if route['fallback_reason'] else 'local-rule-or-explicit-selection'}
            activity.event(route, 'local-selection', 'Sending the request through LiteLLM to the local Ollama model.',
                           upstream_model=backend, state='waiting', candidate=local_candidate)
            try:
                upstream = await app.state.client.send(app.state.client.build_request('POST', '/v1/chat/completions', json=payload), stream=True)
            except httpx.RequestError as exc:
                activity.event(route, 'local-connection-error', 'LiteLLM did not provide a response: ' + type(exc).__name__, status=502)
                raise HTTPException(502, 'Local LiteLLM unavailable', headers=headers)
        if local_candidate is not None:
            local_candidate = {**local_candidate, 'http_status': upstream.status_code,
                               'state': 'rejected' if upstream.is_error else 'selected',
                               'reason': 'upstream-error' if upstream.is_error else 'selected'}
        activity.event(route, 'response-started', 'Upstream response headers received; generation has not completed yet.',
                       status=upstream.status_code, candidate=local_candidate)
        if upstream.is_error or not payload.get('stream'):
            try:
                body = await upstream.aread()
            finally:
                await upstream.aclose()
            activity.received(route, len(body))
            failed = upstream.is_error
            activity.finish(route, 'failed' if failed else 'completed',
                            'Upstream returned an HTTP error.' if failed else 'Response body received in full.', status=upstream.status_code)
            return Response(body, status_code=upstream.status_code, media_type=upstream.headers.get('content-type', 'application/json'), headers=headers)
    except asyncio.CancelledError:
        if upstream is not None:
            await upstream.aclose()
        activity.finish(route, 'cancelled', 'Request was cancelled before response delivery completed.')
        raise
    except Exception as exc:
        if upstream is not None:
            await upstream.aclose()
        activity.finish(route, 'failed', 'Request failed before completion: ' + type(exc).__name__, status=exc.status_code if isinstance(exc, HTTPException) else 502)
        raise

    activity.event(route, 'stream-started', 'Streaming the selected model response; the request remains active until the stream ends.', state='waiting-first-data')

    async def finalize():
        await upstream.aclose()
        activity.finish(route, 'cancelled', 'Response delivery ended before the stream completed.')

    async def stream():
        observation = StreamObservation()
        try:
            async for chunk in upstream.aiter_raw():
                activity.received(route, len(chunk))
                observation.feed(chunk)
                activity.observed(route, observation)
                yield chunk
            activity.finish(route, 'failed' if observation.failed else 'completed',
                            'Upstream reported an error in the response stream.' if observation.failed else 'Upstream stream received in full.')
        except (asyncio.CancelledError, GeneratorExit):
            activity.finish(route, 'cancelled', 'Client disconnected or cancelled the streaming response.')
            raise
        except Exception as exc:
            activity.finish(route, 'failed', 'Stream interrupted: ' + type(exc).__name__)
            raise
        finally:
            await finalize()
    return StreamingResponse(stream(), status_code=upstream.status_code, media_type='text/event-stream', headers=headers, background=BackgroundTask(finalize))


@app.get('/quota')
async def quota():
    if app.state.local_only:
        return {'mode': 'local-only', 'hosted_requests_enabled': False, 'provider_quota': None, 'note': 'Local inference has no provider request quota. Capacity is limited by RAM/VRAM, compute and context size.'}
    async with app.state.pool.lock:
        await app.state.pool.refresh_quota()
        return app.state.pool.status()


@app.get('/', include_in_schema=False)
async def dashboard():
    return FileResponse(STATIC_DIR / 'index.html', headers={
        'Cache-Control': 'no-store',
        'Content-Security-Policy': "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'",
        'X-Content-Type-Options': 'nosniff',
    })


def dashboard_snapshot():
    activity = app.state.activity.view()
    route = activity['current_route'] or activity['latest_completed_route']
    data = snapshot(app.state.local_only, app.state.pool, CONFIG, app.state.local_models,
                    app.state.online_config, app.state.openrouter_config, route)
    data.update(activity)
    return data


@app.get('/api/routing')
async def routing_activity():
    return JSONResponse(app.state.activity.view(), headers={'Cache-Control': 'no-store'})


@app.get('/api/dashboard')
async def dashboard_data(refresh: bool = False):
    refresh_note = None
    if refresh and app.state.pool is not None:
        try:
            async with asyncio.timeout(8):
                async with app.state.pool.lock:
                    await app.state.pool.refresh_quota()
            refresh_note = 'OpenRouter counter refreshed when supplied. Gemini and Groq use observations from inference responses.'
        except TimeoutError:
            refresh_note = 'Quota refresh timed out; showing saved observations.'
    data = dashboard_snapshot()
    data['refresh_note'] = refresh_note
    return JSONResponse(data, headers={'Cache-Control': 'no-store'})


@app.post('/api/route-preview')
async def route_preview(request: Request):
    try:
        data = await request.json()
    except ValueError:
        raise HTTPException(400, 'Expected JSON')
    if not isinstance(data, dict) or not isinstance(data.get('task'), str) or not 1 <= len(data['task'].strip()) <= 10000:
        raise HTTPException(400, 'Enter a task between 1 and 10000 characters')
    requested = data.get('model', 'auto')
    if not isinstance(requested, str):
        raise HTTPException(400, 'model must be a string')
    model, rule, reason = resolve_route({'model': requested, 'messages': [{'role': 'user', 'content': data['task']}]}, app.state.local_only)
    state = dashboard_snapshot()
    chain = state['chains'].get(model, [{'provider': 'ollama', 'alias': model, 'model': app.state.local_models.get(model, model), 'kind': 'local', 'eligible': True, 'reason': None}])
    return JSONResponse({'model': model, 'rule': rule, 'fallback_reason': reason, 'chain': chain,
                         'first_eligible': next((m for m in chain if m['eligible']), None),
                         'note': 'Preview only. No inference, quota reservation, or provider refresh occurs. Eligibility may change before the next request.'},
                        headers={'Cache-Control': 'no-store'})
