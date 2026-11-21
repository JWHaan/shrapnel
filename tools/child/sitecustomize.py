# Records lines run by child Python processes a test starts (subprocess.run etc.).
import os

_key = os.environ.get("MAP_CHILD_TEST")
if _key and os.environ.get("MAP_OUT"):
    import atexit
    import json
    import sys

    _root = os.environ.get("MAP_ROOT", "/src").rstrip("/") + "/"
    _seen = {}
    M = sys.monitoring

    def _cb(code, line):
        fn = code.co_filename
        if fn.startswith(_root):
            _seen.setdefault(fn[len(_root):], set()).add(line)
        return M.DISABLE

    M.use_tool_id(M.COVERAGE_ID, "child")
    M.register_callback(M.COVERAGE_ID, M.events.LINE, _cb)
    M.set_events(M.COVERAGE_ID, M.events.LINE)

    @atexit.register
    def _dump():
        with open(os.path.join(os.environ["MAP_OUT"], f"child-{os.getpid()}.json"), "w") as fh:
            json.dump({"lines": {_key: {f: sorted(v) for f, v in _seen.items()}}, "opens": {}}, fh)
