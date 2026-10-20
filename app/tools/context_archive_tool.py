"""Bounded reads of application-owned artifacts in one conversation session."""

import asyncio
from dataclasses import asdict
import json

from app.agent.step_models import ToolDefinition, ToolOutput


class ContextArchiveTool:
    def __init__(self, archive):
        self._archive = archive

    @property
    def definition(self):
        return ToolDefinition('read_context_artifact',
            'Read a saved context artifact or turn:<turn_id> from this session. Use offset/limit for bounded reads.',
            {'type':'object', 'properties':{
                'ref':{'type':'string','minLength':1},
                'offset':{'type':'integer','minimum':0},
                'limit':{'type':'integer','minimum':1,'maximum':16000}},
             'required':['ref'],'additionalProperties':False})

    async def invoke(self, arguments):
        if not isinstance(arguments,dict) or set(arguments)-{'ref','offset','limit'}:
            return ToolOutput(False,'',error='Expected ref and optional offset/limit')
        ref=arguments.get('ref')
        offset,limit=arguments.get('offset',0),arguments.get('limit',2000)
        if (not isinstance(ref,str) or not ref.strip() or type(offset) is not int or offset<0
                or type(limit) is not int or not 1<=limit<=16000):
            return ToolOutput(False,'',error='Invalid artifact reference or read bounds')
        try:
            if ref.startswith('turn:'):
                turn=await asyncio.to_thread(self._archive.read_turn,ref[5:])
                text=json.dumps(asdict(turn),ensure_ascii=False,default=lambda value:value.isoformat())
                content=text[offset:offset+limit]
            else:
                content=await asyncio.to_thread(self._archive.read_text,ref,offset=offset,limit=limit)
            return ToolOutput(True,content)
        except (ValueError,KeyError,OSError) as exc:
            return ToolOutput(False,'',error=f'Artifact unavailable: {type(exc).__name__}')
