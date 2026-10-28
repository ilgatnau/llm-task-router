#!/usr/bin/env python3
"""Run the router/UI and local LiteLLM without Docker (Python 3.12+)."""
import argparse
import os
from pathlib import Path
import shutil
import signal
import secrets
import socket
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import Request, urlopen

ROOT = Path(__file__).resolve().parent
DEFAULTS = {
    'OLLAMA_API_BASE': 'http://127.0.0.1:11434',
    'LOCAL_CODE_MODEL': 'ollama_chat/llama3.2:3b',
    'LOCAL_GENERAL_MODEL': 'ollama_chat/llama3.2:3b',
    'LOCAL_REASONING_MODEL': 'ollama_chat/llama3.2:3b',
    'LOCAL_ONLY': 'false',
    'QUOTA_DB': str(ROOT / '.data' / 'quota.dev.sqlite3'),
}


def development_env(root=ROOT, overrides=None, environ=None):
    from dotenv import dotenv_values
    if overrides is not None and not overrides.is_file():
        raise RuntimeError(f'Override file does not exist: {overrides}')
    env = dict(DEFAULTS)
    for path in (root / '.env', overrides or root / '.env.local'):
        env.update({k: v for k, v in dotenv_values(path, interpolate=False).items() if v is not None})
    env.update(os.environ if environ is None else environ)
    return env


def require_free_port(host, port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if os.name != 'nt':
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind((host, port))
        except OSError as exc:
            raise RuntimeError(f'{host}:{port} is already in use or unavailable; choose another port.') from exc


def launch(command, env):
    options = {'start_new_session': True} if os.name != 'nt' else {'creationflags': subprocess.CREATE_NEW_PROCESS_GROUP}
    return subprocess.Popen(command, cwd=ROOT, env=env, **options)


def stop(process):
    if process is None or process.poll() is not None:
        return
    try:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGTERM)
        else:
            process.send_signal(signal.CTRL_BREAK_EVENT)
    except ProcessLookupError:
        return
    try:
        process.wait(timeout=8)
    except subprocess.TimeoutExpired:
        if os.name != 'nt':
            os.killpg(process.pid, signal.SIGKILL)
        else:
            process.kill()
        process.wait(timeout=5)


def wait_for_proxy(process, port, key, timeout=45):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError('LiteLLM stopped during startup; inspect its output above.')
        try:
            request = Request(f'http://127.0.0.1:{port}/health/liveliness', headers={'Authorization': f'Bearer {key}'})
            with urlopen(request, timeout=1) as response:
                if response.status == 200:
                    return
        except (URLError, TimeoutError, OSError):
            pass
        time.sleep(.25)
    raise RuntimeError('LiteLLM did not become ready within 45 seconds; inspect its output above.')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--port', type=int, default=8000, help='Router and UI port (default: 8000)')
    parser.add_argument('--proxy-port', type=int, default=4000, help='Local LiteLLM port (default: 4000)')
    parser.add_argument('--env-file', type=Path, help='Override file instead of .env.local; .env is still loaded first')
    parser.add_argument('--local-model', help='Override all local aliases with an Ollama tag, e.g. llama3.2:3b')
    parser.add_argument('--no-reload', action='store_true', help='Disable automatic router reload')
    parser.add_argument('--no-proxy', action='store_true', help='Use an existing LiteLLM at LITELLM_URL (default: localhost:4000)')
    args = parser.parse_args(argv)
    if not all(1 <= n <= 65535 for n in (args.port, args.proxy_port)):
        parser.error('Ports must be between 1 and 65535')
    if not args.no_proxy and args.port == args.proxy_port:
        parser.error('Router and LiteLLM need different ports')
    proxy = router = None
    try:
        env = development_env(overrides=args.env_file)
        if args.local_model:
            model = args.local_model if args.local_model.startswith('ollama_chat/') else 'ollama_chat/' + args.local_model
            for name in ('LOCAL_CODE_MODEL', 'LOCAL_GENERAL_MODEL', 'LOCAL_REASONING_MODEL'):
                env[name] = model
        require_free_port('127.0.0.1', args.port)
        proxy_executable = None
        if not args.no_proxy:
            proxy_executable = shutil.which('litellm', path=str(Path(sys.executable).parent) + os.pathsep + env.get('PATH', ''))
            if not proxy_executable:
                raise RuntimeError('LiteLLM is not installed. Run: python -m pip install -r requirements-dev.txt')
            require_free_port('127.0.0.1', args.proxy_port)
            env['LITELLM_URL'] = f'http://127.0.0.1:{args.proxy_port}'
            env['LITELLM_MASTER_KEY'] = env.get('LITELLM_MASTER_KEY') or 'sk-' + secrets.token_urlsafe(32)
            env['LITELLM_API_KEY'] = env['LITELLM_MASTER_KEY']
        else:
            env.setdefault('LITELLM_URL', f'http://127.0.0.1:{args.proxy_port}')
        if env['QUOTA_DB'] != ':memory:':
            path = Path(env['QUOTA_DB']).expanduser()
            if not path.is_absolute():
                path = ROOT / path
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            env['QUOTA_DB'] = str(path)
        if proxy_executable:
            print(f'Starting LiteLLM on http://127.0.0.1:{args.proxy_port} …', flush=True)
            proxy = launch([proxy_executable, '--config', str(ROOT / 'litellm.yaml'), '--host', '127.0.0.1', '--port', str(args.proxy_port)], env)
            wait_for_proxy(proxy, args.proxy_port, env['LITELLM_MASTER_KEY'])
        command = [sys.executable, '-m', 'uvicorn', 'router.app:app', '--host', '127.0.0.1', '--port', str(args.port)]
        if not args.no_reload:
            command += ['--reload', '--reload-dir', str(ROOT)]
            for pattern in ('*.yaml', '*.html', '*.css', '*.js'):
                command += ['--reload-include', pattern]
        print(f'Router and dashboard: http://127.0.0.1:{args.port}/ (Ctrl+C stops development services)', flush=True)
        router = launch(command, env)
        while router.poll() is None:
            if proxy is not None and proxy.poll() is not None:
                raise RuntimeError('LiteLLM exited; stopping the development router.')
            time.sleep(.25)
        return router.returncode
    except KeyboardInterrupt:
        return 0
    except (RuntimeError, ImportError, OSError) as exc:
        print(f'Development startup failed: {exc}', file=sys.stderr)
        return 1
    finally:
        stop(router)
        stop(proxy)


def interrupted(*_):
    raise KeyboardInterrupt


if __name__ == '__main__':
    # A terminated launcher must clean up its child process groups as on Ctrl+C.
    signal.signal(signal.SIGTERM, interrupted)
    raise SystemExit(main())
