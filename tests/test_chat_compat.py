import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.app import app
from router.chat import normalize_chat


def test_standard_client_options():
    original = {'model': 'auto', 'messages': [], 'stream': True,
                'stream_options': {'include_usage': True}, 'reasoning_effort': 'low',
                'max_completion_tokens': 1024, 'user': 'client-id',
                'metadata': {'session': 'private'}, 'store': False, 'verbosity': 'low'}
    normalized = normalize_chat(original)
    assert normalized['max_tokens'] == 1024
    assert normalized['stream_options'] == {'include_usage': True}
    assert normalized['reasoning_effort'] == 'low'
    assert not {'user', 'metadata', 'store', 'verbosity', 'max_completion_tokens'} & normalized.keys()
    assert original['user'] == 'client-id'  # normalizing does not mutate input


def test_opencode_streaming_request_passes_gateway(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    class Events(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            yield b'data: [DONE]\n\n'
    with TestClient(app) as client:
        sent = []
        def upstream(request):
            sent.append(json.loads(request.content))
            return httpx.Response(200, stream=Events())
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://litellm')
        response = client.post('/v1/chat/completions', json={
            'model': 'auto', 'messages': [{'role': 'user', 'content': 'Write unit tests'}],
            'stream': True, 'stream_options': {'include_usage': True}, 'user': 'session-id',
            'reasoning_effort': 'low', 'max_tokens': 512,
            'tools': [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}],
        })
        assert response.status_code == 200 and '[DONE]' in response.text
        assert sent[0]['stream_options'] == {'include_usage': True}
        assert sent[0]['tools'][0]['function']['name'] == 'read_file'
        assert 'user' not in sent[0]


@pytest.mark.parametrize('extra', [
    {'plugins': [{'id': 'web'}]}, {'api_base': 'https://paid.example'},
    {'api_key': 'must-not-be-echoed'}, {'provider': {'order': ['paid']}},
    {'extra_body': {'plugins': [{'id': 'web'}]}}, {'service_tier': 'priority'},
])
def test_diagnostics_name_blocked_parameters_without_values(extra):
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [], **extra})
        assert response.status_code == 400
        assert response.json()['detail']['unsupported_parameters'] == list(extra)
        assert 'must-not-be-echoed' not in response.text
        assert 'https://paid.example' not in response.text


@pytest.mark.parametrize('extra', [
    {'stream_options': {'plugins': [{'id': 'web'}]}},
    {'stream_options': {'include_usage': 'yes'}}, {'stream_options': []},
    {'max_tokens': 5, 'max_completion_tokens': 6}, {'max_completion_tokens': -1},
    {'max_completion_tokens': True}, {'store': True}, {'reasoning_effort': 'invalid'},
])
def test_invalid_or_unsafe_client_options_rejected(extra):
    with TestClient(app) as client:
        response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [], **extra})
        assert response.status_code == 400


def test_groq_history_normalization_preserves_tools_and_original_payload():
    from copy import deepcopy
    from router.chat import groq_chat
    payload = {'model': 'auto', 'messages': [
        {'role': 'developer', 'content': 'Follow these instructions'},
        {'role': 'user', 'content': [{'type': 'text', 'text': 'first'}, {'type': 'text', 'text': 'second'}], 'name': 'client'},
        {'role': 'assistant', 'content': None, 'reasoning_content': 'private reasoning',
         'tool_calls': [{'id': 'call_123', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"file.py"}'},
                         'extra_content': {'google': {'thought_signature': 'provider-only'}}}]},
        {'role': 'tool', 'tool_call_id': 'call_123', 'content': 'file contents'},
    ]}
    original = deepcopy(payload)
    normalized = groq_chat(payload)
    assert normalized['messages'][0] == {'role': 'system', 'content': 'Follow these instructions'}
    assert normalized['messages'][1] == {'role': 'user', 'content': 'first\nsecond'}
    assistant = normalized['messages'][2]
    assert assistant['content'] is None and 'reasoning_content' not in assistant
    assert assistant['tool_calls'][0] == {'id': 'call_123', 'type': 'function', 'function': {'name': 'read_file', 'arguments': '{"path":"file.py"}'}}
    assert normalized['messages'][3] == original['messages'][3]
    assert payload == original
