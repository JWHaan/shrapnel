"""Static recall of the candidate sets against truth.json from synthetic mutants."""
import json, sys, tempfile
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import blast
root = Path(sys.argv[1]); truth = json.load(open(sys.argv[2])); pdir = Path(sys.argv[3])
m = blast.Map(Path(__file__).resolve().parents[1] / "maps/map.json.gz")
idn = {t: i for i, t in enumerate(m.ids)}
tests = {t["number"]: t["seconds"] for t in json.load(open(sys.argv[4]))["tests"]}
miss_total = 0
for cid, fails in sorted(truth.items()):
    if fails is None:
        continue
    text = (pdir / f"{cid}.patch").read_text()
    d = Path(tempfile.mkdtemp())
    for f, t in blast.apply_patch(text, root).items():
        if t is not None:
            (d / f).parent.mkdir(parents=True, exist_ok=True); (d / f).write_text(t)
    imp = blast.impact(text, m, root, d)
    fnums = set()
    for f in fails:
        if f.startswith("<collect>"):
            fnums |= m.in_file(f[9:])
        else:
            fnums.add(idn.get(f, idn.get(f.split("[")[0], -1)))
    miss = fnums - imp.tests
    miss_total += bool(miss)
    print(f"{cid} fail={len(fnums):3} cand={len(imp.tests):4} sec={sum(tests.get(n,0) for n in imp.tests):6.2f} {'MISS '+str([m.ids[x] for x in miss][:4]) if miss else ''} {imp.notes[:2]}")
print("commits with misses:", miss_total)
