class Metrics:
    """Counters; its emit() only stores a number and is not the event bus."""

    def __init__(self):
        self.values = {}

    def emit(self, name, value=1):
        self.values[name] = self.values.get(name, 0) + value


metrics = Metrics()
metrics.emit("order.paid")
