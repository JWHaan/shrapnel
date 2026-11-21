"""Source-level rule and receipt checks; optional offline pin/raw replay (no patch execution)."""
import hashlib
import json
import os
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import escape_reads as er
from blast import FileDiff, Map
from tools.build_escape_reads import build


class EscapeReads(unittest.TestCase):
    def setUp(self):
        self.fn = "sqlglot/dialects/example.py"
        self.src = 'class Tokenizer:\n    NUMERIC_ESCAPES = {"x": (16, 1, 2, 255)}\n'
        self.fd = FileDiff(self.fn, self.fn, removed=[2])
        self.good = self.src.replace("16,", "0x8,")
        lo = self.src.index("16,")
        ids = [str(n) for n in range(9)]
        id_hash = hashlib.sha256("\n".join(ids).encode()).hexdigest()
        self.index = {"files": {self.fn: [hashlib.sha256(self.src.encode()).hexdigest(), {"2": [[lo, lo+2]]}]},
                      "ids_sha256": id_hash, "reads": [10, 20], "raw_children_set": 0,
                      "coverage_sha256": hashlib.sha256(json.dumps([1, 2, 3, 7]).encode()).hexdigest()}
        self.m = SimpleNamespace(ids=ids, calls={"ids_sha256": id_hash, "unknown": [5],
            "inspected": {"other": [-1], "<child>": [4], "sqlglot.dialects.example": [6]},
            "calls": {"any": [0]}, "validated": {"any": [0]}}, sets=[[3]],
            lines={er.CORE: {20: 1}}, collect={er.CORE: set()},
            opened=lambda fn: {7}, tests_on=lambda f, a, b: {1} if a == 10 else {2})

    def select(self, after=None, edits=None):
        with patch.object(er, "_index", return_value=self.index):
            return er.tests_for(edits if edits is not None else
                                [(self.fd, self.src, self.good if after is None else after)], self.m)

    def test_retains_both_global_readers_source_children_unknown_and_inspections(self):
        self.assertEqual(self.select(), {1, 2, 3, 4, 5, 6, 7})

    def test_rejects_non_value_edits_second_change_and_wrong_pin(self):
        for new in (self.src.replace('"x"', '"X"'), self.good + "# other change\n",
                    self.good.replace("255", "1"), self.src.replace("16,", "8+1,"),
                    self.src.replace("16,", "True,"), self.src.replace("16,", "-1,")):
            self.assertIsNone(self.select(new))
        self.assertIsNone(self.select(edits=[(self.fd, self.src, self.good)] * 2))
        self.assertIsNone(self.select(edits=[(self.fd, self.src + "\n", self.good + "\n")]))

    def test_unknown_profile_malformed_inspections_and_mismatched_inventory(self):
        calls = self.m.calls
        for broken in ({}, {**calls, "ids_sha256": ""}, {**calls, "unknown": None},
                       {**calls, "unknown": [12]}, {**calls, "inspected": {}},
                       {**calls, "inspected": {"other": [-1], "<child>": ["0"]}},
                       {**calls, "inspected": {"sqlglot.dialects.example": [-1]}}):
            self.m.calls = broken
            self.assertIsNone(self.select())
        self.m.calls = calls
        self.m.ids.reverse()
        self.assertIsNone(self.select())

    def test_collection_read_cannot_be_localised(self):
        self.m.collect[er.CORE].add(10)
        self.assertIsNone(self.select())
        self.m.collect[er.CORE].clear()
        self.m.opened = lambda _: {-1}
        self.assertIsNone(self.select())

    def test_wrong_loaded_map_with_matching_inventory(self):
        self.m.tests_on = lambda *args: set()
        self.assertIsNone(self.select())

    def test_actual_sidecar_and_map_must_both_match(self):
        self.assertEqual(len(er._index(er.MAPS)["files"]), 9)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp)
            self.assertIsNone(er._index(path))
            (path / "escape_reads.json").write_bytes((er.MAPS / "escape_reads.json").read_bytes())
            (path / "map.json.gz").write_bytes(b"wrong map")
            er._index.cache_clear()
            self.assertIsNone(er._index(path))
            (path / "map.json.gz").write_bytes((er.MAPS / "map.json.gz").read_bytes())
            (path / "escape_reads.json").write_text('{"files":{}}')
            er._index.cache_clear()
            self.assertIsNone(er._index(path))

    @unittest.skipUnless(os.getenv("ESCAPE_PIN") and os.getenv("ESCAPE_REPORTS"), "offline mapper inputs")
    def test_rebuild_and_replay_all_dialects(self):
        pin, reports = map(Path, (os.environ["ESCAPE_PIN"], os.environ["ESCAPE_REPORTS"]))
        data = build(pin, reports, er.MAPS / "map.json.gz")
        self.assertEqual(json.dumps(data, separators=(",", ":"), sort_keys=True) + "\n",
                         (er.MAPS / "escape_reads.json").read_text())
        m = Map(er.MAPS / "map.json.gz")
        with tarfile.open(pin) as tar:
            for fn, (_, rows) in data["files"].items():
                src = tar.extractfile(fn).read().decode()
                ln = next(iter(rows))
                lo, hi = rows[ln][0]
                fd = FileDiff(fn, fn, removed=[ln])
                edit = (fd, src, src[:lo] + "7" + src[hi:])
                with self.subTest(fn=fn):
                    self.assertIsNone(er.tests_for([(fd, src, edit[2] + "# extra\n")], m))
                    keep = er.tests_for([edit], m)
                    if not m.calls:
                        self.assertIsNone(keep)
                    else:
                        guard = set(m.calls["unknown"]) | set(m.calls["inspected"].get("<child>", ()))
                        guard.update(m.calls["inspected"].get(fn[:-3].replace("/", "."), ()))
                        readers = set.union(*(m.tests_on(er.CORE, n, n) for n in data["reads"]))
                        self.assertEqual(keep, None if -1 in guard else
                                         readers | guard | (m.opened(fn) or set()) | set(m.sets[data["raw_children_set"]]))


if __name__ == "__main__":
    unittest.main()
