import difflib
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast
import fast_impact


class FastImpactTest(unittest.TestCase):
    src = '''\
class A:
    lookup = {"é": lambda x: (
        x if x == 4 else 0
    ), "b": 1}

    @staticmethod
    def f(x=3):
        "text"
        return x + 1

text = """head
tail"""
joined = 1 + \\
    2
def doubled():
    return 1
def doubled():
    return 2
'''

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        self.index = root / "index"
        (root / "sqlglot").mkdir()
        (root / "sqlglot/a.py").write_text(self.src)
        fast_impact.build(root, self.index)

    def narrow(self, src, old, new):
        after = src.replace(old, new)
        diff = "".join(difflib.unified_diff(src.splitlines(True), after.splitlines(True),
                                            "a/sqlglot/a.py", "b/sqlglot/a.py"))
        fd, = blast.parse_patch(diff)
        return fast_impact.narrow_sources(src, after, fd, self.index)

    def test_body(self):
        for old, new in [("x + 1", "x * 3"), ("== 4", "!= 5"), ('"text"', '"é"')]:
            with self.subTest(old=old):
                before, after = self.narrow(self.src, old, new)
                self.assertIn("class A:", before)
                self.assertNotIn('text = """head', before)
                self.assertIn(new, after)
                self.assertEqual(self.src.index(old) - self.src.rindex("\n", 0, self.src.index(old)),
                                 after.index(new) - after.rindex("\n", 0, after.index(new)))
                self.assertEqual(self.src[:self.src.index(old)].count("\n"), after[:after.index(new)].count("\n"))

    def test_fallback(self):
        for old, new in [
            ("=3", "=7"), ("staticmethod", "classmethod"), ('"b": 1', '"b": 2'),
            ("return x + 1", "def g(): return 5"), ("return x + 1", "return )"),
            ("return x + 1", "return x\n        return 3"), ("        return x + 1", "    outside = 1"),
            ('x if x == 4 else 0\n    ), "b": 1', 'x), "c": (\n        5), "b": 1'),
            ('x if x == 4 else 0\n    ), "b": 1', 'x if x == 8 else 0\n    ), "b": 10'),
            ('tail"""', '# data, not a comment\ntail"""'), ("    2\n", "    # breaks continuation\n    2\n"),
            ("return 2\n", "return 5\n"),
        ]:
            with self.subTest(old=old):
                self.assertIsNone(self.narrow(self.src, old, new))
        self.assertIsNone(self.narrow(self.src + "\n# stale\n", "x + 1", "x * 3"))

    def test_noop(self):
        self.assertEqual(self.narrow(self.src, "class A:", "# comment\nclass A:"), ("pass\n", "pass\n"))

    def test_folded_bytecode_is_not_ast_equality(self):
        self.assertIsNone(self.narrow(self.src, '"b": 1', '"b": 2 - 1'))


if __name__ == "__main__":
    unittest.main()
