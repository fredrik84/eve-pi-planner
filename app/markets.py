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

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, add_columns
from app.cache import cache_get_json, cache_set_json, cache_mget_json, cache_mset_json
from app import esi_http
from app.esi import (
    MARKET_SCOPE, _get_valid_token, require_context,
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
    add_columns(con, "pp_markets",
                "build_mfg INTEGER NOT NULL DEFAULT 0", "build_rx INTEGER NOT NULL DEFAULT 0",
                "hull TEXT", "security TEXT",
                "me_rig INTEGER NOT NULL DEFAULT 0", "te_rig INTEGER NOT NULL DEFAULT 0",
                "rx_me_rig INTEGER NOT NULL DEFAULT 0", "rx_te_rig INTEGER NOT NULL DEFAULT 0",
                # price_from: is this a PRICING source (in the priority chain)? 1 by default so
                # existing market rows keep pricing; a build-only structure sets it 0.
                "price_from INTEGER NOT NULL DEFAULT 1",
                # Which Standup M-Set / L-Set rig FAMILIES each fitted rig covers (JSON array of
                # app.industry.structures.RIG_FAMILIES keys). NULL/empty means "all groups" — the
                # behaviour every existing row already has, so nobody's quoted efficiency drops on
                # deploy; narrowing is an explicit choice. Additive ALTER, never DROP.
                "me_rig_groups TEXT", "te_rig_groups TEXT",
                "rx_me_rig_groups TEXT", "rx_te_rig_groups TEXT",
                # The structure's solar system + its own facility tax, so a job routed here is
                # charged THIS system's cost index and THIS structure's tax rather than the
                # account's single build system. NULL system → fall back to the account default.
                "system_id BIGINT", "facility_tax_pct REAL")
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


def _parse_families(value) -> list[str]:
    """A rig's families column holds a JSON array of RIG_FAMILIES keys; NULL/''/garbage = [] =
    'covers everything', which is what every pre-existing row means."""
    import json as _json
    from app.industry.structures import RIG_FAMILIES
    try:
        return [k for k in _json.loads(value or "[]") if k in RIG_FAMILIES]
    except Exception:
        return []


def _list_markets(owner_kind: str, owner_id: int) -> list[dict]:
    ensure_markets_table()
    from app.industry.structures import manufacturing_bonus, reaction_bonus, rig_fit_warnings
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, kind, location_id, name, priority, active, COALESCE(price_from,1) AS price_from, "
            "COALESCE(build_mfg,0) AS build_mfg, COALESCE(build_rx,0) AS build_rx, hull, security, "
            "COALESCE(me_rig,0) AS me_rig, COALESCE(te_rig,0) AS te_rig, "
            "COALESCE(rx_me_rig,0) AS rx_me_rig, COALESCE(rx_te_rig,0) AS rx_te_rig, "
            "me_rig_groups, te_rig_groups, rx_me_rig_groups, rx_te_rig_groups, "
            "system_id, facility_tax_pct FROM pp_markets "
            "WHERE owner_kind=? AND owner_id=? AND active=1 ORDER BY priority, id",
            (owner_kind, owner_id),
        ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        d = dict(r)
        if d["kind"] == "structure":
            for col in ("me_rig_groups", "te_rig_groups", "rx_me_rig_groups", "rx_te_rig_groups"):
                d[col] = _parse_families(d.get(col))
            # The bonus reported here is the structure's BEST case — what it gives a job its rigs
            # actually cover. What any one job gets is resolved per job against the produced type's
            # group (app.industry.structures.route_job); this is the headline for the UI.
            mme, mte = manufacturing_bonus(d["hull"], d["me_rig"], d["te_rig"], d["security"])
            rme, rte = reaction_bonus(d["hull"], d["rx_me_rig"], d["rx_te_rig"], d["security"])
            d["mfg_bonus"] = {"me": mme, "te": mte}
            d["rx_bonus"] = {"me": rme, "te": rte}
            # Rig families this hull cannot fit a rig for. Reported, never repaired — the saved
            # config stays the user's; see structures.rig_fit_warnings.
            d["rig_warnings"] = (
                rig_fit_warnings(d["hull"], "manufacturing", d["me_rig"], d["te_rig"],
                                 d["me_rig_groups"], d["te_rig_groups"], d["security"])
                + rig_fit_warnings(d["hull"], "reaction", d["rx_me_rig"], d["rx_te_rig"],
                                   d["rx_me_rig_groups"], d["rx_te_rig_groups"], d["security"]))
        out.append(d)
    return out


def build_structures(context_id: int) -> list[dict]:
    """The account's structures configured as BUILD facilities (manufacturing and/or reactions),
    for the Industry planner's per-job routing.

    **Deliberately the account's own list only.** A group's shared structures are SUGGESTIONS (see
    `_suggested_structures`), not sites this account silently starts installing jobs in: adopting
    one changes where jobs route and therefore what they cost, and that has to be somebody's
    decision rather than a side effect of an alliance-mate describing a building.
    """
    return [m for m in _list_markets("account", context_id)
            if m["kind"] == "structure" and (m["build_mfg"] or m["build_rx"])]


def _suggested_structures(context_id: int, own: list[dict]) -> list[dict]:
    """Structures the account's GROUP has shared that this account has not added itself.

    The problem this solves, measured in prod (2026-08-05): two accounts had independently
    configured the SAME four alliance structures — the hull, the rig tiers, the families, the
    system, all answered twice and maintained twice. An alliance builds in the same few buildings,
    so describing one should be worth doing once.

    A suggestion is inert until adopted: it appears in the member's structure list as something
    their alliance uses, and `POST /api/markets/adopt` copies it into their own list, where it is
    then theirs to tune. Nothing about their plans moves until they take it — the same reason
    `build_structures` stays personal.

    **Scoped to the ALLIANCE, never global**, and by construction rather than by a filter that could
    be forgotten: `member_group` resolves the group by joining a real character's `alliance_id` to
    `pp_groups.alliance_id`, so a suggestion can only come from the group this account is actually
    in. Another alliance running on the same install is a different group id and is invisible here —
    they don't dock in our structures and must never be told to build in them. An account whose
    alliance has no group gets nothing at all.
    """
    if not _group_structures_on(context_id):
        return []
    group = member_group(context_id)
    if not group:
        return []
    mine = {m.get("location_id") for m in own}
    return [{**m, "suggested": True, "group_name": group["name"]}
            for m in _list_markets("group", group["id"])
            if m["kind"] == "structure" and m.get("location_id") not in mine]


def _group_structures_on(context_id: int) -> bool:
    """Gated while it rolls out: it puts another account's answer in front of a member, and a wrong
    rig answer adopted from a suggestion is an efficiency the plan quotes and cannot see is wrong."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("industry_group_structures", context_id)
    except Exception:
        return False


def structure_info(structure_id: int, token: str, client=None) -> dict | None:
    """`/universe/structures/{id}` — `{name, solar_system_id, type_id}` — or None.

    **The one place this call is made.** Structure visibility is ACL-gated: a character who has no
    docking access gets a 403, which is a normal answer about a normal structure and never an error
    to report. Every caller wants the same degradation (no name rather than a failed operation), so
    they share this rather than each writing their own auth-and-403 dance.
    """
    if not token or not structure_id:
        return None
    try:
        own = client is None
        c = client or esi_http.client(timeout=12)
        try:
            s = esi_http.get(f"universe/structures/{structure_id}/?datasource=tranquility",
                             client=c, token=token)
            if s.status_code != 200:      # 403 = no access; 404 = gone. Both mean "no name".
                return None
            return s.json() or None
        finally:
            if own:
                c.close()
    except Exception:
        return None


def _detect_structure_meta(context_id: int, structure_id: int) -> tuple:
    """ESI-detect a structure's (hull, security_band, system_id) from /universe/structures + its
    system's security. Rigs are NOT exposed by ESI (no fitting endpoint), so those stay a manual
    pick. The system id matters as much as the hull now: a job routed to this structure is charged
    THIS system's cost index. Best-effort: (None, None, None) if it can't be read."""
    ch = _market_character(context_id)
    if not ch:
        return (None, None, None)
    token = _get_valid_token(ch["character_id"])
    if not token:
        return (None, None, None)
    try:
        with esi_http.client(timeout=12) as client:
            # Through the shared single call site, so the ACL-403 degradation is written once.
            sj = structure_info(structure_id, token, client=client)
            if not sj:
                return (None, None, None)
            hull = STRUCTURE_HULLS.get(sj.get("type_id"))
            sys_id = sj.get("solar_system_id")
            sec = None
            if sys_id:
                sy = esi_http.get(f"universe/systems/{sys_id}/?datasource=tranquility", client=client)
                if sy.status_code == 200:
                    v = (sy.json() or {}).get("security_status")
                    sec = "high" if (v or 0) >= 0.45 else "low" if (v or 0) > 0 else "null"
            return (hull, sec, sys_id)
    except Exception:
        return (None, None, None)


# ── Manual (hand-described) structures ─────────────────────────────────────────────────
# A tester who already knows which buildings they run should not have to grant structure-search
# scopes just to describe one. Everything AFTER the location id is manual already (hull, rigs,
# families, tax), so the only ESI-shaped part is where the id comes from — and a hand-described
# structure can simply be given one we mint ourselves.

def is_manual_location(location_id) -> bool:
    """Manual rows are the NEGATIVE-id ones. Every real EVE structure id is positive, so the sign
    alone tells the two apart forever, with no extra column to keep in sync."""
    try:
        return int(location_id) < 0
    except (TypeError, ValueError):
        return False


def _manual_structures_on(context_id: int) -> bool:
    """Gated while it rolls out: a hand-described structure is an unverified claim about a
    building, and its rigs are quoted into every job routed there."""
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("industry_manual_structures", context_id)
    except Exception:
        return False


def _next_manual_location_id(con) -> int:
    """One below the globally-minimum location id — the same allocation `add_dummy_characters`
    uses for placeholder characters, and global for the same reason: the id is compared and stored
    without its owner in enough places (the group-share dedup keys on location_id alone, and the
    per-structure market cache key is `mkt:struct:<id>`) that two accounts must never mint the same
    one. Ids are cheap; a collision between two different buildings is not."""
    row = con.execute("SELECT MIN(location_id) AS m FROM pp_markets").fetchone()
    return min(0, (row["m"] if row else 0) or 0) - 1


def _manual_system(name: str) -> dict | None:
    """Resolve a typed system name to `{system, system_id, security}` from `system_geo` — the same
    table `/api/systems/search` (the typeahead the user picked from) reads, so anything the
    typeahead offered resolves here. Case-insensitive exact match; unknown name → None."""
    con = get_connection()
    try:
        row = con.execute(
            "SELECT system, system_id, security FROM system_geo WHERE LOWER(system)=?",
            ((name or "").strip().lower(),),
        ).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    return dict(row) if row and row["system_id"] else None


def effective_markets(context_id: int) -> list[dict]:
    """Ordered market list actually used to price this account: personal list if any, else the
    account's alliance-group default list if any, else [] (Jita-only). Same personal->group->
    fallback shape as effective_reaction_settings()."""
    # A manual structure can never be a PRICING source: its location id is one we minted, so
    # `/markets/structures/{id}/` resolves to nothing and the book comes back empty — silently, and
    # an empty book reads as "this market quotes nothing" rather than as an error. The writes force
    # `price_from=0` on those rows; this is the second lock, so a row that got one some other way
    # (an old share, a hand-edited DB) still can't poison the chain.
    pricing = lambda ms: [m for m in ms
                          if m.get("price_from", 1) and not is_manual_location(m.get("location_id"))]
    own = pricing(_list_markets("account", context_id))
    if own:
        return own
    group = member_group(context_id)
    if group:
        grp = pricing(_list_markets("group", group["id"]))
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
    base = f"markets/structures/{structure_id}/?datasource=tranquility"
    orders: list[dict] = []
    try:
        with esi_http.client(timeout=20) as client:
            r = esi_http.get(f"{base}&page=1", client=client, token=token)
            if r.status_code in (401, 403, 404):
                return None
            r.raise_for_status()
            orders.extend(r.json() or [])
            pages = int(r.headers.get("X-Pages", "1") or 1)
            for p in range(2, pages + 1):
                rp = esi_http.get(f"{base}&page={p}", client=client, token=token)
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
        with esi_http.client(timeout=20) as client:
            for tid in missing:
                try:
                    r = esi_http.get(
                        f"markets/{region_id}/orders/?datasource=tranquility"
                        f"&order_type=all&type_id={tid}", client=client,
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
    carries an extra `source` label (market name / 'Jita') for UI transparency.

    The two SIDES are resolved independently: a market only claims the SELL price (what it costs
    you to acquire the item) if it actually has sell orders, and only claims the BUY price (what
    you'd get instant-selling into it) if it actually has bids. Resolving them together was a real
    bug — a structure holding only a buy order for Pyerite claimed the type outright and handed
    back sell_price=0.0, which is falsy, so the material read as "no price" and poisoned every
    build that used it. `source` labels where the SELL price came from, since that's the number
    the shopping list spends."""
    type_ids = list(dict.fromkeys(int(t) for t in type_ids))
    if not type_ids:
        return {}
    sell_pick: dict[int, tuple[dict, str]] = {}
    buy_pick: dict[int, tuple[dict, str]] = {}
    for mk in effective_markets(context_id):
        # Stop early only when BOTH sides are settled for every type.
        if len(sell_pick) == len(type_ids) and len(buy_pick) == len(type_ids):
            break
        want = [t for t in type_ids if t not in sell_pick or t not in buy_pick]
        if mk["kind"] == "structure":
            book = fetch_structure_market(context_id, mk["location_id"])
        else:
            book = fetch_region_market(mk["location_id"], want)
        for tid in want:
            m = book.get(tid)
            if not m:
                continue
            if tid not in sell_pick and (m.get("sell_price") or 0) > 0:
                sell_pick[tid] = (m, mk["name"])
            if tid not in buy_pick and (m.get("buy_price") or 0) > 0:
                buy_pick[tid] = (m, mk["name"])
    # Jita backfills any type no followed market could actually sell us.
    missing = [t for t in type_ids if t not in sell_pick]
    jita = fetch_market_data(missing) if missing else {}

    out: dict[int, dict] = {}
    for tid in type_ids:
        base, src = sell_pick.get(tid, (None, None))
        if base is None:
            j = jita.get(tid)
            if j is None:
                # Nothing anywhere can sell it; still surface a local bid if one exists.
                if tid in buy_pick:
                    bm, bsrc = buy_pick[tid]
                    out[tid] = {**bm, "sell_price": 0.0, "source": bsrc, "buy_source": bsrc}
                continue
            base, src = j, "Jita"
        entry = {**base, "source": src}
        if tid in buy_pick:
            bm, bsrc = buy_pick[tid]
            entry["buy_price"] = bm.get("buy_price")
            entry["buy_volume"] = bm.get("buy_volume", entry.get("buy_volume"))
            entry["buy_source"] = bsrc
        else:
            entry["buy_source"] = src
        out[tid] = entry
    return out


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
    try:
        with esi_http.client(timeout=15) as client:
            r = esi_http.get(
                f"characters/{ch['character_id']}/search/", client=client, token=token,
                params={"datasource": "tranquility", "categories": "structure", "search": q},
            )
            if r.status_code != 200:
                return []
            ids = (r.json() or {}).get("structure", [])[:20]
            out = []
            for sid in ids:
                try:
                    d = esi_http.get(f"universe/structures/{sid}/?datasource=tranquility",
                                     client=client, token=token)
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
        with esi_http.client(timeout=15) as client:
            resp = esi_http.post("universe/ids/?datasource=tranquility", client=client, json=names)
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
    price_from: bool = True  # False → build-only structure (not in the pricing chain)
    build_mfg: bool = False
    build_rx: bool = False
    me_rig: int = 0
    te_rig: int = 0
    rx_me_rig: int = 0
    rx_te_rig: int = 0


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
        # Structures the alliance uses that this account hasn't added — inert until adopted.
        "suggested_structures": _suggested_structures(context_id, own),
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


# Everything that describes the BUILDING, including the rig families and its own system and tax —
# a structure shared with the alliance that members then have to re-answer the rigs for has shared
# nothing at all. `price_from` rides along as set: sharing a station shares it as it stands.
_SHAREABLE_COLS = ("kind", "location_id", "name", "hull", "security", "price_from", "build_mfg",
                   "build_rx", "me_rig", "te_rig", "rx_me_rig", "rx_te_rig",
                   "me_rig_groups", "te_rig_groups", "rx_me_rig_groups", "rx_te_rig_groups",
                   "system_id", "facility_tax_pct")


class MarketShare(BaseModel):
    market_id: int


@router.post("/api/markets/share")
def share_market(req: MarketShare, context_id: int = Depends(require_context)):
    """Copy one of this account's structures to the GROUP, so every member has it without adding it
    by hand. The answer to "I set this up for one user and everybody else had to repeat it".

    A COPY, not a move: the account keeps its own row, so nothing it had stops working and the
    member who shared it can still tune their own rig answer afterwards. Re-sharing the same
    location updates the group's row rather than adding a second one — sharing twice is the same
    statement made twice, not two buildings.
    """
    group = member_group(context_id)
    if not group or not is_group_manager(context_id, group["id"]):
        raise HTTPException(status_code=403, detail="Not a manager of your group")
    con = get_connection()
    try:
        row = con.execute(
            "SELECT * FROM pp_markets WHERE id=? AND owner_kind='account' AND owner_id=?",
            (req.market_id, context_id)).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No such market")
        if row["kind"] != "structure":
            raise HTTPException(status_code=400, detail="Only structures can be shared")
        d = dict(row)
        existing = con.execute(
            "SELECT id FROM pp_markets WHERE owner_kind='group' AND owner_id=? AND location_id=?",
            (group["id"], d["location_id"])).fetchone()
        cols = _SHAREABLE_COLS
        vals = [d.get(c) for c in cols]
        if existing:
            con.execute(f"UPDATE pp_markets SET {', '.join(c + '=?' for c in cols)} WHERE id=?",
                        (*vals, existing["id"]))
        else:
            nxt = con.execute("SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets "
                              "WHERE owner_kind='group' AND owner_id=?",
                              (group["id"],)).fetchone()["m"] + 1
            con.execute(
                f"INSERT INTO pp_markets (owner_kind, owner_id, priority, active, "
                f"{', '.join(cols)}) VALUES (?,?,?,1,{','.join('?' * len(cols))})",
                ("group", group["id"], nxt, *vals))
        con.commit()
    finally:
        con.close()
    return _markets_payload(context_id)


@router.post("/api/markets/adopt")
def adopt_market(req: MarketShare, context_id: int = Depends(require_context)):
    """Take one of the group's suggested structures into this account's own list.

    A COPY, so the member owns it from then on: they can change the rigs, turn off building in it,
    or remove it, and none of that touches what the group shared. Adopting a structure the account
    already has at that location is a no-op rather than a duplicate — the same building twice is
    two sets of rig answers that can disagree.
    """
    group = member_group(context_id)
    if not group:
        raise HTTPException(status_code=404, detail="You are not in a group")
    con = get_connection()
    try:
        row = con.execute(
            "SELECT * FROM pp_markets WHERE id=? AND owner_kind='group' AND owner_id=?",
            (req.market_id, group["id"])).fetchone()
        if not row:
            raise HTTPException(status_code=404, detail="No such shared structure")
        d = dict(row)
        dup = con.execute(
            "SELECT id FROM pp_markets WHERE owner_kind='account' AND owner_id=? AND location_id=?",
            (context_id, d["location_id"])).fetchone()
        if not dup:
            nxt = con.execute("SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets "
                              "WHERE owner_kind='account' AND owner_id=?",
                              (context_id,)).fetchone()["m"] + 1
            vals = [d.get(c) for c in _SHAREABLE_COLS]
            con.execute(
                f"INSERT INTO pp_markets (owner_kind, owner_id, priority, active, "
                f"{', '.join(_SHAREABLE_COLS)}) VALUES (?,?,?,1,{','.join('?' * len(_SHAREABLE_COLS))})",
                ("account", context_id, nxt, *vals))
            con.commit()
    finally:
        con.close()
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
    # Real EVE ids are positive. Negative ones are OURS (see `_next_manual_location_id`) and are
    # minted only by `add_manual_structure`, which forces `price_from` off — accepting one here
    # would be a way around that, and around the id allocation.
    if req.location_id <= 0:
        raise HTTPException(status_code=400, detail="Bad location id")
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()
        nxt = (row["m"] if row else -1) + 1
        # ESI-detect hull + security + SYSTEM for a structure so the build-bonus is ready to
        # configure and a job routed here can be charged its own system's cost index.
        hull = sec = sys_id = None
        if req.kind == "structure":
            hull, sec, sys_id = _detect_structure_meta(context_id, req.location_id)
        clamp = lambda v: max(0, min(2, int(v)))
        con.execute(
            "INSERT INTO pp_markets (owner_kind, owner_id, kind, location_id, name, priority, active, "
            "hull, security, price_from, build_mfg, build_rx, me_rig, te_rig, rx_me_rig, rx_te_rig, "
            "system_id) "
            "VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,?,?)",
            (owner_kind, owner_id, req.kind, req.location_id, req.name.strip()[:120], nxt, hull, sec,
             1 if req.price_from else 0, 1 if req.build_mfg else 0, 1 if req.build_rx else 0,
             clamp(req.me_rig), clamp(req.te_rig), clamp(req.rx_me_rig), clamp(req.rx_te_rig),
             sys_id),
        )
        con.commit()
    finally:
        con.close()
    return _markets_payload(context_id)


class ManualStructureAdd(BaseModel):
    name: str
    hull: str
    system: str
    scope: str = "account"


@router.post("/api/markets/manual")
def add_manual_structure(req: ManualStructureAdd, context_id: int = Depends(require_context)):
    """Add a BUILD structure the user describes by hand — a name, a hull and a system — with no ESI
    character, no structure search and no scopes.

    Everything that makes a structure worth describing is already a manual answer (rig tiers, rig
    families, facility tax), and this closes the last ESI-shaped gap: the location id. It is minted
    NEGATIVE (`_next_manual_location_id`) so it can never collide with a real EVE structure id.

    Security is DERIVED from the picked system, never asked: `system_geo` carries the system's
    security status and `_sec_band` bands it, so asking would be asking for something we already
    know (CLAUDE.md rule 3) and would let the two disagree.

    **`price_from` is forced off, and there is no parameter to turn it on.** Reading a structure's
    market is `GET /markets/structures/{id}/` against a REAL id with a real token; a minted id
    resolves to nothing and returns an empty book, which reads as "quotes nothing" rather than as a
    failure. Building here is honest; pricing from here would be silently wrong.

    Rigs, families and tax are then set through the normal `POST /api/markets/{id}/build` — this
    creates the row, it does not duplicate the configuration form.
    """
    from app.industry.structures import MFG_HULLS, RX_HULLS, _sec_band
    if not _manual_structures_on(context_id):
        raise HTTPException(status_code=403, detail="Manual structures are not enabled for you yet")
    name = (req.name or "").strip()[:120]
    if not name:
        raise HTTPException(status_code=400, detail="Give the structure a name")
    hull = (req.hull or "").strip().lower()
    if hull not in MFG_HULLS + RX_HULLS:
        raise HTTPException(status_code=400, detail="Pick a structure hull")
    sysrow = _manual_system(req.system)
    if not sysrow:
        raise HTTPException(status_code=400, detail=f'Unrecognized solar system "{req.system}"')
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    con = get_connection()
    try:
        nxt = con.execute(
            "SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets WHERE owner_kind=? AND owner_id=?",
            (owner_kind, owner_id),
        ).fetchone()["m"] + 1
        loc = _next_manual_location_id(con)
        con.execute(
            "INSERT INTO pp_markets (owner_kind, owner_id, kind, location_id, name, priority, active, "
            "hull, security, price_from, build_mfg, build_rx, system_id) "
            "VALUES (?,?,?,?,?,?,1,?,?,0,?,?,?)",
            (owner_kind, owner_id, "structure", loc, name, nxt, hull,
             _sec_band(sysrow["security"]),
             1 if hull in MFG_HULLS else 0, 1 if hull in RX_HULLS else 0,
             sysrow["system_id"]),
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
    # What each rig is FOR — a list of app.industry.structures.RIG_FAMILIES keys. Omitted (None)
    # leaves the stored value alone; an empty list means "covers everything", which is what a
    # structure configured before this existed already means.
    me_rig_groups: list[str] | None = None
    te_rig_groups: list[str] | None = None
    rx_me_rig_groups: list[str] | None = None
    rx_te_rig_groups: list[str] | None = None
    facility_tax_pct: float | None = None   # this structure's own tax; None → account default
    hull: str | None = None       # optional manual override if ESI couldn't detect it
    security: str | None = None
    price_from: bool | None = None   # also toggle whether this structure is priced from
    scope: str = "account"


@router.get("/api/markets/rig-families")
def list_rig_families(context_id: int = Depends(require_context)):
    """The pickable rig families (Standup M-Set / L-Set / XL-Set), so the UI never hardcodes the
    list — including which of them each HULL can actually fit a rig for. Rig size is a fitting
    constraint (a Raitaru takes M-Set, and no M-Set capital-ship rig exists), so a picker that
    offers every family to every hull lets a user claim a bonus the game will never give them and
    the planner then quotes costs and durations that are wrong in the optimistic direction.
    Served here rather than derived in JS for the same reason as the list itself: one registry."""
    from app.industry.structures import HULL_RIG_SIZE, family_registry, fittable_families
    return {"families": family_registry(),
            "by_hull": {h: sorted(fittable_families(h)) for h in HULL_RIG_SIZE},
            "hull_rig_size": dict(HULL_RIG_SIZE)}


@router.get("/api/markets/hulls")
def list_structure_hulls(context_id: int = Depends(require_context)):
    """The pickable structure hulls, so the UI never hardcodes them — same rule (and the same
    reason) as `/api/markets/rig-families`: the hull keys drive the role bonus, and a list copied
    into JS drifts from `app.industry.structures` the first time a hull is added."""
    from app.industry.structures import MFG_HULLS, RX_HULLS, hull_rig_size, RIG_SIZE_LABEL

    def _row(h: str, activity: str) -> dict:
        # The DISPLAY name travels with the key. Without it the picker rendered the raw key, so the
        # dropdown read "raitaru" / "athanor (reactions)" — the keys are lowercase because they are
        # matched against ESI's hull names, which is no reason to show them that way.
        # `rig_size` rides along because it is the one fact that decides what the structure can
        # claim (see HULL_RIG_SIZE): naming it at the moment you pick a hull is cheaper than
        # explaining afterwards why a rig family is missing from the next control.
        size = hull_rig_size(h)
        return {"key": h, "activity": activity, "label": h.title(),
                "rig_size": size, "rig_size_label": RIG_SIZE_LABEL.get(size or "", "")}

    return {"hulls": [_row(h, "manufacturing") for h in MFG_HULLS]
                     + [_row(h, "reaction") for h in RX_HULLS]}


@router.post("/api/markets/{market_id}/build")
def set_market_build(market_id: int, req: MarketBuildConfig, context_id: int = Depends(require_context)):
    """Configure a structure as a BUILD facility: whether you manufacture / react there, the fitted
    rig tiers (0=none, 1=T1, 2=T2) and which rig FAMILIES those rigs are. Hull + security stay as
    ESI-detected unless overridden; the system is (re-)detected here since a job routed to this
    structure is charged that system's cost index."""
    import json as _json
    from app.industry.structures import RIG_FAMILIES
    owner_kind, owner_id = _owner_for_scope(context_id, req.scope)
    ensure_markets_table()
    clamp = lambda v: max(0, min(2, int(v)))
    fams = lambda ks: _json.dumps([k for k in (ks or []) if k in RIG_FAMILIES])
    con = get_connection()
    try:
        sets = ["build_mfg=?", "build_rx=?", "me_rig=?", "te_rig=?", "rx_me_rig=?", "rx_te_rig=?"]
        vals = [1 if req.build_mfg else 0, 1 if req.build_rx else 0,
                clamp(req.me_rig), clamp(req.te_rig), clamp(req.rx_me_rig), clamp(req.rx_te_rig)]
        for col, val in (("me_rig_groups", req.me_rig_groups), ("te_rig_groups", req.te_rig_groups),
                         ("rx_me_rig_groups", req.rx_me_rig_groups),
                         ("rx_te_rig_groups", req.rx_te_rig_groups)):
            if val is not None:
                sets.append(f"{col}=?"); vals.append(fams(val))
        if req.facility_tax_pct is not None:
            sets.append("facility_tax_pct=?"); vals.append(max(0.0, float(req.facility_tax_pct)))
        if req.hull is not None:
            sets.append("hull=?"); vals.append(req.hull or None)
        if req.security is not None:
            sets.append("security=?"); vals.append(req.security or None)
        row = con.execute("SELECT location_id, kind, system_id FROM pp_markets "
                          "WHERE id=? AND owner_kind=? AND owner_id=?",
                          (market_id, owner_kind, owner_id)).fetchone()
        manual = bool(row) and is_manual_location(row["location_id"])
        if req.price_from is not None:
            # A hand-described structure has no readable market at all (see add_manual_structure),
            # so "price from here" is refused rather than accepted and quietly answered with an
            # empty book. Turning it OFF is always allowed.
            sets.append("price_from=?"); vals.append(1 if (req.price_from and not manual) else 0)
        # Backfill the system for a row added before we stored it — best-effort, and only when the
        # user is already editing this structure, never inside a plan. Skipped for a manual row:
        # there is nothing at that id to detect, and its system was answered when it was created.
        if row and row["kind"] == "structure" and not manual and not row["system_id"]:
            _hull, _sec, sys_id = _detect_structure_meta(context_id, row["location_id"])
            if sys_id:
                sets.append("system_id=?"); vals.append(sys_id)
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
