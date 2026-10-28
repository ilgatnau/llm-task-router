"""Read-only dashboard snapshots. Never sends inference or returns credentials."""
import time


def fallback_model(alias):
    return 'local-reasoning' if alias == 'free-architecture' else 'local-code'


def snapshot(local_only, pool, rules, local_models, online_config, openrouter_config, last_route):
    now = time.time()
    quota = pool.status() if pool is not None else {'mode': 'local-only', 'providers': {}}
    providers = quota['providers']
    direct = providers.copy()
    chains = {}
    for task, candidates in online_config['tasks'].items():
        chain = []
        for alias in candidates:
            model = online_config['models'][alias]
            provider = model['provider']
            state = direct.get(provider, {})
            status = state.get('models', {}).get(alias, {})
            reason = None
            if local_only:
                reason = 'Local-only mode'
            elif not state.get('enabled'):
                reason = state.get('disabled_reason', 'Not configured')
            elif state.get('provider_cooldown_seconds', 0) > 0:
                reason = 'Provider cooldown'
            elif status.get('cooldown_seconds', 0) > 0:
                reason = 'Model cooldown'
            elif status.get('local_daily_estimate', {}).get('remaining') == 0:
                reason = 'Daily safety budget exhausted'
            elif status.get('local_minute_estimate', {}).get('remaining') == 0:
                reason = 'Minute safety budget exhausted'
            chain.append({'provider': provider, 'alias': alias, 'model': model['model'],
                          'kind': 'online', 'eligible': reason is None, 'reason': reason})
        state = providers.get('openrouter', {})
        for alias in openrouter_config['tasks'][task]:
            model = openrouter_config['models'][alias]
            status = state.get('models', {}).get(alias, {})
            reason = None
            if local_only:
                reason = 'Local-only mode'
            elif not pool.key:
                reason = 'missing-api-key'
            elif state.get('conservative_remaining_daily') == 0:
                reason = 'Shared daily quota exhausted'
            elif state.get('shared_cooldown_seconds', 0) > 0:
                reason = 'Shared provider cooldown'
            elif state.get('local_minute_estimate', {}).get('remaining') == 0:
                reason = 'Minute safety budget exhausted'
            elif status.get('cooldown_seconds', 0) > 0:
                reason = 'Model cooldown'
            chain.append({'provider': 'openrouter', 'alias': alias, 'model': model,
                          'kind': 'online', 'eligible': reason is None, 'reason': reason,
                          'note': 'Zero pricing and tool support are verified before use; availability is not probed here.'})
        local = fallback_model(task)
        chain.append({'provider': 'ollama', 'alias': local, 'model': local_models[local],
                      'kind': 'local', 'eligible': True, 'reason': None,
                      'note': 'Local inference readiness is not checked by the dashboard.'})
        chains[task] = chain
    provider_cards = {}
    for provider in ('gemini', 'groq', 'openrouter'):
        data = dict(providers.get(provider, {}))
        if local_only:
            data.update(enabled=False, disabled_reason='Local-only mode')
        elif provider == 'openrouter':
            data.update(enabled=bool(pool.key), disabled_reason=None if pool.key else 'missing-api-key')
        provider_cards[provider] = data
    return {'mode': 'local-only' if local_only else 'hybrid', 'observed_at': now,
            'rules': rules, 'chains': chains, 'providers': provider_cards,
            'local_models': local_models, 'last_route': last_route,
            'note': 'Eligible means configured and not blocked by known limits, not a live availability check. Unknown quota is not unlimited. Local budgets exclude other applications.'}
