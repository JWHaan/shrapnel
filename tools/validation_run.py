"""Run standalone pinned SQLGlot suites offline; labels are never mounted in the image."""
import argparse
import concurrent.futures
import hashlib
import json
import os
import subprocess
import tempfile
import time
from pathlib import Path
from queue import Queue

from validation_mutants import PIN


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(out, image, cid, expected_hash, cores, nodes, watchdog):
    case = out / ("control" if cid == "control" else f"runs/{cid}")
    if nodes:
        case /= "retry"
    case.mkdir(parents=True, exist_ok=False)
    plugin = Path(__file__).with_name("validation_plugin.py").resolve()
    patch = out / "commits" / f"{cid}.patch"
    archive = out / "pin.tar"
    with tempfile.TemporaryDirectory(dir=out) as tmp:
        if cid != "control":
            if digest(patch) != expected_hash:
                raise ValueError(f"manifest hash mismatch for {patch}")
            tree = Path(tmp, "tree")
            tree.mkdir()
            subprocess.run(["tar", "xf", archive, "-C", tree], check=True)
            # Apply exactly as Git would, outside any clone so no live checkout is changed.
            subprocess.run(["git", "apply", "--check", patch], cwd=tree, check=True)
            subprocess.run(["git", "apply", patch], cwd=tree, check=True)
            archive = Path(tmp, "tree.tar")
            subprocess.run(["tar", "--format", "ustar", "-cf", archive, "-C", tree, "."], check=True,
                           env={**os.environ, "COPYFILE_DISABLE": "1"})
        cmd = [
            "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none",
            "--cpuset-cpus", cores, "--memory", "4g", "-e", "SKIP_INTEGRATION=0",
            "-e", "PYTHONPATH=/suite:/offline", "-e", "OPENBLAS_NUM_THREADS=1",
            "-e", "PYTHONHASHSEED=0", "-e", "REPORT=/evidence/report.json",
            "-v", f"{archive}:/input/pin.tar:ro", "-v", f"{plugin}:/offline/validation_plugin.py:ro",
            "-v", f"{case}:/evidence", "--workdir", "/suite", image,
            "sh", "-ec", "tar xf /input/pin.tar; exec timeout --kill-after=15s 3600s "
            "python -m pytest -q -p validation_plugin "
            '-p no:cacheprovider --continue-on-collection-errors "$@"',
        ]
        # Pass node IDs as literal arguments; no shell interpretation.
        cmd.extend(["pytest-args", f"--timeout={watchdog}", "--tb=short", *(nodes or ["tests"])])
        (case / "provenance.json").write_text(json.dumps({
            "image": image, "pin": PIN, "patch_sha256": expected_hash,
            "plugin_sha256": digest(plugin), "command": cmd, "nodes": nodes,
        }, indent=2))
        start = time.monotonic()
        print(f"starting {cid} on cores {cores}", flush=True)
        with (case / "pytest.log").open("xb") as log:
            rc = subprocess.run(cmd, stdout=log, stderr=subprocess.STDOUT).returncode
        receipt = case / "exitcode.tmp"
        receipt.write_text(f"{rc}\n")
        receipt.replace(case / "exitcode")
        print(f"finished {cid} rc {rc} {time.monotonic() - start:.1f}s", flush=True)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("source", type=Path)
    p.add_argument("out", type=Path)
    p.add_argument("--cores", default="0,1", help="separate parallel container CPU sets with ';'")
    p.add_argument("--image", default="br-map")
    p.add_argument("--node", action="append", default=[],
                   help="targeted retry of an ambiguous node (can repeat); preserves original evidence")
    p.add_argument("--watchdog", type=int, default=1200, help="per-node wall seconds, allow for emulation")
    p.add_argument("cases", nargs="*", help="case IDs, 'control', or default: control and all mutants")
    args = p.parse_intermixed_args()
    out = args.out.resolve()
    image, arch = subprocess.check_output([
        "docker", "image", "inspect", args.image, "--format", "{{.Id}} {{.Architecture}}",
    ], text=True).split()
    if arch != "amd64":
        raise ValueError(f"wrong image architecture {arch}")
    lock = Path(__file__).resolve().parents[1] / "requirements.lock"
    check = """
import importlib.metadata as m, json, platform, sys
req = dict(line.split('==') for line in sys.stdin.read().splitlines() if line)
installed = {k:m.version(k) for k in req}
if req != installed or sys.version_info[:3] != (3, 13, 18) or platform.machine() != 'x86_64':
    sys.exit('wrong pytest labelling environment')
print(json.dumps({'python':sys.version, 'architecture':platform.machine(), 'locked_packages':installed}))
"""
    env = json.loads(subprocess.check_output([
        "docker", "run", "--rm", "--platform", "linux/amd64", "--network", "none", "-i",
        image, "python", "-c", check,
    ], input=lock.read_bytes()))
    (out / "environment.json").write_text(json.dumps(
        {**env, "lock_sha256": digest(lock), "image": image}, indent=2) + "\n")
    archive = subprocess.check_output(["git", "-C", args.source, "archive", PIN])
    if not (out / "pin.tar").exists():
        (out / "pin.tar").write_bytes(archive)
    if (out / "pin.tar").read_bytes() != archive:
        raise ValueError("base archive does not match clean pinned Git archive")
    manifest = {"control": {"sha256": None}, **json.loads((out / "manifest.json").read_text())}
    cores = args.cores.split(";")
    slots = Queue()
    for group in cores:
        slots.put(group)

    def one(cid):
        group = slots.get()
        try:
            run(out, image, cid, manifest[cid]["sha256"], group, args.node, args.watchdog)
        finally:
            slots.put(group)

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(cores)) as workers:
        list(workers.map(one, args.cases or manifest))


if __name__ == "__main__":
    main()
