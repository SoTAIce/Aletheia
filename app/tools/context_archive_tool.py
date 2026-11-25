"""Bounded reads of application-owned artifacts in one conversation session."""

import asyncio
from dataclasses import asdict
import json

from app.agent.step_models import ToolDefinition, ToolOutput


MAX_PATTERN_MATCHES = 200
MAX_CONTEXT_LINES = 10
DEFAULT_CONTEXT_LINES = 2
_CHUNK_CHARS = 200000


def _search_lines(text: str, pattern: str, context: int, limit: int) -> str:
    """Search lines for a case-sensitive substring, merge adjacent context blocks, bound by limit."""
    lines = text.splitlines()
    hit_indexes = [idx for idx, line in enumerate(lines) if pattern in line]
    total_matches = len(hit_indexes)
    truncated = False
    if total_matches > MAX_PATTERN_MATCHES:
        hit_indexes = hit_indexes[:MAX_PATTERN_MATCHES]
        truncated = True

    blocks: list[tuple[int, int]] = []
    last_line = len(lines) - 1
    for idx in hit_indexes:
        start = max(0, idx - context)
        end = min(last_line, idx + context)
        if blocks and start <= blocks[-1][1] + 1:
            blocks[-1] = (blocks[-1][0], max(blocks[-1][1], end))
        else:
            blocks.append((start, end))

    matches_out: list[dict] = []
    for start, end in blocks:
        entry = {'line': start + 1, 'text': '\n'.join(lines[start:end + 1])}
        candidate = json.dumps(
            {'matches': [*matches_out, entry], 'total_matches': total_matches, 'truncated': truncated},
            ensure_ascii=False,
        )
        if len(candidate) <= limit:
            matches_out.append(entry)
            continue
        truncated = True
        if not matches_out:
            overhead = len(json.dumps(
                {'matches': [{'line': start + 1, 'text': ''}], 'total_matches': total_matches, 'truncated': True},
                ensure_ascii=False,
            ))
            room = max(0, limit - overhead)
            if room > 0:
                trimmed = entry['text'][:room]
                while trimmed:
                    trial = json.dumps(
                        {'matches': [{'line': start + 1, 'text': trimmed}],
                         'total_matches': total_matches, 'truncated': True},
                        ensure_ascii=False,
                    )
                    if len(trial) <= limit:
                        matches_out.append({'line': start + 1, 'text': trimmed})
                        break
                    trimmed = trimmed[:max(0, len(trimmed) - max(1, len(trial) - limit))]
        break

    return json.dumps(
        {'matches': matches_out, 'total_matches': total_matches, 'truncated': truncated},
        ensure_ascii=False,
    )


class ContextArchiveTool:
    """max_chars bounds one read; the service sets it to the context manager's inline tool-result
    budget, so a read is never archived again (D-052)."""

    def __init__(self, archive, max_chars=16000):
        if type(max_chars) is not int or max_chars < 1:
            raise ValueError('max_chars must be a positive integer')
        self._archive = archive
        self._max_chars = max_chars

    @property
    def definition(self):
        return ToolDefinition('read_context_artifact',
            'Read a saved context artifact or turn:<turn_id> from this session. A saved tool result '
            'holds the tool\'s own text, so offsets count characters of that text. One read returns '
            f'at most {self._max_chars} characters (the default); when more remains, the result ends '
            'with a note giving the offset to continue from. Pass pattern to search lines for a '
            'case-sensitive substring with surrounding context lines.',
            {'type':'object', 'properties':{
                'ref':{'type':'string','minLength':1},
                'offset':{'type':'integer','minimum':0},
                'limit':{'type':'integer','minimum':1,'maximum':self._max_chars},
                'pattern':{'type':'string','minLength':1},
                'context':{'type':'integer','minimum':0,'maximum':MAX_CONTEXT_LINES}},
             'required':['ref'],'additionalProperties':False}, effect='read')   # D-087

    def _read_all_text(self, ref: str) -> str:
        if ref.startswith('turn:'):
            turn = self._archive.read_turn(ref[5:])
            return json.dumps(asdict(turn), ensure_ascii=False, default=lambda value: value.isoformat())
        parts: list[str] = []
        pos = 0
        while True:
            chunk = self._archive.read_text(ref, offset=pos, limit=_CHUNK_CHARS)
            parts.append(chunk)
            if len(chunk) < _CHUNK_CHARS:
                break
            pos += len(chunk)
        return ''.join(parts)

    async def invoke(self, arguments):
        if not isinstance(arguments,dict) or set(arguments)-{'ref','offset','limit','pattern','context'}:
            return ToolOutput(False,'',error='Expected ref and optional offset/limit/pattern/context')
        ref=arguments.get('ref')
        offset,limit=arguments.get('offset',0),arguments.get('limit',self._max_chars)
        pattern=arguments.get('pattern')
        if (not isinstance(ref,str) or not ref.strip() or type(offset) is not int or offset<0
                or type(limit) is not int or limit<1):
            return ToolOutput(False,'',error='Invalid artifact reference or read bounds')
        limit=min(limit,self._max_chars)     # a larger request is served in pieces, not refused
        if pattern is not None:
            context=arguments.get('context',DEFAULT_CONTEXT_LINES)
            if (not isinstance(pattern,str) or not pattern
                    or type(context) is not int or not 0<=context<=MAX_CONTEXT_LINES):
                return ToolOutput(False,'',error='Invalid search pattern or context lines')
            try:
                full_text=await asyncio.to_thread(self._read_all_text,ref)
            except (ValueError,KeyError,OSError) as exc:
                return ToolOutput(False,'',error=f'Artifact unavailable: {type(exc).__name__}')
            return ToolOutput(True,_search_lines(full_text,pattern,context,limit))
        try:
            if ref.startswith('turn:'):
                turn=await asyncio.to_thread(self._archive.read_turn,ref[5:])
                text=json.dumps(asdict(turn),ensure_ascii=False,default=lambda value:value.isoformat())
                content=text[offset:offset+limit+1]
            else:
                content=await asyncio.to_thread(self._archive.read_text,ref,offset=offset,limit=limit+1)
        except (ValueError,KeyError,OSError) as exc:
            return ToolOutput(False,'',error=f'Artifact unavailable: {type(exc).__name__}')
        if len(content)>limit:
            content=content[:limit]+f'\n[more text follows; continue with offset={offset+limit}]'
        return ToolOutput(True,content)
