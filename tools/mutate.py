"""Generate synthetic patches against pinned sqlglot, shaped like the sample kinds."""
import difflib
import random
import re
import sys
import ast
from pathlib import Path

root = Path(sys.argv[1])
out = Path(sys.argv[2])
n = int(sys.argv[3])
seed = int(sys.argv[4]) if len(sys.argv) > 4 else 1
rng = random.Random(seed)
out.mkdir(parents=True, exist_ok=True)

pyfiles = [p for p in (root / "sqlglot").rglob("*.py") if "__pycache__" not in p.parts and p.stat().st_size > 200]
fixtures = [p for p in (root / "tests/fixtures/optimizer").glob("*.sql")]

SWAPS = [
    (r" == ", " != "), (r" != ", " == "), (r" is not ", " is "), (r" is None", " is not None"),
    (r" and ", " or "), (r" or ", " and "), (r" < ", " <= "), (r" > ", " >= "), (r" <= ", " < "),
    (r" >= ", " > "), (r" \+ 1", " + 2"), (r" - 1", " - 2"), (r"\bTrue\b", "False"), (r"\bFalse\b", "True"),
    (r"\bnot ", ""), (r" in ", " not in "), (r"\[0\]", "[-1]"), (r"\[1:\]", "[2:]"),
]


def diff(rel, a, b):
    return "".join(
        [f"diff --git a/{rel} b/{rel}\n"]
        + list(difflib.unified_diff(a.splitlines(True), b.splitlines(True), f"a/{rel}", f"b/{rel}", n=3))
    )


def mutate_code():
    for _ in range(200):
        p = rng.choice(pyfiles)
        src = p.read_text()
        lines = src.splitlines(True)
        i = rng.randrange(len(lines))
        ln = lines[i]
        if ln.strip().startswith(("#", "import", "from", '"""', "def ", "class ", "@")):
            continue
        opts = [(a, b) for a, b in SWAPS if re.search(a, ln)]
        num = list(re.finditer(r"(?<![\w.])(\d+)(?![\w.])", ln))
        strs = list(re.finditer(r'"([A-Z_]{2,})"', ln))
        choice = rng.random()
        new = None
        if opts and choice < 0.7:
            a, b = rng.choice(opts)
            ms = list(re.finditer(a, ln))
            m = rng.choice(ms)
            new = ln[: m.start()] + re.sub(a, b, m.group(0)) + ln[m.end():]
        elif num and choice < 0.85:
            m = rng.choice(num)
            new = ln[: m.start()] + str(int(m.group(1)) + 1) + ln[m.end():]
        elif strs:
            m = rng.choice(strs)
            new = ln[: m.start(1)] + m.group(1)[:-1] + ln[m.end(1):]
        if not new or new == ln:
            continue
        b = "".join(lines[:i] + [new] + lines[i + 1:])
        try:
            ast.parse(b)
        except SyntaxError:
            continue
        return "code", diff(str(p.relative_to(root)), src, b)
    raise RuntimeError


def mutate_comment():
    p = rng.choice(pyfiles)
    src = p.read_text()
    lines = src.splitlines(True)
    for _ in range(100):
        i = rng.randrange(1, len(lines))
        prev = lines[i - 1]
        if prev.rstrip().endswith((",", "(", "[", "{", "\\")) or '"""' in prev:
            continue
        ind = re.match(r"\s*", lines[i]).group(0)
        if not lines[i].strip():
            continue
        b = "".join(lines[:i] + [f"{ind}# {rng.choice(['Cheap check first', 'TODO: revisit', 'Keep in sync with parser', 'Note: order matters'])}\n"] + lines[i:])
        try:
            if ast.dump(ast.parse(b)) != ast.dump(ast.parse(src)):
                continue
        except SyntaxError:
            continue
        return "comment", diff(str(p.relative_to(root)), src, b)
    raise RuntimeError


def mutate_fixture():
    p = rng.choice(fixtures)
    src = p.read_text()
    lines = src.splitlines(True)
    for _ in range(100):
        i = rng.randrange(len(lines))
        ln = lines[i]
        if not ln.strip() or ln.startswith("#"):
            continue
        m = list(re.finditer(r"\b(\d+|AND|OR|SELECT|AS|x|y|a|b)\b", ln))
        if not m:
            continue
        mm = rng.choice(m)
        rep = {"AND": "OR", "OR": "AND", "SELECT": "SELECT DISTINCT", "AS": "AS", "x": "y", "y": "x", "a": "b", "b": "a"}.get(mm.group(1), str(int(mm.group(1)) + 1) if mm.group(1).isdigit() else "z")
        new = ln[: mm.start()] + rep + ln[mm.end():]
        if new == ln:
            continue
        return "fixture", diff(str(p.relative_to(root)), src, "".join(lines[:i] + [new] + lines[i + 1:]))
    raise RuntimeError


kinds = [mutate_code] * 6 + [mutate_comment] * 2 + [mutate_fixture] * 2
for k in range(n):
    f = rng.choice(kinds)
    try:
        kind, patch = f()
        if rng.random() < 0.15:  # multi
            kind2, p2 = rng.choice([mutate_comment, mutate_fixture])()
            if p2.split("\n")[0] != patch.split("\n")[0]:
                patch += p2
                kind = "multi"
    except RuntimeError:
        continue
    (out / f"m{seed}x{k:03d}.patch").write_text(patch)
    print(f"m{seed}x{k:03d}", kind)
