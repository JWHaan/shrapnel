"""Merge the per-worker mapper output into one compact map committed to the repo.

map.json.gz:
  tests:   list of test ids, index = number from tests.json
  sets:    list of sorted test-number lists (deduplicated)
  lines:   {file: {line: set_index}}       which tests ran each line
  collect: {file: [lines]}                 lines run at collection (import) time
  opens:   {file: set_index}               tests that opened each non-.pyc file
"""
import glob
import os
import gzip
import json
import sys

mapdir, tests_json, out = sys.argv[1:4]
tests = json.load(open(tests_json))["tests"]
num = {t["id"]: t["number"] for t in tests}
ntests = max(num.values()) + 1

bad = []
task_lines = {}
base_lines = {}
ntasks = {}
per_line = {}
collect = {}
opens = {}
classes = {}
for f in glob.glob(f"{mapdir}/map-*.json") + glob.glob(f"{mapdir}/child-*.json"):
    d = json.load(open(f))
    for key, b in d["lines"].items():
        task = None
        if "#t" in key:
            key, task = key.split("#t")
            task = int(task)
            n = num.get(key)
            if n is not None:
                tl = task_lines.setdefault(n, {})
                for fn, ls in b.items():
                    f2 = tl.setdefault(fn, {})
                    for ln in ls:
                        f2.setdefault(ln, set()).add(task)
                ntasks[n] = max(ntasks.get(n, 0), task + 1)
        elif key in num:
            bl = base_lines.setdefault(num[key], {})
            for fn, ls in b.items():
                bl.setdefault(fn, set()).update(ls)
        if key == "<collect>":
            for fn, ls in b.items():
                collect.setdefault(fn, set()).update(ls)
            continue
        n = num.get(key)
        if n is None:
            continue
        for fn, ls in b.items():
            pl = per_line.setdefault(fn, {})
            for ln in ls:
                pl.setdefault(ln, set()).add(n)
    for key, ok in d.get("ok", {}).items():
        if not ok:
            bad.append(key)
    for key, names in d.get("classes", {}).items():
        n = num.get(key.split("#t")[0])
        for c in names:
            classes.setdefault(c, set()).add(-1 if n is None else n)
    for key, files in d["opens"].items():
        n = num.get(key.split("#t")[0])
        for fn in files:
            if "__pycache__" in fn or fn.endswith(".pyc"):
                continue
            if n is None:
                opens.setdefault(fn, set()).add(-1)
            else:
                opens.setdefault(fn, set()).add(n)

sets = []
index = {}


def sid(s):
    key = tuple(sorted(s))
    i = index.get(key)
    if i is None:
        i = index[key] = len(sets)
        sets.append(list(key))
    return i


data = {
    "tests": [None] * ntests,
    "sets": sets,
    "lines": {fn: {str(ln): sid(s) for ln, s in sorted(pl.items())} for fn, pl in per_line.items()},
    "collect": {fn: sorted(ls) for fn, ls in collect.items()},
    "opens": {fn: sid(s) for fn, s in opens.items()},
    "classes": {c: sid(s) for c, s in classes.items()},
    "tasks": {
        str(n): {"count": ntasks[n], "base": {fn: sorted(v) for fn, v in base_lines.get(n, {}).items()}, "lines": {fn: {str(ln): sid(ts) for ln, ts in sorted(m.items())} for fn, m in tl.items()}}
        for n, tl in task_lines.items()
    },
}
for t in tests:
    data["tests"][t["number"]] = t["id"]

# attribute index: NAME -> [[file, lo, hi, safe]] for every reference in sqlglot/ and tests/
if len(sys.argv) > 4:
    import ast
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    import blast

    src_root = Path(sys.argv[4])
    DUNDER = {"__new__", "__init_subclass__", "__set_name__", "__class_getitem__", "__prepare__"}
    attrs = {}
    for path in list((src_root / "sqlglot").rglob("*.py")) + list((src_root / "tests").rglob("*.py")):
        rel = str(path.relative_to(src_root))
        try:
            text = path.read_text(encoding="utf-8")
            sc = blast.Scopes(text)
        except Exception:
            continue
        for node in ast.walk(sc.tree):
            name = None
            if isinstance(node, (ast.Attribute, ast.Name)) and not isinstance(node.ctx, ast.Load):
                continue  # definitions are what changed, not readers
            if isinstance(node, ast.Attribute):
                name = node.attr
            elif isinstance(node, ast.Name):
                name = node.id
            elif isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value.isidentifier():
                name = node.value
                ln = node.lineno
                attrs.setdefault(name, []).append([rel, ln, ln, 0])
                continue
            if name is None or not name[:1].isupper() and not name.startswith("_"):
                if name not in ("arg_types",):
                    continue
            ln = node.lineno
            st = sc.stmt[ln] if ln < len(sc.stmt) else None
            func = sc.func[ln] if ln < len(sc.func) else None
            cls = sc.cls[ln] if ln < len(sc.cls) else None
            safe = 1
            if func is None and cls is not None and st is not None and st in cls.body and isinstance(st, (ast.Assign, ast.AnnAssign)):
                # class-body copy such as X = {**Parent.X, ...}: only matters for that class
                tg = st.targets if isinstance(st, ast.Assign) else [st.target]
                if all(isinstance(x, ast.Name) and x.id == name for x in tg):
                    via = ""
                    if isinstance(node, ast.Attribute):
                        v = node.value
                        via = v.attr if isinstance(v, ast.Attribute) else (v.id if isinstance(v, ast.Name) else "?")
                    mod = blast.module_name(rel)
                    attrs.setdefault(name, []).append([rel, ln, ln, 2, f"{mod}:{sc.qualname(cls)}", via])
                    continue
            if st is None or func is None or ln < func.body[0].lineno or func.name in DUNDER:
                safe = 0
                lo = hi = ln
            else:
                lo, hi = sc.header_range(st)
                if not (lo <= ln <= hi):
                    lo, hi = ln, ln
                if any(l in collect.get(rel, ()) for l in range(lo, hi + 1)):
                    safe = 3
            attrs.setdefault(name, []).append([rel, lo, hi, safe])
    data["attrs"] = attrs
# comparison operand evidence (optional): $CMP_DIR holds cmp-*.json from cmp_plugin
cmp_dir = os.environ.get("CMP_DIR")
if cmp_dir:
    observed, disagree, overflow, pairs, badc = {}, {}, {}, {}, []
    for f in glob.glob(f"{cmp_dir}/cmp-*.json"):
        d = json.load(open(f))
        n = num.get(d["test"])
        if n is None:
            continue
        if not d["ok"]:
            badc.append(n)
            continue
        for site, (agree, prs) in d["obs"].items():
            observed.setdefault(site, set()).add(n)
            if not agree:
                disagree.setdefault(site, set()).add(n)
            if prs is None:
                overflow.setdefault(site, set()).add(n)
            else:
                pairs.setdefault(site, {}).setdefault(json.dumps(prs), set()).add(n)
    done = {num[t] for t in num} - set(badc)
    data["cmp"] = {
        "done": sid(done),
        "sites": {
            site: [sid(obs), sid(disagree.get(site, ())), sid(overflow.get(site, ())),
                   [[json.loads(k), sid(v)] for k, v in pairs.get(site, {}).items()]]
            for site, obs in observed.items()
        },
    }
    print(f"cmp sites={len(observed)} unprofiled tests={len(badc)}", file=sys.stderr)

with gzip.open(out, "wt") as fh:
    json.dump(data, fh, separators=(",", ":"))
print("tests not passing under the mapper:", bad, file=sys.stderr)
print(f"files={len(per_line)} sets={len(sets)} opens={len(opens)}", file=sys.stderr)
