"""A tiny synchronous event bus: on(name) registers a handler, emit(name) runs its handlers."""

_handlers = {}
_trace = []


def on(name):
    def register(func):
        _handlers.setdefault(name, []).append(func)
        return func
    return register


def emit(name, payload=None):
    _trace.append(("event", name))
    for func in list(_handlers.get(name, ())):
        _trace.append(("handler", func.__module__.rsplit(".", 1)[-1] + "." + func.__name__))
        func(payload)


def dispatch(name, payload=None):
    """The entry point of outside requests."""
    emit(name, payload)
