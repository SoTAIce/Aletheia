"""Compact the MODEL VIEW of a long tool-calling history (team-recovery-001, team-v2-001).

ContextManager.prepare shrinks tool results and old turns, but never tool-call
arguments, so repeated write_file calls carrying whole files overflow the window
(ContextBudgetError). These helpers only change the messages sent to the model;
the caller's records and tool receipts are never modified.
"""

import json
from dataclasses import replace

from app.agent.step_models import AssistantMessage, ToolMessage

VIEW_COMPACT_SHARE = 0.6
KEEP_RECENT_OUTPUTS = 6
OLD_OUTPUT_CHARS = 300
_BODY_CHARS = 400


def _latest_writes(messages):
    latest = {}
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage):
            for call in message.tool_calls:
                if call.tool_name == 'write_file' and isinstance(call.arguments.get('path'), str):
                    latest[call.arguments['path']] = (index, call.call_id)
    return latest


def _last_write(messages):
    for index in range(len(messages) - 1, -1, -1):
        message = messages[index]
        if isinstance(message, AssistantMessage):
            for call in reversed(message.tool_calls):
                if call.tool_name == 'write_file':
                    return index, call.call_id
    return None


def shorten_output(message, chars=OLD_OUTPUT_CHARS):
    try:
        payload = json.loads(message.content)
    except ValueError:
        return message
    content = payload.get('content') if isinstance(payload, dict) else None
    if not isinstance(content, str) or len(content) <= 2 * chars:
        return message
    payload['content'] = content[:chars] + f'\n[host: older output shortened, {len(content)} chars in total]'
    return replace(message, content=json.dumps(payload, ensure_ascii=False))


STRONG_KEEP_RECENT_OUTPUTS = 2


def compact_model_view(messages, tools, manager, *, share=VIEW_COMPACT_SHARE, all_but_last_write=False):
    """Above `share` of the window: drop superseded write_file bodies and shorten older outputs.

    With all_but_last_write (the strong tier), every write_file body except the most recent
    one is replaced by a pointer to read_file (the workspace holds the current content), any
    other large string argument outside the latest assistant turn (e.g. run_python code) is
    omitted, and only the last STRONG_KEEP_RECENT_OUTPUTS tool outputs stay whole.
    """
    messages = tuple(messages)
    if manager.count_request(messages, tools) <= manager.window_tokens * share:
        return messages
    latest = _latest_writes(messages)
    last = _last_write(messages)
    last_turn = max((i for i, m in enumerate(messages) if isinstance(m, AssistantMessage)), default=-1)
    tool_indexes = [i for i, m in enumerate(messages) if isinstance(m, ToolMessage)]
    keep = STRONG_KEEP_RECENT_OUTPUTS if all_but_last_write else KEEP_RECENT_OUTPUTS
    recent = set(tool_indexes[-keep:])
    view = []
    for index, message in enumerate(messages):
        if isinstance(message, AssistantMessage) and message.tool_calls:
            calls = []
            for call in message.tool_calls:
                body, path = call.arguments.get('content'), call.arguments.get('path')
                if call.tool_name == 'write_file' and isinstance(body, str) and len(body) > _BODY_CHARS:
                    if latest.get(path) != (index, call.call_id):
                        note = f'[host: {len(body)} chars omitted; a later write_file replaced this file]'
                    elif all_but_last_write and last != (index, call.call_id):
                        note = f'[host: {len(body)} chars omitted; read_file this path for its current content]'
                    else:
                        note = None
                    if note:
                        call = replace(call, arguments={**call.arguments, 'content': note})
                elif all_but_last_write and index != last_turn:
                    large = {k: f'[host: {len(v)} chars omitted from an earlier call]'
                             for k, v in call.arguments.items() if isinstance(v, str) and len(v) > _BODY_CHARS}
                    if large:
                        call = replace(call, arguments={**call.arguments, **large})
                calls.append(call)
            message = replace(message, tool_calls=tuple(calls))
        elif isinstance(message, ToolMessage) and index not in recent:
            message = shorten_output(message)
        view.append(message)
    return tuple(view)
