"""Map a unified diff against pinned sqlglot to the tests that could notice it."""
from __future__ import annotations

import ast
import gzip
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


@dataclass
class FileDiff:
    old: str | None
    new: str | None
    removed: list[int] = field(default_factory=list)  # old line numbers removed/changed
    inserts: list[int] = field(default_factory=list)  # old line after which new lines appear (0 = top)
    binary: bool = False


def _strip(p: str) -> str | None:
    p = p.split("\t")[0].strip()
    if p == "/dev/null":
        return None
    if p.startswith(("a/", "b/")):
        p = p[2:]
    return p


def parse_patch(text: str) -> list[FileDiff]:
    files: list[FileDiff] = []
    cur: FileDiff | None = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            cur = FileDiff(m.group(1) if m else None, m.group(2) if m else None)
            files.append(cur)
            i += 1
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            old, new = _strip(line[4:]), _strip(lines[i + 1][4:])
            if cur is None or (cur.removed or cur.inserts):
                cur = FileDiff(old, new)
                files.append(cur)
            else:
                cur.old, cur.new = old, new
            i += 2
            continue
        if cur is not None and (line.startswith("new file mode")):
            cur.old = None
        elif cur is not None and line.startswith("deleted file mode"):
            cur.new = None
        elif cur is not None and line.startswith("rename from "):
            cur.old = line[len("rename from "):]
        elif cur is not None and line.startswith("rename to "):
            cur.new = line[len("rename to "):]
        elif cur is not None and (line.startswith("Binary files") or line.startswith("GIT binary patch")):
            cur.binary = True
        m = HUNK.match(line)
        if m and cur is not None:
            o = int(m.group(1))
            ocount = int(m.group(2)) if m.group(2) is not None else 1
            ncount = int(m.group(4)) if m.group(4) is not None else 1
            i += 1
            pos = o if ocount else o + 1  # next old line number
            seen_o = seen_n = 0
            prev_tag = " "
            while i < len(lines) and (seen_o < ocount or seen_n < ncount):
                h = lines[i]
                if h.startswith("\\"):
                    i += 1
                    continue
                tag = h[:1]
                if tag == "-":
                    cur.removed.append(pos)
                    pos += 1
                    seen_o += 1
                    prev_tag = "-"
                elif tag == "+":
                    if prev_tag != "-":  # pure insertion: the lines around it decide what runs
                        cur.inserts.append(pos - 1)
                    else:
                        prev_tag = "-"
                    seen_n += 1
                    i += 1
                    continue
                else:
                    pos += 1
                    seen_o += 1
                    seen_n += 1
                    prev_tag = " "
                i += 1
            continue
        i += 1
    return files


class Map:
    def __init__(self, path: Path):
        with gzip.open(path, "rt") as fh:
            d = json.load(fh)
        self.ids: list[str] = d["tests"]
        self.sets: list[list[int]] = d["sets"]
        self.lines: dict[str, dict[int, int]] = {
            f: {int(k): v for k, v in m.items()} for f, m in d["lines"].items()
        }
        self.collect: dict[str, set[int]] = {f: set(v) for f, v in d["collect"].items()}
        self.opens: dict[str, int] = d["opens"]
        self.classes: dict[str, int] = d.get("classes", {})
        self.attrs: dict[str, list] = d.get("attrs", {})
        self.cmp: dict = d.get("cmp", {})
        self.tasks: dict[int, dict] = {int(k): v for k, v in d.get("tasks", {}).items()}
        for v in self.tasks.values():
            v["base"] = {f: set(ls) for f, ls in v.get("base", {}).items()}
        self.file_of = [i.split("::")[0] if i else "" for i in self.ids]
        self._touch: dict[str, set[int]] = {}
        self.calls = {}
        sidecar = path.with_name("calls.json.gz")
        if sidecar.is_file():
            with gzip.open(sidecar, "rt") as fh:
                evidence = json.load(fh)
            if evidence["ids_sha256"] == hashlib.sha256("\n".join(self.ids).encode()).hexdigest():
                self.calls = evidence

    def tests_on(self, fn: str, lo: int, hi: int) -> set[int]:
        m = self.lines.get(fn)
        out: set[int] = set()
        if not m:
            return out
        for ln in range(lo, hi + 1):
            s = m.get(ln)
            if s is not None:
                out.update(self.sets[s])
        return out

    def touch(self, fn: str) -> set[int]:
        """Tests that ran any line of fn."""
        if fn not in self._touch:
            m = self.lines.get(fn, {})
            out: set[int] = set()
            for s in set(m.values()):
                out.update(self.sets[s])
            self._touch[fn] = out
        return self._touch[fn]

    def opened(self, fn: str) -> set[int] | None:
        s = self.opens.get(fn)
        return None if s is None else set(self.sets[s])

    def task_subset(self, test: int, ranges: list[tuple[str, int, int]]) -> set[int] | None:
        """Pool tasks of `test` that run any of `ranges`; None when the test itself runs them."""
        info = self.tasks.get(test)
        if info is None:
            return None
        base = info.get("base", {})
        out: set[int] = set()
        for fn, lo, hi in ranges:
            bl = base.get(fn)
            tl = info["lines"].get(fn, {})
            for ln in range(lo, hi + 1):
                if bl is not None and ln in bl:
                    return None
                s = tl.get(str(ln))
                if s is not None:
                    out.update(self.sets[s])
        return out

    def class_users(self, qual: str) -> set[int]:
        s = self.classes.get(qual)
        return set() if s is None else {t for t in self.sets[s] if t >= 0}

    def in_file(self, testfile: str) -> set[int]:
        return {n for n, f in enumerate(self.file_of) if f == testfile}


# ---------------------------------------------------------------- AST scopes


class Scopes:
    """For one source file: what runs a given line."""

    def __init__(self, src: str):
        self.tree = ast.parse(src)
        self.nlines = src.count("\n") + 1
        # innermost statement and innermost function for every line
        self.stmt: list[ast.stmt | None] = [None] * (self.nlines + 2)
        self.func: list[ast.AST | None] = [None] * (self.nlines + 2)
        self.cls: list[ast.AST | None] = [None] * (self.nlines + 2)
        self.parent: dict[ast.AST, ast.AST] = {}
        for node in ast.walk(self.tree):
            for ch in ast.iter_child_nodes(node):
                self.parent[ch] = node
        self._walk(self.tree, None, None)

    def qualname(self, node: ast.AST) -> str:
        parts = []
        n = node
        while n is not None and not isinstance(n, ast.Module):
            if isinstance(n, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                if parts and isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    parts.append("<locals>")
                parts.append(n.name)
            n = self.parent.get(n)
        return ".".join(reversed(parts))

    def top_classes(self) -> list[ast.ClassDef]:
        return [n for n in ast.walk(self.tree) if isinstance(n, ast.ClassDef)]

    def _walk(self, node, func, cls):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.stmt):
                lo, hi = child.lineno, child.end_lineno or child.lineno
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    lo = min([lo] + [d.lineno for d in child.decorator_list])
                for ln in range(lo, min(hi, self.nlines) + 1):
                    self.stmt[ln] = child
                    self.func[ln] = func
                    self.cls[ln] = cls
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    # body lines belong to this function; header stays at outer level
                    body_lo = child.body[0].lineno
                    for ln in range(body_lo, min(hi, self.nlines) + 1):
                        self.func[ln] = child
                    self._walk(child, child, cls)
                elif isinstance(child, ast.ClassDef):
                    body_lo = child.body[0].lineno
                    for ln in range(body_lo, min(hi, self.nlines) + 1):
                        self.cls[ln] = child
                    self._walk(child, func, child)
                else:
                    self._walk(child, func, cls)
            elif isinstance(child, (ast.ExceptHandler, ast.match_case)):
                self._walk(child, func, cls)

    def header_range(self, st: ast.stmt) -> tuple[int, int]:
        """Lines of a statement that run when it starts (header of a compound stmt)."""
        body = getattr(st, "body", None)
        if isinstance(body, list) and body and not isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            return st.lineno, max(st.lineno, body[0].lineno - 1)
        return st.lineno, st.end_lineno or st.lineno


def _is_docstring(st) -> bool:
    return isinstance(st, ast.Expr) and isinstance(getattr(st, "value", None), ast.Constant) and isinstance(st.value.value, str)


@dataclass
class Impact:
    tests: set[int] = field(default_factory=set)
    everything: bool = False
    syntax_error: bool = False
    data_tests: set[int] = field(default_factory=set)
    ranges: list[tuple[str, int, int]] = field(default_factory=list)  # probe ranges matched by line
    other_tests: set[int] = field(default_factory=set)  # tests added for reasons other than line ranges
    line_tests: set[int] = field(default_factory=set)  # tests added only through line ranges  # candidates only because a data file they read changed
    notes: list[str] = field(default_factory=list)


def _defs(tree: ast.AST) -> dict[str, str]:
    """qualname -> AST dump (no positions) for every class and function."""
    out: dict[str, str] = {}

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = prefix + ch.name
                key = q
                n = 2
                while key in out:  # redefinitions (overloads, properties)
                    key = f"{q}#{n}"
                    n += 1
                out[key] = ast.dump(ch)
                walk(ch, q + ".")
            else:
                walk(ch, prefix)

    walk(tree, "")
    return out


def _class_shell(node: ast.ClassDef) -> str:
    """Dump of a class without its methods' bodies (definition-time content)."""
    body = [b for b in node.body if not isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))]
    heads = [
        ast.dump(ast.FunctionDef(name=b.name, args=b.args, body=[], decorator_list=b.decorator_list, returns=b.returns, type_params=[]))
        for b in node.body
        if isinstance(b, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    return ast.dump(ast.ClassDef(name=node.name, bases=node.bases, keywords=node.keywords, body=body, decorator_list=node.decorator_list, type_params=[])) + "|".join(heads)


def py_impact(fn: str, src: str, fd: FileDiff, m: Map, imp: Impact, new_src: str | None = None, only_file: bool = False) -> None:
    single_change = None
    try:
        sc = Scopes(src)
    except SyntaxError:
        imp.tests |= m.touch(fn)
        imp.notes.append(f"{fn}: unparsable pristine")
        return
    old_defs = new_defs = None
    if new_src is not None:
        try:
            new_tree = ast.parse(new_src)
        except SyntaxError:
            # the patched module cannot import: everything that loads it fails
            imp.tests |= m.touch(fn) | _module_users(fn, sc, m)
            if fn.startswith("tests/"):
                imp.tests |= m.in_file(fn)
            imp.syntax_error = True
            imp.notes.append(f"{fn}: patched file does not parse")
            return
        d = _ast_diffs(sc.tree, new_tree)
        if not d:
            imp.notes.append(f"{fn}: AST unchanged")
            o = m.opened(fn)
            if o:
                imp.tests |= {t for t in o if t >= 0}
            return
        if only_file and len(d) == 1:
            single_change = d[0]
            if m.calls:
                kept = _call_refine(fn, sc, single_change, m)
                if kept is not None:
                    imp.tests |= kept | {t for t in (m.opened(fn) or ()) if t >= 0}
                    imp.notes.append(f"{fn}: call evidence -> {len(kept)}")
                    return
        # A lone replaced expression cannot be in an unchanged enclosing
        # definition. Avoid dumping all the unrelated definitions in that file.
        anchor = d[0][1] if isinstance(d[0][1], ast.expr) else d[0][0]
        if not (len(d) == 1 and len(fd.removed) == 1 and not fd.inserts and isinstance(anchor, ast.expr)
                and anchor.lineno <= fd.removed[0] <= anchor.end_lineno):
            old_defs, new_defs = _defs(sc.tree), _defs(new_tree)
    probes: set[int] = set(l for l in fd.removed if 1 <= l <= sc.nlines)
    for p in fd.inserts:
        for q in (p, p + 1):
            if 1 <= q <= sc.nlines:
                probes.add(q)
    is_test = fn.startswith("tests/")
    for ln in sorted(probes):
        st = sc.stmt[ln]
        if st is None:
            continue  # blank line / comment outside statements
        func = sc.func[ln]
        if old_defs is not None:
            # skip probes in a function / class whose AST did not change
            inner = func if (func is not None and ln >= func.body[0].lineno) else None
            if inner is not None:
                q = sc.qualname(inner)
                if q in old_defs and old_defs.get(q) == new_defs.get(q) and list(old_defs).count(q) == 1:
                    continue
            elif sc.cls[ln] is not None and not (isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef))):
                k = sc.cls[ln]
                q = sc.qualname(k)
                newk = _find_class(new_tree, q)
                if newk is not None and _class_shell(newk) == _class_shell(k):
                    continue
        if func is not None and ln >= func.body[0].lineno:
            # inside a function body: whatever runs the enclosing statement
            if _is_docstring(st):
                imp.tests |= _doc_users(fn, m)
                continue
            lo, hi = sc.header_range(st)
            if not (lo <= ln <= hi):
                lo, hi = ln, ln
            got = m.tests_on(fn, lo, hi)
            if single_change is not None and got:
                kept = _cmp_refine(fn, single_change, got, m)
                if kept is not None:
                    imp.notes.append(f"{fn}:{ln} comparison evidence {len(got)} -> {len(kept)}")
                    got = kept
            imp.ranges.append((fn, lo, hi))
            imp.line_tests |= got
            continue
        # definition-time code: module, class body, signatures, decorators
        if is_test:
            imp.tests |= m.in_file(fn)
            if fn == "tests/helpers.py" or not Path(fn).name.startswith("test_"):
                imp.tests |= m.touch(fn)
            continue
        lam = _lambda_at(st, ln)
        if lam is not None:
            got = m.tests_on(fn, lam.body.lineno, lam.body.end_lineno or lam.body.lineno)
            imp.ranges.append((fn, lam.body.lineno, lam.body.end_lineno or lam.body.lineno))
            imp.line_tests |= got
            imp.notes.append(f"{fn}:{ln} lambda body -> {len(got)}")
            continue
        k = sc.cls[ln]
        if k is not None and st in k.body and isinstance(st, (ast.Assign, ast.AnnAssign)) and m.attrs:
            refined = _attr_refine(fn, sc, k, st, m)
            if refined is not None:
                imp.tests |= refined
                imp.notes.append(f"{fn}:{ln} class attr refined -> {len(refined)}")
                continue
        scope = None
        if isinstance(st, (ast.FunctionDef, ast.AsyncFunctionDef)) and ln < st.body[0].lineno:
            scope = st  # signature/decorator: tests that call it
        elif sc.cls[ln] is not None and isinstance(st, ast.stmt):
            scope = None
        if scope is not None:
            imp.tests |= m.tests_on(fn, scope.lineno, scope.end_lineno)
            imp.tests |= _doc_users(fn, m)
        imp.tests |= m.touch(fn)
        mod = module_name(fn)
        k = sc.cls[ln]
        if k is not None and scope is None:
            imp.tests |= _users_of(fn, sc, k, m)
        elif k is None and scope is None:
            for c in sc.top_classes():
                imp.tests |= m.class_users(f"{mod}:{sc.qualname(c)}")
        imp.notes.append(f"{fn}:{ln} definition-time")
    # tests that read the file's text
    o = m.opened(fn)
    if o:
        imp.tests |= {t for t in o if t >= 0}


def _lambda_at(st: ast.AST, ln: int) -> ast.Lambda | None:
    """Innermost lambda in st whose body holds line ln (strictly below the lambda keyword)."""
    best = None
    for node in ast.walk(st):
        if isinstance(node, ast.Lambda):
            b = node.body
            if node.lineno < ln <= (b.end_lineno or b.lineno) and b.lineno <= ln:
                if best is None or node.lineno >= best.lineno:
                    best = node
    return best


def _call_refine(fn, sc, change, m):
    """Find the call that must observe a single value edit, not its import-time definition."""
    parent, old, new = change
    if not fn.startswith("sqlglot/") or -1 in (m.opened(fn) or ()):
        return None
    calls = m.calls
    category, site, source = "calls", None, fn
    candidates = None
    if isinstance(old, ast.Constant) and isinstance(new, ast.Constant):
        # At this pin, booleans in Expr.arg_types are only consumed by
        # __init_subclass__ -> required_args -> Expression.error_messages.
        # Keys, copied declarations and arbitrary dictionary edits aren't equivalent.
        if (fn.startswith("sqlglot/expressions/") and type(old.value) is bool and type(new.value) is bool
                and isinstance(parent, ast.Dict) and old in parent.values):
            st = sc.parent.get(parent)
            cls = sc.parent.get(st)
            if (isinstance(cls, ast.ClassDef) and isinstance(st, ast.Assign) and st.value is parent
                    and len(st.targets) == 1 and isinstance(st.targets[0], ast.Name)
                    and st.targets[0].id == "arg_types" and m.attrs.get("arg_types")
                    and not any(r[3] == 2 and r[5] in {cls.name, "?"} for r in m.attrs["arg_types"])):
                category, site = "validated", f"{module_name(fn)}:{sc.qualname(cls)}"
        call = sc.parent.get(parent) if isinstance(parent, ast.keyword) else parent
        if isinstance(call, ast.Call):
            factory = calls["factories"].get(f"{fn}:{call.lineno}:{call.col_offset}")
            if factory:
                source = factory.rsplit(":", 1)[0]
                site = factory
                # Many declarations may use the same factory; retain its calls
                # among the existing candidates for the declaring class.
                cls = sc.cls[call.lineno]
                if cls is None:
                    return None
                candidates = m.touch(fn) | _users_of(fn, sc, cls, m)
    if site is None:
        # Limit this proof to changes that cannot affect closure binding or
        # turn an ordinary lambda into a generator (or vice versa).
        if not any(isinstance(old, typ) and isinstance(new, typ)
                   for typ in (ast.Constant, ast.cmpop, ast.boolop, ast.operator, ast.unaryop)):
            return None
        node = old if isinstance(old, ast.expr) else parent
        while node is not None:
            outer = sc.parent.get(node)
            if isinstance(outer, ast.Lambda) and outer.body is node:
                site = f"{fn}:{outer.lineno}"
                break
            node = outer
    if site is None:
        return None
    observed = set(calls[category].get(site, ()))
    if category == "validated" and -1 in observed:
        key = parent.keys[parent.values.index(old)]
        missing = calls.get("collect_missing", {}).get(site)
        if isinstance(key, ast.Constant) and isinstance(key.value, str) and missing is not None:
            if "*" not in missing and key.value not in missing:
                observed.remove(-1)
    extra = calls["inspected"]
    unknown = set(calls["unknown"]) | set(extra.get("<child>", ()))
    inspected = set(extra.get(module_name(source), ()))
    if -1 in observed or -1 in unknown or -1 in inspected:
        return None  # collection may have propagated the value to any test
    if candidates is not None:
        observed &= candidates
    elif unknown:
        # No call evidence for these tests: keep the original broader
        # file/class coverage, including a child interpreter's pristine lines.
        line = old.lineno if isinstance(old, ast.expr) else parent.lineno
        cls = sc.cls[line]
        candidates = m.touch(fn) | (_users_of(fn, sc, cls, m) if cls else _module_users(fn, sc, m))
    return observed | (unknown & candidates if unknown else set()) | inspected


def _users_of(fn: str, sc: "Scopes", k: ast.AST, m: Map) -> set[int]:
    """Tests using class k or any class it is nested in."""
    out: set[int] = set()
    mod = module_name(fn)
    n = k
    while n is not None and not isinstance(n, ast.Module):
        if isinstance(n, ast.ClassDef):
            out |= m.class_users(f"{mod}:{sc.qualname(n)}")
        n = sc.parent.get(n)
    return out


def _used_at_collect(fn: str, sc: "Scopes", k: ast.AST, m: Map) -> bool:
    mod = module_name(fn)
    n = k
    while n is not None and not isinstance(n, ast.Module):
        if isinstance(n, ast.ClassDef):
            s = m.classes.get(f"{mod}:{sc.qualname(n)}")
            if s is not None and -1 in m.sets[s]:
                return True
        n = sc.parent.get(n)
    return False


def _attr_refine(fn: str, sc: "Scopes", k: ast.ClassDef, st: ast.stmt, m: Map) -> set[int] | None:
    """Tests that use class k and run a line reading the assigned attribute; None if unsure."""
    targets = st.targets if isinstance(st, ast.Assign) else [st.target]
    names = []
    for tg in targets:
        if not isinstance(tg, ast.Name):
            return None
        names.append(tg.id)
    users = _users_of(fn, sc, k, m) | m.touch(fn)
    readers: set[int] = set()
    for name in names:
        refs = m.attrs.get(name)
        if refs is None:
            continue  # nobody reads it
        for rf, lo, hi, safe, *extra in refs:
            if safe == 2:
                # another class copies NAME at definition time; it sees k's value only if it
                # names k explicitly (subclasses reaching it by inheritance are users of k already)
                if extra and extra[1] in (k.name, "?"):
                    users |= m.class_users(extra[0])
                continue
            if safe == 3:
                # reader also runs at import time: fine unless k was in use then
                if _used_at_collect(fn, sc, k, m):
                    return None
            elif not safe:
                return None
            readers |= m.tests_on(rf, lo, hi)
    return users & readers


def _ast_diffs(a: ast.AST, b: ast.AST, out: list | None = None, parent=None) -> list:
    """Smallest differing node pairs between two trees (stops after 2)."""
    out = [] if out is None else out
    if len(out) > 1:
        return out
    if type(a) is not type(b):
        out.append((parent, a, b))
        return out
    if isinstance(a, ast.AST):
        for f in a._fields:
            va, vb = getattr(a, f, None), getattr(b, f, None)
            if isinstance(va, list) and isinstance(vb, list):
                if len(va) != len(vb):
                    out.append((parent, a, b))
                    return out
                for x, y in zip(va, vb):
                    _ast_diffs(x, y, out, a)
            elif isinstance(va, ast.AST) or isinstance(vb, ast.AST):
                _ast_diffs(va, vb, out, a)
            elif va != vb or type(va) is not type(vb):
                out.append((parent, a, b))
                return out
        return out
    if a != b or type(a) is not type(b):
        out.append((parent, a, b))
    return out


_CMPF = {
    ast.Eq: lambda a, b: a == b, ast.NotEq: lambda a, b: a != b, ast.Is: lambda a, b: a is b,
    ast.IsNot: lambda a, b: a is not b, ast.Lt: lambda a, b: a < b, ast.LtE: lambda a, b: a <= b,
    ast.Gt: lambda a, b: a > b, ast.GtE: lambda a, b: a >= b,
    ast.In: lambda a, b: a in b, ast.NotIn: lambda a, b: a not in b,
}


def _cmp_refine(fn: str, change, got: set[int], m: Map) -> set[int] | None:
    """Drop tests whose recorded operands give the same result before and after the change."""
    if not m.cmp or not fn.startswith("sqlglot/"):
        return None
    parent, a, b = change
    # locate the comparison: either the changed node itself, or a Constant directly inside it
    if isinstance(a, ast.cmpop) and isinstance(b, ast.cmpop) and isinstance(parent, ast.Compare):
        if len(parent.ops) != 1:
            return None
        cmp_old = parent
        cmp_new = ast.Compare(left=parent.left, ops=[b], comparators=parent.comparators)
        side = None
    elif isinstance(a, ast.Constant) and isinstance(b, ast.Constant) and isinstance(parent, ast.Compare) and len(parent.ops) == 1:
        cmp_old = parent
        if parent.left is a:
            side = 0
        elif parent.comparators[0] is a:
            side = 1
        else:
            return None
        cmp_new = None
    else:
        return None
    if cmp_old.lineno in m.collect.get(fn, ()):
        return None  # cmp_plugin clears its observations after collection
    site = m.cmp["sites"].get(f"{fn}:{cmp_old.lineno}:{cmp_old.col_offset}")
    done = set(m.sets[m.cmp["done"]])
    if m.calls:
        done.difference_update(m.calls["inspected"].get("<child>", ()))
    old_op = type(cmp_old.ops[0])
    f_old = _CMPF.get(old_op)
    if f_old is None:
        return None
    identity = (ast.Is, ast.IsNot)
    if side is not None and old_op in identity and not (
        type(a.value) in (bool, type(None)) and type(b.value) in (bool, type(None))
    ):
        return None  # repr does not preserve object identity
    if site is None:
        # A rich comparison can raise before the tracing helper records it.
        return got - done if old_op in (ast.Is, ast.IsNot) else None
    observed = set(m.sets[site[0]])
    disagree = set(m.sets[site[1]])
    overflow = set(m.sets[site[2]])
    pair_of: dict[int, list] = {}
    for prs, sidx in site[3]:
        for t in m.sets[sidx]:
            pair_of[t] = prs
    keep: set[int] = set()
    if cmp_new is not None:
        new_op = type(cmp_new.ops[0])
        f_new = _CMPF.get(new_op)
        if f_new is None:
            return None
        swap = {frozenset((ast.Eq, ast.Is)), frozenset((ast.NotEq, ast.IsNot))}
        if (old_op in identity or new_op in identity) and frozenset((old_op, new_op)) not in swap:
            return None
        for t in got:
            if t not in done:
                keep.add(t)
            elif t not in observed:
                if old_op not in (ast.Is, ast.IsNot):
                    keep.add(t)
            elif frozenset((old_op, new_op)) in swap:
                if t in disagree or ast.NotEq in (old_op, new_op) and t in overflow:
                    keep.add(t)
            elif t in overflow or t not in pair_of or not _same(pair_of[t], f_old, f_new, None, None):
                keep.add(t)
        return keep
    new_val = b.value
    for t in got:
        if t not in done:
            keep.add(t)
        elif t not in observed:
            if old_op not in (ast.Is, ast.IsNot):
                keep.add(t)
        elif t in overflow or t not in pair_of or not _same(pair_of[t], f_old, f_old, side, new_val):
            keep.add(t)
    return keep


def _same(prs, f_old, f_new, side, new_val) -> bool:
    try:
        for ra, rb in prs:
            x, y = ast.literal_eval(ra), ast.literal_eval(rb)
            x2, y2 = x, y
            if side == 0:
                x2 = new_val
            elif side == 1:
                y2 = new_val
            if bool(f_old(x, y)) != bool(f_new(x2, y2)):
                return False
        return True
    except Exception:
        return False


def _find_class(tree, qual):
    node = tree
    for part in qual.split("."):
        nxt = None
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and ch.name == part:
                nxt = ch
        if nxt is None:
            return None
        node = nxt
    return node if isinstance(node, ast.ClassDef) else None


def _module_users(fn: str, sc: "Scopes", m: Map) -> set[int]:
    out: set[int] = set()
    mod = module_name(fn)
    for c in sc.top_classes():
        out |= m.class_users(f"{mod}:{sc.qualname(c)}")
    return out


def module_name(fn: str) -> str:
    mod = fn[:-3].replace("/", ".")
    if mod.endswith(".__init__"):
        mod = mod[: -len(".__init__")]
    return mod


def _doc_users(fn: str, m: Map) -> set[int]:
    return set()


def impact(patch_text: str, m: Map, root: Path, patched_root: Path | None = None) -> Impact:
    imp = Impact()
    diffs = parse_patch(patch_text)
    for fd in diffs:
        for fn in {f for f in (fd.old, fd.new) if f}:
            o = m.opened(fn)
            if o:
                if -1 in o:
                    imp.notes.append(f"{fn}: opened at collection")
                got = {t for t in o if t >= 0}
                if not fn.endswith(".py"):
                    imp.data_tests |= got - imp.tests
                imp.tests |= got
        fn = fd.old or fd.new
        if fn is None:
            continue
        if fd.old is None or fd.new is None or fd.old != fd.new:
            # added / deleted / renamed file
            imp.notes.append(f"{fn}: added/deleted/renamed")
            for f in (fd.old, fd.new):
                if f and f.endswith(".py"):
                    imp.tests |= m.touch(f)
                    if f.startswith("tests/"):
                        imp.tests |= m.in_file(f)
            if (fd.new or "").startswith("sqlglot/") and fd.old is None:
                imp.everything = True  # new module: walk_packages style tests
            continue
        if fn.endswith(".py"):
            p = root / fn
            if not p.exists():
                imp.notes.append(f"{fn}: not in checkout")
                continue
            new_src = None
            if patched_root is not None:
                q = patched_root / fn
                new_src = q.read_text(encoding="utf-8", errors="replace") if q.exists() else None
            py_impact(fn, p.read_text(encoding="utf-8"), fd, m, imp, new_src, only_file=len(diffs) == 1)
        elif fn.startswith("tests/fixtures/"):
            if m.opened(fn) is None:
                imp.notes.append(f"{fn}: fixture never opened")
        else:
            imp.notes.append(f"{fn}: non-python, not opened")
    imp.other_tests = set(imp.tests)
    imp.tests |= imp.line_tests
    return imp


# ---------------------------------------------------------------- applying


class PatchError(Exception):
    pass


def modified_files(text: str, root: Path):
    """Apply ordinary text edits at their stated coordinates. Never guess on a mismatch."""
    lines = text.split("\n")
    if lines[-1] == "":
        lines.pop()
    edits = []
    seen = set()
    i = 0
    while i < len(lines):
        header = re.fullmatch(r"diff --git a/([^\s\\]+) b/\1", lines[i])
        if not header:
            raise PatchError("expected a same-path git diff")
        fn = header[1]
        if fn in seen or any(part in {"", ".", ".."} for part in fn.split("/")):
            raise PatchError("non-canonical or repeated path")
        seen.add(fn)
        i += 1
        if i < len(lines) and lines[i].startswith("index "):
            i += 1
        if lines[i:i + 2] != [f"--- a/{fn}", f"+++ b/{fn}"]:
            raise PatchError("expected a text modification")
        i += 2
        path = root / fn
        if not path.resolve().is_relative_to(root.resolve()):
            raise PatchError("path outside checkout")
        before = path.read_bytes().decode("utf-8")
        if "\r" in before or not before.endswith("\n"):
            raise PatchError("unsupported source line endings")
        source = before[:-1].split("\n")
        output = []
        cursor = 0
        fd = FileDiff(fn, fn)
        while i < len(lines) and lines[i].startswith("@@"):
            h = HUNK.match(lines[i])
            if h is None:
                raise PatchError("invalid hunk")
            old_count, new_count = (int(h[k] or 1) for k in (2, 4))
            pos = int(h[1]) - bool(old_count)
            if not cursor <= pos <= len(source):
                raise PatchError("overlapping or out-of-range hunk")
            output.extend(source[cursor:pos])
            if len(output) != int(h[3]) - bool(new_count):
                raise PatchError("new hunk offset does not match")
            cursor = pos
            i += 1
            removed_block = False
            while old_count or new_count:
                if i >= len(lines) or not lines[i] or lines[i][0] not in " +-":
                    raise PatchError("truncated hunk")
                tag, body = lines[i][0], lines[i][1:]
                if tag != "+":
                    if not old_count or cursor >= len(source) or source[cursor] != body:
                        raise PatchError("hunk differs from pinned source")
                    cursor += 1
                    old_count -= 1
                if tag != "-":
                    if not new_count:
                        raise PatchError("wrong hunk length")
                    output.append(body)
                    new_count -= 1
                if tag == "-":
                    fd.removed.append(cursor)
                    removed_block = True
                elif tag == "+" and not removed_block:
                    fd.inserts.append(cursor)
                elif tag == " ":
                    removed_block = False
                i += 1
        if not fd.removed and not fd.inserts:
            raise PatchError("no changes in diff section")
        output.extend(source[cursor:])
        edits.append((fd, before, "\n".join(output) + ("\n" if output else "")))
    if not edits:
        raise PatchError("empty patch")
    return edits


def apply_patch(patch_text: str, root: Path) -> dict[str, str | None]:
    """Return {path: new text or None if deleted} for every file the patch touches."""
    out: dict[str, str | None] = {}
    lines = patch_text.split("\n")
    i = 0
    old = new = None
    is_new = is_del = False
    rename_from = rename_to = None

    def flush_header():
        nonlocal old, new, is_new, is_del, rename_from, rename_to
        old = new = None
        is_new = is_del = False
        rename_from = rename_to = None

    cur_lines: list[str] | None = None
    cur_eol_missing = False
    cur_target: str | None = None

    def load(path):
        if path in out:
            t = out[path]
            return [] if t is None else t.split("\n")
        p = root / path
        if not p.exists():
            return []
        return p.read_text(encoding="utf-8", errors="surrogateescape").split("\n")

    while i < len(lines):
        line = lines[i]
        if line.startswith("diff --git "):
            # a pure rename / mode change has no hunks; handle it at the header
            flush_header()
            m = re.match(r"diff --git a/(.*) b/(.*)$", line)
            if m:
                old, new = m.group(1), m.group(2)
            j = i + 1
            while j < len(lines) and not lines[j].startswith(("diff --git ", "--- ", "@@")):
                h = lines[j]
                if h.startswith("new file mode"):
                    is_new = True
                elif h.startswith("deleted file mode"):
                    is_del = True
                elif h.startswith("rename from "):
                    rename_from = h[len("rename from "):]
                elif h.startswith("rename to "):
                    rename_to = h[len("rename to "):]
                j += 1
            if rename_from and rename_to:
                content = load(rename_from)
                out[rename_to] = "\n".join(content)
                out[rename_from] = None
            if is_del and old and (j >= len(lines) or not lines[j].startswith("--- ")):
                out[old] = None
            if is_new and new and (j >= len(lines) or not lines[j].startswith("--- ")):
                out[new] = ""
            i = j
            continue
        if line.startswith("--- ") and i + 1 < len(lines) and lines[i + 1].startswith("+++ "):
            o, n = _strip(line[4:]), _strip(lines[i + 1][4:])
            src = o if o is not None else n
            if rename_from and rename_to:
                src = rename_to
            target = n if n is not None else o
            cur_target = target
            if n is None:
                out[o] = None
                cur_lines = None
            else:
                cur_lines = [] if o is None else load(src)
                if cur_lines == [""]:
                    cur_lines = []
                offset = 0
            i += 2
            # consume hunks
            while i < len(lines) and lines[i].startswith("@@"):
                m = HUNK.match(lines[i])
                if not m:
                    raise PatchError("bad hunk header")
                ostart = int(m.group(1))
                ocount = int(m.group(2)) if m.group(2) is not None else 1
                ncount = int(m.group(4)) if m.group(4) is not None else 1
                i += 1
                old_chunk: list[str] = []
                new_chunk: list[str] = []
                so = sn = 0
                while i < len(lines) and (so < ocount or sn < ncount):
                    h = lines[i]
                    if h.startswith("\\"):
                        i += 1
                        continue
                    tag, body = h[:1], h[1:]
                    if tag == "-":
                        old_chunk.append(body)
                        so += 1
                    elif tag == "+":
                        new_chunk.append(body)
                        sn += 1
                    elif tag == " " or h == "":
                        old_chunk.append(body)
                        new_chunk.append(body)
                        so += 1
                        sn += 1
                    else:
                        raise PatchError(f"bad hunk line {h[:40]!r}")
                    i += 1
                while i < len(lines) and lines[i].startswith("\\"):
                    i += 1
                if cur_lines is not None:
                    pos = (ostart - 1 if ocount else ostart) + offset
                    if cur_lines[pos:pos + len(old_chunk)] != old_chunk:
                        # search nearby for the context
                        found = None
                        for d in range(1, 200):
                            for p2 in (pos - d, pos + d):
                                if 0 <= p2 and cur_lines[p2:p2 + len(old_chunk)] == old_chunk:
                                    found = p2
                                    break
                            if found is not None:
                                break
                        if found is None:
                            raise PatchError(f"hunk does not apply to {target}")
                        pos = found
                    cur_lines[pos:pos + len(old_chunk)] = new_chunk
                    offset = pos - (ostart - 1 if ocount else ostart) + len(new_chunk) - len(old_chunk)
            if cur_lines is not None and target is not None:
                text = "\n".join(cur_lines)
                orig = None if o is None else (root / src)
                # keep a trailing newline like the original
                if text and not text.endswith("\n") and (orig is None or not orig.exists() or orig.read_bytes().endswith(b"\n")):
                    if not patch_text.rstrip("\n").endswith("\\ No newline at end of file"):
                        text += "\n"
                out[target] = text
            continue
        i += 1
    return out
