import json

import httpx
import pytest
from fastapi.testclient import TestClient

from router.app import app, CONFIG, resolve_route


def test_dashboard_assets_and_snapshot_do_not_send_inference(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    monkeypatch.setenv('GEMINI_API_KEY', 'sentinel-gemini-key')
    monkeypatch.setenv('GEMINI_FREE_TIER_CONFIRMED', 'true')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'sentinel-openrouter-key')
    with TestClient(app) as client:
        def forbidden(request):
            raise AssertionError('Reading the dashboard must not call providers')
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        before = app.state.pool.db.execute('SELECT count(*) FROM direct_attempts').fetchone()[0]
        page = client.get('/')
        assert page.status_code == 200 and 'Your models. One clear view.' in page.text
        assert "script-src 'self'" in page.headers['content-security-policy']
        assert client.get('/assets/dashboard.js').status_code == 200
        assert client.get('/assets/dashboard.css').status_code == 200
        response = client.get('/api/dashboard')
        assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
        data = response.json()
        assert data['rules'] == CONFIG
        assert len(data['chains']['free-code']) == 8
        assert data['chains']['free-code'][-1]['provider'] == 'ollama'
        assert data['providers']['gemini']['enabled'] is True
        assert data['providers']['gemini']['models']['gemini-flash']['provider_reported_remaining_requests'] is None
        assert data['providers']['openrouter']['enabled'] is True
        assert 'sentinel-gemini-key' not in response.text and 'sentinel-openrouter-key' not in response.text
        assert app.state.pool.db.execute('SELECT count(*) FROM direct_attempts').fetchone()[0] == before


@pytest.mark.parametrize('task,expected', [('write unit tests', 'local-code'), ('review architecture', 'free-architecture'), ('/private implement login', 'local-code'), ('implement feature', 'free-code')])
def test_preview_matches_real_selection_without_using_quota(monkeypatch, task, expected):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_FREE_TIER_CONFIRMED', 'true')
    with TestClient(app) as client:
        def forbidden(request):
            raise AssertionError('A preview must not call providers')
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(forbidden))
        response = client.post('/api/route-preview', json={'task': task})
        data = response.json()
        assert data['model'] == expected
        assert data['model'] == resolve_route({'messages': [{'role': 'user', 'content': task}]}, False)[0]
        assert app.state.pool.db.execute('SELECT count(*) FROM direct_attempts').fetchone()[0] == 0
        assert app.state.pool.db.execute('SELECT count(*) FROM attempts').fetchone()[0] == 0
        assert app.state.last_route is None


def test_preview_skips_exhausted_model(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-key')
    monkeypatch.setenv('GEMINI_FREE_TIER_CONFIRMED', 'true')
    monkeypatch.setenv('GROQ_API_KEY', 'test-key')
    monkeypatch.setenv('GROQ_FREE_TIER_CONFIRMED', 'true')
    with TestClient(app) as client:
        app.state.pool.observe('gemini-flash', httpx.Response(429, headers={'retry-after': '60'}))
        data = client.post('/api/route-preview', json={'task': 'implement login'}).json()
        assert data['chain'][0]['reason'] == 'Model cooldown'
        assert data['first_eligible']['provider'] == 'groq'


def test_local_only_dashboard_and_explicit_override(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    with TestClient(app) as client:
        data = client.get('/api/dashboard?refresh=true').json()
        assert data['mode'] == 'local-only'
        assert all(not p['enabled'] for p in data['providers'].values())
        assert all(not c['eligible'] for c in data['chains']['free-code'][:-1])
        assert client.post('/api/route-preview', json={'task': 'architecture'}).json()['model'] == 'local-reasoning'
        assert client.post('/api/route-preview', json={'task': 'architecture', 'model': 'free-code'}).status_code == 403
        assert client.post('/api/route-preview', json={'task': 'architecture', 'model': 'local-general'}).json()['model'] == 'local-general'


@pytest.mark.parametrize('payload', [[], {}, {'task': ''}, {'task': ' '}, {'task': 'x'*10001}, {'task': 'hi', 'model': []}, {'task': 'hi', 'model': 'paid-model'}])
def test_invalid_preview_is_rejected(payload):
    with TestClient(app) as client:
        assert client.post('/api/route-preview', json=payload).status_code == 400


def test_manual_refresh_updates_only_counter_not_inference(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'false')
    monkeypatch.setenv('OPENROUTER_API_KEY', 'test-key')
    with TestClient(app) as client:
        calls = []
        def counter(request):
            calls.append((request.method, request.url.path))
            assert request.url.path.endswith('/key')
            return httpx.Response(200, json={'data': {'free_model_daily_requests': {'used': 13, 'limit': 50, 'remaining': 37}}})
        app.state.pool.client = httpx.AsyncClient(transport=httpx.MockTransport(counter), base_url='https://openrouter.ai/api/v1')
        data = client.get('/api/dashboard?refresh=true').json()
        assert calls == [('GET', '/api/v1/key')]
        assert data['providers']['openrouter']['provider_reported_daily']['remaining'] == 37


def test_last_route_contains_no_prompt(monkeypatch):
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    with TestClient(app) as client:
        app.state.client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={'choices': []})), base_url='http://litellm')
        client.post('/v1/chat/completions', json={'model': 'auto', 'messages': [{'role': 'user', 'content': '/tests private-content-not-to-store'}]})
        data = client.get('/api/dashboard').json()
        assert data['last_route']['rule'] == 'explicit-tests'
        assert data['last_route']['model'] == 'local-code'
        assert 'private-content-not-to-store' not in json.dumps(data)
