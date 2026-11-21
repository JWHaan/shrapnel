"""Small integrity tests for the offline exporter (no SQLGlot checkout required)."""
import unittest
from pathlib import Path
from unittest.mock import patch

from validation_verify import labels, verify


class TestReport(unittest.TestCase):
    def setUp(self):
        self.id = "tests/test_ok.py::test_x"
        self.broken = "tests/test_broken.py"
        self.skip = "tests/test_ok.py::test_skip"
        self.catalog = {"tests": [{"id": self.id, "number": 0},
                                  {"id": self.broken + "::test_y", "number": 1}],
                        "skipped": [self.skip]}
        self.report = {
            "exitstatus": 1, "failed": [self.id], "skipped": [self.skip],
            "collection_errors": {self.broken: "SyntaxError"},
            "collected": [self.id, self.skip],
            "phases": {self.id: ["setup", "call", "teardown"], self.skip: ["setup", "teardown"]},
        }

    def verify(self, rc=1):
        return labels(self.report, rc, self.catalog, {"tests/test_ok.py", self.broken})

    def test_maps_failure_and_broken_file(self):
        self.assertEqual(self.verify(), [0, 1])

    def test_rejects_interrupted_executor(self):
        with self.assertRaisesRegex(ValueError, "exit"):
            self.verify(137)

    def test_rejects_missing_module_without_collection_error(self):
        self.report["collection_errors"].clear()
        with self.assertRaisesRegex(ValueError, "unaccounted"):
            self.verify()

    def test_rejects_missing_teardown(self):
        self.report["phases"][self.id].remove("teardown")
        with self.assertRaisesRegex(ValueError, "incomplete"):
            self.verify()

    def test_rejects_skipped_numbered_test(self):
        self.report["skipped"].append(self.id)
        with self.assertRaisesRegex(ValueError, "unexpected skips"):
            self.verify()

    def test_rejects_unknown_failed_subtest_id(self):
        self.report["failed"] = [self.id + "[x]"]
        with self.assertRaisesRegex(ValueError, "unknown"):
            self.verify()

    def test_timeout_requires_passing_same_patch_replay(self):
        original = self.report, 1, f"FAILED {self.id} - Failed: Timeout (>300.0s)", {}
        replay = {"failed": [], "skipped": [], "exitstatus": 0, "collection_errors": {},
                  "collected": [self.id], "phases": {self.id: ["setup", "call", "teardown"]}}
        with patch("validation_verify.evidence", side_effect=[original, (replay, 0, "", {"nodes": [self.id]})]):
            self.assertEqual(verify(Path("/run"), "patch-sha", self.catalog, {self.broken}, "image", {self.id}),
                             ([1], [self.broken], {self.id}))

    def test_timeout_not_observed_on_control_is_unresolved(self):
        original = self.report, 1, f"FAILED {self.id} - Failed: Timeout (>300.0s)", {}
        with patch("validation_verify.evidence", return_value=original):
            with self.assertRaisesRegex(ValueError, "original control"):
                verify(Path("/run"), "patch-sha", self.catalog, {self.broken}, "image", set())


if __name__ == "__main__":
    unittest.main()
