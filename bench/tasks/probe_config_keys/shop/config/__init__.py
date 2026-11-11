from shop.config.defaults import DEFAULTS


class Settings:
    """Configuration values: DEFAULTS, overridden by set()."""

    def __init__(self, values):
        self._values = dict(values)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def set(self, key, value):
        self._values[key] = value


settings = Settings(DEFAULTS)
