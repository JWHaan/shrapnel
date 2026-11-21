"""Prune value-only edits to closed, pinned class dictionary bindings."""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path


# No f-strings, escapes, calls, huge integers, adjacent tokens or comments.
_LITERAL = re.compile(r'''(?:"[^"\\]*"|'[^'\\]*'|True|False|None|0|[1-9][0-9]{0,17})''')


@lru_cache(maxsize=1)
def _files():
    try:
        return json.loads((Path(__file__).parent / "maps/mapping_reads.json").read_text())["files"]
    except (OSError, ValueError, KeyError):
        return {}


def value_only_tests(fn, src, new_src, m, removed=None):
    """Return candidate test numbers, including source readers, or None to fall back.

    Only call for a single edited file in the pinned tree. Every changed
    character must lie in a profiled value slot; in particular, key edits,
    additional executable changes, reordering and unaudited copies fall back.
    """
    record = _files().get(fn)
    if record is None or new_src is None:
        return None
    if removed is not None and not any(
        ln in removed for b in record["bindings"].values() for ln, _, _ in b["slots"]
    ):
        return None  # fast rejection only; still verify all bytes on a match
    if hashlib.sha256(src.encode()).hexdigest() != record["sha256"]:
        return None
    before, after = src.split("\n"), new_src.split("\n")
    if len(before) != len(after):
        return None
    changes = {i: (a, b) for i, (a, b) in enumerate(zip(before, after), 1) if a != b}
    if not changes:
        return None
    reads = set()
    for binding in record["bindings"].values():
        for line, start, end in binding["slots"]:
            if line not in changes:
                continue
            a, b = changes[line]
            tail = len(a) - end
            if b[:start] != a[:start] or not b.endswith(a[end:]) or len(b) <= start + tail:
                continue
            replacement = b[start:len(b) - tail]
            if not (replacement.isascii() and replacement.isprintable() and _LITERAL.fullmatch(replacement)):
                continue
            reads.update(map(tuple, binding["reads"]))
            del changes[line]
    opened = m.opened(fn) or set()
    if changes or -1 in opened:
        return None
    tests = set(opened)
    for path, lo, hi in reads:
        if path not in m.lines or any(lo <= ln <= hi for ln in m.collect.get(path, ())):
            return None
        tests.update(m.tests_on(path, lo, hi))
    return tests
