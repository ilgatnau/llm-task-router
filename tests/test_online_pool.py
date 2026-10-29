import asyncio
import json
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from router.app import app, select_model
from router.activity import RoutingActivity
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
            if request.url.host == 'api.groq.com':
                return httpx.Response(429, headers={'retry-after': '120'})
            assert request.url.host == 'generativelanguage.googleapis.com'
            return httpx.Response(200, json={'choices': []}, headers={
                'x-ratelimit-limit-requests': '1000', 'x-ratelimit-remaining-requests': '999',
                'x-ratelimit-limit-tokens': '8000', 'x-ratelimit-remaining-tokens': '7000',
                'x-ratelimit-reset-requests': '2h', 'x-ratelimit-reset-tokens': '60s'})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url='https://openrouter.ai/api/v1') as client:
            db = str(tmp_path / 'online.db')
            pool = OnlinePool(client, '', db)
            payload = {'model': 'free-code', 'messages': [{'role': 'user', 'content': 'implement login'}], 'tools': [{'type': 'function', 'function': {'name': 'read_file'}}]}
            response, model = await pool.send('free-code', payload)
            assert response.status_code == 200 and model == 'gemini/gemini-3.8-flash'
            await response.aclose()
            assert [host for host, _ in calls] == ['api.groq.com', 'api.groq.com', 'generativelanguage.googleapis.com']
            assert all(body['tools'] == payload['tools'] and 'plugins' not in body for _, body in calls)
            state = pool.status()
            assert state['providers']['groq']['models']['groq-reasoning']['cooldown_seconds'] > 0
            selected = state['providers']['gemini']['models']['gemini-flash']
            assert selected['provider_reported_remaining_requests'] == '999'
            assert selected['local_daily_estimate']['used'] == 1
            assert state['providers']['openrouter']['local_daily_estimate']['used'] == 0  # independent of OpenRouter allowance
            pool.db.close()
            restarted = OnlinePool(client, '', db)
            assert restarted.direct_status()['groq']['models']['groq-reasoning']['local_daily_estimate']['used'] == 1
            assert restarted.direct_status()['groq']['models']['groq-reasoning']['cooldown_seconds'] > 0
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


@pytest.mark.parametrize('status', [400, 401, 402, 403, 413, 422, 429, 503])
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
        assert len(attempts) == 8
        assert attempts[-4:] == ['openrouter.ai']*4
        assert local[0]['model'] == 'local-reasoning'
        assert response.headers['x-fallback-reason'] == 'online-pools-unavailable-or-exhausted'
        quota = client.get('/quota').json()['providers']
        assert quota['openrouter']['local_daily_estimate']['used'] == 4
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
        assert calls == ['api.groq.com']


@pytest.mark.parametrize('rejection', [400, 413, 422])
def test_rejected_groq_requests_try_gemini_before_ollama(monkeypatch, rejection):
    enable(monkeypatch)
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    with TestClient(app) as client:
        attempts = []
        def public(request):
            attempts.append(request.url.host)
            if request.url.host == 'api.groq.com':
                return httpx.Response(rejection, json={'error': 'secret-upstream-body'})
            return httpx.Response(200, json={'choices': []})
        def local(request):
            raise AssertionError('An online rejection must not skip eligible Gemini models')
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public))
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local))
        response = client.post('/v1/chat/completions', json={
            'model': 'auto', 'messages': [{'role': 'user', 'content': 'secret-user-prompt'}]})
        assert response.status_code == 200
        assert attempts == ['api.groq.com', 'api.groq.com', 'generativelanguage.googleapis.com']
        assert response.headers['x-upstream-model'] == 'gemini/gemini-3.8-flash'
        state = client.get('/api/routing').json()
        path = state['latest_completed_route']['failover_path']
        assert [(step['provider'], step['state']) for step in path] == [('groq', 'rejected'), ('groq', 'rejected'), ('gemini', 'completed')]
        assert [step['http_status'] for step in path] == [rejection, rejection, 200]
        messages = [e['message'] for e in state['latest_completed_route']['events']]
        assert any(f'upstream-error (HTTP {rejection})' in m and 'groq/' in m for m in messages)
        assert any('selected (HTTP 200)' in m and 'gemini/' in m for m in messages)
        assert 'secret-upstream-body' not in json.dumps(state)
        assert 'secret-user-prompt' not in json.dumps(state)
        assert 'test-key' not in json.dumps(state)


@pytest.mark.parametrize('skip', ['missing-api-key', 'free-tier-not-confirmed', 'daily-cap', 'minute-cap'])
def test_online_skip_reasons_visible_with_remaining_provider_quota(monkeypatch, skip):
    enable(monkeypatch)
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    monkeypatch.delenv('OPENROUTER_API_KEY', raising=False)
    for provider in ('GEMINI', 'GROQ'):
        if skip == 'missing-api-key':
            monkeypatch.delenv(provider+'_API_KEY', raising=False)
        elif skip == 'free-tier-not-confirmed':
            monkeypatch.setenv(provider+'_FREE_TIER_CONFIRMED', 'false')
    with TestClient(app) as client:
        pool = app.state.pool
        if skip in ('daily-cap', 'minute-cap'):
            # Provider reports quota, but a separate local safety budget is full.
            with pool.db:
                for alias, model in pool.online['models'].items():
                    pool.db.executemany('INSERT INTO direct_attempts VALUES (?,?,200)',
                        [(time.time() - (120 if skip == 'daily-cap' else 0), alias)] * model['daily_safety_cap' if skip == 'daily-cap' else 'minute_safety_cap'])
                    pool.observe(alias, httpx.Response(200, headers={'x-ratelimit-remaining-requests': '999'}))
        def forbidden(request):
            raise AssertionError('Disabled or budget-exhausted models must not be called')
        pool.client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': []})), base_url='http://litellm')
        response = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': 'implement login'}]})
        assert response.status_code == 200
        state = client.get('/api/routing').json()
        messages = [e['message'] for e in state['latest_completed_route']['events']]
        expected = {'daily-cap': 'local-daily-safety-cap-exhausted (not provider quota)',
                    'minute-cap': 'local-minute-safety-cap-exhausted (not provider quota)'}.get(skip, skip)
        assert sum(expected in m and m.startswith(('groq/', 'gemini/')) for m in messages) == 4
        assert any('openrouter: missing-api-key' in m for m in messages)


@pytest.mark.parametrize('stream', [False, True])
def test_groq_success_never_invokes_ollama_or_another_provider(monkeypatch, stream):
    enable(monkeypatch)
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    with TestClient(app) as client:
        calls = []
        def public(request):
            calls.append(request.url.host)
            assert request.url.host == 'api.groq.com'
            if stream:
                class Events(httpx.AsyncByteStream):
                    async def __aiter__(self):
                        yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\ndata: [DONE]\n\n'
                return httpx.Response(200, stream=Events())
            return httpx.Response(200, json={'choices': [{'message': {'role': 'assistant', 'content': 'ok'}}]})
        def local(request):
            raise AssertionError('A successful Groq request must never invoke Ollama')
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(public))
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local))
        response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': 'implement pagination'}], 'stream': stream})
        assert response.status_code == 200
        assert response.headers['x-upstream-model'] == 'groq/openai/gpt-oss-120b'
        assert 'x-fallback-reason' not in response.headers
        assert calls == ['api.groq.com']
        path = client.get('/api/routing').json()['latest_completed_route']['failover_path']
        assert len(path) == 1 and path[0]['provider'] == 'groq' and path[0]['state'] == 'completed'


@pytest.mark.parametrize('effort,expected', [('none', None), ('minimal', 'low'), ('xhigh', 'high'), ('medium', 'medium')])
def test_groq_reasoning_compatibility(monkeypatch, effort, expected):
    enable(monkeypatch)
    async def scenario():
        def handler(request):
            body = json.loads(request.content)
            assert body.get('reasoning_effort') == expected
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pool = OnlinePool(client, '')
            response, model = await pool.send('free-code', {'messages': [], 'reasoning_effort': effort})
            assert model.startswith('groq/')
            await response.aclose()
            pool.db.close()
    asyncio.run(scenario())


def test_slow_headers_do_not_serialize_other_requests_and_are_visible(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def handler(request):
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pool = OnlinePool(client, '')
            activity = RoutingActivity()
            route = activity.start('free-code', 'default', {'default': 'free-code', 'rules': []})
            first = asyncio.create_task(pool.send('free-code', {'messages': []}, on_event=lambda n, m, **f: activity.event(route, n, m, **f)))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                assert route['pending_backend'] == 'groq/openai/gpt-oss-120b'
                assert route['state'] == 'waiting-provider-headers'
                second_response, _ = await asyncio.wait_for(pool.send('free-code', {'messages': []}), 1)
                await second_response.aclose()
            finally:
                release.set()
                response, _ = await first
                await response.aclose()
                pool.db.close()
    asyncio.run(scenario())


def test_online_header_timeout_tries_next_model(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        calls = []
        async def handler(request):
            calls.append(json.loads(request.content)['model'])
            if len(calls) == 1:
                await asyncio.Event().wait()
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pool = OnlinePool(client, '')
            pool.header_timeout = .02
            events = []
            response, selected = await asyncio.wait_for(pool.send('free-code', {'messages': []},
                on_event=lambda n, m, **f: events.append(m)), 1)
            assert selected == 'groq/openai/gpt-oss-20b'
            assert any('response-header-timeout' in e for e in events)
            await response.aclose()
            pool.db.close()
    asyncio.run(scenario())


def test_provider_error_summary_redacts_arbitrary_text(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        from router.provider_errors import rejected_request_summary
        response = httpx.Response(400, json={'error': {'code': 'tool_use_failed',
            'message': 'Secret tool arguments and token abc123; invalid reasoning_effort',
            'failed_generation': 'never-publish-this'}})
        summary = await rejected_request_summary(response)
        assert summary == 'tool_use_failed; referenced fields: reasoning_effort'
        assert 'Secret' not in summary and 'abc123' not in summary
    asyncio.run(scenario())


def test_groq_normalized_history_and_gemini_fallback_keep_distinct_metadata(monkeypatch):
    enable(monkeypatch)
    async def scenario():
        messages = [
            {'role': 'user', 'content': 'continue'},
            {'role': 'assistant', 'content': 'previous answer', 'reasoning_content': 'private reasoning'},
            {'role': 'user', 'content': [{'type': 'text', 'text': 'next task'}]},
        ]
        calls = []
        def handler(request):
            body = json.loads(request.content)
            calls.append(request.url.host)
            if request.url.host == 'api.groq.com':
                assert 'reasoning_content' not in body['messages'][1]
                assert body['messages'][2]['content'] == 'next task'
                return httpx.Response(429)
            assert body['messages'] == messages
            return httpx.Response(200, json={'choices': []})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            pool = OnlinePool(client, '')
            response, model = await pool.send('free-code', {'messages': messages})
            assert model.startswith('gemini/')
            assert calls == ['api.groq.com', 'api.groq.com', 'generativelanguage.googleapis.com']
            await response.aclose()
            pool.db.close()
    asyncio.run(scenario())


def test_message_error_paths_are_specific_and_redacted():
    async def scenario():
        from router.provider_errors import rejected_request_summary
        response = httpx.Response(400, json={'error': {'message':
            "'messages.2.reasoning_content': unsupported field; secret-prompt; messages[4].tool_calls private data"}})
        summary = await rejected_request_summary(response)
        assert 'messages[2].reasoning_content' in summary
        assert 'messages[4].tool_calls' in summary
        assert 'secret-prompt' not in summary and 'private data' not in summary
    asyncio.run(scenario())
