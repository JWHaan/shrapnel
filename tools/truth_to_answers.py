"""Turn truth.py output into a sample-style directory scorable by score.py."""
import json
import shutil
import sys
from pathlib import Path

truth = json.loads(Path(sys.argv[1]).read_text())
patches = Path(sys.argv[2])
tests_json = Path(sys.argv[3])
out = Path(sys.argv[4])
tests = json.loads(tests_json.read_text())["tests"]
num = {t["id"]: t["number"] for t in tests}
files = {}
for t in tests:
    files.setdefault(t["file"], []).append(t["number"])
(out / "commits").mkdir(parents=True, exist_ok=True)
shutil.copy(tests_json, out / "tests.json")
ans = {}
for cid, fails in truth.items():
    if fails is None:
        continue
    nums = set()
    for f in fails:
        if f.startswith("<collect>"):
            nums.update(files.get(f[len("<collect>"):], []))
        elif f in num:
            nums.add(num[f])
        else:
            base = f.split("[")[0]  # parametrized / subtest ids
            if base in num:
                nums.add(num[base])
            else:
                print("unknown id", f, file=sys.stderr)
    ans[cid] = {"failing": sorted(nums), "kind": "synthetic"}
    shutil.copy(patches / f"{cid}.patch", out / "commits" / f"{cid}.patch")
(out / "answers.json").write_text(json.dumps({"commits": ans}))
print(len(ans), "commits;", sum(1 for a in ans.values() if a["failing"]), "with failures")
