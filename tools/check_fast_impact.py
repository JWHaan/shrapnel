"""Differentially check full Impact objects and time planning; never reads labels."""
import argparse
from collections import Counter
import gc
from pathlib import Path
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import fast_impact


def check(blast, args, directory, m):
    counts = Counter()
    cpu = [0.0, 0.0]
    original = blast.py_impact

    def fast(fn, src, fd, m, imp, new_src=None, only_file=False):
        subset = fast_impact.narrow_sources(src, new_src, fd, args.index) if fn in m.lines else None
        tag = "fallback" if subset is None else "noop" if subset == ("pass\n", "pass\n") else "body"
        counts[tag] += 1
        if subset is not None:
            src, new_src = subset
        return original(fn, src, fd, m, imp, new_src, only_file)

    with tempfile.TemporaryDirectory() as temp:
        patches = []
        for p in sorted(directory.glob("*.patch")):
            text = p.read_text()
            overlay = Path(temp) / p.stem
            for fn, src in blast.apply_patch(text, args.root).items():
                if src is not None:
                    (overlay / fn).parent.mkdir(parents=True, exist_ok=True)
                    (overlay / fn).write_text(src)
            patches.append((p, text, overlay))
        expected = {}
        for i, impl in enumerate((original, fast)):
            # No cross-charging the reference's abandoned ASTs to the fast pass.
            m._touch.clear()
            fast_impact._load.cache_clear()
            gc.collect()
            for p, text, overlay in patches:
                with patch.object(blast, "py_impact", impl):
                    t = time.process_time()
                    result = blast.impact(text, m, args.root, overlay)
                    cpu[i] += time.process_time() - t
                if i == 0:
                    expected[p] = result
                elif result != expected[p]:
                    fields = [k for k in vars(result) if getattr(result, k) != getattr(expected[p], k)]
                    raise AssertionError((p, fields, expected[p].notes, result.notes))
    print(directory, "patches", len(patches), counts, "CPU", *(round(n, 3) for n in cpu), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--index", required=True, type=Path)
    parser.add_argument("--reference", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("patches", nargs="+", type=Path)
    args = parser.parse_args()
    sys.path.insert(0, str(args.reference))
    import blast

    m = blast.Map(args.reference / "maps/map.json.gz")
    for directory in args.patches:
        check(blast, args, directory, m)
