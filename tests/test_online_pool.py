import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from router.app import app, select_model
from router.online_pool import OnlinePool, delay_seconds


def enable(monkeypatch):
    for provider in ('GEMINI', 'GROQ'):
        monkeypatch.setenv(provider+'_API_KEY', 'test-key')
        monkeypatch.setenv(provider+'_FREE_TIER_CONFIRMED', 'true')


def test_independent_provider_switch_and_quota(monkeypatch, tmp_path):
    enable(monkeypatch)
    async def scenario():
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append((request.url.host, body))
            if request.url.host == 'generativelanguage.googleapis.com':
                return httpx.Response(429, headers={'retry-after': '120'})
            assert request.url.host == 'api.groq.com'
            return httpx.Response(200, json={'choices': []}, headers={
                'x-ratelimit-limit-requests': '1000', 'x-ratelimit-remaining-requests': '999',
                'x-ratelimit-limit-tokens': '8000', 'x-ratelimit-remaining-tokens': '7000',
                'x-ratelimit-reset-requests': '2h', 'x-ratelimit-reset-tokens': '60s'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            db = str(tmp_path / 'online.db')
            pool = OnlinePool(client, '', db)
            payload = {'model': 'free-code', 'messages': [{'role': 'user', 'content': 'implement login'}], 'tools': [{'type': 'function', 'function': {'name': 'read_file'}}]}
            response, model = await pool.send('free-code', payload)
            assert response.status_code == 200 and model == 'groq/openai/gpt-oss-120b'
            await response.aclose()
            assert [host for host, _ in calls] == ['generativelanguage.googleapis.com', 'api.groq.com']
            assert all(body['tools'] == payload['tools'] and 'plugins' not in body for _, body in calls)
            state = pool.status()
            assert state['providers']['gemini']['models']['gemini-flash']['cooldown_seconds'] > 0
            groq = state['providers']['groq']['models']['groq-reasoning']
            assert groq['provider_reported_remaining_requests'] == '999'
            assert groq['local_daily_estimate']['used'] == 1
            assert state['providers']['openrouter']['local_daily_estimate']['used'] == 0  # independent of OpenRouter allowance
            pool.db.close()
            restarted = OnlinePool(client, '', db)
            assert restarted.direct_status()['groq']['models']['groq-reasoning']['local_daily_estimate']['used'] == 1
            assert restarted.direct_status()['gemini']['models']['gemini-flash']['cooldown_seconds'] > 0
            restarted.db.close()
    asyncio.run(scenario())


def test_keys_without_free_tier_confirmation_never_send(monkeypatch):
    for provider in ('GEMINI', 'GROQ'):
        monkeypatch.setenv(provider+'_API_KEY', 'test-key')
        monkeypatch.setenv(provider+'_FREE_TIER_CONFIRMED', 'false')
    async def scenario():
        def forbidden(request):
            raise AssertionError('Unconfirmed account must not receive inference')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            pool = OnlinePool(client, '')
            assert (await pool.send('free-code', {'messages': []}))[0] is None
            assert pool.direct_status()['gemini']['disabled_reason'] == 'free-tier-not-confirmed'
            pool.db.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status', [401, 402, 403, 429, 503])
def test_all_online_limits_fall_back_bounded(monkeypatch, status):
    enable(monkeypatch)
    async def scenario():
        calls = []
        def handler(request):
            calls.append(request.url.host)
            return httpx.Response(status, json={'error': 'failed'}, headers={'retry-after': '60'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pool = OnlinePool(client, '')
            assert (await pool.send('free-code', {'messages': []}))[0] is None
            # Auth/payment errors disable remaining models of the same provider.
            assert len(calls) == (2 if status in (401, 402, 403) else 4)
            assert set(calls) == {'generativelanguage.googleapis.com', 'api.groq.com'}
            if status == 429:
                calls.clear()
                assert (await pool.send('free-code', {'messages': []}))[0] is None
                assert calls == []
            pool.db.close()
    asyncio.run(scenario())


def test_exhausted_safety_caps_skip_online(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        def forbidden(request):
            raise AssertionError('Exhausted local budgets must not send inference')
        async with httpx.AsyncClient(transport=httpx.MockTransport(forbidden)) as client:
            pool = OnlinePool(client, '')
            with pool.db:
                for alias, m in pool.online['models'].items():
                    pool.db.executemany('INSERT INTO direct_attempts VALUES (?,?,200)', [(time.time(), alias)]*m['daily_safety_cap'])
            assert (await pool.send('free-code', {'messages': []}))[0] is None
            pool.db.close()
    asyncio.run(scenario())


def test_provider_zero_tokens_skips_until_reset(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        async with httpx.AsyncClient() as client:
            pool = OnlinePool(client, '')
            pool.observe('groq-reasoning', httpx.Response(200, headers={'x-ratelimit-remaining-tokens': '0', 'x-ratelimit-reset-tokens': '2m59.56s'}))
            assert pool.direct_status()['groq']['models']['groq-reasoning']['cooldown_seconds'] >= 179
            assert delay_seconds('2m59.56s') == 179.56
            assert delay_seconds('bad') == 60
            pool.db.close()
    asyncio.run(scenario())


def test_online_default_local_tests_and_appropriate_fallback(monkeypatch):
    monkeypatch.delenv('LOCAL_ONLY', raising=False)
    for provider in ('GEMINI', 'GROQ', 'OPENROUTER'):
        monkeypatch.delenv(provider+'_API_KEY', raising=False)
    with TestClient(app) as client:
        calls = []
        def local(request):
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local), base_url='http://litellm')
        assert client.get('/health').json()['mode'] == 'hybrid'
        assert select_model({'messages': [{'role': 'user', 'content': 'implement login'}]})[0] == 'free-code'
        assert select_model({'messages': [{'role': 'user', 'content': 'write unit tests'}]})[0] == 'local-code'
        for prompt, expected in [('review architecture', 'local-reasoning'), ('implement login', 'local-code'), ('write unit tests', 'local-code')]:
            response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': prompt}]})
            assert response.status_code == 200 and calls[-1]['model'] == expected
            if prompt == 'write unit tests':
                assert 'x-fallback-reason' not in response.headers
            else:
                assert response.headers['x-fallback-reason'] == 'online-pools-unavailable-or-exhausted'
        assert set(client.get('/quota').json()['providers']) == {'gemini', 'groq', 'openrouter'}


def test_full_chain_exhaustion_reaches_local_reasoning(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    with TestClient(app) as client:
        pool = app.state.pool
        pool.key = 'openrouter-test-key'
        attempts = []
        def public(request):
            if request.url.path.endswith('/models'):
                return httpx.Response(200, json={'data': [{'id': m, 'pricing': {'prompt': '0', 'completion': '0'}, 'supported_parameters': ['tools']} for m in pool.models.values()]})
            if request.url.path.endswith('/key'):
                return httpx.Response(200, json={'data': {'free_model_daily_requests': {'used': 0, 'limit': 50, 'remaining': 50}}})
            attempts.append(request.url.host)
            return httpx.Response(503 if request.url.host == 'openrouter.ai' else 429, headers={'retry-after': '60'})
        local = []
        def ollama(request):
            local.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public), base_url='https://openrouter.ai/api/v1')
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(ollama), base_url='http://litellm')
        response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'review system architecture'}]})
        assert response.status_code == 200
        assert len(attempts) == 7
        assert attempts[-3:] == ['openrouter.ai']*3
        assert local[0]['model'] == 'local-reasoning'
        assert response.headers['x-fallback-reason'] == 'online-pools-unavailable-or-exhausted'
        quota = client.get('/quota').json()['providers']
        assert quota['openrouter']['local_daily_estimate']['used'] == 3
        assert quota['groq']['models']['groq-reasoning']['local_daily_estimate']['used'] == 1


def test_direct_stream_preserves_bytes_and_no_replay(monkeypatch):
    enable(monkeypatch)
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
            raise httpx.ReadError('interrupted')
    with TestClient(app) as client:
        calls = []
        def public(request):
            calls.append(request.url.host)
            return httpx.Response(200, stream=Events())
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public))
        with pytest.raises(httpx.ReadError):
            client.post('/v1/chat/completions', json={'model': 'free-code', 'messages': [], 'stream': True})
        assert calls == ['generativelanguage.googleapis.com']
