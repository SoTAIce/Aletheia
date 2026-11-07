"""Does the agent's chat endpoint reuse its prompt cache? (DECISIONS D-056)

Run from the project root: python -m app.agent.cache_probe [--calls 3] [--cache-key KEY]

Sends the same small request (a fixed, made-up text of about 2,500 tokens and a one-word
reply) a few times in a row to the endpoint the agent uses (AGENT_MODEL_* or DashScope) and
prints, for each call, the prompt tokens and how many the provider served from its cache.
Calls after the first should hit the cache when the endpoint keeps it. Every call is a real,
billed model call. Only the numbers are printed, never the reply, the key or provider text.
"""

import argparse
import asyncio
import time

from openai import APIStatusError, AsyncOpenAI

from app.agent.run_meter import _cached_tokens, _non_negative_int
from app.config import config

MIN_CALLS, MAX_CALLS = 2, 5
# OpenAI caches only prompts of at least 1,024 tokens; the fixed text stays well above that.
_LINES = 160


def probe_messages() -> list[dict]:
    """The fixed request: identical on every call and in every run."""
    rules = '\n'.join(
        f'Rule {n}: when the inventory ledger shows item {n * 7 % 101} on shelf {n % 13}, '
        f'record it under batch {n * 3 % 17} and keep the count unchanged.' for n in range(1, _LINES + 1))
    return [{'role': 'system', 'content': 'You are a warehouse clerk. Follow these rules.\n' + rules},
            {'role': 'user', 'content': 'Reply with the single word OK.'}]


def verdict(entries: list[dict]) -> str:
    """One-line reading of the calls after the first."""
    later = entries[1:]
    if not later or any(entry.get('error') for entry in later):
        return '有调用失败，无法判断'
    reported = [entry['cached'] for entry in later if entry['cached'] is not None]
    if not reported:
        return '服务商没有报告缓存命中，无法判断'
    hits = [cached for cached in reported if cached > 0]
    if len(hits) == len(later):
        return '每次都命中缓存：这个服务的缓存是稳定的'
    if not hits:
        return '后续调用都没有命中缓存：这个服务很可能没有保留缓存'
    return '时而命中、时而不命中：这个服务的缓存不稳定'


async def run_probe(client, model: str, calls: int, cache_key: str | None = None,
                    enable_thinking: bool | None = None) -> list[dict]:
    messages = probe_messages()
    kwargs = {'model': model, 'messages': messages, 'temperature': 0, 'max_tokens': 16}
    extra = {}
    if cache_key:
        extra['prompt_cache_key'] = cache_key
    if enable_thinking is not None:
        extra['enable_thinking'] = enable_thinking
    if extra:
        kwargs['extra_body'] = extra
    entries = []
    for _ in range(calls):
        started = time.monotonic()
        try:
            response = await client.chat.completions.create(**kwargs)
        except APIStatusError as exc:
            entries.append({'error': f'{type(exc).__name__} {exc.status_code}'})
            continue
        except Exception as exc:     # type name only, never provider text
            entries.append({'error': type(exc).__name__})
            continue
        usage = getattr(response, 'usage', None)
        prompt = _non_negative_int(getattr(usage, 'prompt_tokens', None))
        entries.append({'prompt': prompt, 'cached': _cached_tokens(usage),
                        'seconds': round(time.monotonic() - started, 2)})
    return entries


def entry_line(number: int, entry: dict) -> str:
    if entry.get('error'):
        return f'第 {number} 次：失败（{entry["error"]}）'
    cached = '未报告' if entry['cached'] is None else entry['cached']
    return f'第 {number} 次：输入 {entry["prompt"]}，命中缓存 {cached}，用时 {entry["seconds"]} 秒'


def _calls(value: str) -> int:
    number = int(value)
    if not MIN_CALLS <= number <= MAX_CALLS:
        raise argparse.ArgumentTypeError(f'must be {MIN_CALLS}-{MAX_CALLS}')
    return number


async def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog='python -m app.agent.cache_probe', description=__doc__.split('\n')[0])
    parser.add_argument('--calls', type=_calls, default=3, help=f'identical calls to send ({MIN_CALLS}-{MAX_CALLS})')
    parser.add_argument('--cache-key', default=None,
                        help='also send prompt_cache_key with this value (some endpoints reject it)')
    args = parser.parse_args(argv)
    base_url, api_key, model = config.agent_model_endpoint
    if not api_key:
        print('没有配置模型的 API 密钥')
        return 2
    # enable_thinking is a DashScope parameter; a separate endpoint may reject it (D-050).
    thinking = None if config.agent_model_base_url else config.rag_enable_thinking
    async with AsyncOpenAI(api_key=api_key, base_url=base_url,
                           timeout=config.rag_model_timeout_seconds, max_retries=0) as client:
        entries = await run_probe(client, model, args.calls, args.cache_key, thinking)
    print(f'模型 {model}，连发 {args.calls} 次相同请求' + ('，带 prompt_cache_key' if args.cache_key else ''))
    for number, entry in enumerate(entries, 1):
        print(entry_line(number, entry))
    print('结论：' + verdict(entries))
    return 0


if __name__ == '__main__':
    raise SystemExit(asyncio.run(main()))
