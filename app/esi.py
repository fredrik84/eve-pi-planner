"""
EVE SSO OAuth 2.0 integration.

Required env vars:
  EVE_CLIENT_ID      — from https://developers.eveonline.com
  EVE_CLIENT_SECRET  — from https://developers.eveonline.com
  EVE_CALLBACK_URL   — must match the registered callback (default: https://eve-pi.failed.name/auth/callback)

Scopes requested:
  esi-skills.read_skills.v1
  esi-planets.manage_planets.v1
"""

import json as _json
import os
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse

from app.sde import get_connection, ensure_once

router = APIRouter()

CLIENT_ID     = os.environ.get("EVE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EVE_CLIENT_SECRET", "")
CALLBACK_URL  = os.environ.get("EVE_CALLBACK_URL", "https://eve-pi.failed.name/auth/callback")

SCOPES = "esi-skills.read_skills.v1 esi-planets.manage_planets.v1 esi-planets.read_customs_offices.v1"
# Corp-wallet read is requested only on the dedicated "connect wallet" login (?wallet=1) so the
# normal Login flow never asks the public for wallet access. Requires the EVE application (on
# developers.eveonline.com) to also list this scope, and the character to be CEO/Director or hold
# the Accountant / Junior Accountant corp role. The wallet toon is a read-only money viewer, so we
# request ONLY the wallet scope — no skills/planets/POCOs (the callback's skill/planet fetches fail
# silently without those, which is fine: this toon isn't planned over).
WALLET_SCOPE  = "esi-wallet.read_corporation_wallets.v1"
WALLET_SCOPES = WALLET_SCOPE

# Wallet-only toons (corp-wallet scope, no planets scope) aren't PI characters. AND this into any
# single-table pp_characters PI query to exclude them; legacy empty-scope chars are kept. Begins with
# "AND " so it appends directly after an existing WHERE predicate (mind the leading space at the join).
PI_CHAR_SQL = ("AND NOT (COALESCE(scopes,'') LIKE '%read_corporation_wallets%' "
               "AND COALESCE(scopes,'') NOT LIKE '%manage_planets%')")

EVE_AUTH_URL  = "https://login.eveonline.com/v2/oauth/authorize"
EVE_TOKEN_URL = "https://login.eveonline.com/v2/oauth/token"
ESI_BASE      = "https://esi.evetech.net/latest"

# Skill type IDs relevant to Planetary Industry
SKILL_IDS = {
    2495: "interplanetary_consolidation",  # +1 planet per level
    2505: "command_center_upgrades",       # command center tier
    2406: "planetology",                   # remote sensing range
    2403: "advanced_planetology",          # remote sensing precision
}

# P0 resource type IDs → display names
P0_TYPE_NAMES = {
    2268: "Aqueous Liquids",
    2305: "Autotrophs",
    2267: "Base Metals",
    2288: "Carbon Compounds",
    2287: "Complex Organisms",
    2307: "Felsic Magma",
    2272: "Heavy Metals",
    2309: "Ionic Solutions",
    2073: "Microorganisms",
    2310: "Noble Gas",
    2270: "Noble Metals",
    2306: "Non-CS Crystals",
    2286: "Planktic Colonies",
    2311: "Reactive Gas",
    2308: "Suspended Plasma",
}

def _session_lookup(token: str | None) -> tuple[int, int] | None:
    """Look up a session token in the DB. Returns (character_id, context_id) or None."""
    if not token:
        return None
    try:
        con = get_connection()
        row = con.execute(
            "SELECT character_id, context_id FROM pp_sessions WHERE token=?", (token,)
        ).fetchone()
        con.close()
        if row:
            return (row["character_id"], row["context_id"] or 0)
    except Exception:
        pass
    return None


def _save_session(token: str, character_id: int, context_id: int):
    try:
        con = get_connection()
        con.execute(
            "INSERT INTO pp_sessions (token, character_id, context_id, created_at) VALUES (?,?,?,?)"
            " ON CONFLICT (token) DO UPDATE SET character_id=EXCLUDED.character_id,"
            " context_id=EXCLUDED.context_id, created_at=EXCLUDED.created_at",
            (token, character_id, context_id, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
    except Exception:
        pass


def _delete_session(token: str):
    try:
        con = get_connection()
        con.execute("DELETE FROM pp_sessions WHERE token=?", (token,))
        con.commit()
        con.close()
    except Exception:
        pass


def _invalidate_context_sessions(context_id: int):
    """Delete all session rows for a context (used after account deletion)."""
    try:
        con = get_connection()
        con.execute("DELETE FROM pp_sessions WHERE context_id=?", (context_id,))
        con.commit()
        con.close()
    except Exception:
        pass


def _is_configured() -> bool:
    return bool(CLIENT_ID and CLIENT_SECRET)


@ensure_once
def ensure_char_tables():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS solar_systems (
            system_id INTEGER PRIMARY KEY,
            name      TEXT NOT NULL
        )
    """)
    con.execute("CREATE INDEX IF NOT EXISTS idx_ss_name ON solar_systems(name COLLATE NOCASE)")
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_user_contexts (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_characters (
            character_id   INTEGER PRIMARY KEY,
            character_name TEXT    NOT NULL,
            access_token   TEXT    NOT NULL DEFAULT '',
            refresh_token  TEXT    NOT NULL DEFAULT '',
            token_expiry   TEXT,
            interplanetary_consolidation INTEGER DEFAULT 0,
            command_center_upgrades      INTEGER DEFAULT 0,
            planetology                  INTEGER DEFAULT 0,
            advanced_planetology         INTEGER DEFAULT 0,
            context_id     INTEGER,
            scopes         TEXT    DEFAULT ''
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_char_planets (
            character_id    INTEGER NOT NULL,
            planet_id       INTEGER NOT NULL,
            planet_type     TEXT    NOT NULL,
            solar_system_id INTEGER,
            upgrade_level   INTEGER DEFAULT 0,
            num_pins        INTEGER DEFAULT 0,
            is_extractor    INTEGER DEFAULT 0,
            p0_type_id      INTEGER,
            p0_name         TEXT,
            planet_num      INTEGER,
            PRIMARY KEY (character_id, planet_id)
        )
    """)
    con.commit()
    try:
        con.execute("ALTER TABLE pp_char_planets ADD COLUMN planet_num INTEGER")
    except Exception:
        pass
    for _col in ("products TEXT", "pad_contents TEXT", "pad_inputs TEXT", "sim_state TEXT", "issues TEXT", "scanned_at REAL", "checkpoint_at REAL", "storage TEXT", "esi_modified REAL"):  # JSON cols + scan/checkpoint epochs + fullest-container fill state + ESI data vintage (Last-Modified)
        try:
            con.execute(f"ALTER TABLE pp_char_planets ADD COLUMN {_col}")
        except Exception:
            pass
    try:
        # Synthetic "dummy" characters (no ESI token / colonies) added manually so a player
        # needn't log every alt in. is_dummy=1; their character_id is negative to avoid
        # colliding with real EVE ids. They contribute planet slots + CCU only.
        con.execute("ALTER TABLE pp_characters ADD COLUMN is_dummy INTEGER DEFAULT 0")
    except Exception:
        pass
    # Rolling per-colony yield samples: ONE row per extraction program (deduped by install_ts),
    # capped at _YIELD_KEEP per colony. Lets the analysis show the MEASURED install-yield trend
    # across reseats (a colony's hotspots deplete, so successive programs drift down unless you move
    # the heads). Bounded storage: ≤ _YIELD_KEEP rows × colonies × users.
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_colony_yield (
            character_id INTEGER NOT NULL,
            planet_id    INTEGER NOT NULL,
            install_ts   REAL    NOT NULL,
            p0_type_id   INTEGER,
            peak_day     REAL,
            prog_days    REAL,
            scanned_ts   REAL,
            PRIMARY KEY (character_id, planet_id, install_ts)
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_sessions (
            token        TEXT    PRIMARY KEY,
            character_id INTEGER NOT NULL,
            context_id   INTEGER NOT NULL DEFAULT 0,
            created_at   TEXT    NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_oauth_pending (
            state      TEXT PRIMARY KEY,
            context_id INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_admins (
            character_name TEXT PRIMARY KEY COLLATE NOCASE,
            added_by       TEXT,
            added_at       TEXT
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_testers (
            character_name TEXT PRIMARY KEY COLLATE NOCASE,
            added_by       TEXT,
            added_at       TEXT
        )
    """)
    # Commit all CREATE TABLEs before running ALTER TABLE migrations.
    # In Postgres, a failed ALTER (column already exists) triggers auto-rollback
    # which would undo all preceding CREATEs if they're in the same transaction.
    con.commit()
    # Schema migrations for existing deployments
    for col, tbl in [("context_id", "pp_characters"), ("context_id", "pp_sessions")]:
        try:
            con.execute(f"ALTER TABLE {tbl} ADD COLUMN {col} INTEGER")
        except Exception:
            pass
    try:
        con.execute("ALTER TABLE pp_characters ADD COLUMN scopes TEXT DEFAULT ''")
    except Exception:
        pass
    # Migrate existing characters/sessions to context 1
    unscoped = con.execute(
        "SELECT COUNT(*) FROM pp_characters WHERE context_id IS NULL"
    ).fetchone()[0]
    if unscoped > 0:
        con.execute(
            "INSERT OR IGNORE INTO pp_user_contexts (id, created_at) VALUES (1, ?)",
            (datetime.now(timezone.utc).isoformat(),),
        )
        con.execute("UPDATE pp_characters SET context_id = 1 WHERE context_id IS NULL")
        con.execute("UPDATE pp_sessions SET context_id = 1 WHERE context_id IS NULL OR context_id = 0")
    con.commit()
    con.close()


def _get_valid_token(character_id: int) -> str | None:
    """Return a valid access token, refreshing if expired."""
    con = get_connection()
    row = con.execute(
        "SELECT access_token, refresh_token, token_expiry FROM pp_characters WHERE character_id=?",
        (character_id,),
    ).fetchone()
    con.close()
    if not row:
        return None
    expiry = row["token_expiry"] or ""
    now = datetime.now(timezone.utc).isoformat()
    if expiry > now:
        return row["access_token"]
    return _refresh_token(character_id, row["refresh_token"])


def _roman_to_int(s: str) -> int | None:
    vals = {'I': 1, 'V': 5, 'X': 10, 'L': 50}
    s = s.upper().strip()
    if not s or not all(c in vals for c in s):
        return None
    result, prev = 0, 0
    for c in reversed(s):
        v = vals[c]
        result += v if v >= prev else -v
        prev = v
    return result if result > 0 else None


# Colony health check (run at scan, when we have the full pin/route detail). Returns a list of
# short warning strings for anything amiss — wrong/missing routes and near-full storage. Empty
# list = nothing to flag. (Expired/expiring programs are checked live in the dashboard instead,
# since that's time-dependent.)
def _detect_colony_issues(detail: dict, types: dict, pi: dict) -> list[str]:
    """Deduped issue KIND codes for this colony (the dashboard turns them into per-character,
    counted warnings):
       ext_unrouted — an extractor head isn't routed (extracted P0 goes nowhere)
       fac_unfed    — a factory has no input route
       fac_output   — a factory's output isn't routed
       p0_mismatch  — a P0 is being extracted that NO facility on the planet consumes, so it just
                      piles up (e.g. extracting Heavy Metal while the factories are set to make
                      Oxidizing, which wants a different input). A reliable, volume-free signal."""
    pins = detail.get("pins") or []
    routes = detail.get("routes") or []
    src = {r.get("source_pin_id") for r in routes}
    dst = {r.get("destination_pin_id") for r in routes}
    schematics = pi.get("schematics") or {}
    sch_to_out = {v["schematic_id"]: out for out, v in schematics.items()}
    facility_inputs: set = set()      # every type_id consumed by a facility on this planet
    extracted: set = set()            # every P0 the extractors pull
    kinds = set()
    for pin in pins:
        pid = pin.get("pin_id")
        ext = pin.get("extractor_details")
        if ext:
            if ext.get("product_type_id"):
                extracted.add(ext["product_type_id"])
            if pid not in src:
                kinds.add("ext_unrouted")
            continue
        sid = pin.get("schematic_id") or (pin.get("factory_details") or {}).get("schematic_id")
        if sid:
            for inp in (schematics.get(sch_to_out.get(sid), {}) or {}).get("inputs") or []:
                if inp.get("type_id"):
                    facility_inputs.add(inp["type_id"])
            if pid not in dst:
                kinds.add("fac_unfed")
            elif pid not in src:
                kinds.add("fac_output")
    if facility_inputs and any(p0 not in facility_inputs for p0 in extracted):
        kinds.add("p0_mismatch")
    return list(kinds)


_TIER_VOL = {0: 0.01, 1: 0.19, 2: 1.5, 3: 6.0, 4: 100.0}   # m³ per unit by PI tier (P1 verified 0.19 in-game)
def _struct_cap(name: str):
    if "Launchpad" in name: return ("Launchpad", 10000.0)
    if "Storage Facility" in name: return ("Storage", 12000.0)
    if "Command Center" in name: return ("Command center", 500.0)
    return None

def _storage_summary(detail: dict, sim: dict | None, types: dict) -> dict | None:
    """Fullest LAUNCHPAD on the planet + how fast it's filling (the colony's output volume per hour,
    from the sim's SUSTAINED rate — what actually piles up long-term). Returned for every extractor
    planet (None only if it has no launchpad) so the dashboard can both warn at ≥80% AND estimate the
    soonest pad to cap. The dashboard only acts on EXTRACTOR planets — a factory's launchpads are meant
    to sit full of inputs and drain, so they're not flagged. Storage Facilities are intentionally
    ignored: a storage buffer sitting full is expected — only a full launchpad blocks export."""
    best = None
    for pin in (detail.get("pins") or []):
        cap = _struct_cap((types.get(pin.get("type_id"), {}) or {}).get("name") or "")
        if not cap or cap[0] != "Launchpad":
            continue
        _, capacity = cap
        vol = sum((c.get("amount", 0) or 0) * _TIER_VOL.get((types.get(c.get("type_id"), {}) or {}).get("pi_tier") or 0, 0.01)
                  for c in (pin.get("contents") or []))
        if capacity and (best is None or vol / capacity > best["frac"]):
            best = {"frac": vol / capacity, "vol": vol, "cap": capacity}
    if not best:
        return None
    fill_h = 0.0
    if sim:
        # rate_sustained = the long-run production that actually accumulates (extraction-limited), so
        # the time-to-full holds over a multi-hour/day fill (the full factory rate would over-count).
        fill_h = sum((o.get("rate_sustained", o.get("rate", 0)) or 0) * 3600.0 * _TIER_VOL.get(o.get("tier") or 0, 0.01)
                     for o in (sim.get("outputs") or []))
    return {"vol_m3": round(min(best["vol"], best["cap"]), 1), "cap_m3": best["cap"], "fill_m3_h": round(fill_h, 2)}


_YIELD_KEEP = 10    # programs of yield history kept per colony (bounded storage)
def _record_yield_sample(con, character_id: int, planet_id: int, sim: dict | None, scan_ts: float) -> None:
    """Log this colony's install-yield for the CURRENT program (deduped by install_ts), then prune to
    the last _YIELD_KEEP programs. One row per reseat/restart — the measured trend the burn-down uses."""
    try:
        if not sim:
            return
        install = sim.get("install")
        peak = sim.get("peak_p0_day")
        if not install or not peak:
            return
        p0 = None
        for o in (sim.get("outputs") or []):
            if (o.get("tier") or 0) == 0:    # pure P0 output identifies the extracted resource
                p0 = o.get("type_id"); break
        con.execute(
            "INSERT INTO pp_colony_yield "
            "(character_id, planet_id, install_ts, p0_type_id, peak_day, prog_days, scanned_ts) "
            "VALUES (?,?,?,?,?,?,?)"
            " ON CONFLICT (character_id, planet_id, install_ts) DO UPDATE SET"
            " p0_type_id=EXCLUDED.p0_type_id, peak_day=EXCLUDED.peak_day,"
            " prog_days=EXCLUDED.prog_days, scanned_ts=EXCLUDED.scanned_ts",
            (character_id, planet_id, float(install), p0, float(peak),
             sim.get("program_days"), scan_ts))
        # prune older programs beyond the cap (keep the newest _YIELD_KEEP by install_ts)
        con.execute(
            "DELETE FROM pp_colony_yield WHERE character_id=? AND planet_id=? AND install_ts NOT IN "
            "(SELECT install_ts FROM pp_colony_yield WHERE character_id=? AND planet_id=? "
            " ORDER BY install_ts DESC LIMIT ?)",
            (character_id, planet_id, character_id, planet_id, _YIELD_KEEP))
    except Exception:
        pass


def _fetch_planets(character_id: int, access_token: str) -> None:
    """Fetch planet list + pin details from ESI, store in pp_char_planets."""
    try:
        headers = {"Authorization": f"Bearer {access_token}"}
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/planets/",
                headers=headers,
                timeout=10,
            )
            resp.raise_for_status()
            planet_list = resp.json()

        # Resolve any new solar_system_id → name via ESI universe/names
        sys_ids = list({p.get("solar_system_id") for p in planet_list if p.get("solar_system_id")})
        if sys_ids:
            try:
                nr = httpx.post(
                    f"{ESI_BASE}/universe/names/?datasource=tranquility",
                    json=sys_ids, timeout=10,
                )
                nr.raise_for_status()
                ss_con = get_connection()
                for item in nr.json():
                    if item.get("category") == "solar_system":
                        ss_con.execute("INSERT OR IGNORE INTO solar_systems VALUES (?,?)",
                                       (item["id"], item["name"]))
                ss_con.commit()
                ss_con.close()
            except Exception:
                pass

        # SDE lookups: schematic_id → produced type, and type_id → name (for factory products
        # and what's stored in the launchpads/storage).
        try:
            from app.sde import load_pi_data
            _pi = load_pi_data()
            _types = _pi["types"]
            _sch_to_out = {v["schematic_id"]: out for out, v in _pi["schematics"].items()}
        except Exception:
            _types, _sch_to_out = {}, {}

        con = get_connection()
        con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (character_id,))
        _scan_ts = time.time()        # anchor for projecting buffer depletion between scans

        with httpx.Client() as client:
            for planet in planet_list:
                planet_id       = planet["planet_id"]
                planet_type     = planet.get("planet_type", "").capitalize()
                solar_system_id = planet.get("solar_system_id")
                upgrade_level   = planet.get("upgrade_level", 0)
                num_pins        = planet.get("num_pins", 0)

                is_extractor = 0
                p0_type_id   = None
                p0_name      = None
                planet_num   = None
                _detail      = None
                esi_modified = None             # when ESI actually generated this colony's data (its cache vintage)
                products: dict[int, str] = {}   # output type_id → name (factory pins)
                pads: dict[int, int] = {}       # stored item type_id → total amount
                try:
                    pr = client.get(
                        f"{ESI_BASE}/characters/{character_id}/planets/{planet_id}/",
                        headers=headers,
                        timeout=10,
                    )
                    pr.raise_for_status()
                    _detail = pr.json()
                    # ESI caches PI data, so a reseat isn't reflected until ESI regenerates. Last-Modified
                    # is when THIS body was generated — surfacing its age explains a stale "expired".
                    try:
                        from email.utils import parsedate_to_datetime
                        _lm = pr.headers.get("last-modified")
                        if _lm:
                            esi_modified = parsedate_to_datetime(_lm).timestamp()
                    except Exception:
                        esi_modified = None
                    for pin in _detail.get("pins", []):
                        ext = pin.get("extractor_details")
                        if ext:
                            is_extractor = 1
                            p0_type_id   = ext.get("product_type_id")
                            p0_name      = P0_TYPE_NAMES.get(p0_type_id)
                        sch = pin.get("schematic_id") or (pin.get("factory_details") or {}).get("schematic_id")
                        if sch:
                            out = _sch_to_out.get(sch)
                            if out:
                                products[out] = _types.get(out, {}).get("name") or f"#{out}"
                        for cnt in (pin.get("contents") or []):
                            tid, amt = cnt.get("type_id"), cnt.get("amount", 0) or 0
                            if tid and amt:
                                pads[tid] = pads.get(tid, 0) + amt
                except Exception:
                    pass

                # Keep only the planet's highest-tier output (its end product) — the lower tiers
                # are just intermediate steps in that planet's chain.
                if products:
                    _max_tier = max((_types.get(t, {}).get("pi_tier") or 0) for t in products)
                    products = {t: n for t, n in products.items()
                                if (_types.get(t, {}).get("pi_tier") or 0) == _max_tier}
                products_json = _json.dumps(
                    [{"type_id": t, "name": n} for t, n in products.items()]) if products else None
                # Split launchpad/pin contents into the planet's finished product (pad_contents) vs
                # its lower-tier imported inputs still buffered (pad_inputs). Use the planet's
                # PRODUCT tier from its schematics — NOT the max tier currently present — so a
                # launchpad holding only freshly-loaded P1 inputs (no output yet) is still seen as
                # inputs, not mistaken for the product.
                _prod_tier = max((_types.get(t, {}).get("pi_tier") or 0) for t in products) if products else None
                pad_inputs_json = None
                if pads:
                    if _prod_tier is None:   # no schematics (pure storage) → fall back to max present
                        _prod_tier = max((_types.get(t, {}).get("pi_tier") or 0) for t in pads)
                    _inputs = {t: a for t, a in pads.items()
                               if (_types.get(t, {}).get("pi_tier") or 0) < _prod_tier}
                    if _inputs:
                        pad_inputs_json = _json.dumps(sorted(
                            [{"type_id": t, "name": _types.get(t, {}).get("name") or f"#{t}",
                              "amount": a, "tier": _types.get(t, {}).get("pi_tier") or 0}
                             for t, a in _inputs.items()], key=lambda x: -x["amount"]))
                    pads = {t: a for t, a in pads.items()
                            if (_types.get(t, {}).get("pi_tier") or 0) >= _prod_tier}
                pads_json = _json.dumps(sorted(
                    [{"type_id": t, "name": _types.get(t, {}).get("name") or f"#{t}", "amount": a}
                     for t, a in pads.items()],
                    key=lambda x: -x["amount"])) if pads else None

                # Forward-simulation state — lets list_characters project the launchpad contents
                # to request time (ESI's stored contents are a stale checkpoint; the colony keeps
                # producing). Extractor planets only; factory imports fall back to the snapshot.
                sim_state_json = None
                _sim = None
                if _detail:
                    try:
                        from app.pi_sim import colony_sim_state
                        _sim = colony_sim_state(_detail, _pi)
                        sim_state_json = _json.dumps(_sim) if _sim else None
                    except Exception:
                        _sim = None
                        sim_state_json = None
                storage_json = None       # fullest launchpad/storage fill state (% + rate to full)
                try:
                    if _detail:
                        _st = _storage_summary(_detail, _sim, _types)
                        storage_json = _json.dumps(_st) if _st else None
                except Exception:
                    storage_json = None
                issues_json = None        # colony health warnings (routes / extractor-recipe mismatch)
                try:
                    if _detail:
                        _iss = _detect_colony_issues(_detail, _types, _pi)
                        issues_json = _json.dumps(_iss) if _iss else None
                except Exception:
                    issues_json = None

                # Colony checkpoint the reported contents are "as of" (the most recent pin cycle).
                # The dashboard depletes factory input buffers from HERE, not the fetch time.
                checkpoint_at = None
                try:
                    from app.pi_sim import _epoch
                    _lcs = [_epoch(p.get("last_cycle_start")) for p in (_detail.get("pins") or [])] if _detail else []
                    _lcs = [t for t in _lcs if t]
                    checkpoint_at = max(_lcs) if _lcs else None
                except Exception:
                    checkpoint_at = None

                # Fetch planet name to derive in-system ordinal (e.g. "01B-88 VIII" → 8)
                try:
                    pinfo = client.get(
                        f"{ESI_BASE}/universe/planets/{planet_id}/",
                        timeout=10,
                    )
                    pinfo.raise_for_status()
                    pname = pinfo.json().get("name", "")
                    last_word = pname.strip().split()[-1] if pname.strip() else ""
                    planet_num = _roman_to_int(last_word)
                except Exception:
                    pass

                con.execute("""
                    INSERT INTO pp_char_planets
                        (character_id, planet_id, planet_type, solar_system_id,
                         upgrade_level, num_pins, is_extractor, p0_type_id, p0_name,
                         planet_num, products, pad_contents, pad_inputs, sim_state, issues, scanned_at, checkpoint_at, storage, esi_modified)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (character_id, planet_id) DO UPDATE SET
                      planet_type=EXCLUDED.planet_type, solar_system_id=EXCLUDED.solar_system_id,
                      upgrade_level=EXCLUDED.upgrade_level, num_pins=EXCLUDED.num_pins,
                      is_extractor=EXCLUDED.is_extractor, p0_type_id=EXCLUDED.p0_type_id,
                      p0_name=EXCLUDED.p0_name, planet_num=EXCLUDED.planet_num,
                      products=EXCLUDED.products, pad_contents=EXCLUDED.pad_contents,
                      pad_inputs=EXCLUDED.pad_inputs, sim_state=EXCLUDED.sim_state,
                      issues=EXCLUDED.issues, scanned_at=EXCLUDED.scanned_at,
                      checkpoint_at=EXCLUDED.checkpoint_at, storage=EXCLUDED.storage,
                      esi_modified=EXCLUDED.esi_modified
                """, (character_id, planet_id, planet_type, solar_system_id,
                      upgrade_level, num_pins, is_extractor, p0_type_id, p0_name,
                      planet_num, products_json, pads_json, pad_inputs_json, sim_state_json, issues_json, _scan_ts, checkpoint_at, storage_json, esi_modified))

                if is_extractor and _sim:      # log a per-program yield sample for the trend/burn-down
                    _record_yield_sample(con, character_id, planet_id, _sim, _scan_ts)

        con.commit()
        con.close()
    except Exception:
        pass


# ── OAuth endpoints ───────────────────────────────────────────────────────────

@router.get("/auth/login")
def esi_login(wallet: int = 0, pp_session: str = Cookie(default=None)):
    if not _is_configured():
        return HTMLResponse(
            "<h2>ESI not configured</h2>"
            "<p>Set <code>EVE_CLIENT_ID</code> and <code>EVE_CLIENT_SECRET</code> "
            "in <code>.env</code> and redeploy.</p>"
            "<p><a href='/'>Back</a></p>",
            status_code=503,
        )
    # Capture the caller's existing context NOW (this is a first-party request to our own domain,
    # so the session cookie is reliably present) and carry it through the OAuth `state`. The cookie
    # is often dropped on the cross-site redirect back from EVE SSO to /auth/callback, which used to
    # silently create a NEW context — orphaning the previously-added characters ("adding a 2nd char
    # removes the 1st"). Threading the context through `state` makes the link survive that cookie loss.
    sess = _session_lookup(pp_session)
    ctx = sess[1] if sess else 0
    state = secrets.token_urlsafe(16)
    try:
        con = get_connection()
        # Clean up expired pending states (>10 min) opportunistically
        con.execute("DELETE FROM pp_oauth_pending WHERE created_at < datetime('now', '-10 minutes')")
        con.execute(
            "INSERT INTO pp_oauth_pending (state, context_id, created_at) VALUES (?,?,?)"
            " ON CONFLICT (state) DO UPDATE SET context_id=EXCLUDED.context_id, created_at=EXCLUDED.created_at",
            (state, ctx, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
        con.close()
    except Exception:
        pass
    scope_enc = (WALLET_SCOPES if wallet else SCOPES).replace(" ", "%20")
    url = (
        f"{EVE_AUTH_URL}?response_type=code"
        f"&client_id={CLIENT_ID}"
        f"&redirect_uri={CALLBACK_URL}"
        f"&scope={scope_enc}"
        f"&state={state}"
    )
    return RedirectResponse(url)


@router.get("/auth/callback")
def esi_callback(
    code: str = Query(...),
    state: str = Query(...),
    response: Response = None,
    pp_session: str = Cookie(default=None),
):
    try:
        con_p = get_connection()
        pending_row = con_p.execute(
            "SELECT context_id FROM pp_oauth_pending WHERE state=?", (state,)
        ).fetchone()
        if pending_row:
            con_p.execute("DELETE FROM pp_oauth_pending WHERE state=?", (state,))
            con_p.commit()
        con_p.close()
    except Exception:
        pending_row = None
    if not pending_row:
        raise HTTPException(status_code=400, detail="Invalid or expired OAuth state")
    state_ctx = pending_row["context_id"] or 0

    # Exchange code for tokens
    with httpx.Client() as client:
        tok = client.post(
            EVE_TOKEN_URL,
            data={"grant_type": "authorization_code", "code": code,
                  "redirect_uri": CALLBACK_URL},
            auth=(CLIENT_ID, CLIENT_SECRET),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            timeout=10,
        )
    tok.raise_for_status()
    token_data = tok.json()

    access_token  = token_data["access_token"]
    refresh_token = token_data["refresh_token"]
    expires_in    = token_data.get("expires_in", 1199)
    expiry = (datetime.now(timezone.utc) + timedelta(seconds=expires_in)).isoformat()

    # Decode character ID from JWT subject (no full verify — we just got it from EVE)
    import base64, json as _json
    payload_b64 = access_token.split(".")[1]
    payload_b64 += "=" * (4 - len(payload_b64) % 4)
    payload = _json.loads(base64.urlsafe_b64decode(payload_b64))
    sub = payload.get("sub", "")  # "CHARACTER:EVE:12345678"
    character_id = int(sub.split(":")[-1])
    character_name = payload.get("name", str(character_id))
    # Granted scopes (the `scp` claim is a list, or a bare string for a single scope) — stored so
    # the corp-wallet feature can find a character that authorised wallet read.
    scp = payload.get("scp", [])
    if isinstance(scp, str):
        scp = [scp]
    scopes_str = " ".join(scp)

    skills = _fetch_skills(character_id, access_token)

    ensure_char_tables()
    con = get_connection()

    # Resolve context_id:
    # 1. Character already in DB with a context → keep it
    existing = con.execute(
        "SELECT context_id FROM pp_characters WHERE character_id=?", (character_id,)
    ).fetchone()
    if existing and existing["context_id"]:
        context_id = existing["context_id"]
    # 2. Caller was logged in when they started this add → join that context. Taken from the OAuth
    #    `state` (captured at /auth/login), so it works even if the cross-site redirect dropped the
    #    session cookie. Cookie is only a fallback for the rare case state didn't carry it.
    elif state_ctx:
        context_id = state_ctx
    elif (sess := _session_lookup(pp_session)):
        _, context_id = sess
    # 3. New user → create a fresh context
    else:
        row = con.execute(
            "INSERT INTO pp_user_contexts (created_at) VALUES (?) RETURNING id",
            (datetime.now(timezone.utc).isoformat(),),
        ).fetchone()
        context_id = row[0]
        con.commit()

    con.execute("""
        INSERT INTO pp_characters
            (character_id, character_name, access_token, refresh_token, token_expiry,
             interplanetary_consolidation, command_center_upgrades, planetology,
             advanced_planetology, context_id, scopes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (character_id) DO UPDATE SET
          character_name=EXCLUDED.character_name, access_token=EXCLUDED.access_token,
          refresh_token=EXCLUDED.refresh_token, token_expiry=EXCLUDED.token_expiry,
          interplanetary_consolidation=EXCLUDED.interplanetary_consolidation,
          command_center_upgrades=EXCLUDED.command_center_upgrades,
          planetology=EXCLUDED.planetology, advanced_planetology=EXCLUDED.advanced_planetology,
          context_id=EXCLUDED.context_id, scopes=EXCLUDED.scopes
    """, (
        character_id, character_name, access_token, refresh_token, expiry,
        skills.get("interplanetary_consolidation", 0),
        skills.get("command_center_upgrades", 0),
        skills.get("planetology", 0),
        skills.get("advanced_planetology", 0),
        context_id, scopes_str,
    ))
    con.commit()
    con.close()

    _fetch_planets(character_id, access_token)

    # Create session token and persist it
    session_token = secrets.token_urlsafe(32)
    _save_session(session_token, character_id, context_id)

    html_response = HTMLResponse("""
        <html><body>
        <p>Character added. This window will close.</p>
        <script>
          if (window.opener) { window.opener.postMessage('esi-done','*'); window.close(); }
          else { location.href = '/'; }
        </script>
        </body></html>
    """)
    # Set session cookie (httponly, secure in prod via HTTPS proxy)
    html_response.set_cookie(
        "pp_session", session_token,
        httponly=True, samesite="lax", max_age=86400 * 30,
    )
    return html_response


@router.get("/auth/logout")
def esi_logout(pp_session: str = Cookie(default=None)):
    if pp_session:
        _delete_session(pp_session)
    resp = RedirectResponse("/")
    resp.delete_cookie("pp_session")
    return resp


# ── Session check (used by other routers) ────────────────────────────────────

def require_context(pp_session: str = Cookie(default=None)) -> int:
    """FastAPI dependency: return context_id or raise 401."""
    sess = _session_lookup(pp_session)
    if sess:
        return sess[1]
    raise HTTPException(status_code=401, detail="Not authenticated — please log in via EVE SSO")


def require_session(pp_session: str = Cookie(default=None)) -> int:
    """FastAPI dependency: return character_id or raise 401."""
    sess = _session_lookup(pp_session)
    if sess:
        return sess[0]
    raise HTTPException(status_code=401, detail="Not authenticated — please log in via EVE SSO")


def session_character_id(pp_session: str = Cookie(default=None)) -> int | None:
    """Returns character_id or None (no exception)."""
    sess = _session_lookup(pp_session)
    return sess[0] if sess else None


def session_context_id(pp_session: str = Cookie(default=None)) -> int | None:
    """Returns context_id or None (no exception)."""
    sess = _session_lookup(pp_session)
    return sess[1] if sess else None


@router.delete("/api/me")
def delete_account(context_id: int = Depends(require_context)):
    """Permanently delete all data for the calling user's account.
    Runs in a single transaction; invalidates all sessions on success."""
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in
                    con.execute("SELECT character_id FROM pp_characters WHERE context_id=?",
                                (context_id,)).fetchall()]
        if char_ids:
            ph = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_plan_config WHERE character_id IN ({ph})", char_ids)
            con.execute(f"DELETE FROM pp_colony_yield WHERE character_id IN ({ph})", char_ids)
            con.execute(f"DELETE FROM pp_char_planets WHERE character_id IN ({ph})", char_ids)

        con.execute("DELETE FROM pp_plan_snapshots WHERE context_id=?", (context_id,))
        con.execute("DELETE FROM pp_plan_baseline WHERE context_id=?", (context_id,))
        con.execute("DELETE FROM pp_profiles WHERE context_id=?", (context_id,))

        # Private baskets (context_id IS NOT NULL = owned by this context)
        basket_ids = [r["id"] for r in
                      con.execute("SELECT id FROM pp_baskets WHERE context_id=?",
                                  (context_id,)).fetchall()]
        if basket_ids:
            ph = ",".join("?" * len(basket_ids))
            con.execute(f"DELETE FROM pp_basket_items WHERE basket_id IN ({ph})", basket_ids)
        con.execute("DELETE FROM pp_baskets WHERE context_id=?", (context_id,))

        if char_ids:
            ph = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_characters WHERE character_id IN ({ph})", char_ids)

        con.execute("DELETE FROM pp_sessions WHERE context_id=?", (context_id,))
        con.execute("DELETE FROM pp_user_contexts WHERE id=?", (context_id,))
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        raise HTTPException(status_code=500, detail="Deletion failed — no data was changed")

    con.close()
    _invalidate_context_sessions(context_id)
    return {"deleted": True}


# Permanent bootstrap admins (lowercase), un-removable via the UI. Additional admins are
# stored in the pp_admins table and managed from the Admin tab. EVE names are globally
# unique and SSO-verified, so a name match proves ownership.
ADMIN_CHARACTERS = {"ekaoni"}


@ensure_once
def ensure_admin_table():
    """Create pp_admins if missing (also created by ensure_char_tables; this lets the admin
    endpoints stand alone, matching the ensure_bugs_table/ensure_basket_tables pattern)."""
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_admins (
            character_name TEXT PRIMARY KEY COLLATE NOCASE,
            added_by       TEXT,
            added_at       TEXT
        )
    """)
    con.commit()
    con.close()


def _db_admin_names() -> set[str]:
    """Lowercased admin names from pp_admins (empty on any failure)."""
    try:
        con = get_connection()
        rows = con.execute("SELECT character_name FROM pp_admins").fetchall()
        con.close()
        return {(r["character_name"] or "").lower() for r in rows}
    except Exception:
        return set()


def _context_character_names(context_id: int) -> list[str]:
    """Lowercased names of the context's real (non-dummy, SSO-verified) characters — a dummy
    character is just a typed-in name and must never confer admin/tester status."""
    con = get_connection()
    rows = con.execute(
        "SELECT character_name FROM pp_characters "
        "WHERE context_id=? AND COALESCE(is_dummy, 0) = 0", (context_id,)
    ).fetchall()
    con.close()
    return [(r["character_name"] or "").lower() for r in rows]


def is_admin(pp_session: str = Cookie(default=None)) -> bool:
    """True if the session's account owns a character with an admin name (bootstrap set
    or the pp_admins table)."""
    sess = _session_lookup(pp_session)
    if not sess:
        return False
    names = _context_character_names(sess[1])
    admin_names = ADMIN_CHARACTERS | _db_admin_names()
    return any(n in admin_names for n in names)


def require_admin(pp_session: str = Cookie(default=None)) -> int:
    """FastAPI dependency: return context_id for admins, else raise 403."""
    sess = _session_lookup(pp_session)
    if not sess or not is_admin(pp_session):
        raise HTTPException(status_code=403, detail="Admin access required")
    return sess[1]


def _db_tester_names() -> set[str]:
    """Lowercased tester names from pp_testers (empty on any failure)."""
    try:
        con = get_connection()
        rows = con.execute("SELECT character_name FROM pp_testers").fetchall()
        con.close()
        return {(r["character_name"] or "").lower() for r in rows}
    except Exception:
        return set()


def is_tester(pp_session: str = Cookie(default=None)) -> bool:
    """True if the session's account owns a character in pp_testers (or is an admin)."""
    return admin_and_tester_status(pp_session)[1]


def admin_and_tester_status(pp_session: str = Cookie(default=None)) -> tuple[bool, bool]:
    """(is_admin, is_tester) computed together from one session lookup + one character-name
    fetch — for callers that need both (e.g. list_characters, list_features), this avoids the
    duplicate session-lookup + character-name query that calling is_admin() and is_tester()
    separately would otherwise cost."""
    sess = _session_lookup(pp_session)
    if not sess:
        return False, False
    names = _context_character_names(sess[1])
    admin = any(n in (ADMIN_CHARACTERS | _db_admin_names()) for n in names)
    if admin:
        return True, True
    tester = any(n in _db_tester_names() for n in names)
    return False, tester


# ── ESI helpers ───────────────────────────────────────────────────────────────

def _fetch_skills(character_id: int, access_token: str) -> dict[str, int]:
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/skills/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
        resp.raise_for_status()
        skill_data = resp.json()
        result: dict[str, int] = {}
        for s in skill_data.get("skills", []):
            field = SKILL_IDS.get(s["skill_id"])
            if field:
                result[field] = s.get("trained_skill_level", 0)
        return result
    except Exception:
        return {}


def _refresh_token(character_id: int, refresh_token: str) -> str | None:
    """Exchange refresh token for new access token. Returns new access token or None."""
    try:
        with httpx.Client() as client:
            resp = client.post(
                EVE_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(CLIENT_ID, CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        resp.raise_for_status()
        data = resp.json()
        new_access  = data["access_token"]
        new_refresh = data["refresh_token"]
        expiry = (datetime.now(timezone.utc) + timedelta(seconds=data.get("expires_in", 1199))).isoformat()

        con = get_connection()
        con.execute(
            "UPDATE pp_characters SET access_token=?, refresh_token=?, token_expiry=? WHERE character_id=?",
            (new_access, new_refresh, expiry, character_id),
        )
        con.commit()
        con.close()
        return new_access
    except Exception:
        return None

