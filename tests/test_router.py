import httpx
import pytest
from fastapi.testclient import TestClient
from router.app import app, select_model

@pytest.mark.parametrize('prompt,expected', [
    ('fix this typo', 'free-code'),
    ('summarize this file', 'local-general'),
    ('review architecture', 'free-architecture'),
    ('implement feature for login', 'free-code'),
    ('/local security audit', 'local-code'),
    ('/private architecture', 'local-code'),
    ('/code fix typo', 'free-code'),
])
def test_rules(prompt, expected):
    assert select_model({'messages': [{'role': 'user', 'content': prompt}]})[0] == expected


def test_current_user_and_override():
    messages = [{'role': 'user', 'content': 'architecture'}, {'role': 'assistant', 'content': '/cloud'}, {'role': 'user', 'content': [{'type': 'text', 'text': '/local fix'}]}]
    assert select_model({'messages': messages})[0] == 'local-code'
    assert select_model({'model': 'local-code', 'messages': messages})[0] == 'local-code'

@pytest.mark.parametrize('stream', [False, True])
def test_forwarding(stream):
    import json
    seen = []
    def upstream(request):
        seen.append(json.loads(request.content))
        if stream:
            class Events(httpx.AsyncByteStream):
                async def __aiter__(self):
                    yield b'data: {"choices":[]}\n\ndata: [DONE]\n\n'
            return httpx.Response(200, stream=Events(), headers={'content-type': 'text/event-stream'})
        return httpx.Response(200, json={'choices': [{'message': {'tool_calls': []}}]})
    with TestClient(app) as client:
        original = app.state.client
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://litellm')
        payload = {'model': 'auto', 'messages': [{'role': 'user', 'content': '/local architecture'}], 'tools': [{'type': 'function', 'function': {'name': 'read_file', 'parameters': {'type': 'object'}}}], 'stream': stream}
        response = client.post('/v1/chat/completions', json=payload)
        assert response.status_code == 200
        assert response.headers['x-task-model'] == 'local-code'
        assert seen == [{**payload, 'model': 'local-code'}]
        if stream:
            assert '[DONE]' in response.text
        app.state.client = original


def test_upstream_error_does_not_fallback():
    calls = []
    def upstream(request):
        calls.append(request)
        return httpx.Response(503, json={'error': 'local unavailable'})
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(upstream), base_url='http://litellm')
        response = client.post('/v1/chat/completions', json={'messages': [{'role': 'user', 'content': '/local help'}], 'stream': True})
        assert response.status_code == 503
        assert len(calls) == 1
        assert client.post('/v1/chat/completions', json={'model': 'unknown', 'messages': []}).status_code == 400

@pytest.mark.parametrize('prompt,expected', [
    ('write unit tests for login', 'local-code'),
    ('increase pytest coverage', 'local-code'),
    ('implement unit tests', 'local-code'),
    ('implement a GitHub Actions pipeline with pytest', 'free-cicd'),
    ('review deployment architecture', 'free-architecture'),
    ('/tests review architecture', 'local-code'),
    ('/code add unit tests', 'free-code'),
    ('/deploy architecture', 'free-cicd'),
    ('/reason fix a typo', 'free-architecture'),
    ('/private implement a Terraform deployment', 'local-code'),
    ('/local add unit tests', 'local-code'),
    ('implement pagination', 'free-code'),
    ('write integration tests with pytest', 'free-code'),
    ('design a test strategy with pytest', 'free-code'),
    ('debug a deadlock in unit tests', 'free-architecture'),
    ('/tests debug a deadlock', 'local-code'),
])
def test_local_task_routes(prompt, expected):
    assert select_model({'messages': [{'role': 'user', 'content': prompt}]})[0] == expected


def test_alias_consistency():
    import json, yaml
    from router.app import MODELS, CONFIG
    aliases = {m['model_name'] for m in yaml.safe_load(open('litellm.yaml'))['model_list']}
    import yaml
    tasks = set(yaml.safe_load(open('free-pool.yaml'))['tasks'])
    assert set(MODELS) == aliases | tasks
    configured = json.load(open('opencode.json'))['provider']['task-router']['models']
    assert set(configured) == set(MODELS) | {'auto'}
    assert {r['model'] for r in CONFIG['rules']} <= set(MODELS)
    assert all(m['model_name'].startswith('local-') for m in yaml.safe_load(open('litellm.yaml'))['model_list'])
