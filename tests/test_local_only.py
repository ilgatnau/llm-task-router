import json
import httpx
import pytest
from fastapi.testclient import TestClient
from router.app import app


@pytest.mark.parametrize('setting', ['true', 'invalid'])
def test_local_only_blocks_all_hosted_calls(monkeypatch, setting):
    if setting is None:
        monkeypatch.delenv('LOCAL_ONLY', raising=False)
    else:
        monkeypatch.setenv('LOCAL_ONLY', setting)
    monkeypatch.setenv('OPENROUTER_API_KEY', 'configured-but-must-not-be-used')
    with TestClient(app) as client:
        assert app.state.pool is None
        calls = []
        def local(request):
            assert request.url.host == 'litellm'
            calls.append(json.loads(request.content))
            return httpx.Response(200, json={'choices': []})
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(local), base_url='http://litellm')
        assert client.get('/health').json()['mode'] == 'local-only'
        assert client.get('/quota').json()['hosted_requests_enabled'] is False
        assert {m['id'] for m in client.get('/v1/models').json()['data']} == {'auto', 'local-code', 'local-general', 'local-reasoning'}
        for alias in ('free-tests', 'free-architecture', 'free-code', 'free-cicd'):
            assert client.post('/v1/chat/completions', json={'model': alias, 'messages': []}).status_code == 403
        for prompt, alias in [('review architecture', 'local-reasoning'), ('implement login', 'local-code'), ('write pytest tests', 'local-code'), ('build deployment pipeline', 'local-code'), ('summarize this', 'local-code')]:
            response = client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': prompt}]})
            assert response.status_code == 200
            assert response.headers['x-task-model'] == alias
        assert len(calls) == 5
