"""Publish offline answers only after every numbered node is accounted for."""
import argparse
import json
import re
import shutil
import tarfile
from pathlib import Path

from validation_mutants import PIN
from validation_run import digest


def require(ok, why):
    if not ok:
        raise ValueError(why)


def labels(report, rc, catalog, test_files):
    """Return failing numbers; refuse a stopped suite, unknown ID or unexplained skip."""
    numbered = {t["id"]: t for t in catalog["tests"]}
    known_skips = set(catalog["skipped"])
    errors = report["collection_errors"]
    require(set(errors) <= test_files, f"unknown collection error: {set(errors) - test_files}")
    failed = set(report["failed"])
    require(rc == report["exitstatus"] == int(bool(failed or errors)), f"unexpected exit: {rc}")
    collected = set(report["collected"])
    require(len(collected) == len(report["collected"]), "duplicate collected node")
    known = set(numbered) | known_skips
    unimportable = {node for node in known if node.partition("::")[0] in errors}
    expected = known - unimportable
    require(expected == collected, f"unaccounted IDs: {sorted(expected ^ collected)[:10]}")
    require(set(report["phases"]) == expected, "test missing execution evidence")
    skipped = set(report["skipped"])
    require(skipped == known_skips - unimportable, f"unexpected skips: {skipped ^ known_skips}")
    require(failed <= collected - skipped, "unknown/skipped node reported failed")
    for node, stages in report["phases"].items():
        expected_stages = {"setup", "call", "teardown"}
        if node in skipped | failed and "call" not in stages:  # setup failure/skip
            expected_stages.remove("call")
        require(set(stages) == expected_stages, f"incomplete pytest phases for {node}: {stages}")
    return sorted(numbered[n]["number"] for n in (failed | unimportable) & numbered.keys())


def evidence(run, patch_hash, image):
    prov = json.loads((run / "provenance.json").read_text())
    for k, v in {
        "image": image, "pin": PIN, "patch_sha256": patch_hash,
        "plugin_sha256": digest(Path(__file__).with_name("validation_plugin.py")),
    }.items():
        require(prov[k] == v, f"{run}: unexpected {k}")
    log = (run / "pytest.log").read_text(errors="replace")
    require(not re.search(r"BrokenProcessPool|Resource temporarily unavailable|"
                          r"INTERNALERROR>|MemoryError|No space left on device", log),
            f"{run}: infrastructure failure; investigate")
    return (json.loads((run / "report.json").read_text()),
            int((run / "exitcode").read_text()), log, prov)


def verify(run, patch_hash, catalog, files, image, control_timeouts=None):
    report, rc, log, _ = evidence(run, patch_hash, image)
    nums = labels(report, rc, catalog, files)
    timeouts = set()
    if "Failed: Timeout (" in log:
        # A local watchdog on an emulated CPU is not scorer timing. Never
        # subtract a control failure blindly: require a real passing replay
        # on the exact same patch, plus the same timeout on the clean control.
        timeouts = set(re.findall(r"^FAILED (\S+) - Failed: Timeout\b", log, re.M))
        require(timeouts and timeouts <= set(report["failed"]), f"{run}: unattributed timeout")
        require(control_timeouts is None or timeouts <= control_timeouts,
                f"{run}: watchdog tripped on a node that passed the original control")
        replay, rr, rlog, prov = evidence(run / "retry", patch_hash, image)
        require(set(prov["nodes"]) == timeouts and "Failed: Timeout (" not in rlog,
                f"{run}: mismatched or timed out replay")
        subset = {"tests": [t for t in catalog["tests"] if t["id"] in timeouts], "skipped": []}
        require(rr == 0 and not replay["collection_errors"] and not labels(replay, rr, subset, files),
                f"{run}: replay was not clean")
        nums = sorted(set(nums) - {t["number"] for t in subset["tests"]})
    return nums, list(report["collection_errors"]), timeouts


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("run_dir", type=Path)
    p.add_argument("tests_json", type=Path)
    p.add_argument("publish_dir", type=Path, help="data/ for selector, answers.json for scorer only")
    p.add_argument("cases", nargs="*", help="default: all required; use a separate publish_dir for subsets")
    a = p.parse_args()
    out = a.run_dir
    catalog = json.loads(a.tests_json.read_bytes())
    entries = catalog["tests"]
    require([t["number"] for t in entries] == list(range(len(entries))) and
            len({t["id"] for t in entries}) == len(entries) and
            all(t["id"].partition("::")[0] == t["file"] for t in entries), "bad scorer inventory")
    with tarfile.open(out / "pin.tar") as tar:
        files = {p for p in tar.getnames() if p.startswith("tests/") and re.search(r"/test_[^/]+\.py$", p)}
    env = json.loads((out / "environment.json").read_text())
    lock = Path(__file__).resolve().parents[1] / "requirements.lock"
    require(env["lock_sha256"] == digest(lock) and env["architecture"] == "x86_64"
            and env["python"].split()[0] == "3.13.18"
            and env["locked_packages"] == dict(s.split("==") for s in lock.read_text().splitlines()),
            "environment does not match pinned lock")
    failed, broken, control_timeouts = verify(out / "control", None, catalog, files, env["image"])
    require(not failed and not broken, "control not clean")
    manifest = json.loads((out / "manifest.json").read_text())
    answers = {}
    for case in a.cases or manifest:
        sha = digest(out / "commits" / f"{case}.patch")
        require(sha == manifest[case]["sha256"], "patch changed")
        try:
            nums, errors, retries = verify(out / "runs" / case, sha, catalog, files,
                                           env["image"], control_timeouts)
        except ValueError as exc:
            raise ValueError(f"{case}: {exc}") from exc
        answers[case] = {"failing": nums, "kind": manifest[case]["kind"]}
        print(f"{case}: {len(nums)} failed; {len(errors)} collection errors; {len(retries)} verified retries")

    # Do not publish even a partial answers file if any required case was invalid.
    data = a.publish_dir / "data"
    if not data.exists():
        (data / "commits").mkdir(parents=True)
        shutil.copyfile(a.tests_json, data / "tests.json")
        for case in answers:
            name = f"{case}.patch"
            shutil.copyfile(out / "commits" / name, data / "commits" / name)
    require(digest(data / "tests.json") == digest(a.tests_json), "wrong scorer inventory")
    require({p.stem for p in (data / "commits").glob("*.patch")} == set(answers),
            "score directory contains different cases")
    require(all(digest(data / "commits" / f"{c}.patch") == manifest[c]["sha256"] for c in answers),
            "score directory contains different patch bytes")
    dest = a.publish_dir / "answers.json"
    tmp = dest.with_suffix(".tmp")
    tmp.write_text(json.dumps({"commits": answers}, indent=2) + "\n")
    tmp.replace(dest)


if __name__ == "__main__":
    main()
