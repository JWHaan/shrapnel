"""Build optional call evidence, never from patches, test failures or answer files.

From the entry directory, with Python 3.13 and the pinned Git object available:

    stage=$(mktemp -d) && mkdir "$stage/out"
    git -C ../sqlglot-src archive eb11aec4be8cb2122afe713a4816662c3f0fb2fa > "$stage/pin.tar"
    python3 tools/build_calls.py --plan maps/map.json.gz sample/tests.json "$stage/plan.json"
    docker build --platform linux/amd64 -t br-map -f tools/Dockerfile.map .
    docker run --rm --platform linux/amd64 --cpus=1 --memory=3g --network=none \
      -e SKIP_INTEGRATION=0 -e PYTHONPATH=/src:/tools -e MAP_ROOT=/src -e MAP_OUT=/out -e MAP_CALLS=1 \
      -e MAP_CALL_PLAN=/plan.json -v "$stage/plan.json:/plan.json:ro" -v "$stage/pin.tar:/pin.tar:ro" \
      -v "$PWD/tools:/tools:ro" -v "$stage/out:/out" br-map \
      sh -ec 'mkdir /src && tar xf /pin.tar -C /src && cd /src && python -m pytest -q -p map_plugin -p no:cacheprovider tests'
    python3 tools/build_calls.py "$stage/out" sample/tests.json "$stage/pin.tar" "$stage/calls.json.gz"

Review counts before copying the new sidecar alongside map.json.gz. Test IDs
are bound to the ordered inventory; unprofiled tests retain file/class coverage.
"""
import ast
import gzip
import hashlib
import json
import subprocess
import sys
import tarfile
from collections import Counter, defaultdict
from pathlib import Path

PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"


def closure_line(node):
    """A factory that cannot inspect its arguments except from its returned function."""
    body = node.body
    if node.decorator_list:
        return None
    if len(body) == 1 and isinstance(body[0], ast.Return) and isinstance(body[0].value, ast.Lambda):
        inner = body[0].value
    elif (len(body) == 2 and isinstance(body[0], ast.FunctionDef) and not body[0].decorator_list
          and not body[0].type_params and isinstance(body[1], ast.Return)
          and isinstance(body[1].value, ast.Name) and body[1].value.id == body[0].name):
        inner = body[0]
    else:
        return None
    if not inner.args.defaults and not any(inner.args.kw_defaults):
        return inner.lineno


def factory_sites(archive):
    imports, factories, sites = {}, {}, {}
    with archive.open("rb") as fh:
        if subprocess.check_output(["git", "get-tar-commit-id"], stdin=fh).decode().strip() != PIN:
            raise ValueError("expected a git archive of the pinned SQLGlot revision")
    with tarfile.open(archive) as tar:
        for info in tar:
            fn = info.name
            if not (fn.startswith("sqlglot/") and fn.endswith(".py")):
                continue
            tree = ast.parse(tar.extractfile(info).read(), fn)
            mod = fn[:-3].replace("/", ".").removesuffix(".__init__")
            scope = imports[fn] = {}
            bindings = Counter()
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    bindings[node.name] += 1
                elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                    bindings[node.id] += 1
                elif isinstance(node, ast.alias):
                    bindings[node.asname or node.name.split(".")[0]] += 1
            if bindings["*"]:
                continue
            future = any(isinstance(n, ast.ImportFrom) and n.module == "__future__"
                         and any(a.name == "annotations" for a in n.names) for n in tree.body)
            for node in tree.body:
                if isinstance(node, ast.ImportFrom) and not node.level:
                    for name in node.names:
                        alias = name.asname or name.name
                        if bindings[alias] == 1:
                            scope[alias] = (node.module, name.name)
                if isinstance(node, ast.FunctionDef) and bindings[node.name] == 1 and future:
                    target = scope[node.name] = (mod, node.name)
                    line = closure_line(node)
                    if line is not None:
                        factories[target] = f"{fn}:{line}"
            for cls in (n for n in tree.body if isinstance(n, ast.ClassDef)):
                for st in cls.body:
                    if not isinstance(st, (ast.Assign, ast.AnnAssign)) or not isinstance(st.value, ast.Dict):
                        continue
                    for v in st.value.values:
                        if isinstance(v, ast.Call) and isinstance(v.func, ast.Name):
                            sites[f"{fn}:{v.lineno}:{v.col_offset}"] = (fn, v.func.id)
    return {s: factories[t] for s, (fn, name) in sites.items()
            if (t := imports[fn].get(name)) in factories}


def main():
    if sys.argv[1] == "--plan":
        baseline, catalog, out = map(Path, sys.argv[2:])
        with gzip.open(baseline, "rb") as fh:
            base = json.load(fh)
        # Fork isolation pays class fixture setup once per test. Don't recreate
        # pooled integration suites' databases or repeat long stress tests.
        pooled = {base["tests"][int(n)].rsplit("::", 1)[0] for n in base["tasks"]}
        sampled = [r["id"] for r in json.loads(catalog.read_text())["tests"]
                   if r["seconds"] <= 10 and r["id"].rsplit("::", 1)[0] not in pooled]
        with out.open("x") as fh:
            json.dump(sampled, fh)
        print(f"{len(sampled)} tests in bounded call profile")
        return
    reports, catalog, archive, out = map(Path, sys.argv[1:])
    rows = sorted(json.loads(catalog.read_text())["tests"], key=lambda row: row["number"])
    numbers = {row["id"]: row["number"] for row in rows}
    passed = set()
    calls, validated, inspected = (defaultdict(set) for _ in range(3))
    missing = defaultdict(set)
    if not (reports / "calls-main.json").is_file():
        raise ValueError("missing collection report")
    for f in sorted(reports.glob("calls-*.json")):
        data = json.loads(f.read_text())
        for cls, absent in data.get("collect_missing", {}).items():
            missing[cls].update(absent)
        for name, ok in data.get("ok", {}).items():
            if ok and name in numbers:
                passed.add(numbers[name])
        for category, target in [("lines", calls), ("classes", validated), ("opens", inspected)]:
            for key, values in data[category].items():
                num = -1 if key == "<collect>" else numbers.get(key.split("#t")[0])
                if num is not None:
                    sites = (f"{p}:{ln}" for p, ls in values.items() for ln in ls) if category == "lines" else values
                    for site in sites:
                        target[site].add(num)
    data = {k: {s: sorted(v) for s, v in target.items()} for k, target in [
        ("calls", calls), ("validated", validated), ("inspected", inspected)
    ]}
    data["unknown"] = sorted(set(numbers.values()) - passed)
    data["collect_missing"] = {cls: sorted(v) for cls, v in missing.items()}
    data["ids_sha256"] = hashlib.sha256("\n".join(numbers).encode()).hexdigest()
    data["factories"] = factory_sites(archive)
    if not passed or not validated or not calls:
        raise ValueError("empty evidence")
    # No clobbering a known good map when staging or regenerating.
    with out.open("xb") as dest:
        dest.write(gzip.compress(json.dumps(data, sort_keys=True, separators=(",", ":")).encode(), mtime=0))
    print(f"{len(passed)} profiled, {len(data['unknown'])} unknown; "
          f"{len(calls)} call sites, {len(validated)} validated classes, {len(data['factories'])} factory sites")


if __name__ == "__main__":
    main()
