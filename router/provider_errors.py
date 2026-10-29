"""Bounded provider-error classification without retaining arbitrary upstream text."""
import asyncio
import json
import re

import httpx


async def rejected_request_summary(response):
    # Provider messages can echo prompts, tool arguments and credentials. Return
    # only known codes/field names, never provider-supplied prose or field values.
    try:
        async with asyncio.timeout(8):
            if response.is_stream_consumed:
                raw = response.content[:16384]
            else:
                chunks = bytearray()
                async for chunk in response.aiter_bytes():
                    chunks.extend(chunk[:16384-len(chunks)])
                    if len(chunks) >= 16384:
                        break
                raw = bytes(chunks)
        data = json.loads(raw)
        error = data.get('error', {}) if isinstance(data, dict) else {}
        if not isinstance(error, dict):
            return 'Provider rejected the request; details redacted.'
        codes = {'tool_use_failed', 'context_length_exceeded', 'invalid_api_key',
                 'rate_limit_exceeded', 'model_not_found', 'model_decommissioned', 'invalid_request_error'}
        code = error.get('code')
        code = code if isinstance(code, str) and code in codes else 'request-rejected'
        text = error.get('message', '')
        fields = ('content', 'role', 'reasoning_content', 'extra_content', 'tool_calls', 'tool_call_id', 'name', 'reasoning_effort', 'max_tokens', 'max_completion_tokens', 'temperature',
                  'tool_choice', 'tools', 'messages', 'stream_options', 'response_format',
                  'frequency_penalty', 'presence_penalty', 'parallel_tool_calls')
        mentioned = [field for field in fields if isinstance(text, str) and re.search(r'\b'+field+r'\b', text)]
        # Extract only numeric indexes and reviewed schema tokens from field paths.
        paths = []
        if isinstance(text, str):
            for match in re.finditer(r"\bmessages(?:\[([0-9]{1,6})\]|\.([0-9]{1,6}))(?:\.(content|role|reasoning_content|extra_content|tool_calls|tool_call_id|name)\b)?", text):
                path = 'messages[' + (match[1] or match[2]) + ']'
                if match[3]:
                    path += '.' + match[3]
                if path not in paths:
                    paths.append(path)
        result = code + ('; referenced fields: ' + ', '.join(mentioned) if mentioned else '; details redacted')
        if paths:
            result += '; field paths: ' + ', '.join(paths[:8])
        return result
    except (TimeoutError, httpx.HTTPError, ValueError, TypeError):
        return 'Provider rejected the request; error details unavailable.'
