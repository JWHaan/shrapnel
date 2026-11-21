"""Integer value edits to the pinned tokenizer NUMERIC_ESCAPES dictionaries.

Tokenizer._init_core forwards the dictionary unchanged. The generator and dialect
metaclass only test its truth or keys; TokenizerCore._scan_numeric_escape and
_read_byte_escape read values. Use all dialects' readers, not the owner's MRO.

After modified_files and before py_impact, use `keep = escape_reads.tests_for(edits, m)`.
None means normal fallback; otherwise use these map indices (not an intersection)
and still remap indices and add new inventory tests as usual. `m` must come from
the same maps directory. Build the sidecar with tools/build_escape_reads.py.
Benchmark before enabling: a partial call profile keeps every unprofiled test.
"""
import hashlib
import json
import re
from functools import lru_cache
from pathlib import Path

CORE = "sqlglot/tokenizer_core.py"
MAPS = Path(__file__).resolve().parent / "maps"
# Receipt for the generated, pin-audited source slots and baseline profile bindings.
INDEX_SHA256 = "90c6acf4b55ee3a980fce52556ddd154c80158826bd01d55407cf74a7d8e2855"
INTEGER = re.compile(r"(?:0|[1-9][0-9]{0,6}|0[xX][0-9a-fA-F]{1,6}|0[oO][0-7]{1,8})")


@lru_cache(maxsize=1)
def _index(maps):
    try:
        raw = (maps / "escape_reads.json").read_bytes()
        if hashlib.sha256(raw).hexdigest() == INDEX_SHA256:
            data = json.loads(raw)
            with (maps / "map.json.gz").open("rb") as f:
                if hashlib.file_digest(f, "sha256").hexdigest() == data["map_sha256"]:
                    return data
    except OSError:
        pass
    return None


def tests_for(edits, m, maps=MAPS):
    """Return global reader, child, source/inspection and unknown map indices, or None."""
    if len(edits) != 1 or not isinstance(m.calls, dict) or not m.calls:
        return None
    fd, before, after = edits[0]
    fn = fd.old
    if (not fn or fn != fd.new or not fn.startswith("sqlglot/dialects/") or after is None
            or fd.binary or len(fd.removed) != 1 or fd.inserts):
        return None
    data = _index(maps)
    if data is None or fn not in data["files"]:
        return None
    digest, slots = data["files"][fn]
    positions = slots.get(str(fd.removed[0]))
    if not positions or before == after or hashlib.sha256(before.encode()).hexdigest() != digest:
        return None
    # All other characters must match; the trusted slice is exactly one integer token.
    for lo, hi in positions:
        end = hi + len(after) - len(before)
        if INTEGER.fullmatch(after[lo:end]) and after[:lo] == before[:lo] and after[end:] == before[hi:]:
            break
    else:
        return None

    calls = m.calls
    if (hashlib.sha256("\n".join(m.ids).encode()).hexdigest() != data["ids_sha256"]
            or calls.get("ids_sha256") != data["ids_sha256"]):
        return None
    unknown, inspected = calls.get("unknown"), calls.get("inspected")
    if not (all(isinstance(calls.get(k), dict) and calls[k] for k in ("calls", "validated"))
            and isinstance(unknown, list) and isinstance(inspected, dict)
            and all(isinstance(v, list) for v in inspected.values())
            and any(-1 in v for v in inspected.values())):
        return None  # not a populated build_calls profile with a collection record
    if CORE not in m.lines or CORE not in m.collect or m.collect[CORE].intersection(data["reads"]):
        return None
    keep = set(m.sets[data["raw_children_set"]])
    for source in data["files"]:
        keep.update(m.opened(source) or ())
    for n in data["reads"]:
        keep.update(m.tests_on(CORE, n, n))
    # A different in-memory Map can share an inventory and even a file path.
    if -1 in keep or hashlib.sha256(json.dumps(sorted(keep)).encode()).hexdigest() != data["coverage_sha256"]:
        return None
    for group in (unknown, inspected.get("<child>", ()), inspected.get(fn[:-3].replace("/", "."), ())):
        if any(type(n) is not int or not 0 <= n < len(m.ids) for n in group):
            return None  # includes the collection sentinel
        keep.update(group)
    return keep
