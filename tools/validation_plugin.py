"""Offline-only pytest log for a single non-xdist full-suite run."""
import json
import os
from pathlib import Path


collected = []
phases = {}
failed = set()
skipped = set()
collection_errors = {}


def pytest_collection_finish(session):
    collected.extend(item.nodeid for item in session.items)


def pytest_collectreport(report):
    if report.failed:
        collection_errors[report.nodeid] = str(report.longrepr)


def pytest_runtest_logreport(report):
    # SubTestReports may precede a passing call report for the same node.
    phases.setdefault(report.nodeid, set()).add(report.when)
    if report.failed:
        failed.add(report.nodeid)
    elif report.skipped and not hasattr(report, "context"):
        skipped.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    Path(os.environ["REPORT"]).write_text(json.dumps({
        "exitstatus": int(exitstatus), "collected": collected,
        "phases": {k: sorted(v) for k, v in phases.items()},
        "failed": sorted(failed), "skipped": sorted(skipped),
        "collection_errors": collection_errors,
    }))
