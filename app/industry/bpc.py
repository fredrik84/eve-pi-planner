"""Blueprint (BPC/BPO) prices from public contracts, with history.

The planner can price every material off the market, but not the blueprint itself: blueprints trade
via **contracts**, and no market endpoint covers those. For a capital that's the single largest
invisible cost, so this indexes them from `/contracts/public/{region}/`.

Two hard constraints shape the design:

* **ESI cannot query contracts by item.** You list a region's contracts (34 pages ≈ 34k in The
  Forge), then fetch each contract's items separately. There is no way to ask "who is selling a
  Revelation BPC". Live per-plan lookup is therefore impossible — it has to be an index.
* **A blueprint is 0.01 m³.** Filtering the contract list to `volume == 0.01` isolates
  single-blueprint contracts and throws away ~55% of the region before any item lookup, which is
  what makes the scan affordable at all.

So the scan runs in the background, bounded and resumable, and every observation is KEPT. History is
the point: contracts come and go, so when nothing is listed today we can still answer "these went
for about X" instead of shrugging. A sold-out blueprint is the normal case, not an edge case.

Prices are reported, never folded into the build cost. A contract price is one seller's ask, not a
market rate — quietly adding it to the number that drives make-or-buy would let a single optimistic
listing flip build decisions.
"""

from __future__ import annotations

import statistics
import threading
import time

import httpx
from fastapi import Depends

from app.db import get_connection
from app.esi import ESI_BASE, require_context
from app.industry._router import router

THE_FORGE = 10000002          # Jita's region — where blueprint contracts actually are
_BP_VOLUME = 0.01             # one blueprint; the filter that makes the scan affordable
_SCAN_TTL = 22 * 3600         # don't re-scan a region more than ~daily
_HISTORY_DAYS = 120           # how far back an estimate may draw

_scan_lock = threading.Lock()
_scanning: set[int] = set()


def ensure_bpc_tables():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_bpc_observations (
                contract_id  BIGINT PRIMARY KEY,
                region_id    INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                is_bpc       INTEGER NOT NULL DEFAULT 1,
                runs         INTEGER NOT NULL DEFAULT 0,
                me           INTEGER NOT NULL DEFAULT 0,
                te           INTEGER NOT NULL DEFAULT 0,
                price        REAL    NOT NULL,
                first_seen   REAL    NOT NULL,
                last_seen    REAL    NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_bpc_obs_type ON pp_bpc_observations (type_id)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_bpc_scan (
                region_id  INTEGER PRIMARY KEY,
                started_at REAL,
                ended_at   REAL,
                seen       INTEGER NOT NULL DEFAULT 0,
                indexed    INTEGER NOT NULL DEFAULT 0
            )
        """)
        con.commit()
    finally:
        con.close()


def _scan_state(region_id: int) -> dict:
    ensure_bpc_tables()
    con = get_connection()
    try:
        r = con.execute("SELECT * FROM pp_bpc_scan WHERE region_id=?", (region_id,)).fetchone()
    finally:
        con.close()
    return dict(r) if r else {}


def _flush(batch: list[tuple], region_id: int, seen: int, indexed: int) -> None:
    """Write one page's observations plus a progress marker, in a single connection."""
    con = get_connection()
    try:
        for row in batch:
            con.execute(
                "INSERT INTO pp_bpc_observations (contract_id, region_id, type_id, is_bpc, runs, "
                "me, te, price, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT (contract_id) DO UPDATE SET last_seen=excluded.last_seen, "
                "price=excluded.price",
                row,
            )
        con.execute("UPDATE pp_bpc_scan SET seen=?, indexed=? WHERE region_id=?",
                    (seen, indexed, region_id))
        con.commit()
    finally:
        con.close()


def _run_scan(region_id: int) -> None:
    """Walk a region's public contracts and index every single-blueprint one. Long-running by
    nature (~15k item lookups in The Forge), so it's only ever called on a background thread."""
    started = time.time()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_bpc_scan (region_id, started_at, seen, indexed) VALUES (?,?,0,0) "
            "ON CONFLICT (region_id) DO UPDATE SET started_at=excluded.started_at",
            (region_id, started),
        )
        con.commit()
    finally:
        con.close()

    seen = indexed = 0
    batch: list[tuple] = []
    try:
        with httpx.Client(timeout=30, headers={"User-Agent": "eve-pi-planner/1.0"}) as c:
            page, pages = 1, 1
            while page <= pages and page <= 60:
                r = c.get(f"{ESI_BASE}/contracts/public/{region_id}/", params={"page": page})
                if r.status_code != 200:
                    break
                pages = int(r.headers.get("x-pages") or 1)
                rows = r.json() or []
                if not rows:
                    break
                for x in rows:
                    seen += 1
                    if x.get("type") != "item_exchange" or (x.get("price") or 0) <= 0:
                        continue
                    if abs((x.get("volume") or 0) - _BP_VOLUME) > 1e-9:
                        continue      # not a lone blueprint — skip the item lookup entirely
                    try:
                        ir = c.get(f"{ESI_BASE}/contracts/public/items/{x['contract_id']}/")
                        if ir.status_code != 200:
                            continue
                        items = ir.json() or []
                    except Exception:
                        continue
                    if len(items) != 1:
                        continue      # a bundle isn't attributable to one blueprint's price
                    it = items[0]
                    now = time.time()
                    batch.append((int(x["contract_id"]), region_id, int(it["type_id"]),
                                  1 if it.get("is_blueprint_copy") else 0,
                                  int(it.get("runs") or 0), int(it.get("material_efficiency") or 0),
                                  int(it.get("time_efficiency") or 0), float(x["price"]), now, now))
                    indexed += 1
                # Flush per page rather than per row: a full scan is ~15k observations, and a
                # connect/commit/close cycle each would dominate the runtime. Writing progress here
                # too, so a running scan can report how far along it is instead of looking idle.
                _flush(batch, region_id, seen, indexed)
                batch = []
                page += 1
    except Exception:
        pass
    finally:
        con = get_connection()
        try:
            con.execute("UPDATE pp_bpc_scan SET ended_at=?, seen=?, indexed=? WHERE region_id=?",
                        (time.time(), seen, indexed, region_id))
            con.commit()
        finally:
            con.close()
        with _scan_lock:
            _scanning.discard(region_id)


def maybe_scan(region_id: int = THE_FORGE, force: bool = False) -> dict:
    """Kick off a background scan if the index is stale. Returns immediately — callers serve
    whatever is already indexed, including history, rather than blocking on ~15k lookups."""
    ensure_bpc_tables()
    st = _scan_state(region_id)
    fresh = st.get("ended_at") and (time.time() - st["ended_at"]) < _SCAN_TTL
    with _scan_lock:
        busy = region_id in _scanning
        if not busy and (force or not fresh):
            _scanning.add(region_id)
            threading.Thread(target=_run_scan, args=(region_id,), daemon=True).start()
            return {"started": True, "busy": True}
    return {"started": False, "busy": busy, "fresh": bool(fresh)}


def _summarise(rows: list, live_cutoff: float) -> dict | None:
    """Turn observations of ONE blueprint into a price answer.

    `live` = seen in the most recent scan, i.e. you could buy it now. `history` = everything within
    the window, which is what answers the common case of nothing being listed today. The median is
    used rather than the mean because contract prices have absurd outliers (a hopeful seller asking
    10x) that would drag an average badly.
    """
    if not rows:
        return None
    live = [r for r in rows if r["last_seen"] >= live_cutoff]
    hist = rows

    def stat(rs):
        if not rs:
            return None
        prices = sorted(r["price"] for r in rs)
        per_run = sorted((r["price"] / r["runs"]) for r in rs if (r["runs"] or 0) > 0)
        return {
            "count": len(rs),
            "cheapest": round(prices[0], 2),
            "median": round(statistics.median(prices), 2),
            "median_per_run": round(statistics.median(per_run), 2) if per_run else None,
            "last_seen": max(r["last_seen"] for r in rs),
        }

    return {"live": stat(live), "history": stat(hist),
            "sample_runs": sorted({int(r["runs"] or 0) for r in hist})[:6]}


def bpc_prices(type_ids: list[int], region_id: int = THE_FORGE) -> dict[int, dict]:
    """{product_type_id: price info} for the BLUEPRINTS of those products.

    Callers pass product type_ids (what the plan wants to build); the SDE maps each to its blueprint
    type, which is what contracts actually list.
    """
    ensure_bpc_tables()
    if not type_ids:
        return {}
    con = get_connection()
    try:
        marks = ",".join("?" * len(type_ids))
        bp_of = {r["product_type_id"]: r["blueprint_type_id"] for r in con.execute(
            f"SELECT blueprint_type_id, product_type_id FROM blueprints "
            f"WHERE product_type_id IN ({marks})", tuple(type_ids))}
        if not bp_of:
            return {}
        bp_ids = list(set(bp_of.values()))
        bmarks = ",".join("?" * len(bp_ids))
        cutoff = time.time() - _HISTORY_DAYS * 86400
        rows = con.execute(
            f"SELECT type_id, is_bpc, runs, me, te, price, last_seen FROM pp_bpc_observations "
            f"WHERE region_id=? AND type_id IN ({bmarks}) AND last_seen >= ?",
            (region_id, *bp_ids, cutoff),
        ).fetchall()
        st = _scan_state(region_id)
    finally:
        con.close()

    # "Live" means seen in the last completed scan; anything older sold or expired.
    live_cutoff = (st.get("started_at") or 0) - 60

    by_bp: dict[int, list] = {}
    for r in rows:
        by_bp.setdefault(int(r["type_id"]), []).append(dict(r))

    out: dict[int, dict] = {}
    for prod, bp in bp_of.items():
        rs = by_bp.get(bp, [])
        copies = [r for r in rs if r["is_bpc"]]
        originals = [r for r in rs if not r["is_bpc"]]
        info = {"blueprint_type_id": bp,
                "bpc": _summarise(copies, live_cutoff),
                "bpo": _summarise(originals, live_cutoff)}
        if info["bpc"] or info["bpo"]:
            out[prod] = info
    return out


@router.get("/api/industry/bpc")
def industry_bpc(type_ids: str = "", scan: int = 1, ctx: int = Depends(require_context)):
    """Blueprint contract prices for the given product type_ids (comma separated).

    Serves the index immediately and refreshes it in the background when stale, because a cold scan
    is ~15k ESI calls and must never block a page load.
    """
    ids = [int(x) for x in type_ids.split(",") if x.strip().isdigit()]
    state = maybe_scan(THE_FORGE) if scan else {"started": False}
    st = _scan_state(THE_FORGE)
    return {"prices": bpc_prices(ids), "scan": {**state,
            "last_completed": st.get("ended_at"), "indexed": st.get("indexed", 0)}}


@router.post("/api/industry/bpc/scan")
def industry_bpc_scan(ctx: int = Depends(require_context)):
    return {**maybe_scan(THE_FORGE, force=True), **_scan_state(THE_FORGE)}
