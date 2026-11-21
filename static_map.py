"""Immutable selector index built from the two pristine maps."""
import json
import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path

import blast


class Rows(Mapping):
    def __init__(self, db, kind, decode=json.loads):
        self.db, self.kind, self.decode = db, kind, decode
        self.seen = {}

    def __getitem__(self, key):
        if key not in self.seen:
            row = self.db.execute("SELECT value FROM evidence WHERE kind=? AND name=?", (self.kind, key)).fetchone()
            if row is None:
                raise KeyError(key)
            self.seen[key] = self.decode(row[0])
        return self.seen[key]

    def __iter__(self):
        return (row[0] for row in self.db.execute("SELECT name FROM evidence WHERE kind=?", (self.kind,)))

    def __len__(self):
        return self.db.execute("SELECT count(*) FROM evidence WHERE kind=?", (self.kind,)).fetchone()[0]


class StaticMap(blast.Map):
    def __init__(self, path):
        db = sqlite3.connect(path.resolve().as_uri() + "?mode=ro&immutable=1", uri=True)
        core = Rows(db, "core")
        self.ids, self.opens, self.classes, self.calls, self.cmp = core["metadata"]
        self.file_of = [s.partition("::")[0] if s else "" for s in self.ids]
        self.lines = Rows(db, "lines", lambda row: dict(json.loads(row)))
        self.sets = Rows(db, "sets", lambda row: memoryview(row).cast("h"))
        self.collect = Rows(db, "collect", lambda row: set(json.loads(row)))
        self.attrs = Rows(db, "attrs")
        if self.cmp:
            self.cmp["sites"] = Rows(db, "sites")
        self._touch, self.tasks = {}, {}


def build(path):
    from array import array

    m = blast.Map(path / "map.json.gz")
    sites = m.cmp.pop("sites", {})
    with closing(sqlite3.connect(path / "static.db")) as db, db:
        db.execute("CREATE TABLE evidence(kind TEXT, name, value, PRIMARY KEY(kind,name)) WITHOUT ROWID")
        for kind, items, encode in (
            ("sets", enumerate(m.sets), lambda ls: array("h", ls).tobytes()),
            ("lines", m.lines.items(), lambda ls: json.dumps(list(ls.items()))),
            ("collect", m.collect.items(), lambda ls: json.dumps(sorted(ls))),
            ("attrs", m.attrs.items(), json.dumps),
            ("sites", sites.items(), json.dumps),
            ("core", [("metadata", (m.ids, m.opens, m.classes, m.calls, m.cmp))], json.dumps),
        ):
            db.executemany("INSERT INTO evidence VALUES (?, ?, ?)", ((kind, k, encode(v)) for k, v in items))


if __name__ == "__main__":
    build(Path(__file__).resolve().parent / "maps")
