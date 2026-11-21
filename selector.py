"""Blast Radius selector.

For each patch: work out which tests could notice it (coverage + class-usage map
built from the pinned sqlglot), then run the cheap candidates against the patched
tree and keep only the ones that fail. Expensive candidates are scheduled blind.
"""
from __future__ import annotations

import json
import os
import select
import signal
import sys
import time
import traceback
from pathlib import Path

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))
import blast  # noqa: E402

DATA = Path(os.environ.get("BR_DATA", "/data"))
ROOT = Path(os.environ.get("BR_SQLGLOT", "/opt/sqlglot"))
TMP = Path(os.environ.get("BR_TMP", "/tmp"))
MAX_WORKERS = 8
WORKER_COST = 2.0
JOBS = int(os.environ.get("BR_JOBS", "2"))
OUT = os.fdopen(os.dup(1), "w", buffering=1)  # answers only
os.dup2(2, 1)  # anything else printed goes to stderr
sys.stdout = sys.stderr

# Cost model knobs (seconds of CPU)
RUN_OVERHEAD = float(os.environ.get("BR_OVERHEAD", "1.0"))
RUN_FACTOR = float(os.environ.get("BR_FACTOR", "1.0"))
DEADLINE = float(os.environ.get("BR_DEADLINE", "150"))  # wall seconds for the whole batch


def log(*a):
    print(*a, file=sys.stderr, flush=True)


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


def pack(nums: list[int], secs: dict[int, float]) -> tuple[float, list[list[int]]]:
    """Cheap LPT cost estimate for choose; use the scorer's integer milliseconds."""
    ms = {n: int(secs[n] * 1000 + 0.5) for n in nums}
    return min(((peak / 1000 + WORKER_COST * len(ws), ws) for peak, ws in _lpt_options(nums, ms)),
               key=lambda result: result[0], default=(0.0, []))


def _balance(ws, ms, lower):
    from bisect import bisect_left

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


# ---------------------------------------------------------------- overlay


def make_overlay(dest: Path, changes: dict[str, str | None]) -> None:
    """dest mirrors ROOT through symlinks, with changed files written for real."""
    dest.mkdir(parents=True)
    real_dirs: set[str] = {""}
    for f in changes:
        parts = f.split("/")[:-1]
        for i in range(1, len(parts) + 1):
            real_dirs.add("/".join(parts[:i]))
    for d in sorted(real_dirs, key=len):
        src = ROOT / d if d else ROOT
        dst = dest / d if d else dest
        dst.mkdir(exist_ok=True)
        if src.is_dir():
            for entry in os.scandir(src):
                rel = f"{d}/{entry.name}" if d else entry.name
                if rel in real_dirs or rel in changes:
                    continue
                os.symlink(entry.path, dst / entry.name)
    for f, text in changes.items():
        if text is None:
            continue
        p = dest / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", errors="surrogateescape")


# ---------------------------------------------------------------- child


class _SkippedTask(BaseException):
    """Raised by results of pool tasks we chose not to run; swallowed by subTest."""


class _Poison:
    def __getattr__(self, name):
        raise _SkippedTask(name)

    def __bool__(self):
        raise _SkippedTask("bool")

    def __iter__(self):
        raise _SkippedTask("iter")


TASKS = {"allowed": None, "n": 0}


class InlineExecutor:
    """ProcessPoolExecutor stand-in: same pickling, no spawned interpreters."""

    def __init__(self, max_workers=None, mp_context=None, initializer=None, initargs=(), **kw):
        if initializer is not None:
            initializer(*initargs)

    def submit(self, fn, *args, **kwargs):
        import concurrent.futures as cf
        import pickle

        f = cf.Future()
        n = TASKS["n"]
        TASKS["n"] = n + 1
        allowed = TASKS["allowed"]
        if allowed is not None and n not in allowed:
            f.set_result(None if getattr(fn, "__name__", "") == "mp_execute" else _Poison())
            return f
        try:
            fn, args, kwargs = pickle.loads(pickle.dumps((fn, args, kwargs)))
            f.set_result(pickle.loads(pickle.dumps(fn(*args, **kwargs))))
        except BaseException as e:  # noqa: BLE001
            f.set_exception(e)
        return f

    def map(self, fn, *its, **kw):
        return map(fn, *its)

    def shutdown(self, *a, **k):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.shutdown()


class _Timeout(BaseException):
    pass


def install_patches(with_tasks: bool) -> None:
    import concurrent.futures
    import contextlib
    import unittest

    concurrent.futures.ProcessPoolExecutor = InlineExecutor
    if with_tasks:
        orig_subtest = unittest.TestCase.subTest

        @contextlib.contextmanager
        def subTest(self, msg=unittest.case._subtest_msg_sentinel, **params):
            with orig_subtest(self, msg, **params):
                try:
                    yield
                except _SkippedTask:
                    pass

        unittest.TestCase.subTest = subTest


def child_main(overlay: Path, ids: list[str], limits: dict[str, float], wfd: int, tasks: dict[str, set[int]] | None = None) -> None:
    import concurrent.futures
    import contextlib
    import unittest

    import pytest

    tasks = tasks or {}
    install_patches(bool(tasks))
    out = os.fdopen(wfd, "w", buffering=1)
    devnull = os.open(os.devnull, os.O_WRONLY)
    os.dup2(devnull, 1)
    os.dup2(devnull, 2) if not os.environ.get("BR_CHILD_LOG") else None
    os.chdir(overlay)
    sys.path[:0] = [str(overlay)]
    os.environ["SKIP_INTEGRATION"] = "0"
    sys.dont_write_bytecode = True

    status: dict[str, bool] = {}

    class Collect:
        @pytest.hookimpl(hookwrapper=True)
        def pytest_runtest_call(self, item):
            lim = limits.get(item.nodeid, 30.0)

            def on_alarm(signum, frame):
                raise _Timeout(f"timeout after {lim}s")

            TASKS["allowed"] = tasks.get(item.nodeid)
            TASKS["n"] = 0
            old = signal.signal(signal.SIGALRM, on_alarm)
            signal.setitimer(signal.ITIMER_REAL, lim)
            try:
                yield
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old)

        def pytest_runtest_logreport(self, report):
            nid = report.nodeid
            if report.failed:
                status[nid] = False
            elif nid not in status:
                status[nid] = True
            if report.when == "teardown":
                out.write(json.dumps([nid, status.get(nid, False)]) + "\n")

        def pytest_collectreport(self, report):
            if report.failed:
                out.write(json.dumps(["<collect>", report.nodeid]) + "\n")

    args = ["-q", "-p", "no:cacheprovider", "--assert=plain", "-p", "no:randomly", "-o", "addopts=", "--rootdir", str(overlay)] + ids
    try:
        pytest.main(args, plugins=[Collect()])
    except BaseException:
        traceback.print_exc()
    out.write(json.dumps(["<done>"]) + "\n")
    out.flush()
    os._exit(0)


# ---------------------------------------------------------------- main


class Job:
    def __init__(self, cid, run_ids, overlay, limits, budget, tasks=None):
        self.tasks = tasks or {}
        self.cid = cid
        self.run_ids = run_ids
        self.overlay = overlay
        self.limits = limits
        self.budget = budget
        self.results: dict[str, bool] = {}
        self.collect_errors: list[str] = []
        self.done = False
        self.pid = None
        self.cpu = -1.0
        self.fd = None
        self.buf = b""

    def start(self):
        r, w = os.pipe()
        pid = os.fork()
        if pid == 0:
            os.close(r)
            try:
                child_main(self.overlay, self.run_ids, self.limits, w, self.tasks)
            finally:
                os._exit(1)
        os.close(w)
        self.pid, self.fd = pid, r
        self.started = time.monotonic()

    def feed(self, data: bytes):
        self.buf += data
        while b"\n" in self.buf:
            line, self.buf = self.buf.split(b"\n", 1)
            try:
                msg = json.loads(line)
            except Exception:
                continue
            if msg[0] == "<done>":
                self.done = True
            elif msg[0] == "<collect>":
                self.collect_errors.append(msg[1])
            else:
                self.results[msg[0]] = bool(msg[1])


def main() -> None:
    t0 = time.monotonic()
    meta = json.loads((DATA / "tests.json").read_text())
    tests = meta["tests"]
    secs = {t["number"]: float(t["seconds"]) for t in tests}
    by_id = {t["id"]: t["number"] for t in tests}
    m = blast.Map(APP / "maps" / "map.json.gz")
    # map numbers -> tests.json numbers via ids
    remap = {i: by_id[tid] for i, tid in enumerate(m.ids) if tid in by_id}
    unknown = [n for tid, n in by_id.items() if tid not in set(m.ids)]
    all_nums = [t["number"] for t in tests]
    try:
        cpu_by_id = json.loads((APP / "maps" / "cpu.json").read_text())
    except Exception:  # noqa: BLE001
        cpu_by_id = {}
    global CPU
    CPU = {t["number"]: max(float(t["seconds"]), cpu_by_id.get(t["id"], float(t["seconds"]) * 1.5)) for t in tests}
    id_of = {t["number"]: t["id"] for t in tests}

    patches = sorted((DATA / "commits").glob("*.patch"))
    answers: dict[str, list[list[int]]] = {}
    jobs: list[Job] = []
    plans: dict[str, tuple[list[int], list[int]]] = {}

    for p in patches:
        cid = p.stem
        try:
            text = p.read_text(encoding="utf-8", errors="surrogateescape")
            changes = blast.apply_patch(text, ROOT)
            odir = TMP / "w" / cid
            make_overlay(odir, changes)
            imp = blast.impact(text, m, ROOT, odir)
            if imp.everything:
                raise RuntimeError("needs everything")
            cand = sorted({remap[t] for t in imp.tests if t in remap} | set(unknown if imp.tests else []))
            log(cid, "candidates", len(cand), round(sum(secs[n] for n in cand), 2), "|", "; ".join(imp.notes[:4]))
        except Exception as e:  # noqa: BLE001
            log(cid, "fallback to all tests:", repr(e))
            answers[cid] = final_pack(all_nums, secs)
            continue
        if not cand:
            answers[cid] = []
            continue
        if imp.syntax_error:
            answers[cid] = final_pack(cand, secs)
            continue
        code_tests = set(cand) - {remap[t] for t in imp.data_tests if t in remap}
        p_fail = 0.5 if code_tests else 0.9
        # heavy pool tests reached only through lines inside some pool tasks: run just those tasks
        task_sets: dict[int, set[int]] = {}
        inv = {v: k for k, v in remap.items()}
        for n in cand:
            mn = inv.get(n)
            if mn is None or mn in imp.other_tests or mn not in m.tasks:
                continue
            sub = m.task_subset(mn, imp.ranges)
            if sub is not None:
                task_sets[n] = sub
        est = dict(CPU)
        for n, sub in task_sets.items():
            frac = len(sub) / max(1, m.tasks[inv[n]]["count"])
            est[n] = CPU.get(n, secs[n]) * (0.15 + 0.85 * frac)
        include, run = choose(cand, secs, p_fail, est)
        log(cid, f"include {len(include)} ({sum(secs[n] for n in include):.2f}s) run {len(run)} ({sum(secs[n] for n in run):.2f}s)")
        if not run:
            answers[cid] = final_pack(include, secs)
            continue
        plans[cid] = (include, run)
        limits = {id_of[n]: max(3.0, 8 * secs[n] + 2.0) for n in run}
        tsk = {id_of[n]: task_sets[n] for n in run if n in task_sets}
        if tsk:
            log(cid, "task-restricted:", {k.split("::")[-1]: len(v) for k, v in tsk.items()})
        jobs.append(Job(cid, [id_of[n] for n in run], odir, limits, sum(secs[n] for n in run), tsk))

    # warm imports the children share (only when something will run)
    if jobs:
        try:
            import pytest  # noqa: F401
            import _pytest.unittest  # noqa: F401
            import duckdb  # noqa: F401
            import numpy  # noqa: F401
            import pandas  # noqa: F401
            import pandas.testing  # noqa: F401
        except Exception as e:  # noqa: BLE001
            log("warm import failed", e)

    # run the verification jobs, JOBS at a time
    pending = list(jobs)
    active: dict[int, Job] = {}
    while pending or active:
        while pending and len(active) < JOBS and time.monotonic() - t0 < DEADLINE:
            j = pending.pop(0)
            j.start()
            active[j.fd] = j
        if not active:
            break
        rl, _, _ = select.select(list(active), [], [], 0.5)
        for fd in rl:
            j = active[fd]
            data = os.read(fd, 65536)
            if data:
                j.feed(data)
            else:
                os.close(fd)
                _, _, ru = os.wait4(j.pid, 0)
                j.cpu = ru.ru_utime + ru.ru_stime
                del active[fd]
        now = time.monotonic()
        for fd, j in list(active.items()):
            over = now - j.started > 3 * j.budget + 20 or now - t0 > DEADLINE + 10
            if over:
                log(j.cid, "killing slow child")
                try:
                    os.kill(j.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                os.close(fd)
                os.waitpid(j.pid, 0)
                del active[fd]

    for j in jobs:
        include, run = plans[j.cid]
        sel = set(include)
        for n in run:
            ok = j.results.get(id_of[n])
            if ok is not True:  # failed, not run, or unknown
                sel.add(n)
        if j.collect_errors or not j.done:
            log(j.cid, "incomplete run:", j.collect_errors[:3], "done" if j.done else "not done")
            sel |= {n for n in run if id_of[n] not in j.results}
        failed = [n for n in run if j.results.get(id_of[n]) is False]
        log(j.cid, "ran", len(j.results), "failed", len(failed), f"budget {j.budget:.2f} cpu {getattr(j, 'cpu', -1):.2f} wall {getattr(j, 'wall', -1):.2f}")
        answers[j.cid] = final_pack(sorted(sel), secs)

    for p in patches:
        OUT.write(json.dumps({"commit": p.stem, "workers": answers[p.stem]}) + "\n")
    OUT.flush()
    import resource

    ru = [resource.getrusage(w) for w in (resource.RUSAGE_SELF, resource.RUSAGE_CHILDREN)]
    cpu = sum(r.ru_utime + r.ru_stime for r in ru)
    log("total wall", round(time.monotonic() - t0, 1), "cpu", round(cpu, 1))


CPU: dict[int, float] = {}


def choose(cand: list[int], secs: dict[int, float], p_fail: float = 0.5, est: dict[int, float] | None = None) -> tuple[list[int], list[int]]:
    """Split candidates into (include blind, run to check) minimising expected cost."""
    est = est if est is not None else CPU
    order = sorted(cand, key=lambda n: est.get(n, secs[n]))
    best = (pack(cand, secs)[0], len(order))  # include everything
    run_sum = 0.0
    for i, n in enumerate(order):
        run_sum += est.get(n, secs[n])
        rest = order[i + 1:]
        run_secs = sum(secs[x] for x in order[: i + 1])
        extra = min(run_secs * 0.5, 3.0) + (WORKER_COST if not rest else 0.0)
        cost = RUN_OVERHEAD + RUN_FACTOR * run_sum + pack(rest, secs)[0] + p_fail * extra
        if cost < best[0]:
            best = (cost, i + 1)
    k = best[1]
    if k == len(order) and best[0] == pack(cand, secs)[0]:
        return cand, []
    return order[k:], order[:k]


if __name__ == "__main__":
    main()
