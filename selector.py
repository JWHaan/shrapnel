"""Schedule tests from pristine execution evidence without executing a patch."""
from __future__ import annotations

import json
import os
import sys
import time
from bisect import bisect_left
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import blast  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
ROOT = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
MAX_WORKERS = 8
WORKER_COST = 2.0


# ---------------------------------------------------------------- packing


def _lpt_options(nums, ms):
    order = sorted(nums, key=lambda n: -ms[n])
    for k in range(1, min(MAX_WORKERS, len(nums)) + 1):
        loads = [0] * k
        ws: list[list[int]] = [[] for _ in range(k)]
        for n in order:
            j = loads.index(min(loads))
            loads[j] += ms[n]
            ws[j].append(n)
        yield max(loads), ws


def _balance(ws, ms, lower):
    loads = [sum(ms[n] for n in w) for w in ws]
    if max(loads) == lower:
        return ws
    if len(ws) == 2:
        both = ws[0] + ws[1]
        half = sum(loads) // 2
        # One bit per reachable sum; store prefixes to reconstruct the best partition.
        if (half + 1) * len(both) <= 16_000_000:
            mask = (1 << (half + 1)) - 1
            reachable = 1
            history = []
            for n in both:
                history.append((n, reachable))
                reachable |= (reachable << ms[n]) & mask
                if reachable.bit_length() > half:
                    break
            target = reachable.bit_length() - 1
            chosen = set()
            for n, before in reversed(history):
                if not (before >> target) & 1:
                    chosen.add(n)
                    target -= ms[n]
            return [[n for n in both if n in chosen], [n for n in both if n not in chosen]]

    # A move (swap with a virtual zero) or swap reduces imbalance by transferring
    # an amount nearest half the gap. This also makes progress on tied peak loads.
    for _ in range(12):
        i = loads.index(max(loads))
        if loads[i] == lower:
            break
        gain = 0
        for j, worker in enumerate(ws):
            gap = loads[i] - loads[j]
            if gap < 2:
                continue
            right = sorted(worker, key=ms.__getitem__)
            weights = [0] + [ms[n] for n in right]
            for a in ws[i]:
                pos = bisect_left(weights, ms[a] - gap // 2)
                for p in (pos - 1, pos):
                    if 0 <= p < len(weights):
                        d = ms[a] - weights[p]
                        if d * (gap - d) > gain:
                            gain = d * (gap - d)
                            move = j, a, right[p - 1] if p else None, d
        if not gain:
            break
        j, a, b, d = move
        ws[i].remove(a)
        ws[j].append(a)
        if b is not None:
            ws[j].remove(b)
            ws[i].append(b)
        loads[i] -= d
        loads[j] += d
    return ws


def final_pack(nums: list[int], secs: dict[int, float], *, fill_slack: bool = True) -> list[list[int]]:
    """Preserve every selection; improve LPT without increasing its worker count."""
    if not nums:
        return []
    ms = {n: int(secs[n] * 1000 + 0.5) for n in (secs if fill_slack else nums)}
    fee = int(1000 * WORKER_COST)
    options = [(peak + fee * len(ws), ws) for peak, ws in _lpt_options(nums, ms)]
    cost, best = min(options, key=lambda result: result[0])
    baseline_peak = cost - fee * len(best)
    total = sum(ms[n] for n in nums)
    longest = max(ms[n] for n in nums)
    for _, ws in options:
        k = len(ws)
        if k > len(best):
            break
        lower = max(longest, (total + k - 1) // k)
        overhead = fee * k
        if (lower + overhead, k) >= (cost, len(best)):
            continue
        ws = _balance(ws, ms, lower)
        new_cost = max(sum(ms[n] for n in w) for w in ws) + overhead
        if new_cost - overhead <= baseline_peak and (new_cost, k) < (cost, len(best)):
            cost, best = new_cost, ws

    if fill_slack:
        loads = [sum(ms[n] for n in w) for w in best]
        room = [max(loads) - load for load in loads]
        required = set(nums)
        if not any(room):
            best[0].extend(n for n in secs if ms[n] == 0 and n not in required)
            return best
        for n in sorted((n for n in secs if n not in required), key=ms.__getitem__):
            j = min((i for i, r in enumerate(room) if r >= ms[n]), key=room.__getitem__, default=None)
            if j is None:
                break
            best[j].append(n)
            room[j] -= ms[n]
    return best


def main() -> None:
    tests = json.loads((DATA / "tests.json").read_bytes())["tests"]
    secs = {t["number"]: t["seconds"] for t in tests}
    numbers = {t["id"]: t["number"] for t in tests}
    all_tests = final_pack(list(secs), secs, fill_slack=False)
    try:
        m = blast.Map(APP / "maps" / "map.json.gz")
        remap = {i: numbers[tid] for i, tid in enumerate(m.ids) if tid in numbers}
        unknown = set(secs) - set(remap.values())
    except Exception as exc:
        print(f"Maps unavailable: {exc!r}", file=sys.stderr)
        m = None

    patches = sorted((DATA / "commits").glob("*.patch"))
    for patch in patches:
        tick = time.process_time()
        workers = all_tests
        try:
            if m is not None:
                edits = blast.modified_files(patch.read_text(encoding="utf-8"), ROOT)
                imp = blast.Impact()
                for fd, before, after in edits:
                    fn = fd.old
                    if fn.endswith(".py") and fn.startswith(("sqlglot/", "tests/")):
                        if Path(fn).name in {"conftest.py", "__init__.py"}:
                            raise ValueError("collection or package initialization")
                        compile(after, fn, "exec", dont_inherit=True)
                        blast.py_impact(fn, before, fd, m, imp, after, only_file=len(edits) == 1)
                    elif fn.startswith("tests/fixtures/"):
                        readers = m.opened(fn)
                        if readers is None or -1 in readers:
                            raise ValueError("unknown or collection-time fixture readers")
                        imp.tests.update(readers)
                    else:
                        raise ValueError("unsupported file")
                if not imp.everything and not imp.syntax_error:
                    selected = {remap[t] for t in imp.tests | imp.line_tests if t in remap}
                    workers = final_pack(sorted(selected | unknown), secs)
        except Exception as exc:
            print(f"{patch.stem}: full suite ({exc!r})", file=sys.stderr)
        print(json.dumps({"commit": patch.stem, "workers": workers}, separators=(",", ":")))
        print("plan_cpu", patch.stem, time.process_time() - tick, file=sys.stderr)


if __name__ == "__main__":
    main()
