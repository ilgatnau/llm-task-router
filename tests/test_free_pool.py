import asyncio
import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.app import app
from router.free_pool import Pool, free_entry


def entry(model, price='0'):
    return {'id': model, 'pricing': {'prompt': price, 'completion': '0'}, 'supported_parameters': ['tools']}


def run(coro):
    return asyncio.run(coro)


def test_switch_on_model_limit_and_observe(tmp_path):
    async def scenario():
        attempts = []
        quota_calls = []
        pool = None
        async def handler(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [entry(m) for m in pool.models.values()]})
            if request.url.path.endswith('/key'):
                quota_calls.append(1)
                return httpx.Response(200, json={'data': {'free_model_daily_requests': {'used': len(attempts), 'limit': 50, 'remaining': 50-len(attempts)}}})
            body = json.loads(request.content)
            attempts.append(body)
            if len(attempts) == 1:
                return httpx.Response(429, json={'error': 'model capacity'}, headers={'Retry-After': '60'})
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            db = str(tmp_path / 'quota.db')
            pool = Pool(client, 'secret', db)
            response, model = await pool.send('free-tests', {'model': 'free-tests', 'messages': []})
            assert response.status_code == 200
            await response.aclose()
            assert model == pool.models['inkling']
            assert len(attempts) == 2 and len(quota_calls) == 2
            assert all(a['model'].endswith(':free') and a['provider']['max_price'] == {'prompt': 0, 'completion': 0} for a in attempts)
            state = pool.status()
            assert state['local_daily_estimate']['used'] == 2
            assert state['provider_reported_daily']['remaining'] == 48
            assert state['models']['north']['cooldown_seconds'] > 0
            assert state['models']['north']['remaining_model_quota'] is None
            pool.db.close()
            restarted = Pool(client, 'secret', db)
            assert restarted.status()['local_daily_estimate']['used'] == 2
            assert restarted.status()['models']['north']['cooldown_seconds'] > 0
            restarted.db.close()
    run(scenario())


@pytest.mark.parametrize('mode', ['daily', 'platform', 'pricing', 'catalog-error', 'auth', 'all-models'])
def test_stopping_conditions(mode):
    async def scenario():
        calls = []
        pool = None
        async def handler(request):
            if request.url.path.endswith('/key'):
                remaining = 0 if mode == 'daily' else 50
                return httpx.Response(200, json={'data': {'free_model_daily_requests': {'used': 50-remaining, 'limit': 50, 'remaining': remaining}}})
            if request.url.path.endswith('/models'):
                if mode == 'catalog-error':
                    return httpx.Response(503)
                return httpx.Response(200, json={'data': [entry(m, '1' if mode == 'pricing' else '0') for m in pool.models.values()]})
            calls.append(request)
            headers = {'x-ratelimit-limit': '20', 'retry-after': '60'} if mode == 'platform' else {}
            return httpx.Response(401 if mode == 'auth' else 429, headers=headers)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            pool = Pool(client, 'secret')
            response, reason = await pool.send('free-code', {'messages': []})
            assert response is None
            assert len(calls) == {'daily': 0, 'platform': 1, 'pricing': 0, 'catalog-error': 0, 'auth': 1, 'all-models': 4}[mode]
            pool.db.close()
    run(scenario())


def test_missing_remote_counter_is_not_fabricated():
    async def scenario():
        async def handler(request):
            return httpx.Response(200, json={'data': {'limit_remaining': 999}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            pool = Pool(client, 'secret')
            await pool.refresh_quota()
            assert pool.status()['provider_reported_daily'] is None
            assert 'estimate' in pool.status()['quota_note']
            pool.db.close()
    run(scenario())


def test_minute_limit_stops_without_inference():
    async def scenario():
        pool = None
        async def handler(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [entry(m) for m in pool.models.values()]})
            assert request.url.path.endswith('/key')
            return httpx.Response(200, json={'data': {}})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            pool = Pool(client, 'secret')
            for _ in range(20): pool.record(pool.models['north'])
            assert await pool.send('free-tests', {'messages': []}) == (None, 'shared-minute-limit')
            pool.db.close()
    run(scenario())


def test_price_and_addons_guard():
    assert not free_entry(entry('vendor/paid', '0'))
    assert not free_entry(entry('vendor/model:free', '0.01'))
    assert not free_entry({**entry('vendor/model:free'), 'pricing': {'prompt': '0', 'completion': '0', 'web_search': '0.1'}})
    with TestClient(app) as client:
        assert client.post('/v1/chat/completions', json={'model': 'cloud-code', 'messages': []}).status_code == 400
        assert client.post('/v1/chat/completions', json={'model': 'free-code', 'messages': [], 'plugins': [{'id': 'web'}]}).status_code == 400
        assert client.post('/v1/chat/completions', json={'model': 'local-code', 'messages': [], 'api_base': 'https://paid.example'}).status_code == 400


def test_missing_key_falls_back_locally_and_quota_is_available(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    with TestClient(app) as client:
        app.state.pool.key = ''
        local_calls = []
        def local(request):
            local_calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local), base_url='http://litellm')
        response = client.post('/v1/chat/completions', json={'model': 'free-code', 'messages': []})
        assert response.status_code == 200
        assert response.headers['x-fallback-reason'] == 'online-pools-unavailable-or-exhausted'
        assert local_calls[0]['model'] == 'local-code'
        quota = client.get('/quota').json()['providers']['openrouter']
        assert quota['provider_reported_daily'] is None
        assert quota['local_daily_estimate']['used'] == 0


def test_hosted_stream_and_local_isolation(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: [DONE]\n\n'
    with TestClient(app) as client:
        pool = app.state.pool
        pool.key = 'test-key'
        hosted = []
        def public(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [entry(m) for m in pool.models.values()]})
            if request.url.path.endswith('/key'):
                return httpx.Response(200, json={'data': {'free_model_daily_requests': {'used': 0, 'limit': 50, 'remaining': 50}}})
            hosted.append(json.loads(request.content))
            return httpx.Response(200, stream=Events())
        pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public), base_url='https://openrouter.ai/api/v1')
        response = client.post('/v1/chat/completions', json={'model': 'free-tests', 'messages': [], 'stream': True})
        assert response.status_code == 200
        assert '[DONE]' in response.text
        assert response.headers['x-upstream-model'] == 'openrouter/'+pool.models['north']
        assert len(hosted) == 1
        local = []
        def local_handler(request):
            local.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local_handler), base_url='http://litellm')
        client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': '/local architecture'}]})
        assert len(hosted) == 1
        assert local[0]['model'] == 'local-code'


def test_no_replay_after_stream_started(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    class Broken(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError('stream interrupted')
    with TestClient(app) as client:
        pool = app.state.pool
        pool.key = 'test-key'
        calls = []
        def public(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [entry(m) for m in pool.models.values()]})
            if request.url.path.endswith('/key'):
                return httpx.Response(200, json={'data': {}})
            calls.append(request)
            return httpx.Response(200, stream=Broken())
        pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public), base_url='https://openrouter.ai/api/v1')
        with pytest.raises(httpx.ReadError):
            client.post('/v1/chat/completions', json={'model': 'free-code', 'messages': [], 'stream': True})
        assert len(calls) == 1


@pytest.mark.parametrize('availability', ['free', 'paid', 'missing', 'no-tools'])
def test_chinese_reasoning_candidate_is_verified_before_use(availability):
    async def scenario():
        pool = None
        attempts = []
        def handler(request):
            if request.url.path.endswith('/models'):
                catalog = [entry(m) for alias, m in pool.models.items() if alias != 'xing']
                if availability != 'missing':
                    chinese = entry(pool.models['xing'], '0.01' if availability == 'paid' else '0')
                    if availability == 'no-tools':
                        chinese['supported_parameters'] = []
                    catalog.append(chinese)
                return httpx.Response(200, json={'data': catalog})
            if request.url.path.endswith('/key'):
                return httpx.Response(200, json={'data': {}})
            attempts.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            pool = Pool(client, 'test-key')
            response, model = await pool.send('free-architecture', {'messages': []})
            assert response.status_code == 200
            assert model == pool.models['xing' if availability == 'free' else 'nemotron']
            assert len(attempts) == 1
            assert attempts[0]['provider']['max_price'] == {'prompt': 0, 'completion': 0}
            assert pool.status()['local_daily_estimate']['used'] == 1
            await response.aclose()
            pool.db.close()
    run(scenario())


def test_openrouter_rejection_tries_next_verified_free_candidate():
    async def scenario():
        pool = None
        attempts = []
        def handler(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [entry(m) for m in pool.models.values()]})
            if request.url.path.endswith('/key'):
                return httpx.Response(200, json={'data': {}})
            attempts.append(json.loads(request.content))
            return httpx.Response(400 if len(attempts) == 1 else 200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            pool = Pool(client, 'test-key')
            events = []
            response, model = await pool.send('free-tests', {'messages': []},
                on_event=lambda name, message, **fields: events.append(message))
            assert response.status_code == 200 and model == pool.models['inkling']
            assert len(attempts) == 2
            assert any('upstream-error (HTTP 400)' in m for m in events)
            assert all(a['provider']['max_price'] == {'prompt': 0, 'completion': 0} for a in attempts)
            await response.aclose()
            pool.db.close()
    run(scenario())
