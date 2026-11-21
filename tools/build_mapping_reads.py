"""Index value-sensitive reads of closed class dictionaries on the pinned source.

python3 tools/build_mapping_reads.py ../sqlglot-src maps/map.json.gz maps/mapping_reads.json

Uses the existing baseline line/collection profile; does not import a patched
module, execute tests, consume patches or use failure labels.
"""
import ast
import gzip
import hashlib
import io
import json
import subprocess
import sys
import tarfile
from collections import defaultdict
from pathlib import Path


PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
# Audited uppercase config families. In particular, arg_types creates a
# second lookup table in __init_subclass__, and TYPE_MAPPING can escape.
NAMES = {
    "BRACKET_OFFSETS", "EXTRACT_EPOCH_MAPPINGS",
    "EXTRACT_STRFTIME_MAPPINGS", "STRING_TYPE_MAPPING", "TIME_PART_SINGULARS",
    "UNABBREVIATED_UNIT_NAME", "UNSIGNED_TYPE_MAPPING",
}


def binding_name(st):
    if isinstance(st, ast.Assign) and len(st.targets) == 1:
        target = st.targets[0]
    elif isinstance(st, ast.AnnAssign):
        target = st.target
    else:
        return None
    return target.id if isinstance(target, ast.Name) else None


def slots(value, lines):
    if isinstance(value, ast.Tuple):
        for element in value.elts:
            yield from slots(element, lines)
    elif (isinstance(value, ast.Constant) and type(value.value) in (str, int, bool, type(None))
          and value.lineno == value.end_lineno and lines[value.lineno - 1].isascii()):
        yield [value.lineno, value.col_offset, value.end_col_offset]
    else:
        raise ValueError("not a single-line immutable literal")


def use(node, parent):
    """Classify a direct use; never follow a dict passed by reference."""
    p = parent[node]
    if (isinstance(p, ast.Compare) and len(p.ops) == 1 and p.comparators[0] is node
            and isinstance(p.ops[0], (ast.In, ast.NotIn))):
        return "keys"
    if (isinstance(p, ast.For) and p.iter is node
            or isinstance(p, (ast.If, ast.While)) and p.test is node):
        return "keys"
    if isinstance(p, ast.Subscript) and p.value is node and isinstance(p.ctx, ast.Load):
        return "value"
    if isinstance(p, ast.Attribute) and isinstance(call := parent[p], ast.Call) and call.func is p:
        if p.attr == "get":
            return "value"
        if isinstance(loop := parent[call], ast.For) and loop.iter is call:
            if p.attr in {"items", "values"}:
                return "value"
            if p.attr == "keys":
                return "keys"
    return "unknown"


def build(src, base):
    declarations = {}
    uses = defaultdict(list)
    digests = {}
    tar = subprocess.check_output(["git", "-C", src, "archive", PIN, "sqlglot", "tests"])
    with tarfile.open(fileobj=io.BytesIO(tar)) as archive:
        for member in archive:
            fn = member.name
            if not member.isfile() or not fn.endswith(".py"):
                continue
            text = archive.extractfile(member).read().decode()
            names = {name for name in NAMES if name in text}
            if not names:
                continue
            digests[fn] = hashlib.sha256(text.encode()).hexdigest()
            lines = text.split("\n")
            tree = ast.parse(text)
            parent = {child: n for n in ast.walk(tree) for child in ast.iter_child_nodes(n)}
            local_defs = {}
            for cls in tree.body:
                if not isinstance(cls, ast.ClassDef):
                    continue
                for st in cls.body:
                    name = binding_name(st)
                    if name not in names:
                        continue
                    origin = (fn, cls.name, name)
                    local_defs[cls, name] = st
                    d = st.value
                    if (not fn.startswith("sqlglot/") or not isinstance(d, ast.Dict)
                            or not d.keys or None in d.keys):
                        continue
                    try:
                        declarations[origin] = [slot for v in d.values for slot in slots(v, lines)]
                    except ValueError:
                        pass

            for n in ast.walk(tree):
                if isinstance(n, ast.Constant) and isinstance(n.value, str):
                    for name in names:
                        if name in n.value:
                            uses[name].append((None, "unknown", None))
                    continue
                if isinstance(n, ast.Name):
                    name = n.id
                elif isinstance(n, ast.Attribute):
                    name = n.attr
                else:
                    continue
                if name not in names:
                    continue
                st = n
                while not isinstance(st, ast.stmt):
                    st = parent[st]
                cls = parent[st]
                origin = None
                if isinstance(n, ast.Name) and isinstance(cls, ast.ClassDef):
                    local = local_defs.get((cls, name))
                    if local is st and isinstance(n.ctx, ast.Store):
                        continue  # declaration, not an access
                    if local is not None and local.lineno < st.lineno:
                        # A bare name in a class body resolves to its *own*
                        # binding, not an identically named dictionary elsewhere.
                        origin = (fn, cls.name, name)
                scope = cls
                while not isinstance(scope, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                    scope = parent[scope]
                if (isinstance(n, ast.Attribute) and isinstance(n.ctx, ast.Load)
                        and isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef))
                        and scope.body[0].lineno <= n.lineno):
                    kind = use(n, parent)
                else:
                    kind = "unknown"
                site = (fn, st.lineno, st.end_lineno)
                uses[name].append((origin, kind, site))

    files = {}
    for origin, points in sorted(declarations.items()):
        fn, cls, name = origin
        relevant = [(kind, site) for owner, kind, site in uses[name] if owner in (None, origin)]
        kinds = {k for k, _ in relevant}
        if "unknown" in kinds or "keys" not in kinds:
            continue
        reads = sorted({site for k, site in relevant if k == "value"})
        if any(any(lo <= ln <= hi for ln in base["collect"].get(f, [])) for f, lo, hi in reads):
            continue  # a value read during collection could affect all tests
        record = files.setdefault(fn, {"sha256": digests[fn], "bindings": {}})
        record["bindings"][f"{cls}.{name}"] = {"slots": points, "reads": reads}
    return {"pin": PIN, "files": files}


if __name__ == "__main__":
    root, coverage, out = sys.argv[1:]
    with gzip.open(coverage, "rt") as handle:
        index = build(root, json.load(handle))
    Path(out).write_text(json.dumps(index, separators=(",", ":"), sort_keys=True) + "\n")
    print(f"{sum(len(f['bindings']) for f in index['files'].values())} closed bindings")
