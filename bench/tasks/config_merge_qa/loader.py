"""Configuration loader: layers are merged in LAYERS order, later layers win."""

import json
from pathlib import Path

LAYERS = ["base.json", "production.json", "local.json"]


def merge(into, layer):
    """Merge one layer into the config in place.

    - a None value deletes the key;
    - two dicts are merged recursively;
    - a key ending in "+" appends to the existing list of the same name without the "+";
    - anything else replaces the old value (lists are replaced, not concatenated).
    """
    for key, value in layer.items():
        if value is None:
            into.pop(key, None)
        elif isinstance(value, dict) and isinstance(into.get(key), dict):
            merge(into[key], value)
        elif key.endswith("+") and isinstance(into.get(key[:-1]), list):
            into[key[:-1]] = into[key[:-1]] + list(value)
        else:
            into[key] = value
    return into


def load(root):
    config = {}
    for name in LAYERS:
        path = Path(root) / name
        if path.exists():
            merge(config, json.loads(path.read_text(encoding="utf-8")))
    return config
