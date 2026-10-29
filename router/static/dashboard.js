'use strict';
const $ = id => document.getElementById(id);
const names = {gemini: 'Google Gemini', groq: 'Groq', openrouter: 'OpenRouter', ollama: 'Ollama'};
const poolNames = {'free-code': 'Implementation', 'free-architecture': 'Architecture', 'free-cicd': 'CI/CD', 'free-tests': 'Online tests'};
let state = null, selectedPool = 'free-code', configSignature = '', loading = false, pollingActivity = false, activitySignature = '';
function currentRoute() {return state?.current_route || state?.latest_completed_route || state?.last_route;}
function elapsed(ms) {return ms == null ? '—' : (ms / 1000).toFixed(1) + 's';}
function el(tag, cls, text) { const node = document.createElement(tag); if (cls) node.className = cls; if (text !== undefined) node.textContent = text; return node; }
function badge(text, cls = 'neutral') { return el('span', 'badge ' + cls, text); }
function human(value) { return String(value || '').replace(/[-_]/g, ' '); }
function timestamp(value) { return value ? new Date(value * 1000).toLocaleString() : 'No provider observation yet'; }
function number(value) { return value === null || value === undefined ? 'Unknown' : String(value); }
function prettyModel(value) { return String(value).replace(/^ollama_chat\//, '').replace(/:free$/, ''); }
function progress(used, max) { const node = el('progress', 'quota-progress'); node.max = Math.max(1, max); node.value = Math.min(max, Math.max(0, used)); node.setAttribute('aria-label', `${used} of ${max} local request safety budget used`); return node; }
function modelLine(title, status) { const row = el('div', 'provider-model'); const top = el('div', 'model-title'); top.append(el('strong', '', prettyModel(title))); if (status?.cooldown_seconds > 0) top.append(badge(`${status.cooldown_seconds}s cooldown`, 'amber')); row.append(top); return row; }
function renderProviders() {
  const cards = $('provider-cards'); cards.replaceChildren();
  Object.entries(state.providers).forEach(([provider, data]) => {
    const card = el('article', 'provider-card'); const top = el('div', 'provider-top');
    const name = el('div', 'provider-name'); name.append(el('span', 'provider-icon', provider === 'gemini' ? 'G' : provider === 'groq' ? 'g' : '⇄'), el('span', '', names[provider]));
    const cooldown = data.provider_cooldown_seconds || data.shared_cooldown_seconds || 0;
    top.append(name, badge(!data.enabled ? 'Not enabled' : cooldown > 0 ? 'Cooling down' : 'Configured', !data.enabled ? 'neutral' : cooldown > 0 ? 'amber' : 'green')); card.append(top);
    const reported = data.provider_reported_daily;
    const hasHeaders = Object.values(data.models || {}).some(m => m.provider_observed_at);
    card.append(el('div', 'provider-headline', provider === 'openrouter' && reported ? `${reported.remaining} requests left` : hasHeaders ? 'Quota observations' : 'Quota not reported'));
    card.append(el('div', 'provider-help', !data.enabled ? human(data.disabled_reason) + '. No inference is sent to this provider.' : provider === 'openrouter' ? 'Daily quota is shared by all free models in this account.' : 'Provider values are last observations. Local safety budgets are estimates.'));
    if (provider === 'openrouter') {
      const local = data.local_daily_estimate;
      if (reported) { card.append(el('p', 'quota-line', `Provider reported: ${reported.used} used / ${reported.limit} daily limit`)); card.append(el('p', 'budget-line', timestamp(data.provider_observed_at))); }
      if (local) { card.append(el('p', 'budget-line', `Local daily budget: ${local.remaining} of ${local.limit} remaining`), progress(local.used, local.limit)); }
      if (data.quota_note && data.enabled) card.append(el('p', 'budget-line', data.quota_note));
      Object.entries(data.models || {}).forEach(([alias, model]) => {
        const row = modelLine(model.id, model);
        row.append(el('p', 'budget-line', `${model.attempts_today} local attempts today · Model-specific quota unknown`)); card.append(row);
      });
      if (!Object.keys(data.models || {}).length) card.append(el('p', 'budget-line', 'Model pool is disabled in local-only mode.'));
    } else {
      Object.entries(data.models || {}).forEach(([alias, model]) => {
        const row = modelLine(model.id, model);
        row.append(el('p', 'quota-line', `Reported: ${number(model.provider_reported_remaining_requests)} requests · ${number(model.provider_reported_remaining_tokens)} tokens`));
        const local = model.local_daily_estimate, minute = model.local_minute_estimate;
        if (local) row.append(el('p', 'budget-line', `Local daily budget: ${local.remaining} of ${local.safety_cap} remaining`), progress(local.used, local.safety_cap));
        if (minute) row.append(el('p', 'budget-line', `Local minute budget: ${minute.remaining} of ${minute.safety_cap} remaining`));
        row.append(el('p', 'budget-line', timestamp(model.provider_observed_at)));
        const headers = model.provider_reported_headers || {};
        if (headers['x-ratelimit-reset-requests'] || headers['x-ratelimit-reset-tokens']) row.append(el('p', 'budget-line', `Provider reset at observation: requests ${number(headers['x-ratelimit-reset-requests'])}, tokens ${number(headers['x-ratelimit-reset-tokens'])}`));
        card.append(row);
      });
      if (!Object.keys(data.models || {}).length) card.append(el('p', 'budget-line', 'Provider quota is not queried in local-only mode.'));
      if (data.enabled) card.append(el('p', 'budget-line', 'Free tier confirmed by you; account billing tier cannot be verified by the router.'));
    }
    cards.append(card);
  });
}
function renderChain() {
  const list = $('chain'); list.replaceChildren();
  (state.chains[selectedPool] || []).forEach(item => {
    const row = el('li', `${item.kind} ${item.eligible ? '' : 'skip'}`); const body = el('div', 'chain-body');
    body.append(el('strong', '', prettyModel(item.model)), el('small', '', `${names[item.provider]} · ${item.reason ? human(item.reason) : item.kind === 'local' ? 'Final fallback · readiness not checked' : 'Eligible to try · availability not checked'}`));
    row.append(body, badge(item.kind === 'local' ? 'Local' : item.eligible ? 'Eligible to try' : 'Skipped', item.kind === 'local' ? 'green' : item.eligible ? 'purple' : 'neutral')); list.append(row);
  });
  $('pool-tabs').querySelectorAll('button').forEach(button => button.setAttribute('aria-pressed', String(button.dataset.pool === selectedPool)));
}
function renderRules() {
  if (!state) return;
  const filter = $('rule-search').value.toLowerCase(); const rows = $('rule-rows'); rows.replaceChildren();
  state.rules.rules.forEach((rule, index) => {
    if (![rule.name, rule.pattern, rule.model].some(s => s.toLowerCase().includes(filter))) return;
    const row = el('tr', currentRoute()?.rule === rule.name ? 'rule-highlight' : ''); row.append(el('td', '', String(index + 1).padStart(2, '0')));
    const name = el('td'), details = el('details'); details.append(el('summary', '', human(rule.name)), el('pre', '', rule.pattern)); name.append(details);
    const target = el('td'); target.append(el('code', '', rule.model));
    const behavior = el('td'); const online = rule.model.startsWith('free-');
    behavior.append(badge(online && state.mode !== 'local-only' ? 'Online → local fallback' : 'Local inference', online && state.mode !== 'local-only' ? 'purple' : 'green'));
    if (online && state.mode === 'local-only') behavior.append(el('p', 'budget-line', 'Mapped locally by local-only mode'));
    row.append(name, target, behavior); rows.append(row);
  });
  if (!rows.children.length) { const row = el('tr'), cell = el('td', 'muted', 'No rules match this filter.'); cell.colSpan = 4; row.append(cell); rows.append(row); }
  $('default-rule').textContent = `No match → ${state.rules.default}${state.mode === 'local-only' ? ' (mapped locally in local-only mode)' : ' → local fallback if needed'}. Explicit model selection always overrides keyword rules.`;
}
function renderConfig() {
  const signature = JSON.stringify([state.rules, state.local_models, Object.keys(state.chains), state.mode]);
  if (signature === configSignature) return;
  configSignature = signature;
  const tabs = $('pool-tabs'); tabs.replaceChildren();
  Object.keys(state.chains).forEach(pool => { const button = el('button', '', poolNames[pool] || pool); button.type = 'button'; button.dataset.pool = pool; button.addEventListener('click', () => {selectedPool = pool; renderChain();}); tabs.append(button); });
  const select = $('model'), previous = select.value; select.replaceChildren();
  ['auto', ...Object.keys(state.local_models), ...(state.mode === 'local-only' ? [] : Object.keys(state.chains))].forEach(alias => { const option = el('option', '', alias === 'auto' ? 'Automatic routing' : alias); option.value = alias; select.append(option); });
  if ([...select.options].some(o => o.value === previous)) select.value = previous;
  const local = $('local-models'); local.replaceChildren();
  Object.entries(state.local_models).forEach(([alias, model]) => {const row = el('div', 'local-row'); row.append(el('span', '', alias), el('code', '', prettyModel(model))); local.append(row);});
  renderRules();
}
function renderLastRoute() {
  const route = currentRoute(), box = $('last-route'), summary = $('activity-summary'), path = $('request-path'); box.replaceChildren(); summary.replaceChildren(); path.replaceChildren();
  if (!route) {
    summary.append(el('p', 'activity-empty', 'Waiting for a routed request. Previews do not create request logs.'));
    box.textContent = 'No requests observed since this router started.';
    $('activity-status').textContent = 'No requests yet'; $('activity-status').className = 'badge neutral';
    return;
  }
  (route.failover_path || []).forEach((step, index) => {
    const row = el('li', 'request-step ' + step.state);
    row.dataset.state = step.state;
    row.append(el('small', '', `${index + 1} · ${names[step.provider] || step.provider}`),
      el('code', '', prettyModel(step.model || 'Provider availability')),
      badge(human(step.state), ['selected', 'completed'].includes(step.state) ? 'green' : step.state === 'trying' ? 'purple' : 'amber'),
      el('p', '', human(step.reason) + (step.http_status ? ' · HTTP ' + step.http_status : '')));
    path.append(row);
  });
  if (!path.children.length) path.append(el('li', 'muted', 'Selecting a route…'));
  const active = Boolean(state.current_route);
  $('activity-status').textContent = active ? 'Active · ' + human(route.state) : 'Latest · ' + human(route.state);
  $('activity-status').className = 'badge ' + (active ? 'purple' : route.state === 'completed' ? 'green' : 'amber');
  summary.append(el('p', 'activity-current', `Rule: ${human(route.rule)}${route.rule_order ? ' · order ' + route.rule_order : ''}`), el('p', '', route.explanation || 'Explicit route selected.'));
  const meta = el('div', 'activity-meta');
  meta.append(badge('Route: ' + route.model, 'neutral'), badge('Elapsed: ' + elapsed(route.duration_ms), 'neutral'), badge('Received: ' + (route.bytes_received || 0) + ' bytes', 'neutral'));
  if (route.response_phase) meta.append(badge('Receiving: ' + human(route.response_phase), 'purple'));
  if (route.reasoning_chunks) meta.append(badge('Reasoning: ' + route.reasoning_chunks + ' chunks', 'neutral'));
  if (route.answer_chunks) meta.append(badge('Answer: ' + route.answer_chunks + ' chunks', 'neutral'));
  if (route.status) meta.append(badge('HTTP ' + route.status, 'neutral'));
  summary.append(meta, el('p', '', 'Backend: ' + (route.upstream_model || 'Selecting an eligible model…')));
  if (active) summary.append(el('p', 'muted', route.bytes_received ? `Last response data ${elapsed(route.idle_ms)} ago. Bytes can include reasoning or keep-alives.` : 'No response bytes yet. The backend may be loading the model or processing the prompt; this request has not completed.'));
  if (state.active_routes?.length > 1) summary.append(el('p', 'muted', `${state.active_routes.length} concurrent requests are active. Showing the most recently started one.`));
  if (route.fallback_reason) summary.append(el('p', '', 'Fallback: ' + human(route.fallback_reason)));
  summary.append(el('p', 'muted', `Request ${route.request_id} · Started ${timestamp(route.started_at)}${route.completed_at ? ' · Finished ' + timestamp(route.completed_at) : ''}`));
  if (route.pattern) {const details = el('details'); details.append(el('summary', 'muted', 'Matched rule pattern'), el('pre', '', route.pattern)); summary.append(details);}
  const events = el('ol', 'event-list');
  (route.events || []).forEach(event => {const row = el('li'); row.append(el('time', '', timestamp(event.at) + ' · +' + elapsed(event.elapsed_ms)), el('strong', '', human(event.event)), el('p', '', event.message)); if (event.upstream_model) row.append(el('code', '', event.upstream_model)); events.append(row);});
  box.append(events);
  const signature = route.rule + ':' + active;
  if (activitySignature !== signature) {activitySignature = signature; renderRules();}
}
async function pollActivity() {
  if (pollingActivity || !state || !$('auto').checked || document.hidden) return;
  pollingActivity = true;
  try {
    const response = await fetch('/api/routing', {cache:'no-store', signal:AbortSignal.timeout(5000)});
    if (!response.ok) throw new Error('HTTP ' + response.status);
    Object.assign(state, await response.json()); renderLastRoute();
    $('activity-freshness').textContent = 'Activity updated ' + timestamp(state.activity_observed_at) + '. Refreshes every 2 seconds; each OpenCode prompt may make multiple model requests.';
  } catch(error) {$('activity-freshness').textContent = 'Activity refresh unavailable; showing the last observation. ' + error.message;}
  finally {pollingActivity = false;}
}

async function load(refresh = false) {
  if (loading) return;
  loading = true; $('refresh').disabled = true;
  try {
    const response = await fetch('/api/dashboard' + (refresh ? '?refresh=true' : ''), {cache: 'no-store', signal: AbortSignal.timeout(12000)});
    if (!response.ok) throw new Error(`Router returned HTTP ${response.status}`);
    state = await response.json(); $('error').hidden = true;
    $('mode').textContent = state.mode === 'local-only' ? 'Local-only mode' : 'Online-first routing'; $('mode').className = 'badge ' + (state.mode === 'local-only' ? 'neutral' : 'green');
    $('configured').textContent = `${Object.values(state.providers).filter(p => p.enabled).length} / 3`;
    $('rule-count').textContent = state.rules.rules.length;
    $('mode-note').hidden = state.mode !== 'local-only'; $('mode-note').textContent = 'Local-only mode is active. Hosted requests are disabled and automatic online routes are mapped to Ollama. Set LOCAL_ONLY=false to enable configured free-tier providers.';
    renderProviders(); renderConfig(); renderChain(); renderLastRoute();
    $('updated').textContent = `Dashboard updated ${timestamp(state.observed_at)}. Provider observation times are shown on each card.`;
    if (refresh && state.refresh_note) $('refresh-note').textContent = state.refresh_note + ' Automatic updates use saved observations. No inference is sent.';
  } catch (error) { $('error').hidden = false; $('error').textContent = `Could not refresh the dashboard. ${error.message}. ${state ? 'Showing the last successful snapshot.' : 'Check that the router is running, then retry.'}`; }
  finally {loading = false; $('refresh').disabled = false;}
}
$('refresh').addEventListener('click', () => load(true));
$('rule-search').addEventListener('input', renderRules);
document.querySelectorAll('[data-example]').forEach(button => button.addEventListener('click', () => { $('task').value = button.dataset.example; $('model').value = 'auto'; $('preview-form').requestSubmit(); }));
$('preview-form').addEventListener('submit', async event => {
  event.preventDefault(); const button = $('preview-button'); button.disabled = true;
  const result = $('preview-result'); result.replaceChildren(el('p', 'muted', 'Evaluating routing rules…'));
  try {
    const response = await fetch('/api/route-preview', {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify({task: $('task').value, model: $('model').value}), signal: AbortSignal.timeout(10000)});
    const data = await response.json(); if (!response.ok) throw new Error(data.detail || `HTTP ${response.status}`);
    result.replaceChildren(badge(data.model.startsWith('local-') ? 'Local inference' : 'Online pool', data.model.startsWith('local-') ? 'green' : 'purple'));
    result.append(el('p', '', 'Matched rule: ' + human(data.rule)), el('strong', '', data.model));
    if (data.first_eligible) result.append(el('p', '', 'First eligible candidate: ' + names[data.first_eligible.provider] + ' / ' + prettyModel(data.first_eligible.model)));
    if (data.fallback_reason) result.append(el('p', '', human(data.fallback_reason)));
    result.append(el('p', 'muted', 'Preview only. No inference sent and no quota reserved. Availability is checked only when a real request is made.'));
    if (state?.chains[data.model]) {selectedPool = data.model; renderChain();}
  } catch(error) {result.replaceChildren(el('p', '', 'Preview unavailable: ' + error.message));}
  finally {button.disabled = false;}
});
load();
setInterval(() => {if ($('auto').checked && !document.hidden) load();}, 15000);

setInterval(pollActivity, 2000);
