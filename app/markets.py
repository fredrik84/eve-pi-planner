"""Followed-market configuration + local/structure market pricing overlay for the Reactions tab.

A player's alliance often sells inputs (moon goo, fuel blocks) far below Jita on a player-owned
Upwell **structure market** in their staging system, or in a public NPC **region** market. This
module lets an account follow one or more such markets in a **priority order** (e.g. private
structure -> public region -> Jita) and price reactions against them, falling back to Jita
(Fuzzwork, app.market) for anything not listed locally.

Config is per-account with a group-seeded default, mirroring the freight resolver in
app.reactions.settings: `effective_markets(context_id)` = the account's own list, else the
account's alliance-group default list, else empty (= Jita only). Structure markets need ESI auth;
the reading character is any character in the context that authorised the market scope
(`_market_character`, same shape as esi_data._wallet_character for the wallet scope).
"""
import logging

import httpx
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.cache import cache_get_json, cache_set_json, cache_mget_json, cache_mset_json
from app.esi import (
    ESI_BASE, MARKET_SCOPE, _get_valid_token, require_context,
)
from app.groups import member_group, is_group_manager
from app.market import fetch_market_data, CACHE_TTL

from fastapi import APIRouter

router = APIRouter()
log = logging.getLogger(__name__)

_ALLOWED_KINDS = ("structure", "region")


# ── Storage ────────────────────────────────────────────────────────────────────────
# One table, discriminated by owner: owner_kind='account' (owner_id=context_id) is a personal
# list; owner_kind='group' (owner_id=group_id) is the group-manager-set default. Jita is NEVER a
# row — it's the implicit lowest-priority fallback baked into resolve_market_data().

@ensure_once
def ensure_markets_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_markets (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_kind  TEXT    NOT NULL,
            owner_id    INTEGER NOT NULL,
            kind        TEXT    NOT NULL,
            location_id BIGINT  NOT NULL,
            name        TEXT    NOT NULL,
            priority    INTEGER NOT NULL DEFAULT 0,
            active      INTEGER NOT NULL DEFAULT 1
        )
    """)
    # Unified structure list: a followed market row (kind='structure') can also be a BUILD structure
    # for manufacturing and/or reactions, carrying the fitted rig tiers + hull + security ESI can't
    # read as a fitting. Additive ALTER-COLUMN migration (this codebase's convention).
    for coldef in ("build_mfg INTEGER NOT NULL DEFAULT 0", "build_rx INTEGER NOT NULL DEFAULT 0",
                   "hull TEXT", "security TEXT",
                   "me_rig INTEGER NOT NULL DEFAULT 0", "te_rig INTEGER NOT NULL DEFAULT 0",
                   "rx_me_rig INTEGER NOT NULL DEFAULT 0", "rx_te_rig INTEGER NOT NULL DEFAULT 0"):
        try:
            con.execute(f"ALTER TABLE pp_markets ADD COLUMN {coldef}")
            con.commit()
        except Exception:
            pass
    con.commit()
    con.close()


@ensure_once
def ensure_market_config_table():
    """Per-context market config: which character reads the structure market (the designated
    'market character' — defaults to the first that authorised the scope) and whether the user has
    completed the one-time Reactions onboarding (added a character + saved)."""
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_market_config (
            context_id          INTEGER PRIMARY KEY,
            market_character_id BIGINT,
            onboarded           INTEGER NOT NULL DEFAULT 0
        )
    """)
    con.commit()
    con.close()


def _market_config(context_id: int) -> dict:
    ensure_market_config_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT market_character_id, onboarded FROM pp_market_config WHERE context_id=?",
            (context_id,),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else {"market_character_id": None, "onboarded": 0}


def _context_characters(context_id: int) -> list[dict]:
    """Real (non-dummy) characters in this context, each flagged with whether it holds the market
    scope (so it can serve as the market character)."""
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, character_name, scopes FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=0 ORDER BY character_name",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return [{"character_id": r["character_id"], "character_name": r["character_name"],
             "is_market": MARKET_SCOPE in (r["scopes"] or "").split()} for r in rows]


# Engineering Complex / Refinery hull type_ids → our hull key (for ESI auto-detect on add).
STRUCTURE_HULLS = {35825: "raitaru", 35826: "azbel", 35827: "sotiyo",
                   35835: "athanor", 35836: "tatara"}


def _list_markets(owner_kind: str, owner_id: int) -> list[dict]:
    ensure_markets_table()
    from app.industry.structures import manufacturing_bonus, reaction_bonus
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, kind, location_id, name, priority, active, "
            "COALESCE(build_mfg,0) AS build_mfg, COALESCE(build_rx,0) AS build_rx, hull, security, "
            "COALESCE(me_rig,0) AS me_rig, COALESCE(te_rig,0) AS te_rig, "
            "COALESCE(rx_me_rig,0) AS rx_me_rig, COALESCE(rx_te_rig,0) AS rx_te_rig FROM pp_markets "
            "WHERE owner_kind=? AND owner_id=? AND active=1 ORDER BY priority, id",
            (owner_kind, owner_id),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        if d["kind"] == "structure":
            mme, mte = manufacturing_bonus(d["hull"], d["me_rig"], d["te_rig"], d["security"])
            rme, rte = reaction_bonus(d["hull"], d["rx_me_rig"], d["rx_te_rig"], d["security"])
            d["mfg_bonus"] = {"me": mme, "te": mte}
            d["rx_bonus"] = {"me": rme, "te": rte}
        out.append(d)
    return out


def _detect_structure_meta(context_id: int, structure_id: int) -> tuple[str | None, str | None]:
    """ESI-detect a structure's (hull, security_band) from /universe/structures + its system's
    security. Rigs are NOT exposed by ESI (no fitting endpoint), so those stay a manual pick.
    Best-effort: (None, None) if it can't be read."""
    ch = _market_character(context_id)
    if not ch:
        return (None, None)
    token = _get_valid_token(ch["character_id"])
    if not token:
        return (None, None)
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=12) as client:
            s = client.get(f"{ESI_BASE}/universe/structures/{structure_id}/?datasource=tranquility",
                           headers=headers)
            if s.status_code != 200:
                return (None, None)
            sj = s.json() or {}
            hull = STRUCTURE_HULLS.get(sj.get("type_id"))
            sys_id = sj.get("solar_system_id")
            sec = None
            if sys_id:
                sy = client.get(f"{ESI_BASE}/universe/systems/{sys_id}/?datasource=tranquility")
                if sy.status_code == 200:
                    v = (sy.json() or {}).get("security_status")
                    sec = "high" if (v or 0) >= 0.45 else "low" if (v or 0) > 0 else "null"
            return (hull, sec)
    except Exception:
        return (None, None)


def effective_markets(context_id: int) -> list[dict]:
    """Ordered market list actually used to price this account: personal list if any, else the
    account's alliance-group default list if any, else [] (Jita-only). Same personal->group->
    fallback shape as effective_reaction_settings()."""
    own = _list_markets("account", context_id)
    if own:
        return own
    group = member_group(context_id)
    if group:
        grp = _list_markets("group", group["id"])
        if grp:
            return grp
    return []


# ── Structure / region character + token ─────────────────────────────────────────────

def _market_character(context_id: int):
    """The character whose token reads the structure market: the user-designated one if it's set
    and still holds the scope, else the first character that authorised it (back-compat default),
    else None."""
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, character_name, scopes FROM pp_characters WHERE context_id=?",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    scoped = [r for r in rows if MARKET_SCOPE in (r["scopes"] or "").split()]
    if not scoped:
        return None
    chosen = _market_config(context_id).get("market_character_id")
    if chosen:
        for r in scoped:
            if r["character_id"] == chosen:
                return r
    return scoped[0]


# ── Order-book aggregation (matches Fuzzwork's shape for a drop-in with fetch_market_data) ──

def _wavg_percentile(sorted_orders: list[tuple[float, float]], pct: float = 0.05) -> float:
    """Volume-weighted average price of the best `pct` of volume — same idea as Fuzzwork's 5th-
    percentile, robust against a lone 1-unit order setting the price. `sorted_orders` is
    (price, volume) already sorted best-first (sells ascending, buys descending)."""
    total = sum(v for _, v in sorted_orders)
    if total <= 0:
        return 0.0
    cutoff = total * pct
    acc = num = den = 0.0
    for price, vol in sorted_orders:
        take = min(vol, cutoff - acc)
        if take <= 0:
            break
        num += price * take
        den += take
        acc += take
        if acc >= cutoff:
            break
    return num / den if den else sorted_orders[0][0]


def _agg_orders(orders: list[dict]) -> dict:
    sells = sorted(((o["price"], o["volume_remain"]) for o in orders if not o.get("is_buy_order")),
                   key=lambda x: x[0])
    buys = sorted(((o["price"], o["volume_remain"]) for o in orders if o.get("is_buy_order")),
                  key=lambda x: -x[0])
    return {
        "sell_price": _wavg_percentile(sells),
        "buy_price": _wavg_percentile(buys),
        "sell_volume": float(sum(v for _, v in sells)),
        "buy_volume": float(sum(v for _, v in buys)),
    }


# ── Structure market (authed, whole book) ────────────────────────────────────────────

def _fetch_structure_orders(context_id: int, structure_id: int) -> list[dict] | None:
    """All orders in an Upwell structure's market, or None if unreadable (no market character,
    expired token, or no docking/market access -> 401/403). None means 'fall through to the next
    market', distinct from [] (readable but empty)."""
    ch = _market_character(context_id)
    if not ch:
        return None
    token = _get_valid_token(ch["character_id"])
    if not token:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    base = f"{ESI_BASE}/markets/structures/{structure_id}/?datasource=tranquility"
    orders: list[dict] = []
    try:
        with httpx.Client(timeout=20) as client:
            r = client.get(f"{base}&page=1", headers=headers)
            if r.status_code in (401, 403, 404):
                return None
            r.raise_for_status()
            orders.extend(r.json() or [])
            pages = int(r.headers.get("X-Pages", "1") or 1)
            for p in range(2, pages + 1):
                rp = client.get(f"{base}&page={p}", headers=headers)
                if rp.status_code != 200:
                    break
                orders.extend(rp.json() or [])
    except Exception:
        log.warning("structure market fetch failed for %s", structure_id, exc_info=True)
        return None
    return orders


def fetch_structure_market(context_id: int, structure_id: int) -> dict[int, dict]:
    """{type_id: {buy_price, sell_price, buy_volume, sell_volume}} for one structure. The book is
    identical whoever reads it, so it's Redis-cached by structure_id (shared across accounts that
    follow the same structure) for CACHE_TTL. Unreadable -> {} (caller falls through to Jita)."""
    key = f"mkt:struct:{structure_id}"
    cached = cache_get_json(key)
    if cached is not None:
        return {int(k): v for k, v in cached.items()}
    orders = _fetch_structure_orders(context_id, structure_id)
    if orders is None:
        return {}
    by_type: dict[int, list] = {}
    for o in orders:
        by_type.setdefault(int(o["type_id"]), []).append(o)
    result = {tid: _agg_orders(os) for tid, os in by_type.items()}
    cache_set_json(key, result, ttl=CACHE_TTL)
    return result


# ── Region market (public, per type) ─────────────────────────────────────────────────

def fetch_region_market(region_id: int, type_ids: list[int]) -> dict[int, dict]:
    """{type_id: aggregate} for a public NPC region market. Aggregates the whole region's order
    book per type (fine for a hub-dominated region). Redis-cached per (region, type)."""
    if not type_ids:
        return {}
    keys = {tid: f"mkt:region:{region_id}:{tid}" for tid in type_ids}
    hits = cache_mget_json(list(keys.values()))
    result: dict[int, dict] = {}
    missing = []
    for tid in type_ids:
        v = hits.get(keys[tid])
        if v is not None:
            result[tid] = v
        else:
            missing.append(tid)
    if missing:
        to_cache = {}
        with httpx.Client(timeout=20) as client:
            for tid in missing:
                try:
                    r = client.get(
                        f"{ESI_BASE}/markets/{region_id}/orders/?datasource=tranquility"
                        f"&order_type=all&type_id={tid}"
                    )
                    orders = r.json() if r.status_code == 200 else []
                except Exception:
                    orders = []
                agg = _agg_orders(orders or [])
                result[tid] = agg
                to_cache[keys[tid]] = agg
        cache_mset_json(to_cache, ttl=CACHE_TTL)
    return result


# ── Priority resolution (the overlay reactions pricing uses) ──────────────────────────

def resolve_market_data(context_id: int, type_ids: list[int]) -> dict[int, dict]:
    """Like app.market.fetch_market_data, but walks the account's followed markets in priority
    order and takes the first that quotes each type, falling back to Jita. Each returned entry
    carries an extra `source` label (market name / 'Jita') for UI transparency."""
    type_ids = list(dict.fromkeys(int(t) for t in type_ids))
    if not type_ids:
        return {}
    remaining = set(type_ids)
    picked: dict[int, tuple[dict, str]] = {}
    for mk in effective_markets(context_id):
        if not remaining:
            break
        if mk["kind"] == "structure":
            book = fetch_structure_market(context_id, mk["location_id"])
        else:
            book = fetch_region_market(mk["location_id"], list(remaining))
        for tid in list(remaining):
            m = book.get(tid)
            if m and (m.get("sell_price") or m.get("buy_price")):
                picked[tid] = (m, mk["name"])
                remaining.discard(tid)
    if remaining:
        for tid, m in fetch_market_data(list(remaining)).items():
            picked[tid] = (m, "Jita")
    return {tid: {**m, "source": src} for tid, (m, src) in picked.items()}


def best_local_buy(context_id: int, type_ids: list[int]) -> dict[int, dict]:
    """Highest BUY price for each type across ALL the account's followed local markets (structure +
    region), with the winning market's name and its total buy depth: {type_id: {buy_price,
    buy_volume, market}}. Deliberately scans every followed market and keeps the max (not the
    priority-first pick resolve_market_data uses) — for SELLING output you want the best bid you can
    hit, wherever it is. Excludes Jita entirely: Jita is the comparison baseline (haul-and-sell), not
    a local sell target. {} when the account follows no markets or none quote a buy for these types."""
    type_ids = list(dict.fromkeys(int(t) for t in type_ids))
    if not type_ids:
        return {}
    best: dict[int, dict] = {}
    for mk in effective_markets(context_id):
        if mk["kind"] == "structure":
            book = fetch_structure_market(context_id, mk["location_id"])
        else:
            book = fetch_region_market(mk["location_id"], type_ids)
        for tid in type_ids:
            m = book.get(tid)
            if not m or not m.get("buy_price"):
                continue
            if tid not in best or m["buy_price"] > best[tid]["buy_price"]:
                best[tid] = {"buy_price": m["buy_price"], "buy_volume": m.get("buy_volume", 0.0),
                             "market": mk["name"]}
    return best


# ── Search ────────────────────────────────────────────────────────────────────────────

def _search_structures(context_id: int, q: str) -> list[dict]:
    ch = _market_character(context_id)
    if not ch:
        return []
    token = _get_valid_token(ch["character_id"])
    if not token:
        return []
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=15) as client:
            r = client.get(
                f"{ESI_BASE}/characters/{ch['character_id']}/search/",
                params={"datasource": "tranquility", "categories": "structure", "search": q},
                headers=headers,
            )
            if r.status_code != 200:
                return []
            ids = (r.json() or {}).get("structure", [])[:20]
            out = []
            for sid in ids:
                try:
                    d = client.get(
                        f"{ESI_BASE}/universe/structures/{sid}/?datasource=tranquility",
                        headers=headers,
                    )
                    if d.status_code == 200:
                        out.append({"kind": "structure", "location_id": int(sid),
                                    "name": (d.json() or {}).get("name", f"Structure {sid}")})
                except Exception:
                    continue
            return out
    except Exception:
        log.warning("structure search failed", exc_info=True)
        return []


def _search_regions(q: str) -> list[dict]:
    """Match distinct region names from the SDE, then resolve ids via the public /universe/ids/."""
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT DISTINCT region FROM constellations WHERE LOWER(region) LIKE ? ORDER BY region LIMIT 10",
            (f"%{q.lower()}%",),
        ).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    names = [r["region"] for r in rows]
    if not names:
        return []
    try:
        with httpx.Client(timeout=15) as client:
            resp = client.post(f"{ESI_BASE}/universe/ids/?datasource=tranquility", json=names)
            regions = (resp.json() or {}).get("regions", []) if resp.status_code == 200 else []
    except Exception:
        regions = []
    return [{"kind": "region", "location_id": int(x["id"]), "name": x["name"]} for x in regions]


# ── Endpoints ──────────────────────────────────────────────────────────────────────────

class MarketAdd(BaseModel):
    kind: str
    location_id: int
    name: str
    scope: str = "account"   # 'account' | 'group'


class MarketReorder(BaseModel):
    order: list[int]
    scope: str = "account"


class MarketReader(BaseModel):
    character_id: int


def _owner_for_scope(context_id: int, scope: str) -> tuple[str, int]:
    """Resolve the (owner_kind, owner_id) a write targets, enforcing group-manager permission."""
    if scope == "group":
        group = member_group(context_id)
        if not group or not is_group_manager(context_id, group["id"]):
            raise HTTPException(status_code=403, detail="Not a manager of your group")
        return "group", group["id"]
    return "account", context_id


def _markets_payload(context_id: int) -> dict:
    """The account's markets + the effective (resolved) chain + group-editing capability + the
    context's characters, the designated market reader, and the onboarding-complete flag."""
    own = _list_markets("account", context_id)
    group = member_group(context_id)
    can_manage = bool(group and is_group_manager(context_id, group["id"]))
    group_markets = _list_markets("group", group["id"]) if group else []
    if own:
        level = "account"
    elif group_markets:
        level = "group"
    else:
        level = "none"
    characters = _context_characters(context_id)
    reader = _market_character(context_id)
    cfg = _market_config(context_id)
    return {
        "markets": own,
        "group_markets": group_markets,
        "effective": effective_markets(context_id),
        "effective_level": level,
        "connected": reader is not None,
        "characters": characters,
        "market_character_id": reader["character_id"] if reader else None,
        "onboarded": bool(cfg.get("onboarded")),
        "group": {"id": group["id"], "name": group["name"]} if group else None,
        "can_manage_group": can_manage,
    }


@router.get("/api/markets")
def list_markets(context_id: int = Depends(require_context)):
    return _markets_payload(context_id)


@router.get("/api/markets/search")
def search_markets(q: str = "", context_id: int = Depends(require_context)):
    q = (q or "").strip()
    if len(q) < 3:
        return {"structures": [], "regions": [], "connected": _market_character(context_id) is not None}
    return {
        "structures": _search_structures(context_id, q),
        "regions": _search_regions(q),
        "connected": _market_character(context_id) is not None,
    }


@router.post("/api/markets")
def add_market(req: MarketAdd, context_id: int = Depends(require_context)):
    if req.kind not in _ALLOWED_KINDS:
        raise HTTPException(status_code=400, detail="Bad market kind")
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        nxt = (row["m"] if row else -1) + 1
        # ESI-detect hull + security for a structure so the build-bonus is ready to configure.
        hull = sec = None
        if req.kind == "structure":
            hull, sec = _detect_structure_meta(context_id, req.location_id)
        con.execute(
            "INSERT INTO pp_markets (owner_kind, owner_id, kind, location_id, name, priority, active, "
            "hull, security) VALUES (?,?,?,?,?,?,1,?,?)",
            (owner_kind, owner_id, req.kind, req.location_id, req.name.strip()[:120], nxt, hull, sec),
        )
        con.commit()
    finally:
        con.close()
    return _markets_payload(context_id)


class MarketBuildConfig(BaseModel):
    build_mfg: bool = False
    build_rx: bool = False
    me_rig: int = 0
    te_rig: int = 0
    rx_me_rig: int = 0
    rx_te_rig: int = 0
    hull: str | None = None       # optional manual override if ESI couldn't detect it
    security: str | None = None
    scope: str = "account"


@router.post("/api/markets/{market_id}/build")
def set_market_build(market_id: int, req: MarketBuildConfig, context_id: int = Depends(require_context)):
    """Configure a structure as a BUILD facility: whether you manufacture / react there and the
    fitted rig tiers (0=none, 1=T1, 2=T2). Hull + security stay as ESI-detected unless overridden."""
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    clamp = lambda v: max(0, min(2, int(v)))
    con = get_connection()
    try:
        sets = ["build_mfg=?", "build_rx=?", "me_rig=?", "te_rig=?", "rx_me_rig=?", "rx_te_rig=?"]
        vals = [1 if req.build_mfg else 0, 1 if req.build_rx else 0,
                clamp(req.me_rig), clamp(req.te_rig), clamp(req.rx_me_rig), clamp(req.rx_te_rig)]
        if req.hull is not None:
            sets.append("hull=?"); vals.append(req.hull or None)
        if req.security is not None:
            sets.append("security=?"); vals.append(req.security or None)
        vals += [market_id, owner_kind, owner_id]
        con.execute(f"UPDATE pp_markets SET {', '.join(sets)} WHERE id=? AND owner_kind=? AND owner_id=?", vals)
        con.commit()
    finally:
        con.close()
    return _markets_payload(context_id)


@router.delete("/api/markets/{market_id}")
def delete_market(market_id: int, scope: str = "account", context_id: int = Depends(require_context)):
    owner_kind, owner_id = _owner_for_scope(context_id, scope)
    ensure_markets_table()
    con = get_connection()
    try:
        con.execute(
            "DELETE FROM pp_markets WHERE id=? AND owner_kind=? AND owner_id=?",
            (market_id, owner_kind, owner_id),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.post("/api/markets/reorder")
def reorder_markets(req: MarketReorder, context_id: int = Depends(require_context)):
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    con = get_connection()
    try:
        for i, mid in enumerate(req.order):
            con.execute(
                "UPDATE pp_markets SET priority=? WHERE id=? AND owner_kind=? AND owner_id=?",
                (i, mid, owner_kind, owner_id),
            )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


def _upsert_market_config(context_id: int, **cols) -> None:
    """Insert-or-update the single pp_market_config row for this context (only the given columns)."""
    ensure_market_config_table()
    con = get_connection()
    try:
        exists = con.execute("SELECT 1 FROM pp_market_config WHERE context_id=?", (context_id,)).fetchone()
        if exists:
            sets = ", ".join(f"{k}=?" for k in cols)
            con.execute(f"UPDATE pp_market_config SET {sets} WHERE context_id=?",
                        (*cols.values(), context_id))
        else:
            keys = ", ".join(["context_id", *cols.keys()])
            marks = ", ".join(["?"] * (1 + len(cols)))
            con.execute(f"INSERT INTO pp_market_config ({keys}) VALUES ({marks})",
                        (context_id, *cols.values()))
        con.commit()
    finally:
        con.close()


@router.post("/api/markets/reader")
def set_market_reader(req: MarketReader, context_id: int = Depends(require_context)):
    """Designate which character reads the structure market. Must be a character in this context
    that holds the market scope."""
    valid = {c["character_id"] for c in _context_characters(context_id) if c["is_market"]}
    if req.character_id not in valid:
        raise HTTPException(status_code=400, detail="That character can't read markets (no market scope)")
    _upsert_market_config(context_id, market_character_id=req.character_id)
    return _markets_payload(context_id)


@router.post("/api/markets/complete")
def complete_onboarding(context_id: int = Depends(require_context)):
    """Mark the one-time Reactions onboarding done. Requires at least one character in the context
    (reaction slots come from characters), so the tab isn't unblocked with nothing to run on."""
    if not _context_characters(context_id):
        raise HTTPException(status_code=400, detail="Add at least one character first")
    _upsert_market_config(context_id, onboarded=1)
    return _markets_payload(context_id)
