"""Focused checks of the guards on the pristine call map (no patch labels)."""
import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast
from tools.build_calls import closure_line


class CallEvidence(unittest.TestCase):
    def setUp(self):
        self.fn = "sqlglot/expressions/demo.py"
        self.m = SimpleNamespace(
            calls={"calls": {f"{self.fn}:2": [3]}, "factories": {}, "validated": {},
                   "unknown": [5], "inspected": {"<child>": [6], "sqlglot.expressions.demo": [7]}},
            attrs={"arg_types": [["p", 1, 1, 1]]}, opened=lambda f: None,
            touch=lambda f: {1, 3, 4, 5, 6}, class_users=lambda c: {3, 4},
        )

    def refine(self, src, old, new):
        sc = blast.Scopes(src)
        change, = blast._ast_diffs(sc.tree, ast.parse(src.replace(old, new)))
        return blast._call_refine(self.fn, sc, change, self.m)

    def test_defined_lambda_is_not_a_call(self):
        src = "class Demo:\n f = {1: lambda n=20: n + 30}\n"
        self.assertEqual(self.refine(src, "30", "31"), {3, 5, 6, 7})
        self.assertIsNone(self.refine(src, "20", "21"))
        self.assertIsNone(self.refine(src, "{1:", "{2:"))
        self.m.calls["calls"][f"{self.fn}:2"].append(-1)
        self.assertIsNone(self.refine(src, "30", "31"))

    def test_validated_receiver_only_for_value_edits_to_uncopied_metadata(self):
        src = 'class Demo:\n arg_types = {"field": True}\n'
        self.m.calls["validated"]["sqlglot.expressions.demo:Demo"] = [4]
        self.assertEqual(self.refine(src, "True", "False"), {4, 5, 6, 7})
        self.assertIsNone(self.refine(src, "field", "other"))
        self.m.calls["validated"]["sqlglot.expressions.demo:Demo"].append(-1)
        self.assertIsNone(self.refine(src, "True", "False"))
        self.m.calls["collect_missing"] = {"sqlglot.expressions.demo:Demo": ["other"]}
        self.assertEqual(self.refine(src, "True", "False"), {4, 5, 6, 7})
        self.m.calls["collect_missing"]["sqlglot.expressions.demo:Demo"].append("*")
        self.assertIsNone(self.refine(src, "True", "False"))
        self.m.calls["validated"]["sqlglot.expressions.demo:Demo"].remove(-1)
        self.m.attrs["arg_types"] = [["p", 1, 1, 2, "q:Other", "Demo"]]
        self.assertIsNone(self.refine(src, "True", "False"))

    def test_factory_creation_must_not_run_user_code(self):
        for src in [
            "def outer(x): return lambda y=x(): y",
            "def outer(x):\n @x\n def f(): pass\n return f",
            "def outer(x):\n def f(y=x()): pass\n return f",
            "def outer(x): return fn(x)",
        ]:
            with self.subTest(src=src):
                self.assertIsNone(closure_line(ast.parse(src).body[0]))

    def test_pure_factory_evidence_is_shared_but_import_is_not_a_call(self):
        self.m.calls["factories"][f"{self.fn}:2:9"] = "sqlglot/other.py:5"
        self.m.calls["calls"]["sqlglot/other.py:5"] = [3, 9]
        src = 'class Demo:\n f = {1: builder(flag=True)}'
        self.assertEqual(self.refine(src, "True", "False"), {3, 5, 6, 7, 9})
        self.assertIsNone(self.refine(src, "{1:", "{2:"))

    def test_test_file_comparisons_have_no_cmp_profile(self):
        old, new = "assert value is None", "assert value == None"
        change, = blast._ast_diffs(ast.parse(old), ast.parse(new))
        self.assertIsNone(blast._cmp_refine("tests/test_demo.py", change, {1}, SimpleNamespace(cmp={"done": 0})))


if __name__ == "__main__":
    unittest.main()
