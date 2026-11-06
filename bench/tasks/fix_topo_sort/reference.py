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
    prereqs = {}
    for node, items in deps.items():
        prereqs.setdefault(node, set()).update(items)
        for item in items:
            prereqs.setdefault(item, set())
    indegree = {node: len(items) for node, items in prereqs.items()}
    dependents = {node: [] for node in prereqs}
    for node, items in prereqs.items():
        for item in items:
            dependents[item].append(node)
    ready = sorted(node for node, count in indegree.items() if count == 0)
    order = []
    while ready:
        node = ready.pop(0)
        order.append(node)
        for other in dependents[node]:
            indegree[other] -= 1
            if indegree[other] == 0:
                ready.append(other)
        ready.sort()
    if len(order) != len(prereqs):
        raise CycleError(sorted(set(prereqs) - set(order)))
    return order
