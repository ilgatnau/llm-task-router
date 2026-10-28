"""Free-only OpenRouter transport, persistent accounting, and bounded failover."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path

import httpx
import yaml

# Reject addons and provider overrides rather than trusting arbitrary request extras.
CHAT_FIELDS = {'model', 'messages', 'tools', 'tool_choice', 'parallel_tool_calls',
               'stream', 'max_tokens', 'temperature', 'top_p', 'stop', 'seed',
               'presence_penalty', 'frequency_penalty', 'response_format'}


def free_entry(entry):
    try:
        prices = entry.get('pricing', {})
        return (entry['id'].endswith(':free') and
                {'prompt', 'completion'} <= prices.keys() and
                all(Decimal(str(v)) == 0 for v in prices.values()) and
                'tools' in entry.get('supported_parameters', []))
    except (KeyError, InvalidOperation, TypeError, ValueError):
        return False


class Pool:
    def __init__(self, client, key, path=':memory:', config_path='free-pool.yaml'):
        self.client, self.key = client, key
        self.config = yaml.safe_load(Path(config_path).read_text())
        self.models = self.config['models']
        for model in self.models.values():
            if not isinstance(model, str) or not model.endswith(':free') or ':' in model[:-5]:
                raise ValueError('Only exact :free IDs are allowed')
        for candidates in self.config['tasks'].values():
            if not candidates or any(c not in self.models for c in candidates):
                raise ValueError('Invalid free pool candidates')
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS attempts
            (at REAL, model TEXT, status INTEGER);
          CREATE TABLE IF NOT EXISTS cooldowns
            (model TEXT PRIMARY KEY, until REAL);
        ''')
        self.lock = asyncio.Lock()
        self.catalog = {}
        self.catalog_at = 0
        self.remote = None
        self.remote_at = None
        self.quota_error = None
        self.last_event = None
        self.shared_until = 0

    async def refresh_quota(self):
        self.remote = None
        self.quota_error = None
        if not self.key:
            self.quota_error = 'OPENROUTER_API_KEY not configured'
            return
        try:
            r = await self.client.get('/key', headers={'Authorization': f'Bearer {self.key}'})
            r.raise_for_status()
            data = r.json()['data'].get('free_model_daily_requests')
            if isinstance(data, dict) and all(isinstance(data.get(k), (int, float)) and not isinstance(data[k], bool) and data[k] >= 0 for k in ('used', 'limit', 'remaining')):
                self.remote = {k: data[k] for k in ('used', 'limit', 'remaining')}
                # Preserve provider reset information only if actually supplied.
                self.remote.update({k: data[k] for k in ('reset', 'reset_at') if k in data})
                self.remote_at = time.time()
            else:
                self.quota_error = 'Provider did not report a free-request counter; local estimate only'
        except (httpx.HTTPError, ValueError, KeyError, TypeError):
            self.quota_error = 'Quota API unavailable; local estimate only'

    def status(self):
        now = time.time()
        midnight = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        used = self.db.execute('SELECT count(*) FROM attempts WHERE at>=?', (midnight,)).fetchone()[0]
        minute = self.db.execute('SELECT count(*) FROM attempts WHERE at>?', (now-60,)).fetchone()[0]
        local_remaining = max(0, 50-used)
        remaining = min(local_remaining, self.remote['remaining']) if self.remote is not None else local_remaining
        models = {}
        for alias, model in self.models.items():
            n = self.db.execute('SELECT count(*) FROM attempts WHERE model=? AND at>=?', (model, midnight)).fetchone()[0]
            row = self.db.execute('SELECT until FROM cooldowns WHERE model=?', (model,)).fetchone()
            models[alias] = {'id': model, 'attempts_today': n, 'cooldown_seconds': max(0, int(row[0]-now)) if row else 0,
                             'remaining_model_quota': None}
        return {'policy': 'free-only; no paid fallback', 'provider': 'openrouter',
                'provider_reported_daily': self.remote,
                'provider_observed_at': self.remote_at if self.remote is not None else None,
                'quota_note': self.quota_error,
                'local_daily_estimate': {'limit': 50, 'used': used, 'remaining': local_remaining, 'window': 'UTC day'},
                'local_minute_estimate': {'limit': 20, 'used': minute, 'remaining': max(0, 20-minute)},
                'conservative_remaining_daily': remaining,
                'shared_cooldown_seconds': max(0, int(self.shared_until-now)),
                'models': models, 'last_event': self.last_event,
                'scope_note': 'Local counts include failed attempts and persist in Compose. Other clients are visible only through the provider counter. Per-model remaining quotas are unknown.'}

    async def validate_catalog(self):
        if time.time()-self.catalog_at < 60:
            return
        r = await self.client.get('/models')
        r.raise_for_status()
        self.catalog = {m['id']: m for m in r.json()['data']}
        self.catalog_at = time.time()

    def record(self, model):
        with self.db:
            cursor = self.db.execute('INSERT INTO attempts VALUES (?,?,NULL)', (time.time(), model))
        return cursor.lastrowid

    def outcome(self, row, status):
        with self.db:
            self.db.execute('UPDATE attempts SET status=? WHERE rowid=?', (status, row))

    async def send(self, task, payload):
        """Return (response, model), or (None, reason) to continue locally."""
        if not self.key:
            return None, 'missing-free-api-key'
        if set(payload)-CHAT_FIELDS:
            return None, 'unsupported-hosted-parameters'
        if any(t.get('type') != 'function' for t in payload.get('tools', [])):
            return None, 'hosted-builtin-tools-disabled'
        # Prevent URL/file processing plugins and unpriced media inputs.
        for message in payload['messages']:
            content = message.get('content')
            if isinstance(content, list) and any(not isinstance(p, dict) or p.get('type') != 'text' for p in content):
                return None, 'hosted-media-disabled'
        async with self.lock:
            try:
                await self.validate_catalog()
            except (httpx.HTTPError, ValueError, KeyError, TypeError):
                return None, 'free-pricing-unverified'
            for alias in self.config['tasks'][task]:
                # Refresh for each attempt. All models share this provider-wide allowance.
                await self.refresh_quota()
                state = Pool.status(self)
                if state['conservative_remaining_daily'] <= 0:
                    return None, 'shared-daily-quota-exhausted'
                if state['local_minute_estimate']['remaining'] <= 0 or self.shared_until > time.time():
                    return None, 'shared-minute-limit'
                model = self.models[alias]
                row = self.db.execute('SELECT until FROM cooldowns WHERE model=?', (model,)).fetchone()
                if row and row[0] > time.time():
                    continue
                if not free_entry(self.catalog.get(model, {})):
                    self.last_event = {'model': model, 'reason': 'not-verified-zero-price-with-tools'}
                    continue
                body = dict(payload, model=model)
                # Cap every provider token price at zero; do not enable plugins or model fallbacks.
                body['provider'] = {'max_price': {'prompt': 0, 'completion': 0}, 'require_parameters': True}
                attempt = self.record(model)
                if self.remote is not None:
                    # Reserve pessimistically until the next provider refresh.
                    self.remote['remaining'] = max(0, self.remote['remaining']-1)
                    self.remote['used'] += 1
                try:
                    response = await self.client.send(self.client.build_request('POST', '/chat/completions',
                        json=body, headers={'Authorization': f'Bearer {self.key}'}), stream=True)
                except httpx.RequestError:
                    self.outcome(attempt, 0)
                    self.last_event = {'model': model, 'reason': 'network-error'}
                    continue
                self.outcome(attempt, response.status_code)
                if response.status_code < 400:
                    self.last_event = {'model': model, 'reason': 'selected'}
                    return response, model
                # Read before closing, but never return upstream error text (can contain sensitive data).
                await response.aread()
                await response.aclose()
                self.last_event = {'model': model, 'reason': 'upstream-error', 'status': response.status_code}
                if response.status_code in (401, 402, 403):
                    return None, 'free-provider-unavailable'
                if response.status_code not in (404, 408, 429, 500, 502, 503, 504):
                    return None, 'free-request-rejected'
                if response.status_code == 429:
                    # OpenRouter platform-limit headers identify a shared limit, not a model's capacity.
                    if 'x-ratelimit-limit' in response.headers:
                        try:
                            delay = min(86400, max(1, float(response.headers.get('retry-after', '60'))))
                        except ValueError:
                            delay = 60
                        self.shared_until = time.time()+delay
                        return None, 'shared-provider-rate-limit'
                    try:
                        delay = min(86400, max(1, float(response.headers.get('retry-after', '60'))))
                    except ValueError:
                        delay = 60
                    with self.db:
                        self.db.execute('INSERT OR REPLACE INTO cooldowns VALUES (?,?)', (model, time.time()+delay))
            return None, 'free-pool-exhausted'
