"""
Database abstraction layer. Supports SQLite (default) and PostgreSQL (when DATABASE_URL is set).

All app code that writes to pp_* tables imports get_connection from here (via app.sde re-export).
SDE reads (types, schematics) use a separate private SQLite connection in sde.py.
"""
import functools
import os
import re
import sqlite3
import time
from pathlib import Path

DATABASE_URL = os.environ.get("DATABASE_URL", "")
_SQLITE_PATH = Path("data/sde.db")
_IS_POSTGRES = DATABASE_URL.startswith(("postgresql://", "postgres://"))


def ensure_once(fn):
    """Wrap an idempotent ensure_*_table() DDL function so it runs once per process instead
    of on every request. Table schemas can't change while the process is running, so re-running
    CREATE TABLE IF NOT EXISTS / ALTER TABLE ADD COLUMN on every call is pure waste — and on a
    multi-node cluster, each call is a real network round-trip to Postgres, not a free no-op."""
    ran = False

    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        nonlocal ran
        if not ran:
            fn(*args, **kwargs)
            ran = True

    return wrapper


def add_columns(con, table: str, *coldefs: str) -> None:
    """Additive schema migration — this codebase's only migration mechanism (never DROP COLUMN).
    Adds each column if it isn't there yet; an already-existing column is a no-op.

    **Each ADD COLUMN commits immediately on success, and that is not optional.** Postgres aborts
    the WHOLE current transaction on any failed statement, so a later already-exists ALTER rolls
    back the earlier ones that had genuinely just succeeded — leaving columns that the code is
    certain it created and that are not actually in the database. This bit us for real once
    (esi_expires/skills_expires silently erased by the next ALTER, then UndefinedColumn on every
    request that touched them). Nine call sites were still doing the bare try/except chain without
    the per-statement commit when this helper was introduced; they are all routed through here now.
    """
    # Commit whatever DDL came before us — almost always the CREATE TABLE this migration extends —
    # BEFORE risking an ALTER. Postgres aborts the entire transaction on a failed statement and the
    # connection wrapper rolls it back, which silently discards an uncommitted CREATE TABLE. The
    # result on a FRESH database is the table never existing at all: the CREATE runs, the very next
    # ALTER fails because the column is already in the CREATE body, the rollback undoes both, and
    # the trailing commit() commits nothing. Existing databases never showed it (their CREATE is a
    # no-op and the ALTER genuinely adds the column) — it only bites a new install, which is exactly
    # where nobody looks. Cost the dev stack its pp_industry_settings table (2026-07-29).
    try:
        con.commit()
    except Exception:
        pass

    for coldef in coldefs:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            con.commit()
        except Exception:
            pass          # already exists — the only failure worth swallowing here


# Columns that store a Unix epoch and were declared `REAL`. On SQLite that's an 8-byte double and
# fine; on Postgres `real` is **float4** — about 7 significant digits, where an epoch needs 10. In
# the current epoch range float4's spacing is 128 seconds, so every one of these was quantised to
# within ~64s of the truth. Measured on prod 2026-07-30: wrote 1785409464.304, read back
# 1785409408.0, and 75% of sub-minute job runs rendered as a 0-second duration.
#
# Only EPOCHS are listed. The other float4 columns in this schema (percentages, volumes, ISK
# amounts, security status, day counts) are perfectly well served by 7 significant digits — it is
# specifically the 1.79e9 magnitude of a Unix timestamp that overruns them, which is why this is a
# targeted list and not "every real column".
_EPOCH_COLUMNS = [
    ("pp_char_planets", "scanned_at"), ("pp_char_planets", "checkpoint_at"),
    ("pp_char_planets", "esi_modified"), ("pp_char_planets", "esi_expires"),
    ("pp_characters", "skills_expires"), ("pp_characters", "scan_failed_at"),
    ("pp_colony_flags", "created_at"),
    ("pp_colony_yield", "install_ts"), ("pp_colony_yield", "scanned_ts"),
    ("pp_pi_program_ledger", "install_ts"), ("pp_pi_program_ledger", "recorded_at"),
    ("pp_asset_sources", "updated_at"),
    ("pp_industry_completions", "completed_at"),
    ("pp_industry_manual_done", "marked_at"),
    ("pp_industry_orders", "created_at"),
    ("pp_industry_sourced", "updated_at"),
    ("pp_industry_settings", "updated_at"), ("pp_industry_settings", "auto_refreshed_at"),
    ("pp_industry_shares", "created_at"), ("pp_industry_shares", "last_at"),
    ("pp_job_config", "run_requested"), ("pp_job_config", "updated_at"),
    ("pp_job_leases", "lease_until"),
    # The BPC scan lease and its run stamps. The 2026-07-31 inventory said these were covered and
    # they were not — prod still had all three at float4 on 2026-08-05, which quantises the lease to
    # ~128s and makes "an expired lease is reclaimable" pass or fail depending on where in that
    # bucket the clock happens to sit (it is what `test_scan_lease_is_single_writer_across_replicas`
    # was intermittently failing on). Same slop-against-a-900s-TTL argument as `pp_job_leases`, so
    # the scanner was never really at risk; the flapping test was the tell.
    ("pp_bpc_scan", "lease_until"),
    ("pp_bpc_scan", "started_at"), ("pp_bpc_scan", "ended_at"),
    ("pp_job_runs", "started_at"), ("pp_job_runs", "ended_at"),
    ("pp_plan_baseline", "saved_at"),
    ("pp_reaction_assignments", "created_at"),
    ("pp_reaction_assignments", "last_completed_at"),
    ("pp_reaction_completions", "completed_at"),
    ("pp_reaction_manual_done", "marked_at"),
    ("pp_reaction_orders", "created_at"),
]


# Arbitrary fixed key for the Postgres advisory lock guarding the widening below — only matters
# that every replica uses the same constant, and that it differs from build_sde's.
_EPOCH_WIDEN_LOCK_KEY = 918_273_646


def _release_advisory(con, key: int) -> None:
    try:
        con.execute("SELECT pg_advisory_unlock(?)", (key,))
        con.commit()
    except Exception:
        pass          # the lock is session-scoped; closing the connection frees it regardless


def widen_epoch_columns() -> list[str]:
    """Widen every `_EPOCH_COLUMNS` entry still sitting at float4 to `double precision`.

    Postgres-only: SQLite's REAL is already a double, and SQLite has no ALTER COLUMN TYPE anyway.

    Idempotent by construction — it asks information_schema which columns are actually still `real`
    and touches only those, so a second run is a no-op and a table that doesn't exist yet is simply
    absent from the answer. Widening is non-lossy, but it does NOT recover precision already thrown
    away: rows written before this ran keep their rounded values, and only new writes are exact.

    Each ALTER gets its own commit for the reason spelled out in `add_columns`: Postgres aborts the
    whole transaction on a failed statement, so batching these would let one failure silently undo
    the ones that had already succeeded. Returns the list of columns it changed, for the log.
    """
    if not _IS_POSTGRES:
        return []
    con = get_connection()
    changed: list[str] = []
    try:
        # Every replica boots at once and would otherwise each read information_schema before any
        # of them had converted anything, then all run the same ALTERs — N sequential table
        # rewrites under ACCESS EXCLUSIVE locks instead of one. Observed for real on the first
        # deploy of this (three pods, 22/22/21 columns, same millisecond). Harmless at these table
        # sizes, but it would block queries on a big one. Same advisory-lock pattern as
        # scripts/build_sde.py, with the schema RE-READ inside the lock so the losers of the race
        # find the work already done and skip it.
        locked = False
        try:
            con.execute("SELECT pg_advisory_lock(?)", (_EPOCH_WIDEN_LOCK_KEY,))
            con.commit()
            locked = True
        except Exception:
            pass          # no lock is still better than no migration; worst case is the old race
        try:
            rows = con.execute(
                "SELECT table_name, column_name FROM information_schema.columns "
                "WHERE table_schema='public' AND data_type='real'").fetchall()
        except Exception:
            if locked:
                _release_advisory(con, _EPOCH_WIDEN_LOCK_KEY)
            return []
        real_now = {(r["table_name"], r["column_name"]) for r in rows}
        try:
            con.commit()
        except Exception:
            pass
        for table, col in _EPOCH_COLUMNS:
            if (table, col) not in real_now:
                continue
            try:
                con.execute(f"ALTER TABLE {table} ALTER COLUMN {col} TYPE double precision")
                con.commit()
                changed.append(f"{table}.{col}")
            except Exception:
                # Missing table, permissions, a lock timeout — none of them worth failing startup
                # over, and the next boot retries. The column simply stays float4 until then.
                try:
                    con.rollback()
                except Exception:
                    pass
        if locked:
            _release_advisory(con, _EPOCH_WIDEN_LOCK_KEY)
    finally:
        con.close()
    return changed


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

    # Both return paths below finish in a `finally`: on a dead connection the commit/rollback
    # itself raises, and returning the slot has to happen anyway — otherwise the exception that
    # tells us the DB went away is also the thing that permanently loses a slot.

    def close(self):
        try:
            self._conn.rollback()  # leave no dangling transaction for the next borrower
        except Exception:
            _pg_discard(self._conn)
            raise
        finally:
            _pg_release(self._conn)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *_):
        try:
            if exc_type:
                self._conn.rollback()
            else:
                self._conn.commit()
        except Exception:
            _pg_discard(self._conn)
            raise
        finally:
            _pg_release(self._conn)


def _sqlite_row_factory(cursor, row):
    return _Row(zip((d[0] for d in cursor.description), row))


_PG_POOL_SIZE = 8
_PG_POOL = None
_PG_POOL_INIT_LOCK = None   # threading.Lock, created lazily to avoid importing threading for SQLite-only runs

# How long a connection may sit idle in the pool before it gets a liveness ping on the way out.
# The ping is a real round-trip, so it must not run on every borrow — under load connections are
# recycled in milliseconds and the far end is provably alive (we just used it). Anything idle
# longer than this is worth one cheap SELECT 1 to find out, since that is exactly the window in
# which a Postgres pod can have moved without us noticing.
_PG_IDLE_PING_AFTER = 30.0


def _pg_discard(conn) -> None:
    """Close a connection we're throwing away, ignoring the errors a dead socket raises."""
    if conn is None:
        return
    try:
        conn.close()
    except Exception:
        pass


def _pg_usable(conn, idle_since: float) -> bool:
    """Is this pooled connection still good? `conn.closed` only catches connections WE closed —
    a Postgres that restarted or moved to another node leaves the socket looking fine until the
    next statement fails, which is why a stale pool used to serve nothing but errors until the
    app was restarted by hand."""
    if conn is None or conn.closed:
        return False
    if time.monotonic() - idle_since < _PG_IDLE_PING_AFTER:
        return True
    try:
        cur = conn.cursor()
        try:
            cur.execute("SELECT 1")
        finally:
            cur.close()
        conn.rollback()  # the ping opens a transaction; don't hand over an idle-in-transaction conn
        return True
    except Exception:
        return False


def _pg_release(conn) -> None:
    """Return a connection to its pool slot. A connection that died mid-request goes back as an
    EMPTY slot rather than not at all: the slot count is what bounds concurrency, so silently
    dropping one shrinks the pool for the life of the process — eight bad requests during a
    Postgres restart used to be enough to strand every slot."""
    q = _pg_pool()
    if conn is None or conn.closed:
        _pg_discard(conn)
        q.put((None, 0.0))
    else:
        q.put((conn, time.monotonic()))


def _pg_pool():
    """Lazily-created per-process pool of real psycopg2 connections, handed out via a plain
    queue.Queue rather than psycopg2.pool.ThreadedConnectionPool. Two problems with the latter,
    both reproduced live on 2026-07-01 under a genuine concurrent burst (not just theorized):

    1. ThreadedConnectionPool.getconn()/putconn() track checked-out connections by an internal
       'key' (id(conn) by default, or a caller-supplied key) in a plain dict — under real
       concurrency this bookkeeping proved fragile: putconn() would occasionally find no
       matching entry ('trying to put unkeyed connection', or a bare KeyError on a supplied
       key), permanently leaking that connection out of the pool (the exception fires before
       the connection is returned to the free list).
    2. Both getconn() and putconn() share ONE lock for the pool's entire lifetime — and
       putconn() does real network I/O under that lock (conn.rollback() / checking
       conn.info.transaction_status). So concurrent connection RETURNS were fully serialized,
       each paying real cross-node network latency while blocking every other thread's
       getconn()/putconn() — turning a 'give the connection back' step into the actual
       bottleneck under a concurrent burst (measured multi-second stalls for what should be a
       sub-5ms round trip).

    A queue.Queue sidesteps both: handoff is by plain object reference (no key/id bookkeeping to
    get out of sync), and put()/get() don't hold a pool-wide lock across a network call. get()
    blocks if the queue is empty (a burst beyond pool size waits for a free connection — graceful
    backpressure) instead of ThreadedConnectionPool's immediate PoolError.

    The queue holds SLOTS — `(connection_or_None, idle_since)` — not bare connections. An empty
    slot is a permit to open one, so a connection can be thrown away the moment it looks dead
    without shrinking the pool, and the replacement is opened lazily by whoever borrows the slot
    next. That is what makes the pool survive a Postgres restart/move on its own; before this it
    handed out its original eight connections forever and a DB move meant a manual
    `rollout restart` of the app (cost real downtime on 2026-07-28)."""
    global _PG_POOL, _PG_POOL_INIT_LOCK
    if _PG_POOL is None:
        import threading
        if _PG_POOL_INIT_LOCK is None:
            _PG_POOL_INIT_LOCK = threading.Lock()
        with _PG_POOL_INIT_LOCK:
            if _PG_POOL is None:
                import queue
                import psycopg2
                q = queue.Queue()
                for _ in range(_PG_POOL_SIZE):
                    # Warm the pool, but never let a DB that happens to be down at import time
                    # abort pool creation — an empty slot connects on first use instead.
                    try:
                        q.put((psycopg2.connect(DATABASE_URL), time.monotonic()))
                    except Exception:
                        q.put((None, 0.0))
                _PG_POOL = q
    return _PG_POOL


def get_connection():
    """Return a DB connection for user (pp_*) tables. Postgres when DATABASE_URL is set."""
    if _IS_POSTGRES:
        import queue
        q = _pg_pool()
        try:
            # Bounded wait, not an unbounded block: queue.Queue.get() with no timeout would hang
            # a request forever if a connection ever genuinely leaks (some path skipping close()),
            # instead of failing clearly. 15s comfortably rides out a real concurrent burst
            # queueing for a free connection, without turning a real leak into a silent full hang.
            conn, idle_since = q.get(timeout=15)
        except queue.Empty:
            raise RuntimeError("Database connection pool exhausted (all connections busy for 15s)")
        if not _pg_usable(conn, idle_since):
            import psycopg2
            _pg_discard(conn)
            try:
                conn = psycopg2.connect(DATABASE_URL)
            except Exception:
                # Hand the (empty) slot back before propagating, or a DB that is briefly
                # unreachable would eat one slot per failed request and never give them back.
                q.put((None, 0.0))
                raise
        conn.autocommit = False
        return _PgConn(conn)
    con = sqlite3.connect(str(_SQLITE_PATH))
    con.row_factory = _sqlite_row_factory
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con
