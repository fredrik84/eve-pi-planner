"""
Database abstraction layer. Supports SQLite (default) and PostgreSQL (when DATABASE_URL is set).

All app code that writes to pp_* tables imports get_connection from here (via app.sde re-export).
SDE reads (types, schematics) use a separate private SQLite connection in sde.py.
"""
import os
import re
import sqlite3
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_SQLITE_PATH = Path("data/sde.db")
_IS_POSTGRES = DATABASE_URL.startswith(("postgresql://", "postgres://"))


class _Row(dict):
    """Dict with integer positional indexing for sqlite3.Row compatibility."""
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


def _pg_translate(sql: str) -> str:
    """Translate SQLite SQL to Postgres-compatible SQL."""
    stripped = sql.strip()

    # No-op PRAGMAs
    if re.match(r"^\s*PRAGMA\s+(journal_mode|busy_timeout|synchronous)\b", sql, re.IGNORECASE):
        return "SELECT 1"

    # PRAGMA table_info(t) → information_schema query
    m = re.match(r"^\s*PRAGMA\s+table_info\((\w+)\)\s*$", stripped, re.IGNORECASE)
    if m:
        t = m.group(1)
        return (
            f"SELECT column_name AS name, data_type AS type, '' AS dflt_value, 0 AS pk "
            f"FROM information_schema.columns "
            f"WHERE table_name='{t}' AND table_schema='public' ORDER BY ordinal_position"
        )

    # sqlite_master table-existence check → information_schema
    m = re.search(
        r"FROM\s+sqlite_master\s+WHERE\s+type='table'\s+AND\s+name='(\w+)'",
        sql, re.IGNORECASE,
    )
    if m:
        t = m.group(1)
        return (
            f"SELECT 1 FROM information_schema.tables "
            f"WHERE table_schema='public' AND table_name='{t}'"
        )

    # INSERT OR IGNORE → INSERT ... ON CONFLICT DO NOTHING
    if re.search(r"\bINSERT\s+OR\s+IGNORE\b", sql, re.IGNORECASE):
        sql = re.sub(r"\bINSERT\s+OR\s+IGNORE\b", "INSERT", sql, flags=re.IGNORECASE)
        sql = sql.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    # datetime('now', '-N unit') / ('+N unit')
    # Return TEXT (ISO 8601) so comparisons against TEXT columns work on Postgres.
    # All created_at/updated_at columns store Python .isoformat() strings (TEXT), and
    # TEXT < timestamptz is a type error in Postgres. Casting to text keeps it consistent.
    sql = re.sub(r"datetime\('now',\s*'-(\d+)\s+(\w+)'\)",
                 r"TO_CHAR(NOW() AT TIME ZONE 'UTC' - INTERVAL '\1 \2', 'YYYY-MM-DD\"T\"HH24:MI:SS')", sql)
    sql = re.sub(r"datetime\('now',\s*'\+(\d+)\s+(\w+)'\)",
                 r"TO_CHAR(NOW() AT TIME ZONE 'UTC' + INTERVAL '\1 \2', 'YYYY-MM-DD\"T\"HH24:MI:SS')", sql)
    # datetime('now') (bare)
    sql = re.sub(r"datetime\('now'\)",
                 r"TO_CHAR(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')", sql)
    # DEFAULT (datetime('now')) in DDL
    sql = re.sub(r"DEFAULT\s*\(datetime\('now'\)\)",
                 r"DEFAULT TO_CHAR(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD\"T\"HH24:MI:SS')",
                 sql, flags=re.IGNORECASE)

    # INTEGER PRIMARY KEY AUTOINCREMENT → BIGSERIAL PRIMARY KEY
    sql = re.sub(
        r"\bINTEGER\s+PRIMARY\s+KEY\s+AUTOINCREMENT\b",
        "BIGSERIAL PRIMARY KEY",
        sql, flags=re.IGNORECASE,
    )

    # COLLATE NOCASE → (remove)
    sql = re.sub(r"\s+COLLATE\s+NOCASE\b", "", sql, flags=re.IGNORECASE)

    # IFNULL → COALESCE
    sql = re.sub(r"\bIFNULL\b", "COALESCE", sql, flags=re.IGNORECASE)

    # Escape literal % (e.g. in LIKE patterns) before ? → %s so psycopg2
    # doesn't mistake them for parameter placeholders.
    sql = sql.replace("%", "%%")
    # ? → %s (must be last so earlier replacements don't double-translate)
    sql = sql.replace("?", "%s")

    return sql


class _PgCursor:
    """Wraps a psycopg2 cursor to match the sqlite3.Cursor interface used in app code."""

    def __init__(self, pg_cursor, pg_conn=None):
        self._cur = pg_cursor
        self._conn = pg_conn

    def execute(self, sql: str, params=()):
        sql = _pg_translate(sql)
        try:
            self._cur.execute(sql, params if params else None)
        except Exception:
            if self._conn:
                self._conn.rollback()
            raise
        return self

    def _row(self, raw):
        if raw is None:
            return None
        desc = self._cur.description or []
        return _Row(zip((d[0] for d in desc), raw))

    def fetchone(self):
        return self._row(self._cur.fetchone())

    def fetchall(self):
        return [self._row(r) for r in self._cur.fetchall()]

    def fetchmany(self, size=None):
        rows = self._cur.fetchmany(size) if size is not None else self._cur.fetchmany()
        return [self._row(r) for r in rows]

    def __iter__(self):
        for r in self._cur:
            yield self._row(r)

    @property
    def rowcount(self):
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return None


class _PgConn:
    """Wraps a psycopg2 connection to match the sqlite3.Connection interface used in app code."""

    def __init__(self, pg_conn):
        self._conn = pg_conn

    def execute(self, sql: str, params=()) -> _PgCursor:
        sql = _pg_translate(sql)
        cur = self._conn.cursor()
        try:
            cur.execute(sql, params if params else None)
        except Exception:
            self._conn.rollback()
            raise
        return _PgCursor(cur)

    def executemany(self, sql: str, seq) -> _PgCursor:
        sql = _pg_translate(sql)
        cur = self._conn.cursor()
        try:
            cur.executemany(sql, seq)
        except Exception:
            self._conn.rollback()
            raise
        return _PgCursor(cur)

    def cursor(self) -> _PgCursor:
        return _PgCursor(self._conn.cursor(), self._conn)

    def commit(self):
        self._conn.commit()

    def rollback(self):
        self._conn.rollback()

    def close(self):
        self._conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        if exc_type:
            self._conn.rollback()
        else:
            self._conn.commit()
        self._conn.close()


def _sqlite_row_factory(cursor, row):
    return _Row(zip((d[0] for d in cursor.description), row))


def get_connection():
    """Return a DB connection for user (pp_*) tables. Postgres when DATABASE_URL is set."""
    if _IS_POSTGRES:
        import psycopg2
        conn = psycopg2.connect(DATABASE_URL)
        conn.autocommit = False
        return _PgConn(conn)
    con = sqlite3.connect(str(_SQLITE_PATH))
    con.row_factory = _sqlite_row_factory
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con
