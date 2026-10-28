import pytest

import dev


def test_environment_precedence_and_literal_secrets(tmp_path):
    (tmp_path / '.env').write_text('GEMINI_API_KEY=from-shared\nLOCAL_ONLY=true\nOLLAMA_API_BASE=http://ollama:11434\n')
    (tmp_path / '.env.local').write_text('GEMINI_API_KEY=literal-${TOKEN}\nOLLAMA_API_BASE=http://127.0.0.1:11434\n')
    env = dev.development_env(root=tmp_path, environ={})
    assert env['GEMINI_API_KEY'] == 'literal-${TOKEN}'
    assert env['LOCAL_ONLY'] == 'true'
    assert env['OLLAMA_API_BASE'] == 'http://127.0.0.1:11434'
    assert dev.development_env(root=tmp_path, environ={'GEMINI_API_KEY': 'from-shell'})['GEMINI_API_KEY'] == 'from-shell'


def test_custom_override_file_does_not_load_dotenv_local(tmp_path):
    (tmp_path / '.env').write_text('LOCAL_ONLY=true\n')
    (tmp_path / '.env.local').write_text('LOCAL_ONLY=false\n')
    custom = tmp_path / 'custom.env'
    custom.write_text('OLLAMA_API_BASE=http://wsl-host:11434\n')
    env = dev.development_env(root=tmp_path, overrides=custom, environ={})
    assert env['LOCAL_ONLY'] == 'true'
    assert env['OLLAMA_API_BASE'] == 'http://wsl-host:11434'


def test_proxy_startup_failure_stops_owned_process(monkeypatch):
    process = object()
    stopped = []
    monkeypatch.setattr(dev, 'development_env', lambda **_: dict(dev.DEFAULTS, QUOTA_DB=':memory:'))
    monkeypatch.setattr(dev, 'require_free_port', lambda *_: None)
    monkeypatch.setattr(dev.shutil, 'which', lambda *_, **__: '/venv/bin/litellm')
    monkeypatch.setattr(dev, 'launch', lambda *_: process)
    def failure(*_):
        raise RuntimeError('proxy failed')
    monkeypatch.setattr(dev, 'wait_for_proxy', failure)
    monkeypatch.setattr(dev, 'stop', lambda p: stopped.append(p))
    assert dev.main(['--no-reload']) == 1
    assert stopped == [None, process]


def test_external_proxy_is_not_started_or_stopped(monkeypatch):
    class Router:
        returncode = 0
        def poll(self):
            return 0
    router = Router()
    started, stopped = [], []
    monkeypatch.setattr(dev, 'development_env', lambda **_: dict(dev.DEFAULTS, QUOTA_DB=':memory:', LITELLM_URL='http://127.0.0.1:4999'))
    monkeypatch.setattr(dev, 'require_free_port', lambda *_: None)
    def launch(command, env):
        started.append((command, env))
        return router
    monkeypatch.setattr(dev, 'launch', launch)
    monkeypatch.setattr(dev, 'stop', lambda p: stopped.append(p))
    assert dev.main(['--no-proxy', '--no-reload']) == 0
    assert len(started) == 1
    assert started[0][1]['LITELLM_URL'] == 'http://127.0.0.1:4999'
    assert '--reload' not in started[0][0]
    assert stopped == [router, None]


def test_conflicting_ports_are_rejected_before_startup():
    with pytest.raises(SystemExit):
        dev.main(['--port', '4000', '--proxy-port', '4000'])


def test_managed_proxy_uses_same_ephemeral_key_for_router(monkeypatch):
    class Process:
        returncode = 0
        def poll(self):
            return 0
    proxy, router = Process(), Process()
    started = []
    monkeypatch.setattr(dev, 'development_env', lambda **_: dict(dev.DEFAULTS, QUOTA_DB=':memory:'))
    monkeypatch.setattr(dev, 'require_free_port', lambda *_: None)
    monkeypatch.setattr(dev.shutil, 'which', lambda *_, **__: '/venv/bin/litellm')
    monkeypatch.setattr(dev, 'wait_for_proxy', lambda *_: None)
    def launch(command, env):
        started.append(dict(env))
        return proxy if len(started) == 1 else router
    monkeypatch.setattr(dev, 'launch', launch)
    monkeypatch.setattr(dev, 'stop', lambda _: None)
    assert dev.main(['--no-reload']) == 0
    assert started[0]['LITELLM_MASTER_KEY'].startswith('sk-')
    assert started[0]['LITELLM_MASTER_KEY'] == started[1]['LITELLM_API_KEY']


def test_proxy_auth_is_not_exposed_in_dashboard(monkeypatch):
    from fastapi.testclient import TestClient
    from router.app import app
    monkeypatch.setenv('LITELLM_API_KEY', 'proxy-key-not-to-expose')
    monkeypatch.setenv('LOCAL_ONLY', 'true')
    with TestClient(app) as client:
        assert app.state.client.headers['Authorization'] == 'Bearer proxy-key-not-to-expose'
        assert 'proxy-key-not-to-expose' not in client.get('/api/dashboard').text


def test_real_port_conflict_is_detected():
    import socket
    with socket.socket() as occupied:
        occupied.bind(('127.0.0.1', 0))
        occupied.listen()
        with pytest.raises(RuntimeError, match='already in use'):
            dev.require_free_port('127.0.0.1', occupied.getsockname()[1])


def test_local_model_flag_overrides_existing_large_models(monkeypatch):
    class Router:
        returncode = 0
        def poll(self): return 0
    captured = []
    monkeypatch.setattr(dev, 'development_env', lambda **_: dict(dev.DEFAULTS, QUOTA_DB=':memory:', LOCAL_CODE_MODEL='ollama_chat/old:70b'))
    monkeypatch.setattr(dev, 'require_free_port', lambda *_: None)
    def launch(command, env):
        captured.append(dict(env))
        return Router()
    monkeypatch.setattr(dev, 'launch', launch)
    monkeypatch.setattr(dev, 'stop', lambda _: None)
    assert dev.main(['--no-proxy', '--no-reload', '--local-model', 'llama3.2:3b']) == 0
    for name in ('LOCAL_CODE_MODEL', 'LOCAL_GENERAL_MODEL', 'LOCAL_REASONING_MODEL'):
        assert captured[0][name] == 'ollama_chat/llama3.2:3b'
    assert captured[0]['LOCAL_ONLY'] == dev.DEFAULTS['LOCAL_ONLY']


def test_reasoning_model_flag_overrides_only_reasoning_alias(monkeypatch):
    class Router:
        returncode = 0
        def poll(self): return 0
    captured = []
    monkeypatch.setattr(dev, 'development_env', lambda **_: dict(dev.DEFAULTS, QUOTA_DB=':memory:'))
    monkeypatch.setattr(dev, 'require_free_port', lambda *_: None)
    def launch(command, env):
        captured.append(dict(env))
        return Router()
    monkeypatch.setattr(dev, 'launch', launch)
    monkeypatch.setattr(dev, 'stop', lambda _: None)
    assert dev.main(['--no-proxy', '--no-reload', '--local-model', 'llama3.2:3b', '--reasoning-model', 'qwen3:1.7b']) == 0
    assert captured[0]['LOCAL_CODE_MODEL'] == 'ollama_chat/llama3.2:3b'
    assert captured[0]['LOCAL_GENERAL_MODEL'] == 'ollama_chat/llama3.2:3b'
    assert captured[0]['LOCAL_REASONING_MODEL'] == 'ollama_chat/qwen3:1.7b'
