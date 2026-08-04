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

import os
import socket
import math
import statistics
import threading
import time

from fastapi import Depends

from app.db import get_connection, add_columns
from app import esi_http
from app.esi import require_context
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
        # A thread lock only guards ONE process, and prod runs several replicas — so the lease that
        # decides who scans has to live in the shared database, not in memory.
        add_columns(con, "pp_bpc_scan", "lease_until REAL", "owner TEXT")
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


def _owner_id() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


_LEASE_TTL = 900          # 15 min; renewed every page, so a dead scanner frees the region


def _claim(region_id: int, owner: str) -> bool:
    """Win the right to scan this region, across every replica. True if we got it.

    The conditional UPDATE is the whole mechanism: only one row exists per region, and only a
    caller whose WHERE clause still matches an expired lease can take it. Postgres makes that
    atomic, so two pods racing produce exactly one winner.
    """
    ensure_bpc_tables()
    now = time.time()
    con = get_connection()
    try:
        con.execute("INSERT INTO pp_bpc_scan (region_id, seen, indexed) VALUES (?,0,0) "
                    "ON CONFLICT (region_id) DO NOTHING", (region_id,))
        cur = con.execute(
            "UPDATE pp_bpc_scan SET lease_until=?, owner=?, started_at=?, ended_at=NULL "
            "WHERE region_id=? AND (lease_until IS NULL OR lease_until < ?)",
            (now + _LEASE_TTL, owner, now, region_id, now),
        )
        won = (cur.rowcount or 0) == 1
        con.commit()
        return won
    finally:
        con.close()


def _renew(region_id: int, owner: str) -> bool:
    """Extend our lease. False means someone else now owns the region and we must stop — otherwise
    a stalled scanner would keep writing over a healthy one's progress."""
    con = get_connection()
    try:
        cur = con.execute(
            "UPDATE pp_bpc_scan SET lease_until=? WHERE region_id=? AND owner=?",
            (time.time() + _LEASE_TTL, region_id, owner),
        )
        con.commit()
        return (cur.rowcount or 0) == 1
    finally:
        con.close()


def _release(region_id: int, owner: str, seen: int, indexed: int) -> None:
    con = get_connection()
    try:
        con.execute(
            "UPDATE pp_bpc_scan SET ended_at=?, seen=?, indexed=?, lease_until=NULL "
            "WHERE region_id=? AND owner=?",
            (time.time(), seen, indexed, region_id, owner),
        )
        con.commit()
    finally:
        con.close()


def _flush(batch: list[tuple], region_id: int, seen: int, indexed: int,
           still_live: list[int] | None = None) -> None:
    """Write one page's observations plus a progress marker, in a single connection."""
    now = time.time()
    con = get_connection()
    try:
        # Contracts we'd already indexed only need their "still on offer" stamp refreshed.
        for i in range(0, len(still_live or []), 500):
            chunk = (still_live or [])[i:i + 500]
            marks = ",".join("?" * len(chunk))
            con.execute(f"UPDATE pp_bpc_observations SET last_seen=? WHERE contract_id IN ({marks})",
                        (now, *chunk))
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
    owner = _owner_id()
    seen = indexed = refreshed = 0
    batch: list[tuple] = []
    # A contract_id is stable for the life of the contract, and blueprints sit on contract for days
    # or weeks. So the expensive half of a scan — one item lookup per contract — is almost entirely
    # re-fetching things we already know. Load what we've seen and skip those: the first scan costs
    # ~15k lookups, every later one costs only the genuinely new contracts.
    con = get_connection()
    try:
        known = {r["contract_id"] for r in con.execute(
            "SELECT contract_id FROM pp_bpc_observations WHERE region_id=?", (region_id,))}
    finally:
        con.close()
    still_live: list[int] = []
    try:
        with esi_http.client(timeout=30) as c:
            page, pages = 1, 1
            while page <= pages and page <= 60:
                r = esi_http.get(f"contracts/public/{region_id}/", client=c, params={"page": page})
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
                    cid = int(x["contract_id"])
                    if cid in known:
                        # Already indexed: just mark it still on offer. A contract that stops
                        # appearing simply stops being refreshed and ages out of "live" by itself.
                        still_live.append(cid)
                        refreshed += 1
                        continue
                    try:
                        ir = esi_http.get(f"contracts/public/items/{cid}/", client=c)
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
                _flush(batch, region_id, seen, indexed, still_live)
                batch = []
                still_live = []
                if not _renew(region_id, owner):
                    break        # another replica took the region — stop rather than fight it
                page += 1
    except Exception:
        pass
    finally:
        _release(region_id, owner, seen, indexed)
        with _scan_lock:
            _scanning.discard(region_id)


def maybe_scan(region_id: int = THE_FORGE, force: bool = False) -> dict:
    """Kick off a background scan if the index is stale. Returns immediately — callers serve
    whatever is already indexed, including history, rather than blocking on ~15k lookups."""
    ensure_bpc_tables()
    st = _scan_state(region_id)
    fresh = st.get("ended_at") and (time.time() - st["ended_at"]) < _SCAN_TTL
    if fresh and not force:
        return {"started": False, "busy": False, "fresh": True}
    with _scan_lock:
        if region_id in _scanning:                 # this process is already on it
            return {"started": False, "busy": True}
        # The DB lease is the real gate — with several replicas the in-memory set only stops this
        # process spawning twice. A replica that loses the race simply serves the existing index.
        if not _claim(region_id, _owner_id()):
            return {"started": False, "busy": True, "held_by_other": True}
        _scanning.add(region_id)
        threading.Thread(target=_run_scan, args=(region_id,), daemon=True).start()
        return {"started": True, "busy": True}


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
                "bpo": _summarise(originals, live_cutoff),
                # ME/TE ride along with each listing: a contracted copy's research is what the build
                # would actually run at, and it's already in the row — no extra query for it.
                "bpc_listings": [{"price": r["price"], "runs": int(r["runs"] or 0),
                                  "me": int(r["me"] or 0), "te": int(r["te"] or 0)}
                                 for r in copies if r["last_seen"] >= live_cutoff],
                "bpc_listings_hist": [{"price": r["price"], "runs": int(r["runs"] or 0),
                                       "me": int(r["me"] or 0), "te": int(r["te"] or 0)}
                                      for r in copies]}
        if info["bpc"] or info["bpo"]:
            out[prod] = info
    return out


def cost_for_runs(info: dict, runs_needed: int) -> dict:
    """Cheapest real way to buy `runs_needed` runs of a blueprint, using each listing at most once.

    A copy carries a fixed number of runs and one contract is one item, so covering 40 runs from
    10-run copies means buying four separate contracts.

    Minimises TOTAL cost, not cost-per-run. Those differ, and the difference is expensive: needing a
    single run, a greedy per-run pick takes a 300m 10-run copy over a 50m 1-run copy — six times the
    price for runs you'll never use. A small bounded knapsack over the run count gets it right, and
    the listing count is tiny (tens), so it's cheap.

    If the market can't cover the batch, the shortfall is priced at the median per-run rate and
    flagged, with the remaining copies estimated from the LARGEST copy on offer — the smallest would
    imply an absurd number of purchases.
    """
    listings = [l for l in (info.get("listings") or []) if (l.get("runs") or 0) > 0]
    need = max(0, int(runs_needed))
    if need == 0:
        return {"cost": 0.0, "copies": 0, "covered": True, "short_runs": 0}
    if not listings:
        # No per-listing detail. Price from the per-run median if we have one, else fall back to a
        # single copy at the known price — never return 0, which would read as "free" and is the
        # most damaging way to be wrong here.
        per_run = info.get("median_per_run") or 0.0
        if per_run:
            return {"cost": round(need * per_run, 2), "copies": 1,
                    "covered": False, "short_runs": need}
        price = float(info.get("price") or 0.0)
        return {"cost": round(price, 2), "copies": 1 if price else 0,
                "covered": False, "short_runs": need}

    # dp[r] = (cost, copies) to obtain at least r runs. Runs are capped at `need` because anything
    # beyond it is waste we don't pay extra to model.
    cap = min(need, 20000)                 # bound the table for a pathological batch size
    INF = float("inf")
    dp = [(INF, 0)] * (cap + 1)
    dp[0] = (0.0, 0)
    for l in listings:
        gain = min(int(l["runs"]), cap)
        price = float(l["price"])
        for r in range(cap, -1, -1):       # 0/1: each contract can only be bought once
            cost_r, n_r = dp[r]
            if cost_r == INF:
                continue
            nxt = min(cap, r + gain)
            if cost_r + price < dp[nxt][0]:
                dp[nxt] = (cost_r + price, n_r + 1)

    cost, copies = dp[cap]
    if cost < INF:
        return {"cost": round(cost, 2), "copies": copies, "covered": True, "short_runs": 0}

    # Can't cover it from what's listed: take everything, then extrapolate the rest.
    total_runs = sum(int(l["runs"]) for l in listings)
    cost = sum(float(l["price"]) for l in listings)
    copies = len(listings)
    short = max(0, need - total_runs)
    per_run = info.get("median_per_run") or (cost / total_runs if total_runs else 0.0)
    biggest = max(int(l["runs"]) for l in listings)
    cost += short * per_run
    copies += math.ceil(short / biggest) if biggest else 0
    return {"cost": round(cost, 2), "copies": copies, "covered": False, "short_runs": short}


def cost_for_copies(info: dict, n_copies: int, skip: int = 0) -> dict:
    """What `n_copies` MORE prints of a blueprint cost, whatever runs they happen to carry.

    A different question from `cost_for_runs`, and it has to be asked separately: that one covers a
    run count, this one puts N prints in the builder's hands AT ONCE. A blueprint is locked while a
    job runs on it, so parallelism is bought in copies and never in runs — a single 20-run copy is
    one job at a time, not twenty.

    Cheapest listings first, `skip`ping the ones the run-shortfall purchase has already spent so the
    same contract is never sold to this build twice. If the market doesn't list enough, the rest are
    priced at the median of what IS listed — never at zero, which would read as "parallelism is
    free" and is the most damaging way to be wrong about a purchase the builder didn't ask for.
    """
    n = max(0, int(n_copies))
    if n == 0:
        return {"cost": 0.0, "copies": 0, "covered": True}
    prices = sorted(float(l["price"]) for l in (info.get("listings") or []) if l.get("price"))
    take = prices[max(0, int(skip)):][:n]
    missing = n - len(take)
    cost = sum(take)
    if missing:
        each = statistics.median(prices) if prices else float(info.get("price") or 0.0)
        cost += missing * each
    return {"cost": round(cost, 2), "copies": n, "covered": missing == 0}


def representative_me_te(info: dict) -> tuple[int, int] | None:
    """(me, te) the build should assume for a print it doesn't own — the research on the copy it
    would actually buy.

    Which copy is that? `cost_for_runs` minimises total price, so the copy the plan reaches for is
    the cheapest per run; that listing's own ME/TE is what the build would really run at. Ties break
    toward better research, since between two equally-priced copies you would take the researched
    one.

    ONE function decides this for both the costing pass and what the UI reports, because the
    alternative — costing against one copy's price and another copy's efficiency — is exactly the
    mismatch that makes a plan quietly wrong. A build that buys several copies of mixed research is
    approximated by this single value; the per-product override exists for that case.

    None when there's nothing to go on, which leaves the old behaviour (the global ME/TE fallback).
    """
    listings = [l for l in (info.get("listings") or []) if (l.get("runs") or 0) > 0]
    if not listings:
        return None
    best = min(listings, key=lambda l: (l["price"] / l["runs"], -int(l.get("me") or 0),
                                        -int(l.get("te") or 0)))
    return (int(best.get("me") or 0), int(best.get("te") or 0))


def acquisition_costs(type_ids: list[int], owned: dict, region_id: int = THE_FORGE) -> dict[int, dict]:
    """What it would COST to get a blueprint you don't already have, per product type.

    Returns {type_id: {kind, price, runs_per_copy, live}} where kind is:
      * ``bpc``      — copies are listed; a consumable, so its price is fully attributable to this
                       build (you buy however many copies the run count needs).
      * ``bpo_only`` — no copies listed, only originals. An original is a durable asset costing
                       billions; charging it to one build would be nonsense, and charging nothing
                       hides that you can't start at all. The caller treats these as "buy the
                       component instead", which is what a person would actually do.

    Types whose owned blueprint is an ORIGINAL are omitted — you have it, and it never runs out.
    An owned COPY is still priced: it carries a fixed number of runs, so a batch longer than that
    needs more copies, and the caller nets its own copy off before charging for the rest.
    """
    # Owned BPOs are done — an original never runs out. An owned COPY is not: it carries a fixed
    # number of runs, and a batch bigger than that needs more copies from somewhere. So those stay
    # in the list, and the caller decides how much of the batch its own copy already covers.
    wanted = [t for t in type_ids
              if t not in owned or (owned[t] or {}).get("kind") == "bpc"]
    prices = bpc_prices(wanted, region_id)
    out: dict[int, dict] = {}
    for tid, info in prices.items():
        bpc = info.get("bpc") or {}
        live, hist = bpc.get("live"), bpc.get("history")
        pick = live or hist
        if pick:
            ls = (info.get("bpc_listings") if live else info.get("bpc_listings_hist")) or []
            runs = [int(l.get("runs") or 0) for l in ls if (l.get("runs") or 0) > 0]
            out[tid] = {"kind": "bpc",
                        "price": pick["cheapest"] if live else pick["median"],
                        "median_per_run": pick.get("median_per_run"),
                        # The real contracts, each with its own run count — what actually decides
                        # how many you have to buy.
                        "listings": ls,
                        # The most runs ANY single copy on offer carries. `build_tasks` caps a job
                        # at this: a job needs one copy, so a 20-run job off a market where the
                        # biggest copy is 5 runs cannot be installed at all. It is the *loosest*
                        # honest cap — the plan may buy a smaller copy — but it is the one that is
                        # true whatever combination `cost_for_runs` picks, and until this key was
                        # populated the cap in build_tasks was dead code (only `bpo_only` set it),
                        # which is how a capital job could be planned longer than any copy sold.
                        "runs_per_copy": max(runs) if runs else 0,
                        "live": bool(live)}
            continue
        bpo = info.get("bpo") or {}
        b = bpo.get("live") or bpo.get("history")
        if b:
            out[tid] = {"kind": "bpo_only", "price": b["cheapest"], "runs_per_copy": 0,
                        "live": bool(bpo.get("live"))}
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
