"""修复后 topo_sort.py 的隐藏检查，绝不复制到智能体的根目录。"""


def checks(mod):
    f = mod.topo_sort
    cases = {
        'empty': ({}, []),
        'single': ({'a': []}, ['a']),
        'chain': ({'c': ['b'], 'b': ['a'], 'a': []}, ['a', 'b', 'c']),
        'prereq only node': ({'b': ['a']}, ['a', 'b']),
        'prereq only chain': ({'c': ['b'], 'b': ['a']}, ['a', 'b', 'c']),
        'ties sorted': ({'z': [], 'y': [], 'x': []}, ['x', 'y', 'z']),
        'smallest ready first': ({'z': [], 'y': [], 'a': ['y']}, ['y', 'a', 'z']),
        'diamond': ({'d': ['c', 'b'], 'c': ['a'], 'b': ['a'], 'a': []}, ['a', 'b', 'c', 'd']),
        'duplicate prereq': ({'a': [], 'b': ['a', 'a']}, ['a', 'b']),
        'tuple and set values': ({'c': {'b'}, 'b': ('a',), 'a': []}, ['a', 'b', 'c']),
        'build graph': ({'app': ['lib', 'cfg'], 'lib': ['core'], 'test': ['app', 'mock'],
                         'cfg': [], 'mock': ['core']},
                        ['cfg', 'core', 'lib', 'app', 'mock', 'test']),
    }
    for name, (graph, expected) in cases.items():
        yield name, lambda g=graph, e=expected: f(g) == e

    yield 'two node cycle', lambda: _raises(lambda: f({'a': ['b'], 'b': ['a']}), mod.CycleError)
    yield 'self loop', lambda: _raises(lambda: f({'a': ['a']}), mod.CycleError)
    yield 'cycle is ValueError', lambda: _raises(lambda: f({'a': ['b'], 'b': ['a']}), ValueError)

    def cycle_nodes():
        try:
            f({'a': ['b'], 'b': ['a'], 'c': ['a'], 'd': []})
        except mod.CycleError as exc:
            return exc.nodes == ['a', 'b', 'c']
        return False
    yield 'cycle nodes listed', cycle_nodes

    def keeps_input():
        graph = {'b': ['a', 'a'], 'c': ['b']}
        f(graph)
        return graph == {'b': ['a', 'a'], 'c': ['b']}
    yield 'input not mutated', keeps_input
    yield 'rejects list', lambda: _raises(lambda: f([('a', 'b')]), TypeError)
    yield 'rejects string value', lambda: _raises(lambda: f({'b': 'a'}), TypeError)
    yield 'rejects int node', lambda: _raises(lambda: f({1: []}), TypeError)
    yield 'rejects int prereq', lambda: _raises(lambda: f({'a': [1]}), TypeError)


def _raises(func, error):
    try:
        func()
    except error:
        return True
    except Exception:
        return False
    return False
