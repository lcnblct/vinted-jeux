#!/usr/bin/env python3
"""Merge monitor state without dropping rows written by another run.

The command is deliberately independent of monitor.py so it can also be used by
CI while resolving a rebase.  SQLite tables are merged by row, and the ``meta``
table keeps the lexicographically greatest value for each key (ISO dates and
timestamps sort chronologically).  The history log is merged as a line union.
"""

from __future__ import annotations

import argparse
from contextlib import closing
import os
import sqlite3
import tempfile
from pathlib import Path


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _tables(path: Path) -> list[str]:
    if not path.exists() or path.stat().st_size == 0:
        return []
    with closing(sqlite3.connect(path)) as con:
        return [row[0] for row in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        )]


def _table_info(con: sqlite3.Connection, table: str) -> list[tuple]:
    return list(con.execute(f"PRAGMA table_info({_quote(table)})"))


def _create_table(out: sqlite3.Connection, source: sqlite3.Connection, table: str) -> None:
    row = source.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    if row and row[0]:
        out.execute(row[0])
        return
    # SQLite permits an empty table with no declared columns only in unusual
    # virtual-table cases.  Such tables have no state rows worth merging.
    raise RuntimeError(f"cannot obtain CREATE TABLE statement for {table!r}")


def _ensure_columns(out: sqlite3.Connection, source: sqlite3.Connection, table: str) -> None:
    existing = {row[1] for row in _table_info(out, table)}
    for row in _table_info(source, table):
        name, col_type, notnull, default, _pk = row[1], row[2], row[3], row[4], row[5]
        if name in existing:
            continue
        # Adding constraints can make an otherwise recoverable merge fail when
        # old databases lack a newly introduced value.  Preserve the type and
        # default, while letting the existing table policy validate new rows.
        definition = _quote(name)
        if col_type:
            definition += f" {col_type}"
        if default is not None:
            definition += f" DEFAULT {default}"
        out.execute(f"ALTER TABLE {_quote(table)} ADD COLUMN {definition}")
        existing.add(name)


def _merge_db(output: Path, sources: list[Path]) -> None:
    usable = [path for path in sources if path.exists() and path.stat().st_size]
    if not usable:
        return

    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{output.name}.", dir=output.parent)
    os.close(fd)
    temp = Path(temp_name)
    try:
        with closing(sqlite3.connect(temp)) as out:
            out.execute("PRAGMA foreign_keys=OFF")
            known_tables: set[str] = set()
            for source_path in usable:
                with closing(sqlite3.connect(source_path)) as source:
                    for table in _tables(source_path):
                        if table not in known_tables:
                            _create_table(out, source, table)
                            known_tables.add(table)
                        _ensure_columns(out, source, table)

                        source_cols = [row[1] for row in _table_info(source, table)]
                        target_cols = {row[1] for row in _table_info(out, table)}
                        cols = [name for name in source_cols if name in target_cols]
                        if not cols:
                            continue
                        quoted_cols = ", ".join(_quote(name) for name in cols)
                        rows = source.execute(
                            f"SELECT {quoted_cols} FROM {_quote(table)}"
                        )
                        placeholders = ", ".join("?" for _ in cols)
                        for values in rows:
                            try:
                                if table == "meta" and "key" in cols and "value" in cols:
                                    key_value = values[cols.index("key")]
                                    incoming = values[cols.index("value")]
                                    current = out.execute(
                                        "SELECT value FROM meta WHERE key=?", (key_value,)
                                    ).fetchone()
                                    if current is None:
                                        out.execute(
                                            f"INSERT OR IGNORE INTO meta ({quoted_cols}) "
                                            f"VALUES ({placeholders})", values
                                        )
                                    elif incoming is not None and (
                                        current[0] is None or str(incoming) > str(current[0])
                                    ):
                                        out.execute(
                                            "UPDATE meta SET value=? WHERE key=?",
                                            (incoming, key_value),
                                        )
                                elif table == "delivery_outbox" and {
                                    "item_id", "destination", "payload", "status", "attempts",
                                    "last_error", "updated_at"
                                }.issubset(cols):
                                    # A concurrent worker may have advanced the
                                    # same queue row to sent. Preserve that
                                    # progress while retaining the greatest retry
                                    # count and newest timestamp.
                                    out.execute(
                                        f"INSERT INTO delivery_outbox ({quoted_cols}) VALUES ({placeholders}) "
                                        "ON CONFLICT(item_id, destination) DO UPDATE SET "
                                        "payload=excluded.payload, "
                                        "status=CASE WHEN delivery_outbox.status='sent' OR excluded.status='sent' THEN 'sent' ELSE excluded.status END, "
                                        "attempts=MAX(delivery_outbox.attempts, excluded.attempts), "
                                        "last_error=COALESCE(excluded.last_error, delivery_outbox.last_error), "
                                        "updated_at=MAX(delivery_outbox.updated_at, excluded.updated_at)",
                                        values,
                                    )
                                else:
                                    out.execute(
                                        f"INSERT OR IGNORE INTO {_quote(table)} ({quoted_cols}) "
                                        f"VALUES ({placeholders})", values
                                    )
                            except sqlite3.IntegrityError as exc:
                                # Never silently discard state.  A schema or row
                                # problem must fail the persistence step so CI
                                # retains the recovery artifact and alerts.
                                raise RuntimeError(
                                    f"cannot merge row into {table!r} from {source_path}: {exc}"
                                ) from exc

            # Meta values are dates/timestamps in this project.  Resolve a key
            # collision explicitly so a stale checkout cannot roll state back.
            if "meta" in known_tables:
                keys = [row[0] for row in out.execute("SELECT key FROM meta")]
                for key in keys:
                    values = [row[0] for row in out.execute(
                        "SELECT value FROM meta WHERE key=?", (key,)
                    ) if row[0] is not None]
                    if values:
                        out.execute("UPDATE meta SET value=? WHERE key=?", (max(values), key))
            out.commit()
        os.replace(temp, output)
    finally:
        if temp.exists():
            temp.unlink()


def merge_history(output: Path, sources: list[Path]) -> None:
    lines: set[str] = set()
    headers: list[str] = []
    for path in sources:
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.rstrip()
            if not line:
                continue
            if line.startswith("#"):
                if line not in headers:
                    headers.append(line)
            else:
                lines.add(line)
    if not lines and not headers and not any(path.exists() for path in sources):
        return
    output.parent.mkdir(parents=True, exist_ok=True)
    content = headers[:1] + sorted(lines)
    output.write_text("\n".join(content) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="merged SQLite output")
    parser.add_argument("--db", type=Path, action="append", default=[], help="SQLite input")
    parser.add_argument("--history-output", type=Path)
    parser.add_argument("--history", type=Path, action="append", default=[])
    args = parser.parse_args()
    _merge_db(args.output, args.db)
    if args.history_output:
        merge_history(args.history_output, args.history)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
