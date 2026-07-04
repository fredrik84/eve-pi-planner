"""
Character data API: the ESI-authenticated character list, corp-wallet donation
summary, dummy (synthetic) characters, and manual planet refresh. OAuth login /
callback / logout and session/admin helpers live in app.esi — this module
imports what it needs from there rather than duplicating it.
"""
import json as _json
import logging
import time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.cache import cache_get_json, cache_set_json, cache_invalidate, charlist_key
from app.esi import (
    ESI_BASE, WALLET_SCOPE,
    _session_lookup, _is_configured, admin_and_tester_status_for_context,
    require_context, _get_valid_token, _fetch_skills, _fetch_planets,
    ensure_char_tables,
)

router = APIRouter()
log = logging.getLogger(__name__)

# /api/characters is invalidated (not TTL-expired-only) by every write path that changes what it
# returns: rescan, character add/remove, dummy edits. TTL is only a safety net for a missed spot.
_CHARLIST_TTL = 300

# ── Corp wallet (admin: see donations without logging the toon into the game) ──────

def _resolve_names(ids: list[int], client: "httpx.Client") -> dict[int, str]:
    """Resolve character/corp ids to names via /universe/names/ (best-effort)."""
    out: dict[int, str] = {}
    ids = [int(i) for i in ids if i]
    for i in range(0, len(ids), 1000):
        chunk = ids[i:i + 1000]
        try:
            r = client.post(f"{ESI_BASE}/universe/names/?datasource=tranquility", json=chunk, timeout=15)
            if r.status_code == 200:
                for x in r.json():
                    out[x["id"]] = x["name"]
        except Exception:
            pass
    return out


def _wallet_character(context_id: int):
    """First character in this context that authorised the corp-wallet scope, or None."""
    con = get_connection()
    rows = con.execute(
        "SELECT character_id, character_name, scopes FROM pp_characters WHERE context_id=?",
        (context_id,),
    ).fetchall()
    con.close()
    for r in rows:
        if WALLET_SCOPE in (r["scopes"] or "").split():
            return r
    return None


def corp_wallet_summary(context_id: int) -> dict:
    """Corp wallet balance + recent player donations, read via the context's wallet character.

    Shapes (all carry `connected`):
      {connected: False}                              → no wallet character linked yet
      {connected: True, error: 'token'|'role'|'fetch'}→ linked but unavailable (re-auth / no role)
      {connected: True, balance, total_balance, donations:[...], total_donated, corp_name, ...}
    """
    ch = _wallet_character(context_id)
    if not ch:
        return {"connected": False}
    cid, name = ch["character_id"], ch["character_name"]
    token = _get_valid_token(cid)
    if not token:
        return {"connected": True, "character_name": name, "error": "token"}
    headers = {"Authorization": f"Bearer {token}"}
    try:
        with httpx.Client(timeout=15) as client:
            pub = client.get(f"{ESI_BASE}/characters/{cid}/?datasource=tranquility").json()
            corp_id = pub.get("corporation_id")
            corp = client.get(f"{ESI_BASE}/corporations/{corp_id}/?datasource=tranquility").json()
            corp_name = corp.get("name")

            wresp = client.get(f"{ESI_BASE}/corporations/{corp_id}/wallets/?datasource=tranquility", headers=headers)
            if wresp.status_code in (401, 403):
                # Authorised, but the character lacks the in-game role to read the corp wallet.
                return {"connected": True, "character_name": name, "corp_id": corp_id,
                        "corp_name": corp_name, "error": "role"}
            wresp.raise_for_status()
            wallets = sorted(wresp.json() or [], key=lambda w: w.get("division", 0))
            total = sum(w.get("balance", 0) for w in wallets)
            master = next((w.get("balance", 0) for w in wallets if w.get("division") == 1),
                          wallets[0].get("balance", 0) if wallets else 0)

            donations: list[dict] = []
            total_donated = 0.0
            journal_status = None
            journal_count = 0
            journal_modified = None
            journal_expires = None
            ref_types: dict[str, int] = {}
            try:
                jresp = client.get(
                    f"{ESI_BASE}/corporations/{corp_id}/wallets/1/journal/?datasource=tranquility",
                    headers=headers,
                )
                journal_status = jresp.status_code
                # ESI caches the corp journal ~1h; surface when its snapshot was taken / next refreshes
                # so the UI can explain why a fresh donation (in the balance) isn't in the log yet.
                journal_modified = jresp.headers.get("last-modified")
                journal_expires = jresp.headers.get("expires")
                if jresp.status_code == 200:
                    entries = jresp.json() or []
                    journal_count = len(entries)
                    for e in entries:                                   # diagnostic: what ref_types exist
                        rt = e.get("ref_type", "?")
                        ref_types[rt] = ref_types.get(rt, 0) + 1
                    # A player giving ISK to the corp lands as `player_donation`; match any
                    # positive donation-typed entry so a CCP relabel can't silently hide it.
                    dons = [e for e in entries
                            if (e.get("amount") or 0) > 0 and "donation" in (e.get("ref_type") or "")]
                    dons.sort(key=lambda e: e.get("date", ""), reverse=True)
                    total_donated = sum(e.get("amount", 0) for e in dons)
                    dons = dons[:30]
                    names = _resolve_names(list({e.get("first_party_id") for e in dons if e.get("first_party_id")}), client)
                    for e in dons:
                        donations.append({
                            "date": e.get("date"),
                            "amount": e.get("amount"),
                            "donor": names.get(e.get("first_party_id"), str(e.get("first_party_id") or "?")),
                            "reason": (e.get("reason") or "").strip(),
                            "ref_type": e.get("ref_type"),
                        })
            except Exception:
                pass

            return {"connected": True, "character_name": name, "corp_id": corp_id, "corp_name": corp_name,
                    "balance": master, "total_balance": total,
                    "divisions": [{"division": w.get("division"), "balance": w.get("balance", 0)} for w in wallets],
                    "donations": donations, "total_donated": total_donated,
                    "journal_status": journal_status, "journal_count": journal_count,
                    "journal_modified": journal_modified, "journal_expires": journal_expires,
                    "ref_types": dict(sorted(ref_types.items(), key=lambda kv: -kv[1])[:12])}
    except Exception:
        return {"connected": True, "character_name": name, "error": "fetch"}


# ── Character API ─────────────────────────────────────────────────────────────

@router.get("/api/characters")
def list_characters(pp_session: str = Cookie(default=None)):
    ensure_char_tables()
    session_info = _session_lookup(pp_session)
    session_char = session_info[0] if session_info else None
    context_id   = session_info[1] if session_info else None

    if context_id:
        t0 = time.monotonic()
        cached = cache_get_json(charlist_key(context_id))
        if cached is not None:
            log.info("charlist cache HIT context=%s in %.1fms", context_id, (time.monotonic() - t0) * 1000)
            return {**cached, "logged_in": session_char is not None, "session_character_id": session_char}
        log.info("charlist cache MISS context=%s", context_id)
    t0 = time.monotonic()

    con = get_connection()
    if context_id:
        rows = con.execute("""
            SELECT character_id, character_name, token_expiry, refresh_token,
                   interplanetary_consolidation, command_center_upgrades,
                   planetology, advanced_planetology, COALESCE(is_dummy, 0) AS is_dummy,
                   COALESCE(scopes, '') AS scopes
            FROM pp_characters WHERE context_id=?
            ORDER BY COALESCE(is_dummy,0), character_name COLLATE NOCASE
        """, (context_id,)).fetchall()

        # Scoped to this session's own characters via the JOIN below — these used to fetch
        # EVERY character's planets/yield history system-wide (no WHERE at all) and filter down
        # in Python, so every user's load scaled with the WHOLE app's data instead of their own.
        # Measured directly: the unscoped pp_colony_yield fetch alone cost ~360ms with just 12
        # accounts' worth of history — that only gets worse as more users sign up.
        try:
            planet_rows = con.execute("""
                SELECT cp.character_id, cp.planet_id, cp.planet_type, cp.is_extractor, cp.p0_name, cp.upgrade_level,
                       cp.planet_num, cp.num_pins, cp.products, cp.pad_contents, cp.pad_inputs, cp.checkpoint_at,
                       cp.sim_state, cp.esi_modified, cp.esi_expires,
                       COALESCE(ss.name, '') AS system_name
                FROM pp_char_planets cp
                JOIN pp_characters ch ON ch.character_id = cp.character_id
                LEFT JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
                WHERE ch.context_id=?
            """, (context_id,)).fetchall()
        except Exception:  # no geo table → no system names
            planet_rows = con.execute("""
                SELECT cp.character_id, cp.planet_id, cp.planet_type, cp.is_extractor, cp.p0_name, cp.upgrade_level,
                       cp.planet_num, cp.num_pins, cp.products, cp.pad_contents, cp.pad_inputs, cp.checkpoint_at,
                       cp.sim_state, cp.esi_modified, cp.esi_expires, '' AS system_name
                FROM pp_char_planets cp
                JOIN pp_characters ch ON ch.character_id = cp.character_id
                WHERE ch.context_id=?
            """, (context_id,)).fetchall()
    else:
        rows = []
        planet_rows = []

    # Per-colony yield history (oldest→newest), for the measured-decline burn-down.
    yield_hist: dict[tuple, list] = {}
    try:
        for y in (con.execute("""
            SELECT y.character_id, y.planet_id, y.install_ts, y.peak_day, y.prog_days, y.scanned_ts
            FROM pp_colony_yield y
            JOIN pp_characters ch ON ch.character_id = y.character_id
            WHERE ch.context_id=?
            ORDER BY y.install_ts ASC
        """, (context_id,)).fetchall() if context_id else []):
            yield_hist.setdefault((y["character_id"], y["planet_id"]), []).append(
                {"install": y["install_ts"], "peak": round(y["peak_day"] or 0),
                 "prog_days": y["prog_days"], "ts": y["scanned_ts"]})
    except Exception:
        yield_hist = {}
    con.close()

    char_planets: dict[int, list] = {}
    for p in planet_rows:
        cid = p["character_id"]
        try:
            products = _json.loads(p["products"]) if p["products"] else []
        except Exception:
            products = []
        # Parse sim_state once and reuse below — it used to be re-parsed independently for pads,
        # production rate and program_days (3x _json.loads() of the same string per planet), which
        # added up across a real fleet's ~300+ planets. A parse failure here still degrades each of
        # the three downstream uses exactly as before (each already falls back gracefully on its own).
        sim = None
        if p["sim_state"]:
            try:
                sim = _json.loads(p["sim_state"])
            except Exception:
                sim = None
        # Pads: forward-simulate the colony to request time (matches the live in-game launchpad);
        # fall back to the raw ESI checkpoint snapshot for planets we can't simulate.
        pads = []
        if sim:
            try:
                from app.pi_sim import project
                pads = [{"type_id": o["type_id"], "name": o["name"], "amount": o["amount"]}
                        for o in project(sim)]
            except Exception:
                pads = []
        if not pads:
            try:
                pads = _json.loads(p["pad_contents"]) if p["pad_contents"] else []
            except Exception:
                pads = []
        # FACTORY planets have no sim_state, so `pads` above is the raw (often days-old) ESI checkpoint.
        # Project the FINAL product forward at its effective rate, capped by P1 runout — so the "In pads
        # ~est" tracks reality (P4 especially) and agrees with the dashboard instead of freezing.
        if not p["is_extractor"]:
            try:
                if products:
                    from app.planner import project_factory_pad
                    _tid = products[0]["type_id"]
                    _base = next((it.get("amount", 0) or 0 for it in pads if it.get("type_id") == _tid), 0)
                    _proj = project_factory_pad(_tid, _json.loads(p["pad_inputs"] or "[]"), _base, p["checkpoint_at"])
                    pads = [it for it in pads if it.get("type_id") != _tid]
                    if round(_proj) >= 1:
                        pads.insert(0, {"type_id": _tid, "name": products[0].get("name"), "amount": int(round(_proj))})
            except Exception:
                pass
        # Production rate (units/day) per output — reliable (from the extractor program config,
        # not the stale stored volume). Used by the Analyze tab to map setup vs a plan's needs.
        production = []
        if sim:
            try:
                for o in (sim.get("outputs") or []):
                    # Sustainable (extraction-limited) rate — poor planets can't hold full factory
                    # output, so this is the honest "units/day toward a quota". Falls back to the
                    # launchpad rate for sim states scanned before rate_sustained existed.
                    full = o.get("rate", 0) or 0                     # full factory appetite
                    sustained = o.get("rate_sustained")             # None on pre-rate_sustained scans
                    stale = sustained is None                       # → we're showing the optimistic full rate
                    rate = (sustained if sustained is not None else full) or 0
                    ext = o.get("ext_refined", full) or full      # heads' refined rate, BEFORE factory clip
                    if rate > 0:
                        production.append({
                            "type_id": o["type_id"], "name": o["name"],
                            "per_day": round(rate * 86400),
                            "full_per_day": round(full * 86400),
                            # unclipped extraction the heads sustain — the surplus over full_per_day is
                            # the decay/overshoot buffer that rate_sustained hides once factory-limited
                            "ext_per_day": round(ext * 86400),
                            # extraction-limited: the planet can't keep its own factories fed
                            "capped": (not stale) and full > 0 and rate < full * 0.97,
                            "stale": stale,
                        })
            except Exception:
                production = []
        program_days = None      # the extraction-program length the player set (install→expiry)
        prog_expiry = None       # extraction-program expiry (epoch) → drives the "extraction left" readout
        ext_p0_day = None        # game-true average P0 extraction/day (matches the in-game units/hour)
        if sim:
            try:
                program_days = sim.get("program_days")
                ext_p0_day = sim.get("peak_p0_day")
                prog_expiry = sim.get("expiry")
            except Exception:
                program_days = None
        char_planets.setdefault(cid, []).append({
            "planet_type":   p["planet_type"],
            "is_extractor":  bool(p["is_extractor"]),
            "p0_name":       p["p0_name"],
            "upgrade_level": p["upgrade_level"],
            "system":        p["system_name"],
            "planet_num":    p["planet_num"],
            "num_pins":      p["num_pins"],
            "products":      products,
            "pads":          pads,
            "production":    production,
            "program_days":  program_days,
            "expiry":        prog_expiry,
            "esi_modified":  p["esi_modified"],
            "esi_expires":   p["esi_expires"],
            "ext_p0_day":    ext_p0_day,
            "yield_history": yield_hist.get((cid, p["planet_id"]), []),
        })

    now = datetime.now(timezone.utc).isoformat()
    chars = []
    for r in rows:
        expiry = r["token_expiry"] or ""
        is_dummy = bool(r["is_dummy"])
        # A character's token is usable as long as a refresh token exists — access tokens
        # expire in 20 minutes but are transparently refreshed on use. Only a truly revoked
        # or missing refresh token means the character needs to be re-added.
        token_ok = True if is_dummy else bool(r["refresh_token"])
        # A wallet-only toon (corp-wallet scope, no planets scope) is just a money viewer — it isn't
        # a PI character. Flagged so the UI can label it and keep it out of PI pickers; the planner /
        # dashboard exclude it in SQL already.
        sc = r["scopes"] or ""
        wallet_only = ("read_corporation_wallets" in sc) and ("manage_planets" not in sc)
        my_planets = char_planets.get(r["character_id"], [])
        # Earliest moment ESI will have anything new for this character (min of its planets'
        # Expires, ignoring ones already in the past — those are already rescan-ready now).
        _future_expiries = [p["esi_expires"] for p in my_planets
                             if p["esi_expires"] and p["esi_expires"] > time.time()]
        next_data_at = min(_future_expiries) if _future_expiries and len(_future_expiries) == len(my_planets) else None
        chars.append({
            "character_id":   r["character_id"],
            "name":           r["character_name"],
            "token_ok":       token_ok,
            "is_dummy":       is_dummy,
            "wallet_only":    wallet_only,
            "max_planets":    1 + r["interplanetary_consolidation"],
            "ccu":            r["command_center_upgrades"],
            "planetology":    r["planetology"],
            "adv_planetology":r["advanced_planetology"],
            "planets":        my_planets,
            "next_data_at":   next_data_at,
        })
    _admin, _tester = admin_and_tester_status_for_context(context_id)
    result = {
        "characters": chars,
        "configured": _is_configured(),
        "is_admin":   _admin,
        "is_tester":  _tester,
    }
    if context_id:
        log.info("charlist cache MISS context=%s built in %.1fms", context_id, (time.monotonic() - t0) * 1000)
        cache_set_json(charlist_key(context_id), result, ttl=_CHARLIST_TTL)
    return {
        **result,
        "logged_in": session_char is not None,
        "session_character_id": session_char,
    }


@router.delete("/api/characters/{character_id}")
def remove_character(character_id: int, context_id: int = Depends(require_context)):
    ensure_char_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_characters WHERE character_id=? AND context_id=?",
                (character_id, context_id))
    con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (character_id,))
    con.commit()
    con.close()
    cache_invalidate(charlist_key(context_id))
    return {"removed": character_id}


# ── Dummy (synthetic) characters ─────────────────────────────────────────────
# Let a player who won't log every alt in add placeholder toons that contribute planet
# slots + a CCU level to the plan. They carry no ESI token and no colonies; the planner
# picks them up by context_id exactly like real characters. is_admin ignores them.

class DummyCreate(BaseModel):
    count: int = 1
    max_planets: int = 6        # 1–6 (→ interplanetary_consolidation = max_planets − 1)
    ccu: int = 5               # 1–5 command-centre level
    name_prefix: str = "Alt"


class DummyEdit(BaseModel):
    name: str | None = None
    max_planets: int | None = None
    ccu: int | None = None


def _clamp(v, lo, hi, default):
    try:
        return max(lo, min(hi, int(v)))
    except (TypeError, ValueError):
        return default


@router.post("/api/characters/dummy")
def add_dummy_characters(req: DummyCreate, context_id: int = Depends(require_context)):
    ensure_char_tables()
    count = _clamp(req.count, 1, 100, 1)
    mp = _clamp(req.max_planets, 1, 6, 6)
    ccu = _clamp(req.ccu, 1, 5, 5)
    prefix = (req.name_prefix or "Alt").strip()[:40] or "Alt"
    con = get_connection()
    # Globally unique negative ids (below any existing id) so synthetic chars never collide
    # with real EVE character ids (always positive).
    row = con.execute("SELECT MIN(character_id) AS m FROM pp_characters").fetchone()
    next_id = min(0, row["m"] or 0) - 1
    # Number new dummies after the count this context already has, so names stay readable.
    have = con.execute(
        "SELECT COUNT(*) AS c FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=1",
        (context_id,),
    ).fetchone()["c"]
    created = []
    for i in range(count):
        name = f"{prefix} {have + i + 1}"
        con.execute(
            "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
            "command_center_upgrades, context_id, is_dummy) VALUES (?,?,?,?,?,1)",
            (next_id, name, mp - 1, ccu, context_id),
        )
        created.append(next_id)
        next_id -= 1
    con.commit()
    con.close()
    cache_invalidate(charlist_key(context_id))
    return {"created": created, "count": len(created)}


@router.put("/api/characters/dummy/{character_id}")
def edit_dummy_character(character_id: int, req: DummyEdit,
                         context_id: int = Depends(require_context)):
    ensure_char_tables()
    con = get_connection()
    row = con.execute(
        "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=? AND COALESCE(is_dummy,0)=1",
        (character_id, context_id),
    ).fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="Dummy character not found")
    sets, params = [], []
    if req.name is not None and req.name.strip():
        sets.append("character_name=?"); params.append(req.name.strip()[:60])
    if req.max_planets is not None:
        sets.append("interplanetary_consolidation=?"); params.append(_clamp(req.max_planets, 1, 6, 6) - 1)
    if req.ccu is not None:
        sets.append("command_center_upgrades=?"); params.append(_clamp(req.ccu, 1, 5, 5))
    if sets:
        params += [character_id, context_id]
        con.execute(f"UPDATE pp_characters SET {', '.join(sets)} WHERE character_id=? AND context_id=?", params)
        con.commit()
    con.close()
    cache_invalidate(charlist_key(context_id))
    return {"ok": True}


@router.post("/api/characters/{character_id}/refresh-planets")
def refresh_char_planets(character_id: int, context_id: int = Depends(require_context)):
    con = get_connection()
    ok = con.execute(
        "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
        (character_id, context_id),
    ).fetchone()
    con.close()
    if not ok:
        raise HTTPException(status_code=403, detail="Character not in your context")
    token = _get_valid_token(character_id)
    if not token:
        raise HTTPException(status_code=400, detail="No valid token for character")
    # Re-fetch skills too. Skills (CCU, interplanetary consolidation, planetology) were only
    # written at add-time, so characters added before a skill column existed keep a stale 0
    # until refreshed. Only overwrite when the fetch actually returned data — a transient ESI
    # failure returns {} and must not wipe good values to 0.
    skills = _fetch_skills(character_id, token)
    if skills:
        con = get_connection()
        con.execute(
            "UPDATE pp_characters SET interplanetary_consolidation=?, command_center_upgrades=?, "
            "planetology=?, advanced_planetology=? WHERE character_id=?",
            (
                skills.get("interplanetary_consolidation", 0),
                skills.get("command_center_upgrades", 0),
                skills.get("planetology", 0),
                skills.get("advanced_planetology", 0),
                character_id,
            ),
        )
        con.commit()
        con.close()
    scan = _fetch_planets(character_id, token)
    cache_invalidate(charlist_key(context_id))
    return {"ok": True, "skills_updated": bool(skills),
            "planets_fetched": scan["fetched"], "planets_skipped_cached": scan["skipped"]}
