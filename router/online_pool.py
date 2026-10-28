"""Independent free-tier providers, followed by the verified OpenRouter free pool."""
import json
import math
import os
import re
import time
from datetime import datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
import yaml

from router.free_pool import CHAT_FIELDS, Pool

# Reviewed against official pricing/free-plan docs on 2026-10-28.
# Account-tier checks are not available through these inference APIs.
PROVIDERS = {
    'gemini': {'url': 'https://generativelanguage.googleapis.com/v1beta/openai/chat/completions',
               'key': 'GEMINI_API_KEY', 'confirm': 'GEMINI_FREE_TIER_CONFIRMED',
               'models': {'gemini-3.8-flash', 'gemini-3.5-flash-lite'}, 'timezone': 'America/Los_Angeles'},
    'groq': {'url': 'https://api.groq.com/openai/v1/chat/completions',
             'key': 'GROQ_API_KEY', 'confirm': 'GROQ_FREE_TIER_CONFIRMED',
             'models': {'openai/gpt-oss-120b', 'openai/gpt-oss-20b'}, 'timezone': 'UTC'},
}
RATE_HEADERS = ('x-ratelimit-limit-requests', 'x-ratelimit-remaining-requests',
                'x-ratelimit-reset-requests', 'x-ratelimit-limit-tokens',
                'x-ratelimit-remaining-tokens', 'x-ratelimit-reset-tokens')


def delay_seconds(value, default=60):
    """Retry-After seconds/date, or Groq reset duration such as 2m59.56s."""
    try:
        delay = float(value)
    except (ValueError, TypeError):
        try:
            delay = parsedate_to_datetime(value).timestamp() - time.time()
        except (ValueError, TypeError, OverflowError):
            matches = re.findall(r'(\d+(?:\.\d+)?)(ms|d|h|m|s)', value or '')
            if not matches or ''.join(n+u for n, u in matches) != (value or ''):
                return default
            delay = sum(float(n)*{'ms': .001, 's': 1, 'm': 60, 'h': 3600, 'd': 86400}[u] for n, u in matches)
    if not 0 <= delay <= 86400:
        return default
    return max(1, delay)


class OnlinePool(Pool):
    def __init__(self, client, key, path=':memory:', config_path='free-pool.yaml', online_path='online-pool.yaml'):
        super().__init__(client, key, path, config_path)
        self.online = yaml.safe_load(Path(online_path).read_text())
        for entry in self.online['models'].values():
            if entry['provider'] not in PROVIDERS or entry['model'] not in PROVIDERS[entry['provider']]['models']:
                raise ValueError('Online model is not on the reviewed free-tier allowlist')
            for field in ('daily_safety_cap', 'minute_safety_cap'):
                if not isinstance(entry[field], int) or entry[field] <= 0:
                    raise ValueError('Positive local safety caps are required')
        identities = [(m['provider'], m['model']) for m in self.online['models'].values()]
        if len(identities) != len(set(identities)):
            raise ValueError('Duplicate online model identities would split quota accounting')
        for task, candidates in self.online['tasks'].items():
            if task not in self.config['tasks'] or not candidates or len(set(candidates)) != len(candidates) or any(c not in self.online['models'] for c in candidates):
                raise ValueError('Invalid online task candidates')
        if set(self.online['tasks']) != set(self.config['tasks']):
            raise ValueError('Online and OpenRouter task aliases must match')
        self.keys = {p: os.getenv(s['key'], '') for p, s in PROVIDERS.items()}
        self.confirmed = {p: os.getenv(s['confirm'], '').lower() == 'true' for p, s in PROVIDERS.items()}
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS direct_attempts (at REAL, alias TEXT, status INTEGER);
          CREATE TABLE IF NOT EXISTS direct_state (scope TEXT PRIMARY KEY, state TEXT);
        ''')

    def saved(self, scope):
        row = self.db.execute('SELECT state FROM direct_state WHERE scope=?', (scope,)).fetchone()
        return json.loads(row[0]) if row else {}

    def save(self, scope, state):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO direct_state VALUES (?,?)', (scope, json.dumps(state)))

    def direct_status(self):
        result = {}
        now = time.time()
        for provider in PROVIDERS:
            enabled = bool(self.keys[provider]) and self.confirmed[provider]
            result[provider] = {'enabled': enabled,
                'disabled_reason': None if enabled else ('missing-api-key' if not self.keys[provider] else 'free-tier-not-confirmed'),
                'free_tier_confirmed_by_operator': self.confirmed[provider],
                'account_tier_verified_by_api': False,
                'provider_cooldown_seconds': max(0, math.ceil(self.saved('provider:'+provider).get('until', 0)-now)),
                'models': {}}
        for alias, entry in self.online['models'].items():
            provider = entry['provider']
            zone = ZoneInfo(PROVIDERS[provider]['timezone'])
            midnight = datetime.now(zone).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
            used = self.db.execute('SELECT count(*) FROM direct_attempts WHERE alias=? AND at>=?', (alias, midnight)).fetchone()[0]
            minute = self.db.execute('SELECT count(*) FROM direct_attempts WHERE alias=? AND at>?', (alias, now-60)).fetchone()[0]
            snapshot = self.saved('model:'+alias)
            result[provider]['models'][alias] = {'id': entry['model'],
                'local_daily_estimate': {'used': used, 'safety_cap': entry['daily_safety_cap'],
                    'remaining': max(0, entry['daily_safety_cap']-used), 'timezone': PROVIDERS[provider]['timezone']},
                'local_minute_estimate': {'used': minute, 'safety_cap': entry['minute_safety_cap'],
                    'remaining': max(0, entry['minute_safety_cap']-minute)},
                'provider_reported_headers': snapshot.get('headers', {}),
                'provider_observed_at': snapshot.get('at'),
                'provider_reported_remaining_requests': snapshot.get('headers', {}).get('x-ratelimit-remaining-requests'),
                'provider_reported_remaining_tokens': snapshot.get('headers', {}).get('x-ratelimit-remaining-tokens'),
                'cooldown_seconds': max(0, math.ceil(snapshot.get('until', 0)-now))}
        return result

    def status(self):
        return {'mode': 'hybrid', 'policy': 'online first; local unit tests; local fallback; no paid API routes',
                'providers': {**self.direct_status(), 'openrouter': super().status()},
                'last_event': self.last_event,
                'quota_note': 'Safety caps are local budgets, not provider entitlements. Provider header observations can become stale; absent quotas are unknown. Other clients are not included in local counts.'}

    def observe(self, alias, response):
        headers = {k: response.headers[k] for k in RATE_HEADERS if k in response.headers}
        snapshot = {'headers': headers, 'at': time.time()}
        wait = 0
        for dimension in ('requests', 'tokens'):
            if headers.get('x-ratelimit-remaining-'+dimension) == '0':
                wait = max(wait, delay_seconds(headers.get('x-ratelimit-reset-'+dimension)))
        if response.status_code == 429:
            wait = max(wait, delay_seconds(response.headers.get('retry-after')))
        if wait:
            snapshot['until'] = time.time()+wait
        self.save('model:'+alias, snapshot)

    async def send(self, task, payload):
        if set(payload)-CHAT_FIELDS:
            return None, 'unsupported-hosted-parameters'
        if any(t.get('type') != 'function' for t in payload.get('tools', [])):
            return None, 'hosted-builtin-tools-disabled'
        if any(isinstance(m.get('content'), list) and any(not isinstance(p, dict) or p.get('type') != 'text' for p in m['content']) for m in payload['messages']):
            return None, 'hosted-media-disabled'
        async with self.lock:
            for alias in self.online['tasks'][task]:
                entry = self.online['models'][alias]
                provider = entry['provider']
                info = self.direct_status()[provider]
                model_info = info['models'][alias]
                if (not info['enabled'] or info['provider_cooldown_seconds'] > 0 or
                    model_info['cooldown_seconds'] > 0 or
                    model_info['local_daily_estimate']['remaining'] <= 0 or
                    model_info['local_minute_estimate']['remaining'] <= 0):
                    continue
                body = dict(payload, model=entry['model'])
                with self.db:
                    row = self.db.execute('INSERT INTO direct_attempts VALUES (?,?,NULL)', (time.time(), alias)).lastrowid
                try:
                    response = await self.client.send(self.client.build_request('POST', PROVIDERS[provider]['url'],
                        json=body, headers={'Authorization': f'Bearer {self.keys[provider]}'}), stream=True)
                except httpx.RequestError:
                    self.last_event = {'provider': provider, 'model': entry['model'], 'reason': 'network-error'}
                    continue
                with self.db:
                    self.db.execute('UPDATE direct_attempts SET status=? WHERE rowid=?', (response.status_code, row))
                self.observe(alias, response)
                self.last_event = {'provider': provider, 'model': entry['model'], 'status': response.status_code}
                if response.status_code < 400:
                    return response, provider+'/'+entry['model']
                await response.aclose()
                if response.status_code in (401, 402, 403):
                    self.save('provider:'+provider, {'until': time.time()+300, 'status': response.status_code})
                elif response.status_code not in (404, 408, 429, 500, 502, 503, 504):
                    return None, provider+'-request-rejected'
        # This independent pool retains its own shared daily cap and pricing checks.
        response, selected = await super().send(task, payload)
        if response is not None:
            return response, 'openrouter/'+selected
        return None, 'online-pools-unavailable-or-exhausted'
