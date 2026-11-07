"""Bounded reads of application-owned artifacts in one conversation session."""

import asyncio
from dataclasses import asdict
import json

from app.agent.step_models import ToolDefinition, ToolOutput


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
            'with a note giving the offset to continue from.',
            {'type':'object', 'properties':{
                'ref':{'type':'string','minLength':1},
                'offset':{'type':'integer','minimum':0},
                'limit':{'type':'integer','minimum':1,'maximum':self._max_chars}},
             'required':['ref'],'additionalProperties':False})

    async def invoke(self, arguments):
        if not isinstance(arguments,dict) or set(arguments)-{'ref','offset','limit'}:
            return ToolOutput(False,'',error='Expected ref and optional offset/limit')
        ref=arguments.get('ref')
        offset,limit=arguments.get('offset',0),arguments.get('limit',self._max_chars)
        if (not isinstance(ref,str) or not ref.strip() or type(offset) is not int or offset<0
                or type(limit) is not int or limit<1):
            return ToolOutput(False,'',error='Invalid artifact reference or read bounds')
        limit=min(limit,self._max_chars)     # a larger request is served in pieces, not refused
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
