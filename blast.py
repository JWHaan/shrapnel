"""Map a unified diff against pinned sqlglot to the tests that could notice it."""
from __future__ import annotations

import ast
import gzip
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
        self.tasks: dict[int, dict] = {int(k): v for k, v in d.get("tasks", {}).items()}
        for v in self.tasks.values():
            v["base"] = {f: set(ls) for f, ls in v.get("base", {}).items()}
        self.file_of = [i.split("::")[0] if i else "" for i in self.ids]
        self._touch: dict[str, set[int]] = {}

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


def py_impact(fn: str, src: str, fd: FileDiff, m: Map, imp: Impact, new_src: str | None = None) -> None:
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
        if ast.dump(new_tree) == ast.dump(sc.tree):
            imp.notes.append(f"{fn}: AST unchanged")
            o = m.opened(fn)
            if o:
                imp.tests |= {t for t in o if t >= 0}
            return
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
            imp.ranges.append((fn, lo, hi))
            imp.line_tests |= got
            continue
        # definition-time code: module, class body, signatures, decorators
        if is_test:
            imp.tests |= m.in_file(fn)
            if fn == "tests/helpers.py" or not Path(fn).name.startswith("test_"):
                imp.tests |= m.touch(fn)
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
        for rf, lo, hi, safe in refs:
            if not safe:
                return None
            readers |= m.tests_on(rf, lo, hi)
    return users & readers


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
    for fd in parse_patch(patch_text):
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
            py_impact(fn, p.read_text(encoding="utf-8"), fd, m, imp, new_src)
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
