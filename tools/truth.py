"""Ground truth inside the scorer image: apply each patch to a copy, run the full suite, list failures."""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, "/app")
import blast

src = Path("/src")
patches = sorted(Path(sys.argv[1]).glob("*.patch"))
out = Path(sys.argv[2])
res = json.loads(out.read_text()) if out.exists() else {}
for p in patches:
    if p.stem in res:
        continue
    w = Path("/w")
    if w.exists():
        shutil.rmtree(w)
    shutil.copytree(src, w, ignore=shutil.ignore_patterns(".git", "__pycache__"))
    for f, t in blast.apply_patch(p.read_text(), src).items():
        q = w / f
        if t is None:
            q.unlink(missing_ok=True)
        else:
            q.parent.mkdir(parents=True, exist_ok=True)
            q.write_text(t)
    env = dict(os.environ, SKIP_INTEGRATION="0", PYTHONPATH="/w:/tools", FAILS_OUT="/tmp/fails.json")
    subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "fail_plugin", "-p", "no:cacheprovider", "-n", os.environ.get("NPROC", "8"), "--timeout", "300", "tests"], cwd=w, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    fails = json.load(open("/tmp/fails.json")) if os.path.exists("/tmp/fails.json") else None
    res[p.stem] = fails
    out.write_text(json.dumps(res))
    print(p.stem, None if fails is None else len(fails), flush=True)
