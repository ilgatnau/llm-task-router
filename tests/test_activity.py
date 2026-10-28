import asyncio
import io
import json
import logging

import httpx

from router.activity import RoutingActivity, StreamObservation, logger
from router.app import app, lifespan, CONFIG


def test_waiting_request_visible_before_headers_and_then_completed(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def backend(request):
            entered.set()
            await release.wait()
            return httpx.Response(200, json={'choices': []})
        async with lifespan(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(backend), base_url='http://litellm') as proxy:
                app.state.client = proxy
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://router') as client:
                    request = asyncio.create_task(client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': '/tests sensitive-prompt'}]}))
                    await asyncio.wait_for(entered.wait(), 2)
                    current = (await client.get('/api/routing')).json()['current_route']
                    assert current['rule'] == 'explicit-tests'
                    assert current['state'] == 'waiting' and current['bytes_received'] == 0
                    assert current['completed_at'] is None
                    assert current['rule_order'] == 2
                    assert 'first matching rule' in current['explanation']
                    release.set()
                    response = await request
                    state = (await client.get('/api/routing')).json()
                    assert state['current_route'] is None
                    assert state['latest_completed_route']['state'] == 'completed'
                    assert state['latest_completed_route']['request_id'] == response.headers['x-route-request-id']
                    assert 'sensitive-prompt' not in json.dumps(state)
    asyncio.run(scenario())


def test_stream_remains_active_until_actual_end(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        class Events(httpx.AsyncByteStream):
            async def __aiter__(self):
                yield b'data: {"choices":[{"delta":{"content":"secret-model-output"}}]}\n\n'
                entered.set()
                await release.wait()
                yield b'data: [DONE]\n\n'
        async with lifespan(app):
            async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Events())), base_url='http://litellm') as proxy:
                app.state.client = proxy
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://router') as client:
                    request = asyncio.create_task(client.post('/v1/chat/completions', json={'model': 'local-code', 'messages': [], 'stream': True}))
                    await asyncio.wait_for(entered.wait(), 2)
                    state = (await client.get('/api/routing')).json()
                    assert state['current_route']['state'] == 'streaming'
                    assert state['current_route']['status'] == 200
                    assert state['current_route']['completed_at'] is None
                    assert state['current_route']['bytes_received'] > 0
                    assert state['current_route']['first_byte_ms'] is not None
                    assert 'secret-model-output' not in json.dumps(state)
                    release.set(); await request
                    assert (await client.get('/api/routing')).json()['latest_completed_route']['state'] == 'completed'
    asyncio.run(scenario())


def test_connection_failure_finishes_and_names_timeout(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    def failure(request):
        raise httpx.ReadTimeout('must-not-log-this-secret')
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(failure), base_url='http://litellm')
        assert client.post('/v1/chat/completions', json={'model': 'local-code', 'messages': []}).status_code == 502
        state = client.get('/api/routing').json()
        assert state['current_route'] is None
        assert state['latest_completed_route']['state'] == 'failed'
        assert 'ReadTimeout' in json.dumps(state)
        assert 'must-not-log-this-secret' not in json.dumps(state)


def test_concurrent_activity_and_bounded_history():
    activity = RoutingActivity(history_limit=2)
    first = activity.start('local-code', 'default', CONFIG)
    second = activity.start('local-code', 'explicit-model', CONFIG, 'local-code')
    activity.finish(second, 'completed', 'Done')
    assert activity.view()['current_route']['request_id'] == first['request_id']
    assert activity.view()['latest_completed_route']['request_id'] == second['request_id']
    activity.finish(first, 'completed', 'Done')
    third = activity.start('local-code', 'default', CONFIG)
    activity.finish(third, 'failed', 'Failed')
    assert len(activity.view()['recent_routes']) == 2


def test_progress_logging_keeps_request_active_and_stops_after_finish():
    async def scenario():
        activity = RoutingActivity()
        capture = io.StringIO()
        handler = logging.StreamHandler(capture)
        logger.addHandler(handler)
        try:
            route = activity.start('local-code', 'explicit-model', CONFIG, 'local-code')
            activity.watch(route, interval=.005)
            await asyncio.sleep(.025)
            assert 'still-waiting' in capture.getvalue()
            assert activity.view()['current_route'] is not None
            activity.finish(route, 'completed', 'Response complete')
            before = capture.getvalue()
            await asyncio.sleep(.015)
            assert capture.getvalue() == before
        finally:
            logger.removeHandler(handler)
    asyncio.run(scenario())


def test_sse_error_detected_across_chunk_boundaries_without_publishing_body():
    observation = StreamObservation()
    observation.feed(b'data: {"err')
    observation.feed(b'or":{"message":"secret-provider-text"},"choices":[]}\n\n')
    assert observation.failed
    assert not hasattr(observation, 'message')


def test_stream_error_marks_request_failed_without_replay(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"error":{"message":"secret-error"},"choices":[{"finish_reason":"error"}]}\n\n'
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, stream=Events())), base_url='http://litellm')
        assert client.post('/v1/chat/completions', json={'model':'local-code','messages':[],'stream':True}).status_code == 200
        state = client.get('/api/routing').json()
        assert state['latest_completed_route']['state'] == 'failed'
        assert 'secret-error' not in json.dumps(state)


def test_local_fallback_keeps_original_rule_explanation(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    for provider in ('GEMINI', 'GROQ', 'OPENROUTER'):
        monkeypatch.delenv(provider+'_API_KEY', raising=False)
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,json={'choices':[]})),base_url='http://litellm')
        response = client.post('/v1/chat/completions',json={'model':'auto','messages':[{'role':'user','content':'review architecture for sensitive-project'}]})
        assert response.status_code == 200
        route = client.get('/api/routing').json()['latest_completed_route']
        assert route['rule'] == 'architecture-and-hard-debugging'
        assert route['configured_route'] == 'free-architecture'
        assert route['model'] == 'local-reasoning'
        assert route['fallback_reason'] == 'online-pools-unavailable-or-exhausted'
        assert any(event['event'] == 'local-fallback' for event in route['events'])
        assert 'sensitive-project' not in json.dumps(route)


def test_reasoning_channel_is_forwarded_but_only_counts_are_logged(monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    reasoning = b'data: {"choices":[{"delta":{"reasoning_content":"private-reasoning-text"}}]}\n\n'
    answer = b'data: {"choices":[{"delta":{"content":"private-answer-text"}}]}\n\n'
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield reasoning[:20]
            yield reasoning[20:]
            yield answer
            yield b'data: [DONE]\n\n'
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200,stream=Events())),base_url='http://litellm')
        response = client.post('/v1/chat/completions',json={'model':'local-reasoning','messages':[],'stream':True,'reasoning_effort':'low'})
        assert response.status_code == 200
        assert 'private-reasoning-text' in response.text and 'private-answer-text' in response.text
        route = client.get('/api/routing').json()['latest_completed_route']
        assert route['reasoning_chunks'] == 1 and route['answer_chunks'] == 1
        assert route['response_phase'] == 'answer'
        assert any(e['event'] == 'reasoning-data' for e in route['events'])
        assert any(e['event'] == 'answer-data' for e in route['events'])
        assert 'private-reasoning-text' not in json.dumps(route)
        assert 'private-answer-text' not in json.dumps(route)
