"""Compare every selector-visible row, without loading any benchmark inputs."""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import blast
from static_map import StaticMap, build


class StaticMapTest(unittest.TestCase):
    def test_pristine_round_trip(self):
        with tempfile.TemporaryDirectory() as dest:
            path = Path(dest)
            for name in ("map.json.gz", "calls.json.gz"):
                (path / name).symlink_to(ROOT / "maps" / name)
            expected = blast.Map(path / "map.json.gz")
            build(path)
            actual = StaticMap(path / "static.db")
            try:
                for name in vars(expected).keys() - {"tasks", "sets"}:
                    self.assertEqual(getattr(actual, name), getattr(expected, name), name)
                self.assertEqual(len(actual.sets), len(expected.sets))
                for i, row in enumerate(expected.sets):
                    self.assertEqual(list(actual.sets[i]), row, i)
            finally:
                actual.lines.db.close()


if __name__ == "__main__":
    unittest.main()
