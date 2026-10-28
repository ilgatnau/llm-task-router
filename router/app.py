"""Task selection gateway. LiteLLM owns provider protocol translation."""
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import yaml
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response, StreamingResponse
from router.free_pool import CHAT_FIELDS, Pool

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


@asynccontextmanager
async def lifespan(app):
    app.state.local_only = os.getenv('LOCAL_ONLY', 'true').lower() != 'false'
    async with httpx.AsyncClient(base_url=os.getenv('LITELLM_URL', 'http://127.0.0.1:4000'), timeout=httpx.Timeout(300, connect=10)) as client:
        app.state.client = client
        async with httpx.AsyncClient(base_url='https://openrouter.ai/api/v1', timeout=httpx.Timeout(300, connect=10)) as public:
            app.state.pool = None if app.state.local_only else Pool(public, os.getenv('OPENROUTER_API_KEY', ''), os.getenv('QUOTA_DB', ':memory:'))
            try:
                yield
            finally:
                if app.state.pool is not None:
                    app.state.pool.db.close()


app = FastAPI(lifespan=lifespan)


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
    if set(payload) - CHAT_FIELDS:
        raise HTTPException(400, 'Unsupported parameters; provider overrides and paid addons are disabled')
    model, rule = select_model(payload)
    if app.state.local_only and not model.startswith('local-'):
        raise HTTPException(403, 'Hosted inference is disabled in local-only mode')
    headers = {'x-task-model': model, 'x-task-rule': rule}
    upstream = None
    if model.startswith('free-'):
        upstream, selected = await app.state.pool.send(model, payload)
        if upstream is not None:
            headers['x-upstream-model'] = selected
        else:
            model = 'local-code'
            headers.update({'x-task-model': model, 'x-fallback-reason': selected})
    if upstream is None:
        payload['model'] = model
        # Local requests never enter the hosted pool, including after errors.
        try:
            upstream = await app.state.client.send(app.state.client.build_request('POST', '/v1/chat/completions', json=payload), stream=True)
        except httpx.RequestError:
            raise HTTPException(502, 'Local LiteLLM unavailable')
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
