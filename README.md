# Blast Radius entry

Schedules SQLGlot tests using evidence from the pinned revision. The runtime
uses the Python standard library and does not execute or import patched code.

## How it works

1. **Maps, built once from the pinned commit** (`maps/`, made by `tools/`):
   - per-test line coverage, recorded with `sys.monitoring` while every test runs in its own
     forked process (so caches filled by earlier tests cannot hide lines), with the tests'
     process pools run inline and pickled the same way, and child Python processes recorded too;
   - per pool task coverage for the big fixture-driven tests (TPC-DS, simplify, ...);
   - which sqlglot classes each test uses (the MRO of every `self`/`cls` a method runs with);
   - which data files each test opens; which lines read each class attribute;
   - each test's CPU cost when run in-process;
   - comparison operands (`tools/cmp_plugin.py`): every single-operator comparison in sqlglot is
     rewritten to record, per test, whether `is` and `==` agreed and up to 24 primitive operand
     pairs. When a patch's only change is one comparison (operator swap or compared constant),
     tests whose recorded operands give the same result before and after are dropped.
2. **For each patch** (`blast.py`): apply it in pure Python, compare ASTs (comment-only edits
   drop out), map each changed statement to the tests that run it. Class-body edits go to the
   tests that use that class *and* run a line reading that attribute, falling back to every user
   of the class when the attribute is read at import time.
3. **Call evidence** (`tools/build_calls.py`): distinguish a lambda or closure being
   defined from its body being invoked. For a boolean required-argument edit, track
   validation of that expression class. Unprofiled tests keep broader coverage.
4. **Scheduling** (`selector.py`): compare up to eight worker counts in whole milliseconds,
   balance uneven loads and add extra tests only within the winning schedule's spare
   capacity. Unsupported patch formats, syntax errors and analysis exceptions schedule
   the full inventory. Only patches and the test manifest are mounted at runtime.

## Rebuilding the maps

```sh
docker build --platform linux/amd64 -t br-map -f tools/Dockerfile.map .
docker run --rm --platform linux/amd64 -e SKIP_INTEGRATION=0 \
  -e PYTHONPATH=/src:/tools:/tools/child -e MAP_ROOT=/src -e MAP_OUT=/out \
  -v "$PWD/../sqlglot-src":/src -v "$PWD/tools":/tools:ro -v "$PWD/../mapout":/out -w /src \
  br-map python -m pytest -q -p map_plugin -p no:cacheprovider -n 8 tests
python3 tools/build_map.py ../mapout sample/tests.json maps/map.json.gz ../sqlglot-src
```

`make sample` scores the 30 sample commits locally.

See `tools/build_calls.py` for the call-profile build from a clean pinned Git
archive, and `PROGRESS.md` for measurements and validation limits.
