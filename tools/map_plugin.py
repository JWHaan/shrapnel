"""pytest plugin: record, per test, which sqlglot/test source lines run and which files open.

Used only to build maps from the pinned sqlglot commit. Writes one JSON file per
process into $MAP_OUT.
"""
import json
import os
import sys

import pytest

ROOT = os.environ.get("MAP_ROOT", "/src")
OUT = os.environ.get("MAP_OUT", "/out")
MON = sys.monitoring
TOOL = MON.COVERAGE_ID

_current = {"key": "<collect>"}
_task: dict[str, int] = {}
_lines: dict[str, dict[str, set[int]]] = {}
_opens: dict[str, set[str]] = {}
_classes: dict[str, set[str]] = {}
_mro_cache: dict[type, tuple[str, ...]] = {}
_getframe = sys._getframe


def _mro_names(tp):
    r = _mro_cache.get(tp)
    if r is None:
        r = tuple(
            f"{c.__module__}:{c.__qualname__}"
            for c in getattr(tp, "__mro__", ())
            if isinstance(getattr(c, "__module__", None), str) and c.__module__.startswith("sqlglot")
        )
        _mro_cache[tp] = r
    return r


def _on_start(code, offset):
    if not code.co_filename.startswith(_prefix):
        return MON.DISABLE
    if not code.co_argcount or code.co_varnames[0] not in ("self", "cls"):
        return MON.DISABLE
    try:
        obj = _getframe(1).f_locals[code.co_varnames[0]]
    except Exception:
        return None
    tp = obj if isinstance(obj, type) else type(obj)
    names = _mro_names(tp)
    if names:
        s = _classes.get(_current["key"])
        if s is None:
            s = _classes[_current["key"]] = set()
        s.update(names)
    return None
_prefix = ROOT.rstrip("/") + "/"


def _bucket(key):
    b = _lines.get(key)
    if b is None:
        b = _lines[key] = {}
    return b


def _on_line(code, line):
    fn = code.co_filename
    if not fn.startswith(_prefix):
        return MON.DISABLE
    rel = fn[len(_prefix):]
    b = _bucket(_current["key"])
    s = b.get(rel)
    if s is None:
        s = b[rel] = set()
    s.add(line)
    return MON.DISABLE


def _audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        p = os.fsdecode(args[0])
        if not os.path.isabs(p):
            try:
                p = os.path.abspath(p)
            except Exception:
                return
        if p.startswith(_prefix):
            _opens.setdefault(_current["key"], set()).add(p[len(_prefix):])


class InlineExecutor:
    """Stand-in for ProcessPoolExecutor so work runs (and is recorded) in this process."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf

        import pickle

        f = cf.Future()
        base = _current["key"].split("#t")[0]
        n = _task.get(base, 0)
        _task[base] = n + 1
        _current["key"] = f"{base}#t{n}"
        MON.restart_events()
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            res = fn(*args, **kwargs)
            _current["key"] = base
            f.set_result(pickle.loads(pickle.dumps(res)))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        finally:
            _current["key"] = base
            MON.restart_events()
        return f

    def map(self, fn, *its, **kw):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


def pytest_configure(config):
    import concurrent.futures

    os.makedirs(OUT, exist_ok=True)

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    MON.use_tool_id(TOOL, "mapper")
    MON.register_callback(TOOL, MON.events.LINE, _on_line)
    MON.register_callback(TOOL, MON.events.PY_START, _on_start)
    MON.set_events(TOOL, MON.events.LINE | MON.events.PY_START)
    sys.addaudithook(_audit)


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_protocol(item, nextitem):
    """Run every test in a forked child so caches filled by earlier tests do not hide lines."""
    from _pytest.runner import runtestprotocol

    pid = os.fork()
    if pid == 0:
        _lines.clear()
        _opens.clear()
        _classes.clear()
        _current["key"] = item.nodeid
        os.environ["MAP_CHILD_TEST"] = item.nodeid
        MON.restart_events()
        ok = True
        try:
            reports = runtestprotocol(item, nextitem=None, log=False)
            ok = all(not r.failed for r in reports)
        except BaseException:
            ok = False
        MON.set_events(TOOL, 0)
        data = {
            "lines": {k: {f: sorted(v) for f, v in b.items()} for k, b in _lines.items()},
            "opens": {k: sorted(v) for k, v in _opens.items()},
            "classes": {k: sorted(v) for k, v in _classes.items()},
            "ok": {item.nodeid: ok},
        }
        import hashlib

        h = hashlib.sha1(item.nodeid.encode()).hexdigest()[:16]
        with open(os.path.join(OUT, f"map-t{h}.json"), "w") as fh:
            json.dump(data, fh)
        os._exit(0)
    os.waitpid(pid, 0)
    return True


def pytest_unconfigure(config):
    MON.set_events(TOOL, 0)
    wid = os.environ.get("PYTEST_XDIST_WORKER", "main")
    os.makedirs(OUT, exist_ok=True)
    data = {
        "lines": {k: {f: sorted(v) for f, v in b.items()} for k, b in _lines.items()},
        "opens": {k: sorted(v) for k, v in _opens.items()},
        "classes": {k: sorted(v) for k, v in _classes.items()},
    }
    with open(os.path.join(OUT, f"map-{wid}.json"), "w") as fh:
        json.dump(data, fh)
