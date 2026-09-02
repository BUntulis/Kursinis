"""Inspect (and lightly edit) the SQLite database of a built model-run project.

Reads ``<workspace>/db.sqlite3`` directly with the stdlib ``sqlite3`` module (it is a
*separate* project's database, not the dashboard's ORM). Building the project on demand
(via :func:`project_builder.ensure_runnable_project`) is what creates that file, so every
read first ensures the project is built. Table and column names are validated against the
live schema before being interpolated into SQL, and all values are bound as parameters.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path

#: Tables that are internal sqlite/Django bookkeeping and not interesting to show first.
_INTERNAL_TABLES = {"sqlite_sequence"}
MAX_ROWS = 500


def _db_path(model_run) -> Path:
    return Path(model_run.workspace_path or "") / "db.sqlite3"


def _ensure_built(model_run) -> dict:
    try:
        from .project_builder import ensure_runnable_project

        return ensure_runnable_project(model_run)
    except Exception as exc:  # pragma: no cover - defensive
        return {"ok": False, "error": f"Could not build the project: {exc}"}


def _connect(model_run) -> sqlite3.Connection | None:
    path = _db_path(model_run)
    if not path.exists():
        return None
    # busy timeout so an insert doesn't immediately error if the preview runserver briefly
    # holds a write lock on the same file-backed SQLite database.
    conn = sqlite3.connect(str(path), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _columns(conn: sqlite3.Connection, table: str) -> list[dict]:
    cols = []
    for row in conn.execute(f'PRAGMA table_info("{table}")'):
        cols.append({
            "name": row["name"],
            "type": (row["type"] or "").upper(),
            "pk": bool(row["pk"]),
            "notnull": bool(row["notnull"]),
            "default": row["dflt_value"],
        })
    return cols


def _cell(value):
    if isinstance(value, bytes):
        return f"<{len(value)} bytes>"
    return value


def list_tables(model_run) -> dict:
    """Return every user table with its row count + column schema (building the DB if needed)."""
    build = _ensure_built(model_run)
    conn = _connect(model_run)
    if conn is None:
        return {"available": False, "tables": [], "error": build.get("error") or "No database has been built yet."}
    try:
        names = [
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
            )
        ]
        tables = []
        for name in names:
            if name in _INTERNAL_TABLES:
                continue
            try:
                count = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
            except sqlite3.Error:
                count = 0
            tables.append({"name": name, "count": count, "columns": _columns(conn, name)})
        return {"available": True, "tables": tables}
    finally:
        conn.close()


def table_rows(model_run, table: str, limit: int = 200, offset: int = 0) -> dict:
    """Return columns + a page of rows for one table."""
    conn = _connect(model_run)
    if conn is None:
        return {"error": "No database has been built yet."}
    try:
        if not _table_exists(conn, table):
            return {"error": f"Unknown table: {table}"}
        limit = max(1, min(MAX_ROWS, int(limit)))
        offset = max(0, int(offset))
        cols = _columns(conn, table)
        names = [c["name"] for c in cols]
        total = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        rows = []
        for row in conn.execute(f'SELECT * FROM "{table}" LIMIT ? OFFSET ?', (limit, offset)):
            rows.append([_cell(row[name]) for name in names])
        return {"table": table, "columns": cols, "rows": rows, "total": total, "limit": limit, "offset": offset}
    finally:
        conn.close()


def insert_row(model_run, table: str, values: dict) -> dict:
    """Insert one row into ``table``; empty strings become NULL. Returns ``{ok, rowid|error}``."""
    conn = _connect(model_run)
    if conn is None:
        return {"ok": False, "error": "No database has been built yet."}
    try:
        if not _table_exists(conn, table):
            return {"ok": False, "error": f"Unknown table: {table}"}
        valid = {c["name"] for c in _columns(conn, table)}
        cols = [c for c in values.keys() if c in valid]
        if not cols:
            return {"ok": False, "error": "No valid columns were supplied."}
        params = [(values[c] if values[c] not in ("", None) else None) for c in cols]
        col_sql = ", ".join(f'"{c}"' for c in cols)
        placeholders = ", ".join("?" for _ in cols)
        try:
            cur = conn.execute(f'INSERT INTO "{table}" ({col_sql}) VALUES ({placeholders})', params)
            conn.commit()
        except sqlite3.Error as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True, "rowid": cur.lastrowid}
    finally:
        conn.close()
