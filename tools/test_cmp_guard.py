"""Regression checks for the successful-attempt-only comparison profile."""
import ast
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast


class ComparisonGuard(unittest.TestCase):
    fn = "sqlglot/demo.py"

    def setUp(self):
        self.site = f"{self.fn}:1:0"
        self.m = SimpleNamespace(
            # done, observed, disagree, overflow, pair group
            sets=[[0], [0], [], [], [0]],
            cmp={"done": 0, "sites": {self.site: [1, 2, 3, [[[["1", "2"]], 4]]]}},
            calls={"unknown": [], "inspected": {}}, collect={},
        )

    def kept(self, old, new, candidates=frozenset({0})):
        change, = blast._ast_diffs(ast.parse(old), ast.parse(new))
        result = blast._cmp_refine(self.fn, change, set(candidates), self.m)
        return candidates if result is None else result

    def test_incomplete_observations_keep_line_candidates(self):
        # Even 0 could be a skipped call with observations from setup.
        # Keep 1 unobserved, 2 not done (including failed tests), 3
        # disagreeing, 4 opaque/overflow, 5 missing pairs, 6 empty pairs,
        # 7 with child execution, 8 without call audit and 9 inspecting code.
        candidates = set(range(10))
        self.m.sets = [
            sorted(candidates - {2}), sorted(candidates - {1}), [3], [4],
            [0, 2, 3, 7, 8, 9], [6],
        ]
        self.m.cmp["sites"][self.site][3].append([[], 5])
        self.m.calls = {"unknown": [8], "inspected": {"<child>": [7], "sqlglot.demo": [9]}}
        for old, new in [("x is y", "x == y"), ("x is not y", "x != y")]:
            with self.subTest(old=old):
                self.assertEqual(self.kept(old, new, candidates), candidates)

    def test_missing_site_is_not_negative_evidence(self):
        self.m.cmp["sites"].clear()
        self.assertEqual(self.kept("x is y", "x == y"), {0})

    def test_missing_child_audit_is_not_negative_evidence(self):
        self.m.calls = {}
        self.assertEqual(self.kept("x is y", "x == y"), {0})

    def test_collection_observations_are_not_per_test(self):
        self.m.collect = {self.fn: {1}}
        self.assertEqual(self.kept("x is y", "x == y"), {0})
        self.m.collect.clear()
        self.m.calls["inspected"]["<child>"] = [-1]
        self.assertEqual(self.kept("x is y", "x == y"), {0})

    def test_rich_comparison_can_have_both_recorded_and_raising_attempts(self):
        # (1, 2) was recorded. A later object can raise in __lt__, __eq__
        # or __ne__ before cmp_plugin records even an overflow; the test may
        # catch that exception, so it still belongs to the "done" set.
        for old, new in [
            ("x < y", "x <= y"), ("x == y", "x is y"), ("x != y", "x is not y"),
            ("x == 2", "x == 3"),
        ]:
            with self.subTest(old=old):
                self.assertEqual(self.kept(old, new), {0})

    def test_identical_reprs_do_not_prove_identical_objects(self):
        self.m.cmp["sites"][self.site][3] = [[[["'long string'", "'long string'"]], 4]]
        self.m.sets[2] = [0]  # live equality and identity disagreed
        self.assertEqual(self.kept("x is y", "x == y"), {0})
        self.assertEqual(self.kept("x is 'long string'", "x is 'new string'"), {0})


if __name__ == "__main__":
    unittest.main()
