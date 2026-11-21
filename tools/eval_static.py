import json, sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast
root = Path(sys.argv[1]); sample = Path(sys.argv[2])
import os
m = blast.Map(Path(os.environ.get("MAP", Path(__file__).resolve().parents[1] / "maps/map.json.gz")))
tests = {t["number"]: t["seconds"] for t in json.load(open(sample / "tests.json"))["tests"]}
ans = json.load(open(sample / "answers.json"))["commits"]
tot_s = 0
for p in sorted((sample / "commits").glob("*.patch")):
    import tempfile
    d = Path(tempfile.mkdtemp())
    for f, t in blast.apply_patch(p.read_text(), root).items():
        if t is not None:
            (d / f).parent.mkdir(parents=True, exist_ok=True); (d / f).write_text(t)
    imp = blast.impact(p.read_text(), m, root, d)
    fail = set(ans[p.stem]["failing"])
    miss = fail - imp.tests
    s = sum(tests[t] for t in imp.tests)
    tot_s += s
    print(f"{p.stem} {ans[p.stem]['kind']:8} cand={len(imp.tests):4} sec={s:7.2f} fail={len(fail):3} MISS={sorted(miss)} {imp.notes[:3]}")
print("total cand seconds", round(tot_s, 1))
