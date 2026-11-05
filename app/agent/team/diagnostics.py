"""Bounded exception structure without raw messages, prompts or provider bodies."""
from app.agent.tool_calling_model import ModelOutputLimitError, ModelToolArgumentsError


def failure_details(error: BaseException) -> dict:
    seen: set[int] = set()

    def visit(exc, depth=0):
        if id(exc) in seen or len(seen) >= 32 or depth >= 8:
            return {'type': type(exc).__name__, 'truncated': True}
        seen.add(id(exc))
        detail = {'type': type(exc).__name__}
        if isinstance(exc, ModelOutputLimitError):
            detail.update(finish_reason=exc.finish_reason, prompt_tokens=exc.prompt_tokens,
                completion_tokens=exc.completion_tokens, output_token_limit=exc.output_token_limit)
        if isinstance(exc, ModelToolArgumentsError):
            detail.update(reason=exc.reason, tool_name=exc.tool_name, argument_chars=exc.argument_chars)
        if isinstance(exc, BaseExceptionGroup):
            detail['exceptions'] = [visit(child, depth + 1) for child in exc.exceptions[:32]]
            if len(exc.exceptions) > 32:
                detail['truncated'] = True
        cause = exc.__cause__
        if cause is None and not exc.__suppress_context__:
            cause = exc.__context__
        if cause is not None:
            detail['cause'] = visit(cause, depth + 1)
        return detail

    return visit(error)


def failure_types(detail: dict) -> str:
    names = []

    def visit(node):
        if node['type'] not in names:
            names.append(node['type'])
        for child in node.get('exceptions', ()):
            visit(child)
        if 'cause' in node:
            visit(node['cause'])

    visit(detail)
    return ' -> '.join(names)
