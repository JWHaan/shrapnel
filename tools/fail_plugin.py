import json, os
_f = set()
def pytest_runtest_logreport(report):
    if report.failed:
        _f.add(report.nodeid)
def pytest_collectreport(report):
    if report.failed:
        _f.add("<collect>" + report.nodeid)
def pytest_sessionfinish(session):
    if not os.environ.get("PYTEST_XDIST_WORKER"):
        json.dump(sorted(_f), open(os.environ["FAILS_OUT"], "w"))
