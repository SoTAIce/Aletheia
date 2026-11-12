"""What the provider did to a request, measured from its usage report (provider-state-001).

The host estimates each request it sends (``ContextBuilder.estimate_tokens`` over the serialized
messages and tools, about four characters a token, so slightly high). The ratio of the provider's
reported prompt tokens to that estimate tells three relay states apart that the 4c holdout rerun
showed (docs/provider_state_plan.md): a large hidden addition, a small one, and part of the request
dropped. Only numbers and model names are used; never request or response text.
"""

from __future__ import annotations

# Bands of reported / estimated prompt tokens. The estimate runs high, so a direct endpoint lands
# near or a little under 1; the lower bound leaves room for that.
DROPPED_BELOW = 0.8      # the provider counted less than we sent: content was probably dropped
PLAIN_BELOW = 1.5        # about what we sent
PADDED_BELOW = 4.0       # a small hidden addition (about 4,000 tokens on a 2,500-token request)
STATES = ('dropped', 'plain', 'padded', 'heavy', 'unknown')
ANOMALIES = ('dropped',)


def ratio(reported, estimate) -> float | None:
    """Reported over estimated prompt tokens, two decimals; None when either is missing."""
    if type(reported) is not int or type(estimate) is not int or reported < 0 or estimate <= 0:
        return None
    return round(reported / estimate, 2)


def band(value: float | None) -> str:
    if value is None:
        return 'unknown'
    if value < DROPPED_BELOW:
        return 'dropped'
    if value < PLAIN_BELOW:
        return 'plain'
    if value < PADDED_BELOW:
        return 'padded'
    return 'heavy'


def different_model(requested, reported) -> bool:
    """True when the provider named another model. A dated or versioned name of the same model
    (requested gpt-4o, reported gpt-4o-2024-08-06) is the same model; missing names never count."""
    if not requested or not reported:
        return False
    asked, got = requested.strip().lower(), reported.strip().lower()
    return not (got == asked or got.startswith(asked + '-') or got.startswith(asked + '@')
                or got.endswith('/' + asked))


def summary(ratios, requested_models, response_models) -> dict:
    """The run's provider state: the first call's band (it carries the provider's fixed addition
    before caches and tool results), every band seen in order, and whether any response named a
    different model than was requested (names only, no judgement)."""
    states = [band(value) for value in ratios]
    seen = list(dict.fromkeys(states))
    mismatch = any(different_model(asked, got) for asked, got in zip(requested_models, response_models))
    result = {'state': states[0] if states else 'unknown', 'states': seen,
              'ratios': list(ratios), 'model_mismatch': mismatch,
              'anomaly': mismatch or any(state in ANOMALIES for state in seen)}
    # Host-authored Chinese text for the web page, which cannot call notice() itself.
    result['notice'] = notice(result)
    return result


def notice(provider: dict | None) -> str | None:
    """One line for the command line and the web page when something looks wrong, else None."""
    if not isinstance(provider, dict) or not provider.get('anomaly'):
        return None
    parts = []
    if 'dropped' in (provider.get('states') or ()):
        parts.append('服务商报告的提示令牌少于本机发出的内容，部分请求可能被丢弃')
    if provider.get('model_mismatch'):
        parts.append('服务商回答所用的模型和请求的不同')
    return '注意：' + '；'.join(parts) + '，本次结果可能不可靠。' if parts else None
