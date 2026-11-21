import difflib
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast


class ImportGuard(unittest.TestCase):
    fn = "sqlglot/demo.py"

    def impact(self, before, after, calls=None, *, only_file=True):
        patch = difflib.unified_diff(before.splitlines(True), after.splitlines(True),
                                     f"a/{self.fn}", f"b/{self.fn}")
        fd, = blast.parse_patch("".join(patch))
        # Per-test coverage has no way to identify transitive collection failures.
        m = SimpleNamespace(calls=calls or {}, cmp={}, collect={}, attrs={"FLAG": []},
                            opened=lambda fn: None, class_users=lambda cls: set(),
                            touch=lambda fn: {1}, tests_on=lambda fn, lo, hi: {1})
        result = blast.Impact()
        blast.py_impact(self.fn, before, fd, m, result, after, only_file=only_file)
        return result

    def test_executable_module_insertion_between_unchanged_functions(self):
        before = "def a():\n    return 1\n\ndef b():\n    return 2\n"
        for stmt in ["raise RuntimeError()", "import missing_dependency"]:
            with self.subTest(stmt=stmt):
                after = before.replace("def b():", f"{stmt}\n\ndef b():")
                self.assertTrue(self.impact(before, after).everything)

    def test_raising_rhs_is_not_a_class_attribute_read(self):
        before = "class Config:\n    FLAG = True\n"
        self.assertTrue(self.impact(before, before.replace("True", "1 / 0")).everything)

    def test_default_expression_is_not_a_deferred_function_body(self):
        before = "def f(value=True): return value\n"
        self.assertTrue(self.impact(before, before.replace("True", "1 / 0")).everything)

    def test_literal_operand_can_break_import(self):
        for before in ("class Config:\n    FLAG = 2 / 1\n", "def f(value=2 / 1): return value\n"):
            self.assertTrue(self.impact(before, before.replace("/ 1", "/ 0")).everything)

    def test_multiple_body_changes_do_not_hide_a_later_import_edit(self):
        before = "def f():\n    a = 1\n    b = 2\n\nclass Config:\n    FLAG = True\n"
        after = before.replace("1", "3").replace("2", "4").replace("True", "1 / 0")
        self.assertTrue(self.impact(before, after).everything)

    def test_literal_edit_keeps_its_attribute_readers(self):
        before = "class Config:\n    FLAG = True\n"
        for only_file in (True, False):
            with self.subTest(only_file=only_file):
                impact = self.impact(before, before.replace("True", "False"), only_file=only_file)
                self.assertFalse(impact.everything)
                self.assertEqual(impact.tests | impact.line_tests, set())

    def test_body_edit_including_new_statement_keeps_line_users(self):
        before = "class Config:\n    def f(self):\n        return 1\n"
        after = before.replace("return 1", "import optional_dependency\n        return 2")
        impact = self.impact(before, after)
        self.assertFalse(impact.everything)
        self.assertEqual(impact.line_tests, {1})

    def test_proven_class_lambda_edit_keeps_callers(self):
        before = "class Config:\n    FLAG = lambda x: x + 2\n"
        calls = {"calls": {f"{self.fn}:2": [2]}, "unknown": [], "inspected": {}}
        impact = self.impact(before, before.replace("+", "-"), calls)
        self.assertFalse(impact.everything)
        self.assertEqual(impact.tests, {2})


if __name__ == "__main__":
    unittest.main()
