"""Generate the offline challenge set from Git blobs, without reading labels/maps."""
import argparse
import difflib
import hashlib
import json
import subprocess
from pathlib import Path


PIN = "eb11aec4be8cb2122afe713a4816662c3f0fb2fa"
# Hand-selected edit sites, fixed before evaluating the first patched suite.
# A case ID is opaque; neither the kind nor the labels belong in selector input.
CASES = [
    ("token", [("sqlglot/time.py", "subsecond_digit_count > 3:", "subsecond_digit_count >= 3:")]),
    ("literal", [("sqlglot/generators/drill.py", 'ARRAY_SIZE_NAME = "REPEATED_COUNT"',
                  'ARRAY_SIZE_NAME = "ARRAY_LENGTH"')]),
    ("deletion", [("sqlglot/parsers/doris.py", '        "DATE_TRUNC": _build_date_trunc,\n', "")]),
    ("comment", [("sqlglot/parsers/doris.py", "    # Determine which argument is the unit\n",
                  "    # Recognise the unit before the datetime.\n")]),
    ("doc", [("sqlglot/trie.py", "Creates a new trie out of a collection of keywords.",
              "Build a keyword trie, extending the supplied mapping when present.")]),
    ("format", [("sqlglot/generators/drill.py", '    return self.func("TO_DATE", this, time_format)',
                 '    return self.func(\n        "TO_DATE", this, time_format,\n    )')]),
    ("syntax", [("sqlglot/trie.py", "class TrieResult(Enum):", "class TrieResult(Enum)")]),
    ("syntax-test", [("tests/test_dialect_imports.py", "def test_dialect_import_no_deadlock(self):",
                      "def test_dialect_import_no_deadlock(self)")]),
    ("fixture", [("tests/fixtures/optimizer/pushdown_predicates.sql",
                  "WITH x AS (SELECT y.a FROM y WHERE y.a = 1) SELECT * FROM x WHERE TRUE;",
                  "WITH x AS (SELECT y.a FROM y WHERE y.a = 2) SELECT * FROM x WHERE TRUE;")]),
    ("test", [("tests/test_helper.py", '("38", 38),', '("38", 37),')]),
    ("multi-file", [
        ("sqlglot/generators/drill.py", 'exp.RegexpLike: rename_func("REGEXP_MATCHES"),',
         'exp.RegexpLike: rename_func("REGEXP_LIKE"),'),
        ("tests/test_jsonpath.py", '("$.select", "$.select"),', '("$.select", "$.Select"),'),
    ]),
]


def generate(source, out):
    (out / "commits").mkdir(parents=True, exist_ok=False)
    manifest = {}
    for kind, edits in CASES:
        patch = ""
        for path, old, new in edits:
            src = subprocess.check_output(["git", "-C", source, "show", f"{PIN}:{path}"]).decode()
            if src.count(old) != 1:
                raise ValueError((path, old))
            patch += f"diff --git a/{path} b/{path}\n" + "".join(difflib.unified_diff(
                src.splitlines(True), src.replace(old, new).splitlines(True),
                fromfile=f"a/{path}", tofile=f"b/{path}",
            ))
        digest = hashlib.sha256(patch.encode()).hexdigest()
        case = digest[:12]
        (out / "commits" / f"{case}.patch").write_bytes(patch.encode())
        manifest[case] = {"kind": kind, "sha256": digest}
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("out", type=Path)
    a = p.parse_args()
    generate(a.source, a.out)
