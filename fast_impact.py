"""Run the existing impact algorithm on a position-preserving slice, or not at all.

Build with `python fast_impact.py /opt/sqlglot`. The index describes pristine
source, never patches or test outcomes. An absent index leaves impact unchanged.
"""
from __future__ import annotations

import ast
import hashlib
import json
import keyword
import sys
from collections import Counter
from functools import lru_cache
from pathlib import Path

INDEX = Path(__file__).with_name("fast-index")
FUNCTION = (ast.FunctionDef, ast.AsyncFunctionDef)
DEFINITION = (*FUNCTION, ast.ClassDef)
FORMAT = (sys.implementation.cache_tag, 2)


def _digest(data):
    return hashlib.sha256(data).hexdigest()


def _semantic(tree):
    # Bytecode equality is not sufficient: e.g. 1 + 1 and 2 compile alike.
    return _digest(ast.dump(tree).encode())


def _start(n):
    return n.lineno, n.col_offset


def _end(n):
    return n.end_lineno, n.end_col_offset


def _bounds(n):
    return (*_start(n), *_end(n))


def _regions(tree):
    names = Counter()

    def count(node, scope):
        if isinstance(node, DEFINITION):
            scope += (node.name,)
            names[scope] += 1
        for ch in ast.iter_child_nodes(node):
            count(ch, scope)

    count(tree, ())
    result = []

    def visit(parent, scope, path):
        for i, node in enumerate(parent.body):
            slot = path + (i,)
            if isinstance(node, DEFINITION):
                name = scope + (node.name,)
                if names[name] != 1:
                    continue
                if isinstance(node, ast.ClassDef):
                    visit(node, name, slot)
                elif node.body[0].lineno > node.lineno and not any(
                    isinstance(c, DEFINITION) for stmt in node.body for c in ast.walk(stmt)
                ):
                    result.append((slot, _bounds(node), None, (*_start(node.body[0]), *_end(node))))
            elif isinstance(node, (ast.Assign, ast.AnnAssign)):
                for lam in ast.walk(node):
                    if isinstance(lam, ast.Lambda) and lam.lineno < lam.body.end_lineno:
                        result.append((slot, _bounds(node), _start(lam), _bounds(lam.body)))

    visit(tree, (), ())
    return result


def build(root, dest=INDEX):
    dest.mkdir(parents=True, exist_ok=True)
    for folder in ("sqlglot", "tests"):
        for p in (root / folder).rglob("*.py"):
            src = p.read_text(encoding="utf-8")
            try:
                tree = ast.parse(src)
            except SyntaxError:
                continue
            # A standalone comment may be data inside a multiline literal
            # (including an f-string debug expression) or break a continuation.
            protected = {i for i, line in enumerate(src.split("\n"), 1) if line.endswith("\\")}
            for n in ast.walk(tree):
                if isinstance(n, (ast.Constant, ast.JoinedStr)):
                    protected.update(range(n.lineno, n.end_lineno))
            digest = _digest(src.encode())
            (dest / digest).write_text(json.dumps(
                [FORMAT, digest, _semantic(tree), _regions(tree), sorted(protected)], separators=(",", ":")
            ))


@lru_cache(maxsize=32)
def _load(path):
    try:
        tag, digest, semantic, records, protected = json.loads(path.read_bytes())
        if tuple(tag) == FORMAT and digest == path.name:
            return semantic, records, frozenset(protected)
    except (OSError, TypeError, ValueError):
        return None


def _only_comments(src, new_src, inserts, protected):
    if (not src.endswith("\n") or not new_src.endswith("\n")
            or inserts != sorted(inserts) or any(i < 0 or i in protected for i in inserts)):
        return False
    lines = new_src.split("\n")
    for j in range(len(inserts) - 1, -1, -1):
        i = inserts[j] + j
        if not 0 <= i < len(lines):
            return False
        line = lines.pop(i).lstrip(" \t")
        if not (not line or (line.startswith("#") and line.isascii() and line.isprintable())):
            return False
    return "\n".join(lines) == src


def _outside_equal(a, b, lo, col, hi, end, new):
    nlo, ncol, nhi, nend = new
    # AST columns are UTF-8 byte offsets, not Python string offsets.
    return (lo == nlo and hi == nhi and a[:lo-1] == b[:lo-1] and a[hi:] == b[hi:]
            and a[lo-1][:col] == b[lo-1][:ncol] and a[hi-1][end:] == b[hi-1][nend:])


def _slice(lines, pos, classes):
    lo, col, hi, end = pos
    # Do not swallow unrelated statements on a shared physical line.
    if lines[lo-1][:col].strip() or lines[hi-1][end:].strip().split(b"#", 1)[0]:
        return None
    prefix = [b""] * (lo - 1)
    for cls in classes:
        if keyword.iskeyword(cls.name):
            return None
        prefix[cls.lineno-1] = lines[cls.lineno-1][:cls.col_offset] + b"class " + cls.name.encode() + b":"
    return (b"\n".join(prefix + lines[lo-1:hi]) + b"\n").decode()


def narrow_sources(src, new_src, fd, index=INDEX):
    """Return smaller equivalent inputs to blast.py_impact, or None for fallback."""
    if new_src is None or fd.binary or fd.old != fd.new:
        return None
    data = src.encode()
    info = _load(index / _digest(data))
    if info is None:
        return None
    semantic, regions, protected = info
    if fd.inserts and not fd.removed and _only_comments(src, new_src, fd.inserts, protected):
        return "pass\n", "pass\n"
    same_lines = src.count("\n") == new_src.count("\n")
    candidates = []
    if fd.removed and not fd.inserts and same_lines:
        lo, hi = min(fd.removed), max(fd.removed)
        candidates = [r for r in regions if r[3][0] <= lo <= hi <= r[3][2] and (not r[2] or lo > r[2][0])]
    if not candidates and same_lines and not fd.inserts and len(fd.removed) == 1:
        i = fd.removed[0] - 1
        # Avoid an extra module parse/dump on ordinary definition-time edits.
        # This is only a cost filter; equality is always checked on both ASTs.
        if i < 0 or i > src.count("\n"):
            return None
        x, y = (s.split("\n")[i].partition("#")[0].split() for s in (src, new_src))
        if "".join(x) != "".join(y):
            return None
    try:
        patched = ast.parse(new_src)
    except (ValueError, SyntaxError):
        return None  # Preserve full-module import/collection failure handling.
    if candidates:
        a, b = data.split(b"\n"), new_src.encode().split(b"\n")
        for path, pos, lam, bounds in candidates:
            node = patched
            classes = []
            for i in path:
                if isinstance(node, ast.ClassDef):
                    classes.append(node)
                body = getattr(node, "body", None)
                if not isinstance(body, list) or i >= len(body):
                    break
                node = body[i]
            else:
                if lam is None and isinstance(node, FUNCTION):
                    if any(isinstance(c, DEFINITION) for st in node.body for c in ast.walk(st)):
                        continue
                    edited = (*_start(node.body[0]), *_end(node))
                elif lam and isinstance(node, (ast.Assign, ast.AnnAssign)):
                    target = next((c for c in ast.walk(node) if isinstance(c, ast.Lambda)
                                   and _start(c) == tuple(lam)), None)
                    if target is None:
                        continue
                    edited = _bounds(target.body)
                else:
                    continue
                # Both parsers must enclose the edit in the same body. This
                # rejects valid Python that ends a lambda early or dedents.
                if not _outside_equal(a, b, *bounds, edited):
                    continue
                old, new = _slice(a, pos, classes), _slice(b, _bounds(node), classes)
                if old is not None and new is not None:
                    return old, new
    if _semantic(patched) == semantic:
        # Keep the legacy AST-unchanged path, including tests opening the text.
        return "pass\n", "pass\n"
    return None


if __name__ == "__main__":
    build(Path(sys.argv[1]), Path(sys.argv[2]) if len(sys.argv) > 2 else INDEX)
