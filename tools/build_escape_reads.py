"""Index the audited NUMERIC_ESCAPES declarations, not patches or failures.

From entry/: python3 tools/build_escape_reads.py ../work/validation/pin.tar \
    ../mapout2 maps/map.json.gz maps/escape_reads.json

Receipts identify the exact audited archive (eb11aec4be8cb2122afe713a4816662c3f0fb2fa)
and complete line-profile run, including its collection, pool and child evidence.
A different run needs a new audit; do not silently bless a partial directory.
"""
import ast
import gzip
import hashlib
import json
import sys
import tarfile
from itertools import accumulate
from pathlib import Path

ARCHIVE = "578d90b1a8e1130094aaf0746117a596e6d94b63c9e4d38aa74f0fea61a37395"
REPORTS = "74479f9054c6f06de47076b59ecb88a9e1cd53f180f41999c1c686372c6e173c"
BASE = "ca12eea7fab6d73c7c50aedb054ef6d66a3f59bc89990a3a016ea9fedc5e2ad3"
CORE = "sqlglot/tokenizer_core.py"


def build(archive, reports, baseline):
    compressed = baseline.read_bytes()
    if hashlib.sha256(compressed).hexdigest() != BASE:
        raise ValueError("unaudited baseline map")
    m = json.loads(gzip.decompress(compressed))
    ids = m["tests"]
    numbers = {name: n for n, name in enumerate(ids)}
    files = {}
    with archive.open("rb") as fh:
        if hashlib.file_digest(fh, "sha256").hexdigest() != ARCHIVE:
            raise ValueError("unaudited source archive")
        fh.seek(0)
        with tarfile.open(fileobj=fh) as tar:
            reads = sorted(n.lineno for n in ast.walk(ast.parse(tar.extractfile(CORE).read()))
                           if isinstance(n, ast.Call) and ast.unparse(n.func) == "self.numeric_escapes.get")
            for member in tar:
                if not member.name.startswith("sqlglot/dialects/") or not member.name.endswith(".py"):
                    continue
                raw = tar.extractfile(member).read()
                if b"NUMERIC_ESCAPES = {" not in raw:
                    continue
                lines = raw.decode().splitlines(keepends=True)
                offsets = list(accumulate(map(len, lines), initial=0))
                slots = {}
                for cls in ast.walk(ast.parse(raw)):
                    if not isinstance(cls, ast.ClassDef) or cls.name != "Tokenizer":
                        continue
                    for s in cls.body:
                        if not (isinstance(s, ast.Assign) and len(s.targets) == 1
                                and ast.unparse(s.targets[0]) == "NUMERIC_ESCAPES" and isinstance(s.value, ast.Dict)):
                            continue
                        for key, v in zip(s.value.keys, s.value.values):
                            if not (isinstance(key, ast.Constant) and type(key.value) is str
                                    and isinstance(v, ast.Tuple) and len(v.elts) == 4):
                                raise ValueError("unexpected dictionary shape")
                            for p in v.elts:
                                if not (isinstance(p, ast.Constant) and type(p.value) is int
                                        and lines[p.lineno - 1].isascii()):
                                    raise ValueError("not an integer literal slot")
                                start = offsets[p.lineno - 1]
                                slots.setdefault(p.lineno, []).append([start + p.col_offset, start + p.end_col_offset])
                if slots:
                    files[member.name] = [hashlib.sha256(raw).hexdigest(), slots]

    receipt = hashlib.sha256()
    passed, children, observed = set(), set(), set()
    for f in sorted([*reports.glob("map-*.json"), *reports.glob("child-*.json")]):
        raw = f.read_bytes()
        receipt.update(f.name.encode() + b"\0" + hashlib.sha256(raw).digest())
        d = json.loads(raw)
        passed.update(numbers[k] for k, ok in d.get("ok", {}).items() if ok and k in numbers and d["lines"].get(k))
        for key, paths in d["lines"].items():
            n = numbers.get(key.split("#t")[0])
            hit = bool(set(reads).intersection(paths.get(CORE, ())))
            if hit and key == "<collect>":
                raise ValueError("collection-time value read")
            if n is not None:
                if hit:
                    observed.add(n)
                if f.name.startswith("child-"):
                    children.add(n)
    if receipt.hexdigest() != REPORTS or len(passed) != len(ids):
        raise ValueError("missing or unaudited raw reports")
    if observed != {i for n in reads if (s := m["lines"][CORE].get(str(n))) is not None for i in m["sets"][s]}:
        raise ValueError("raw and merged reader coverage disagree")
    source_readers = {i for fn in files if (s := m["opens"].get(fn)) is not None for i in m["sets"][s]}
    return {"version": 1, "archive_sha256": ARCHIVE, "reports_sha256": REPORTS, "map_sha256": BASE,
            "ids_sha256": hashlib.sha256("\n".join(ids).encode()).hexdigest(),
            "coverage_sha256": hashlib.sha256(json.dumps(sorted(observed | children | source_readers)).encode()).hexdigest(),
            "files": files, "reads": reads, "raw_children_set": m["sets"].index(sorted(children))}


if __name__ == "__main__":
    archive, reports, baseline, output = map(Path, sys.argv[1:])
    evidence = build(archive, reports, baseline)
    output.write_text(json.dumps(evidence, sort_keys=True, separators=(",", ":")) + "\n")
