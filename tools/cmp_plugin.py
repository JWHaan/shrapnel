"""pytest plugin: record what reaches every single-operator comparison in sqlglot.

Each sqlglot module is compiled from a rewritten AST where `a OP b` becomes
`__br_cmp__(site, a, b)`; the helper computes the same result and notes, per test:
  - for is / is not / == / !=: whether `a is b` agreed with `a == b` every time
  - for primitive operands: up to CAP distinct (a, b) pairs, else marks overflow
Every test runs in its own forked child. Output: one JSON per test in $CMP_OUT.
"""
import ast
import builtins
import hashlib
import importlib.abc
import importlib.machinery
import json
import operator
import os
import sys

import pytest

ROOT = os.environ.get("MAP_ROOT", "/src").rstrip("/") + "/"
OUT = os.environ.get("CMP_OUT", "/out")
CAP = 24
OPS = {
    ast.Eq: operator.eq, ast.NotEq: operator.ne, ast.Is: operator.is_, ast.IsNot: operator.is_not,
    ast.Lt: operator.lt, ast.LtE: operator.le, ast.Gt: operator.gt, ast.GtE: operator.ge,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}
PRIM = (int, float, str, bool, type(None))
_sites: list[tuple] = []  # site index -> (file, line, col, opfunc, opname)
_obs: dict[int, list] = {}  # site -> [agree(bool|None), pairs set|None(overflow)]


def __br_cmp__(i, a, b):
    f = _sites[i][3]
    r = f(a, b)
    o = _obs.get(i)
    if o is None:
        o = _obs[i] = [True, set()]
    if _sites[i][4] in ("Eq", "NotEq", "Is", "IsNot") and o[0]:
        try:
            if (a is b) != bool(a == b):
                o[0] = False
        except Exception:
            o[0] = False
    if o[1] is not None:
        if type(a) in PRIM and type(b) in PRIM:
            o[1].add((repr(a), repr(b)))
            if len(o[1]) > CAP:
                o[1] = None
        elif _sites[i][4] in ("In", "NotIn") and type(a) in PRIM:
            o[1] = None  # container operand: values not kept
        else:
            o[1] = None
    return r


builtins.__br_cmp__ = __br_cmp__


class _Rewrite(ast.NodeTransformer):
    def __init__(self, rel):
        self.rel = rel

    def visit_Compare(self, node):
        self.generic_visit(node)
        if len(node.ops) != 1 or type(node.ops[0]) not in OPS:
            return node
        op = type(node.ops[0])
        _sites.append((self.rel, node.lineno, node.col_offset, OPS[op], op.__name__))
        call = ast.Call(
            func=ast.Name("__br_cmp__", ast.Load()),
            args=[ast.Constant(len(_sites) - 1), node.left, node.comparators[0]],
            keywords=[],
        )
        return ast.copy_location(call, node)


class _Loader(importlib.machinery.SourceFileLoader):
    def source_to_code(self, data, path, *, _optimize=-1):
        rel = path[len(ROOT):]
        tree = ast.parse(data, path)
        tree = ast.fix_missing_locations(_Rewrite(rel).visit(tree))
        return compile(tree, path, "exec", dont_inherit=True)

    def get_code(self, fullname):  # never use cached bytecode
        path = self.get_filename(fullname)
        with open(path, "rb") as fh:
            return self.source_to_code(fh.read(), path)


class _Finder(importlib.abc.MetaPathFinder):
    def find_spec(self, name, path, target=None):
        if not name.startswith("sqlglot"):
            return None
        spec = importlib.machinery.PathFinder.find_spec(name, path)
        if spec and spec.origin and spec.origin.startswith(ROOT + "sqlglot/") and spec.origin.endswith(".py"):
            spec.loader = _Loader(name, spec.origin)
        return spec


sys.meta_path.insert(0, _Finder())


class InlineExecutor:
    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        import pickle

        f = cf.Future()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def pytest_configure(config):
    import concurrent.futures

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    os.makedirs(OUT, exist_ok=True)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        _obs.clear()
        ok = True
        try:
            reports = runtestprotocol(item, nextitem=None, log=False)
            ok = all(not r.failed for r in reports)
        except BaseException:
            ok = False
        data = {
            "test": item.nodeid,
            "ok": ok,
            "obs": {
                f"{_sites[i][0]}:{_sites[i][1]}:{_sites[i][2]}": [o[0], None if o[1] is None else sorted(o[1])]
                for i, o in _obs.items()
            },
        }
        h = hashlib.sha1(item.nodeid.encode()).hexdigest()[:16]
        with open(os.path.join(OUT, f"cmp-{h}.json"), "w") as fh:
            json.dump(data, fh)
        os._exit(0)
    os.waitpid(pid, 0)
    return True
