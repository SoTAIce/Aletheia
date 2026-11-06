from collections import deque


class CycleError(ValueError):
    def __init__(self, nodes):
        super().__init__('cycle among: ' + ', '.join(nodes))
        self.nodes = nodes


def _validate(deps):
    if not isinstance(deps, dict):
        raise TypeError('deps must be a dict')
    for node, items in deps.items():
        if not isinstance(node, str):
            raise TypeError('nodes must be str')
        if not isinstance(items, (list, tuple, set, frozenset)):
            raise TypeError('prerequisites must be a list, tuple or set')
        for item in items:
            if not isinstance(item, str):
                raise TypeError('nodes must be str')


def topo_sort(deps):
    _validate(deps)
    indegree = {}
    dependents = {}
    for node, items in deps.items():
        indegree[node] = len(items)
        for item in items:
            dependents.setdefault(item, []).append(node)
    ready = deque(node for node in deps if indegree[node] == 0)
    order = []
    while ready:
        node = ready.popleft()
        order.append(node)
        for other in dependents.get(node, []):
            indegree[other] -= 1
            if indegree[other] == 0:
                ready.append(other)
    return order
