"""Normalize standard OpenAI-compatible client options without allowing addons."""
from fastapi import HTTPException

CHAT_FIELDS = {'model', 'messages', 'tools', 'tool_choice', 'parallel_tool_calls',
               'stream', 'stream_options', 'max_tokens', 'temperature', 'top_p',
               'stop', 'seed', 'presence_penalty', 'frequency_penalty',
               'response_format', 'reasoning_effort'}
# These are accepted at the gateway but normalized/removed before upstream calls.
CLIENT_FIELDS = {'max_completion_tokens', 'user', 'metadata', 'store', 'verbosity'}


def normalize_chat(payload):
    unknown = sorted(set(payload) - CHAT_FIELDS - CLIENT_FIELDS)
    if unknown:
        raise HTTPException(400, {
            'message': 'Unsupported parameters; provider overrides and paid addons are disabled',
            'unsupported_parameters': unknown,
        })
    result = dict(payload)
    stream_options = result.get('stream_options')
    if stream_options is not None:
        if (not isinstance(stream_options, dict) or set(stream_options) - {'include_usage'} or
                ('include_usage' in stream_options and not isinstance(stream_options['include_usage'], bool))):
            raise HTTPException(400, 'stream_options supports only the boolean include_usage option')
    if 'max_completion_tokens' in result:
        value = result.pop('max_completion_tokens')
        if value is not None:
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise HTTPException(400, 'max_completion_tokens must be a positive integer')
            if result.get('max_tokens') is not None and result['max_tokens'] != value:
                raise HTTPException(400, 'max_tokens and max_completion_tokens conflict')
            result['max_tokens'] = value
    if result.get('reasoning_effort') is not None and result['reasoning_effort'] not in ('none', 'minimal', 'low', 'medium', 'high', 'xhigh'):
        raise HTTPException(400, 'Unsupported reasoning_effort value')
    if result.get('store') not in (None, False):
        raise HTTPException(400, 'Server-side response storage is disabled')
    # Client bookkeeping is not needed for routing and may not be supported by
    # Gemini/Groq/Ollama. Do not forward user identifiers or storage metadata.
    for field in ('user', 'metadata', 'store', 'verbosity'):
        result.pop(field, None)
    return result


def groq_chat(payload):
    """Translate shared chat history to Groq's text/function-tool message schema.

    Other providers' reasoning and thought signatures are history metadata, not
    portable request fields. Do not send them to Groq, and do not change the
    original payload: Gemini/local fallback may still need that metadata.
    """
    body = dict(payload)
    messages = []
    for original in payload['messages']:
        message = {key: original[key] for key in ('role', 'content', 'tool_calls', 'tool_call_id') if key in original}
        if message.get('role') == 'developer':
            message['role'] = 'system'
        content = message.get('content')
        if isinstance(content, list) and all(isinstance(part, dict) and part.get('type') == 'text' and isinstance(part.get('text'), str) for part in content):
            message['content'] = '\n'.join(part['text'] for part in content)
        if isinstance(message.get('tool_calls'), list):
            message['tool_calls'] = [{key: call[key] for key in ('id', 'type', 'function') if key in call}
                                     if isinstance(call, dict) else call for call in message['tool_calls']]
        messages.append(message)
    body['messages'] = messages
    # Groq gpt-oss supports low/medium/high. "none" cannot disable its reasoning;
    # omit it to use the provider default rather than send an unsupported value.
    if body.get('reasoning_effort') in ('none', 'minimal', 'xhigh'):
        effort = body.pop('reasoning_effort')
        if effort != 'none':
            body['reasoning_effort'] = 'low' if effort == 'minimal' else 'high'
    return body
