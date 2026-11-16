"""
Postgres connection-pool recovery tests.

Background: the pool opened its 8 psycopg2 connections once per process and never revalidated
them, so a Postgres restart or a move to another node left every pooled connection pointing at a
dead socket — the app served errors until someone ran `rollout restart` by hand. That cost real
downtime on 2026-07-28. These assert the pool now heals itself, and (just as important) that it
never shrinks: the slot count is what bounds concurrency, so a connection thrown away has to
leave its slot behind.

Runs entirely in-process against a fake psycopg2 — no Postgres, no container needed:
    python3 tests/test_db_pool.py
"""

import gc
import os
import subprocess
import sys
import types
from unittest.mock import patch

sys.path.insert(0, ".")

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# --- fake psycopg2 -------------------------------------------------------------------------

class FakeCursor:
    def __init__(self, conn):
        self._conn = conn

    def execute(self, sql, params=None):
        if sql == "SELECT 1":
            self._conn.pings += 1   # count ONLY the pool's liveness probe, not ordinary queries
        self._conn._fail_if_dead()

    def close(self):
        pass


class FakeConn:
    """Mimics the part of psycopg2 that matters here: a connection whose server has gone away
    still reports closed == 0, and only raises when you actually try to use it."""

    def __init__(self):
        self.closed = 0
        self.autocommit = False
        self.dead = False          # server went away; conn looks fine until used
        self.pings = 0

    def _fail_if_dead(self):
        if self.dead:
            raise OperationalError("server closed the connection unexpectedly")

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self._fail_if_dead()

    def rollback(self):
        self._fail_if_dead()

    def close(self):
        self.closed = 1


class OperationalError(Exception):
    pass


CONNECT_CALLS = []
CONNECT_OPTIONS = []
CONNECT_FAILS = [0]   # how many upcoming connect() attempts should fail


def fake_connect(dsn, **options):
    CONNECT_CALLS.append(dsn)
    CONNECT_OPTIONS.append(options)
    if CONNECT_FAILS[0] > 0:
        CONNECT_FAILS[0] -= 1
        raise OperationalError("could not connect to server")
    return FakeConn()


fake_psycopg2 = types.ModuleType("psycopg2")
fake_psycopg2.connect = fake_connect
fake_psycopg2.OperationalError = OperationalError
sys.modules["psycopg2"] = fake_psycopg2

import app.db as db  # noqa: E402  (must follow the psycopg2 stub)

db.DATABASE_URL = "postgresql://fake/fake"
db._IS_POSTGRES = True


def reset_pool():
    db._PG_POOL = None
    CONNECT_CALLS.clear()
    CONNECT_OPTIONS.clear()
    CONNECT_FAILS[0] = 0


def pool_slots():
    """Drain the pool into a list of (conn, idle_since) without disturbing the count."""
    q = db._pg_pool()
    slots = [q.get_nowait() for _ in range(q.qsize())]
    for s in slots:
        q.put(s)
    return slots


# --- tests ---------------------------------------------------------------------------------

def test_pool_starts_full():
    reset_pool()
    slots = pool_slots()
    check(len(slots) == db._PG_POOL_SIZE, f"pool opens {db._PG_POOL_SIZE} slots (got {len(slots)})")
    check(all(c is not None for c, _ in slots), "every slot is warmed with a connection")


def test_healthy_connection_is_not_pinged():
    """The ping is a real round-trip. Under load connections are recycled in milliseconds and
    the far end is provably alive, so borrowing must not cost an extra query."""
    reset_pool()
    pool_slots()                       # force lazy pool creation before counting
    opened = len(CONNECT_CALLS)
    # Two full laps of the (FIFO) pool: a returned connection goes to the back, so this borrows
    # every slot twice over.
    for _ in range(db._PG_POOL_SIZE * 2):
        db.get_connection().close()
    check(sum(c.pings for c, _ in pool_slots()) == 0,
          "no liveness ping while connections are being recycled inside the idle window")
    check(len(CONNECT_CALLS) == opened, "and no connection is needlessly reopened")


def test_idle_connection_is_revalidated():
    reset_pool()
    con = db.get_connection()
    inner = con._conn
    con.close()
    # Backdate the slot past the idle threshold.
    q = db._pg_pool()
    slots = [q.get_nowait() for _ in range(q.qsize())]
    q.put((inner, 0.0))   # idle_since = 0 -> way past _PG_IDLE_PING_AFTER
    for s in slots:
        if s[0] is not inner:
            q.put(s)
    before = inner.pings
    con2 = db.get_connection()
    check(con2._conn.pings > before, "a long-idle connection gets a liveness ping on borrow")
    con2.close()


def test_dead_connection_is_replaced_on_borrow():
    """The 2026-07-28 case: Postgres moved, every pooled socket is stale."""
    reset_pool()
    for conn, _ in pool_slots():
        conn.dead = True
    # Backdate every slot so they all qualify for a ping.
    q = db._pg_pool()
    slots = [q.get_nowait() for _ in range(q.qsize())]
    for conn, _ in slots:
        q.put((conn, 0.0))

    opened = len(CONNECT_CALLS)
    con = db.get_connection()
    check(len(CONNECT_CALLS) == opened + 1, "a dead connection is replaced with a fresh one")
    check(not con._conn.dead, "the borrower gets a live connection, not the corpse")
    con.close()
    check(db._pg_pool().qsize() == db._PG_POOL_SIZE, "pool still holds every slot after a replace")


def test_connection_that_dies_mid_request_still_returns_its_slot():
    """A commit() against a vanished server raises. The slot has to come back anyway, or eight
    failed requests strand the whole pool for the life of the process."""
    reset_pool()
    size = db._PG_POOL_SIZE
    for _ in range(size + 2):
        con = db.get_connection()
        con._conn.dead = True
        try:
            con.close()
        except OperationalError:
            pass
    check(db._pg_pool().qsize() == size, f"pool still has {size} slots after repeated failures")

    reset_pool()
    for _ in range(size + 2):
        try:
            with db.get_connection() as con:
                con._conn.dead = True
        except OperationalError:
            pass
    check(db._pg_pool().qsize() == size, "same via the context-manager path")


def test_slot_survives_a_failed_reconnect():
    """DB briefly unreachable: we can neither reuse nor replace. The slot must still go back."""
    reset_pool()
    q = db._pg_pool()
    slots = [q.get_nowait() for _ in range(q.qsize())]
    for conn, _ in slots:
        conn.dead = True
        q.put((conn, 0.0))

    CONNECT_FAILS[0] = 3
    raised = 0
    for _ in range(3):
        try:
            db.get_connection().close()
        except OperationalError:
            raised += 1
    check(raised == 3, "a failed reconnect surfaces the error instead of hanging")
    check(q.qsize() == db._PG_POOL_SIZE, "no slot is lost while the DB is unreachable")

    # ...and once the DB comes back, the pool refills itself with no restart.
    con = db.get_connection()
    check(con._conn is not None and not con._conn.dead, "pool recovers on its own once the DB is back")
    con.close()


def test_pool_creation_survives_a_down_database():
    """First request lands while Postgres is unreachable — the pool must still come up with a
    full set of (empty) slots rather than raising and leaving _PG_POOL unset."""
    reset_pool()
    CONNECT_FAILS[0] = 1
    slots = pool_slots()
    check(len(slots) == db._PG_POOL_SIZE, "pool is created at full size even with the DB down")
    check(all(c is None for c, _ in slots), "slots are empty, to be filled on first borrow")
    check(len(CONNECT_CALLS) == 1, "warming stops after a failed connection instead of amplifying an outage")
    con = db.get_connection()
    check(con._conn is not None, "an empty slot connects lazily on borrow")
    con.close()


def test_abandoned_connection_rolls_back_without_losing_capacity():
    reset_pool()
    q = db._pg_pool()
    con = db.get_connection()
    raw = con._conn
    del con  # An exception or forgotten close in any caller must not permanently lose a slot.
    gc.collect()
    check(raw.closed, "an abandoned connection is closed, never committed or reused dirty")
    check(q.qsize() == db._PG_POOL_SIZE, "garbage collection restores the abandoned pool slot")
    con = db.get_connection()
    con.close()


def test_returning_twice_cannot_duplicate_a_connection():
    reset_pool()
    q = db._pg_pool()
    con = db.get_connection()
    con.close()
    con.close()
    con.__exit__(None, None, None)
    del con
    gc.collect()
    check(q.qsize() == db._PG_POOL_SIZE, "close/context-exit/finalization can return each slot only once")
    borrowed = [db.get_connection() for _ in range(db._PG_POOL_SIZE)]
    check(len({id(c._conn) for c in borrowed}) == db._PG_POOL_SIZE,
          "concurrent borrowers never receive the same underlying connection")
    for con in borrowed:
        con.close()


def test_live_cursor_keeps_its_checkout_until_released():
    reset_pool()
    q = db._pg_pool()
    con = db.get_connection()
    raw = con._conn
    cursor = con.cursor()
    del con
    gc.collect()
    check(not raw.closed and q.qsize() == db._PG_POOL_SIZE-1,
          "a live cursor keeps its connection checked out after its wrapper leaves scope")
    del cursor
    gc.collect()
    check(raw.closed and q.qsize() == db._PG_POOL_SIZE,
          "the abandoned slot is restored after the last cursor leaves scope")


def test_connection_and_acquisition_waits_are_bounded():
    reset_pool()
    q = db._pg_pool()
    check(all(0 < opts.get("connect_timeout", 0) <= 10 for opts in CONNECT_OPTIONS),
          "pool warming uses a bounded connection timeout")
    check(all(opts.get("keepalives_idle", 0) > 0 and opts.get("tcp_user_timeout", 0) > 0
              for opts in CONNECT_OPTIONS), "dead network sockets have explicit failure detection")
    # Replacements must get the same protection as the initial sockets.
    slots = [q.get_nowait() for _ in range(q.qsize())]
    for conn, _ in slots:
        conn.dead = True
        q.put((conn, 0.0))
    db.get_connection().close()
    check(CONNECT_OPTIONS[-1] == CONNECT_OPTIONS[0], "reconnections retain the network timeouts")
    import queue
    with patch.object(q, "get", side_effect=queue.Empty) as take:
        try:
            db.get_connection()
        except RuntimeError:
            pass
        else:
            check(False, "exhaustion must raise rather than silently overbook")
        check(take.call_args.kwargs.get("timeout", 0) > 0, "pool acquisition has a finite wait")


def test_capacity_and_wait_are_configurable_and_validated():
    for size, wait, valid in [("3", "0.2", True), ("0", "15", False),
                              ("16", "0", False), ("16", "nan", False), ("16", "inf", False)]:
        result = subprocess.run([sys.executable, "-c",
                                 "from app import db; print(db._PG_POOL_SIZE, db._PG_POOL_WAIT_SECONDS)"],
                                env={**os.environ, "DB_POOL_SIZE": size, "DB_POOL_WAIT_SECONDS": wait},
                                capture_output=True, text=True, timeout=5)
        check((result.returncode == 0) == valid, f"pool configuration size={size} wait={wait} is validated")
        if valid:
            check(result.stdout.strip() == "3 0.2", "capacity and acquisition wait use the configured values")


if __name__ == "__main__":
    test_pool_starts_full()
    test_healthy_connection_is_not_pinged()
    test_idle_connection_is_revalidated()
    test_dead_connection_is_replaced_on_borrow()
    test_connection_that_dies_mid_request_still_returns_its_slot()
    test_slot_survives_a_failed_reconnect()
    test_pool_creation_survives_a_down_database()
    test_abandoned_connection_rolls_back_without_losing_capacity()
    test_returning_twice_cannot_duplicate_a_connection()
    test_live_cursor_keeps_its_checkout_until_released()
    test_connection_and_acquisition_waits_are_bounded()
    test_capacity_and_wait_are_configurable_and_validated()
    print()
    if FAILS:
        print(f"{len(FAILS)} FAILED:")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("all passed")
