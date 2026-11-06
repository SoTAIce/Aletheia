"""Hidden reference solution for parse_duration; never copied into the agent's root."""

import re

_PART = re.compile(r'(\d+)([hms])')
_ORDER = 'hms'
_SECONDS = {'h': 3600, 'm': 60, 's': 1}


def parse_duration(text):
    if not isinstance(text, str):
        raise TypeError('text must be a str')
    parts = text.split()
    if not parts:
        raise ValueError('empty duration')
    total, last = 0, -1
    for part in parts:
        pieces = _PART.findall(part)
        if not pieces or ''.join(n + u for n, u in pieces) != part:
            raise ValueError('bad duration segment')
        for number, unit in pieces:
            position = _ORDER.index(unit)
            if position <= last:
                raise ValueError('units repeated or out of order')
            last = position
            total += int(number) * _SECONDS[unit]
    return total
