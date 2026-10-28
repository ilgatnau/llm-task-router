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
    app.state.local_models = {
        'local-code': os.getenv('LOCAL_CODE_MODEL', 'ollama_chat/qwen2.5-coder:7b'),
        'local-general': os.getenv('LOCAL_GENERAL_MODEL', 'ollama_chat/qwen3:8b'),
        'local-reasoning': os.getenv('LOCAL_REASONING_MODEL', 'ollama_chat/qwen3:8b'),
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
    headers = {'x-task-model': model, 'x-task-rule': rule}
    if local_reason:
        headers['x-fallback-reason'] = local_reason
    upstream = None
    if model.startswith('free-'):
        upstream, selected = await app.state.pool.send(model, payload)
        if upstream is not None:
            headers['x-upstream-model'] = selected
        else:
            model = fallback_model(model)
            headers.update({'x-task-model': model, 'x-fallback-reason': selected})
    if upstream is None:
        payload['model'] = model
        # Local requests never enter the hosted pool, including after errors.
        try:
            upstream = await app.state.client.send(app.state.client.build_request('POST', '/v1/chat/completions', json=payload), stream=True)
        except httpx.RequestError:
            raise HTTPException(502, 'Local LiteLLM unavailable')
    app.state.last_route = {'at': time.time(), 'model': model, 'rule': rule,
                            'upstream_model': headers.get('x-upstream-model'),
                            'fallback_reason': headers.get('x-fallback-reason'),
                            'status': upstream.status_code}
    if upstream.is_error or not payload.get('stream'):
        try:
            body = await upstream.aread()
        finally:
            await upstream.aclose()
        return Response(body, status_code=upstream.status_code, media_type=upstream.headers.get('content-type', 'application/json'), headers=headers)
    async def stream():
        try:
            async for chunk in upstream.aiter_raw():
                yield chunk
        finally:
            await upstream.aclose()
    return StreamingResponse(stream(), status_code=upstream.status_code, media_type='text/event-stream', headers=headers)


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
    return snapshot(app.state.local_only, app.state.pool, CONFIG, app.state.local_models,
                    app.state.online_config, app.state.openrouter_config, app.state.last_route)


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
