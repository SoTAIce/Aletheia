import re

_VERSION = re.compile(
    r'(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)'
    r'(?:-([0-9A-Za-z.-]+))?(?:\+([0-9A-Za-z.-]+))?')
_IDENT = re.compile(r'[0-9A-Za-z-]+')
_NUMERIC = re.compile(r'[0-9]+')


def _parse(text):
    if not isinstance(text, str):
        raise TypeError('version must be str')
    match = _VERSION.fullmatch(text)
    if not match:
        raise ValueError(f'invalid version: {text!r}')
    major, minor, patch, pre, build = match.groups()
    pre_ids = pre.split('.') if pre is not None else []
    for ident in pre_ids:
        if not _IDENT.fullmatch(ident):
            raise ValueError(f'invalid prerelease: {text!r}')
        if _NUMERIC.fullmatch(ident) and len(ident) > 1 and ident[0] == '0':
            raise ValueError(f'leading zero in prerelease: {text!r}')
    if build is not None:
        for ident in build.split('.'):
            if not _IDENT.fullmatch(ident):
                raise ValueError(f'invalid build: {text!r}')
    return (int(major), int(minor), int(patch)), pre_ids


def _cmp(x, y):
    return (x > y) - (x < y)


def _cmp_ident(x, y):
    x_num, y_num = bool(_NUMERIC.fullmatch(x)), bool(_NUMERIC.fullmatch(y))
    if x_num and y_num:
        return _cmp(int(x), int(y))
    if x_num != y_num:
        return -1 if x_num else 1
    return _cmp(x, y)


def compare_versions(a, b):
    core_a, pre_a = _parse(a)
    core_b, pre_b = _parse(b)
    if core_a != core_b:
        return _cmp(core_a, core_b)
    if not pre_a or not pre_b:
        return _cmp(len(pre_b), len(pre_a)) if (pre_a or pre_b) else 0
    for x, y in zip(pre_a, pre_b):
        result = _cmp_ident(x, y)
        if result:
            return result
    return _cmp(len(pre_a), len(pre_b))
