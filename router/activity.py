"""Prompt-free, in-memory request lifecycle and console routing logs."""
import asyncio
from collections import deque
from copy import deepcopy
import json
import logging
import re
import time
import uuid

logger = logging.getLogger('task_router.routing')
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(name)s %(message)s'))
    logger.addHandler(handler)
logger.setLevel(logging.INFO)
logger.propagate = False


class StreamObservation:
    """Inspect bounded SSE frames for error markers; never publish their contents."""
    def __init__(self):
        self.buffer = b''
        self.failed = False
        self.reasoning_chunks = 0
        self.answer_chunks = 0
        self.phase = None

    def feed(self, chunk):
        self.buffer = (self.buffer + chunk)[-131072:]
        frames = re.split(br'\r?\n\r?\n', self.buffer)
        self.buffer = frames.pop()
        for frame in frames:
            data = b'\n'.join(line[5:].strip() for line in frame.splitlines() if line.startswith(b'data:'))
            if not data or data == b'[DONE]':
                continue
            try:
                event = json.loads(data)
                if isinstance(event, dict) and (event.get('error') or any(isinstance(c, dict) and c.get('finish_reason') == 'error' for c in (event.get('choices') or []))):
                    self.failed = True
                if isinstance(event, dict):
                    for choice in event.get('choices') or []:
                        delta = choice.get('delta') if isinstance(choice, dict) else None
                        if not isinstance(delta, dict):
                            continue
                        if delta.get('reasoning_content') or delta.get('reasoning'):
                            self.reasoning_chunks += 1
                            self.phase = 'reasoning'
                        if delta.get('content'):
                            self.answer_chunks += 1
                            self.phase = 'answer'
                        if delta.get('tool_calls'):
                            self.phase = 'tool-call'
            except (ValueError, TypeError):
                pass


class RoutingActivity:
    def __init__(self, history_limit=30):
        self.active = {}
        self.completed = deque(maxlen=history_limit)
        self.started = {}
        self.last_data = {}
        self.watchers = {}

    def start(self, model, rule, config, requested='auto'):
        now = time.time()
        matched = next(((i+1, r) for i, r in enumerate(config['rules']) if r['name'] == rule), None)
        if rule == 'explicit-model':
            explanation = f'Client explicitly selected {requested}; keyword rules were bypassed.'
            configured = requested
        elif matched:
            explanation = f'Rule {matched[0]} ({rule}) is the first matching rule for the latest user message; it selects {matched[1]["model"]}.'
            configured = matched[1]['model']
        else:
            explanation = f'No ordered rule matched the latest user message; default route {config["default"]} applies.'
            configured = config['default']
        record = {'request_id': uuid.uuid4().hex[:12], 'at': now, 'started_at': now,
                  'updated_at': now, 'completed_at': None, 'state': 'routing',
                  'rule': rule, 'rule_order': matched[0] if matched else None,
                  'pattern': matched[1]['pattern'] if matched else None,
                  'explanation': explanation, 'configured_route': configured,
                  'model': model, 'upstream_model': None, 'fallback_reason': None,
                  'status': None, 'duration_ms': None, 'bytes_received': 0,
                  'first_byte_ms': None, 'last_data_at': None, 'response_phase': None,
                  'reasoning_chunks': 0, 'answer_chunks': 0, 'events': [], 'failover_path': []}
        self.active[record['request_id']] = record
        self.started[record['request_id']] = time.monotonic()
        self.event(record, 'rule-selected', explanation)
        return record

    def event(self, record, name, message, **fields):
        if record['request_id'] not in self.active:
            return
        candidate = fields.pop('candidate', None)
        if candidate is not None:
            # A candidate's attempt/outcome occupy one step in chronological order.
            path = record['failover_path']
            previous = next((step for step in path if (step['provider'], step['model']) ==
                             (candidate['provider'], candidate['model'])), None)
            if previous is None:
                previous = {'started_at': time.time()}
                path.append(previous)
            previous.update(candidate, updated_at=time.time())
        record.update(fields)
        record['updated_at'] = time.time()
        elapsed = round((time.monotonic()-self.started[record['request_id']])*1000)
        event = {'at': record['updated_at'], 'event': name, 'message': message,
                 'model': record['model'], 'upstream_model': record['upstream_model'],
                 'status': record['status'], 'elapsed_ms': elapsed}
        if candidate is not None:
            event['candidate'] = deepcopy(candidate)
        record['events'].append(event)
        # One request can encounter at most eight candidates; keep traces bounded.
        record['events'] = record['events'][-40:]
        logger.info('routing %s', json.dumps({'request_id': record['request_id'], 'rule': record['rule'], **event}))

    def finish(self, record, state, message, **fields):
        if record['request_id'] not in self.active:
            return
        elapsed = round((time.monotonic()-self.started[record['request_id']])*1000)
        path = record['failover_path']
        candidate = None
        if path and path[-1]['state'] in ('trying', 'selected'):
            candidate = {**path[-1], 'state': state, 'reason': message}
        if candidate is not None:
            fields['candidate'] = candidate
        self.event(record, state, message, state=state, completed_at=time.time(), duration_ms=elapsed, **fields)
        self.active.pop(record['request_id'], None)
        self.started.pop(record['request_id'], None)
        self.last_data.pop(record['request_id'], None)
        watcher = self.watchers.pop(record['request_id'], None)
        if watcher is not None:
            watcher.cancel()
        self.completed.appendleft(record)

    def received(self, record, size):
        if record['request_id'] not in self.active or size == 0:
            return
        now = time.monotonic()
        record['bytes_received'] += size
        record['last_data_at'] = time.time()
        self.last_data[record['request_id']] = now
        if record['first_byte_ms'] is None:
            record['first_byte_ms'] = round((now-self.started[record['request_id']])*1000)
            self.event(record, 'first-response-data', 'First response bytes received; bytes can include reasoning or keep-alives.', state='streaming')

    def observed(self, record, observation):
        if record['request_id'] not in self.active:
            return
        record['reasoning_chunks'] = observation.reasoning_chunks
        record['answer_chunks'] = observation.answer_chunks
        if observation.phase and observation.phase != record['response_phase']:
            self.event(record, observation.phase+'-data',
                       'Model is emitting '+observation.phase+' chunks; content is forwarded to the client without being logged.',
                       response_phase=observation.phase)

    def watch(self, record, interval=10):
        async def progress():
            while record['request_id'] in self.active:
                await asyncio.sleep(interval)
                if record['request_id'] not in self.active:
                    break
                idle = round(time.monotonic()-self.last_data.get(record['request_id'], self.started[record['request_id']]))
                self.event(record, 'still-waiting',
                           f'Request remains active in phase {record["state"]}/{record["response_phase"] or "awaiting-data"}; {record["bytes_received"]} response bytes received, {idle}s since start or last data. Model loading/prompt processing may still be in progress.')
        self.watchers[record['request_id']] = asyncio.create_task(progress())

    def view(self):
        now = time.monotonic()
        active = []
        for key, record in reversed(list(self.active.items())):
            item = deepcopy(record)
            item['duration_ms'] = round((now-self.started[key])*1000)
            item['idle_ms'] = round((now-self.last_data.get(key, self.started[key]))*1000)
            active.append(item)
        latest = deepcopy(self.completed[0]) if self.completed else None
        return {'active_routes': active, 'current_route': active[0] if active else None,
                'latest_completed_route': latest, 'recent_routes': deepcopy(list(self.completed)),
                'activity_observed_at': time.time()}
