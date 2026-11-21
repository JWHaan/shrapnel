# Blast Radius entry

Picks the sqlglot tests a patch can break, then checks the cheap ones for real.

## How it works

1. **Maps, built once from the pinned commit** (`maps/`, made by `tools/`):
   - per-test line coverage, recorded with `sys.monitoring` while every test runs in its own
     forked process (so caches filled by earlier tests cannot hide lines), with the tests'
     process pools run inline and pickled the same way, and child Python processes recorded too;
   - per pool task coverage for the big fixture-driven tests (TPC-DS, simplify, ...);
   - which sqlglot classes each test uses (the MRO of every `self`/`cls` a method runs with);
   - which data files each test opens; which lines read each class attribute;
   - each test's CPU cost when run in-process.
2. **For each patch** (`blast.py`): apply it in pure Python, compare ASTs (comment-only edits
   drop out), map each changed statement to the tests that run it. Class-body edits go to the
   tests that use that class *and* run a line reading that attribute, falling back to every user
   of the class when the attribute is read at import time.
3. **Runtime check** (`selector.py`): when it is cheaper than scheduling them blind, run the
   candidate tests against the patched tree (a symlink overlay in `/tmp`, forked from a process
   with pytest/duckdb/pandas already imported) and keep only the failures. Heavy pooled tests run
   only the tasks that reach the change. Anything that times out, crashes or does not report is
   scheduled anyway.
4. Pack the chosen tests onto 1–8 workers (longest first, best worker count).

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
