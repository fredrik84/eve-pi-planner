"""
EVE SSO OAuth 2.0 integration.

Required env vars:
  EVE_CLIENT_ID      — from https://developers.eveonline.com
  EVE_CLIENT_SECRET  — from https://developers.eveonline.com
  EVE_CALLBACK_URL   — must match the registered callback (default: https://eveindustry.net/auth/callback)

Scopes requested:
  esi-skills.read_skills.v1
  esi-planets.manage_planets.v1
"""

import json as _json
import logging
import os
import re
import secrets
import time
from datetime import datetime, timedelta, timezone

import httpx

from app import esi_http
from fastapi import APIRouter, Cookie, Depends, HTTPException, Query, Response
from pydantic import BaseModel
from fastapi.responses import HTMLResponse, RedirectResponse

from app.sde import get_connection, ensure_once
from app.cache import cache_invalidate, charlist_key

router = APIRouter()
log = logging.getLogger(__name__)

CLIENT_ID     = os.environ.get("EVE_CLIENT_ID", "")
CLIENT_SECRET = os.environ.get("EVE_CLIENT_SECRET", "")
CALLBACK_URL  = os.environ.get("EVE_CALLBACK_URL", "https://eveindustry.net/auth/callback")

_NATURAL_SPLIT = re.compile(r"(\d+)")


def natural_name_key(name: str):
    """Sort key so 'alt 2' < 'alt 10' < 'alt 20' (plain string sort gives alt 1, alt 10,
    alt 2, alt 20 — digit runs compare lexicographically, not numerically)."""
    return [int(chunk) if chunk.isdigit() else chunk.lower()
            for chunk in _NATURAL_SPLIT.split(name or "")]

SCOPES = "esi-skills.read_skills.v1 esi-planets.manage_planets.v1 esi-planets.read_customs_offices.v1"
# Corp-wallet read is requested only on the dedicated "connect wallet" login (?wallet=1) so the
# normal Login flow never asks the public for wallet access. Requires the EVE application (on
# developers.eveonline.com) to also list this scope, and the character to be CEO/Director or hold
# the Accountant / Junior Accountant corp role. The wallet toon is a read-only money viewer, so we
# request ONLY the wallet scope — no skills/planets/POCOs (the callback's skill/planet fetches fail
# silently without those, which is fine: this toon isn't planned over).
WALLET_SCOPE  = "esi-wallet.read_corporation_wallets.v1"
WALLET_SCOPES = WALLET_SCOPE

# Reactions-industry job tracking, requested only on the dedicated "connect for reactions
# tracking" login (?reactions=1) — same opt-in shape as the wallet scope, since only accounts
# actually using the Reactions tool should be prompted for this, not every PI-planner user.
# esi-universe.read_structures.v1 resolves a job's facility_id (an Upwell structure) to a readable
# name via GET /universe/structures/{id}/. NOTE: an earlier value here — "esi-structures.read_character.v1"
# — is NOT a real ESI scope (verified against the SSO scope catalog); EVE SSO silently dropped it,
# so no reactions token ever carried a working structure-read grant and every facility name fell back
# to the raw "Structure #<id>". Characters must RE-authorise reactions (?reactions=1) to pick up the
# corrected scope, same as the corp-jobs rollout below. Unlike the wallet flow (a dedicated read-only
# alt), this is the player's OWN PI character — they still need normal PI planning to keep working, so
# this UNIONS with the base SCOPES rather than replacing them.
INDUSTRY_JOBS_SCOPE  = "esi-industry.read_character_jobs.v1"
STRUCTURES_SCOPE     = "esi-universe.read_structures.v1"
# A reaction installed "for corporation" (e.g. a shared corp hangar/reactor, not the character's
# own personal jobs) only shows up via the CORPORATION industry-jobs endpoint, not the character
# one above — confirmed 2026-07-13 (a real job installed at a corp POS never appeared under the
# personal endpoint). Bundled into the same reactions opt-in rather than a separate login, since
# anyone tracking reaction jobs at all plausibly has some installed this way. Requires the
# character to actually hold the in-game Factory_Manager/Director corp role to read it — ESI
# grants the OAuth scope regardless, but the corp jobs call itself 401s/403s without the role
# (same "best-effort, skip don't error" shape as the wallet flow's role check).
CORP_INDUSTRY_JOBS_SCOPE = "esi-industry.read_corporation_jobs.v1"
# INDUSTRY_JOBS_SCOPES is defined below as an alias of the unified REACTIONS_SCOPES (see there) —
# it used to be a jobs-only set disjoint from MARKET_SCOPES, which caused the market/jobs scope silo.

# Local/alliance market pricing (Reactions tab), requested only on the dedicated "connect for
# market pricing" login (?market=1). MARKET_SCOPE reads a player-owned Upwell structure's market
# (GET /markets/structures/{id}/); SEARCH_STRUCT_SCOPE lets the character search structures it has
# docking/market access to by name (GET /characters/{id}/search/?categories=structure); the shared
# STRUCTURES_SCOPE resolves a structure_id to a readable name (GET /universe/structures/{id}/).
# Like the reactions flow (and unlike the read-only wallet alt), this is the player's OWN character,
# so it UNIONS with the base SCOPES — a character added from the market setup card is a full
# PI + market character, not a stub. The EVE application (developers.eveonline.com) must also LIST
# the two new scopes; listing != requesting, so the public Login never asks for them.
MARKET_SCOPE        = "esi-markets.structure_markets.v1"
SEARCH_STRUCT_SCOPE = "esi-search.search_structures.v1"

# ── Unified reactions-ecosystem scope set (fixes the market/jobs scope silo) ──────────
# A player's OWN reactions character is simultaneously a reaction-job/slot character AND a potential
# local-market reader. Historically ?reactions=1 and ?market=1 requested DISJOINT sets
# (INDUSTRY_JOBS_SCOPES had no market scope; MARKET_SCOPES had no read_character_jobs). Since a
# character stores exactly one token/scope-set (EVE refresh tokens rotate — last auth wins), it could
# only ever be one or the other: a market character supplied no reaction slots to the job engine, and
# re-authing it as a reaction character silently DROPPED market access — with no way back, because
# designating a market reader requires the very market scope the re-auth had just removed. Both flows
# now request this single superset, so a character connected either way holds both capabilities and
# re-authing via either preserves the other. Existing single-purpose characters pick up the missing
# scopes on their next re-auth (same "must re-authorise" rollout as the corp-jobs/structures additions
# above). Wallet stays deliberately separate — that's a read-only money alt, not a PI character.
# Industry (Manufacturing planner): read the character's owned blueprints (ME/TE + which BPOs/BPCs
# they hold). Folded into the SAME unified superset below — NOT a separate set. An earlier version
# gave ?industry=1 its own scope list that omitted the market scope, which re-broke the exact silo
# this superset exists to prevent: connecting a market character for blueprints stripped its market
# access (EVE tokens carry only the last auth's scopes). Every opt-in flow must request the full set.
BLUEPRINTS_SCOPE = "esi-characters.read_blueprints.v1"
# Assets: what the character actually owns, so the planner stops telling you to build components
# already sitting in your hangar and can report queue progress without guessing a start date.
# Same rule as every scope above — it joins the ONE superset, never its own set.
ASSETS_SCOPE = "esi-assets.read_assets.v1"
# Corp assets + division names. ESI gates BOTH behind the Director role and offers nothing weaker,
# so for a character without it they can never answer — which is why this was deliberately left out
# at first, and why nothing here treats a 403 as an error: for most characters it is simply the
# expected answer, reported as "not a director" and nothing more. Directors, though, run their
# builds out of corp hangars and containers, and pasting a hangar every time stock moves is not a
# substitute for reading it. The division-name scope rides along because a director picking "which
# hangar do I pull this build from" needs the names they gave those hangars, not "Corp hangar 3".
# Pasting (app/industry/assets.py add_pasted_source) remains the path for everyone else.
CORP_ASSETS_SCOPE    = "esi-assets.read_corporation_assets.v1"
CORP_DIVISIONS_SCOPE = "esi-corporations.read_divisions.v1"
REACTIONS_SCOPES = (
    f"{SCOPES} {INDUSTRY_JOBS_SCOPE} {CORP_INDUSTRY_JOBS_SCOPE} "
    f"{MARKET_SCOPE} {SEARCH_STRUCT_SCOPE} {STRUCTURES_SCOPE} {BLUEPRINTS_SCOPE} {ASSETS_SCOPE}"
)
# The two corp scopes are the ONE exception to the single-superset rule above, and the exception is
# the point: EVE gates both behind the **Director** role, so for almost everybody they are
# permissions that can never be used — and every one of them is a line on the consent screen an
# ordinary member has to agree to before they can plan a build. Asking a whole userbase to hand over
# corporation-wide read access so that the occasional director can skip a copy-paste is the wrong
# trade, and it was mine to get wrong: these were folded into the superset when corp hangars shipped.
#
# So they are requested ONLY by the explicit "connect a director" flow (`/auth/login?director=1`),
# which asks for the full superset PLUS these — a strict superset, so a director who connects that
# way keeps everything a normal character has.
#
# The one wrinkle: a director who later re-auths through a normal flow drops the corp scopes again.
# That is recoverable and visible rather than silent — the corp-scan panel goes back to offering
# "Connect a director" — and it is a far better failure than the alternative of asking everyone.
DIRECTOR_SCOPES = f"{REACTIONS_SCOPES} {CORP_ASSETS_SCOPE} {CORP_DIVISIONS_SCOPE}"
# All opt-in "connect a character" flows request this ONE superset so re-authing a character for
# any tool never drops the scopes another relies on. Wallet stays deliberately separate.
MARKET_SCOPES = REACTIONS_SCOPES
INDUSTRY_JOBS_SCOPES = REACTIONS_SCOPES
INDUSTRY_SCOPES = REACTIONS_SCOPES

# Wallet-only toons (corp-wallet scope, no planets scope) aren't PI characters. AND this into any
# single-table pp_characters PI query to exclude them; legacy empty-scope chars are kept. Begins with
# "AND " so it appends directly after an existing WHERE predicate (mind the leading space at the join).
PI_CHAR_SQL = ("AND NOT (COALESCE(scopes,'') LIKE '%read_corporation_wallets%' "
               "AND COALESCE(scopes,'') NOT LIKE '%manage_planets%')")

EVE_AUTH_URL   = "https://login.eveonline.com/v2/oauth/authorize"
EVE_TOKEN_URL  = "https://login.eveonline.com/v2/oauth/token"
EVE_REVOKE_URL = "https://login.eveonline.com/v2/oauth/revoke"

# Skill type IDs relevant to Planetary Industry
SKILL_IDS = {
    2495: "interplanetary_consolidation",  # +1 planet per level
    2505: "command_center_upgrades",       # command center tier
    2406: "planetology",                   # remote sensing range
    2403: "advanced_planetology",          # remote sensing precision
    # Reactions (moon-goo tool): reaction job slots = 1 base + 1/level each, max 11.
    45748: "mass_reactions",
    45749: "advanced_mass_reactions",
    # Manufacturing (Industry planner): manufacturing job slots = 1 base + 1/level each, max 11.
    3387: "mass_production",
    24625: "advanced_mass_production",
    # Industry job TIME skills: Industry −4%/level (manufacturing), Advanced Industry −3%/level
    # (all industry jobs incl. reactions) — drive the planner's makespan.
    3380: "industry",
    3388: "advanced_industry",
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
    # Virtually every per-user query in the app joins/filters pp_characters by context_id (it's
    # the tenant boundary) — without this index, each of those queries seq-scans the whole table.
    # Cheap at today's row count, but it's the one index that matters most as the app grows.
    con.execute("CREATE INDEX IF NOT EXISTS idx_pp_characters_context ON pp_characters(context_id)")
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
    # Each ADD COLUMN commits immediately on success — Postgres aborts the WHOLE current
    # transaction on any failed statement (e.g. a later column that already exists), and that
    # rollback silently erases any earlier ADD COLUMN in the same uncommitted transaction that
    # had actually just succeeded. Bit us for real: esi_expires/skills_expires (below) were
    # being added successfully, then erased by the next already-exists ALTER (is_dummy) rolling
    # back before anything committed — the columns were never actually persisted, and every
    # request touching them threw psycopg2.errors.UndefinedColumn. Don't go back to a bare
    # try/except-pass chain of ALTERs without a commit after each one.
    def _add_col(table: str, coldef: str):
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            con.commit()
        except Exception:
            pass

    _add_col("pp_char_planets", "planet_num INTEGER")
    for _col in ("products TEXT", "pad_contents TEXT", "pad_inputs TEXT", "sim_state TEXT", "drain TEXT", "issues TEXT", "scanned_at REAL", "checkpoint_at REAL", "storage TEXT", "esi_modified REAL", "esi_expires REAL"):  # JSON cols + scan/checkpoint epochs + fullest-container fill state + ESI data vintage (Last-Modified) + next-refresh time (Expires)
        _add_col("pp_char_planets", _col)
    # Hand-built ("hybrid") colonies run extraction + a chained P1->P2+ factory together on one
    # planet — a shape our own generated templates never produce. `products` above is deliberately
    # collapsed to only the highest tier (see _fetch_planets), which throws away the evidence that
    # lower tiers coexist. product_chain keeps ALL tiers present (uncollapsed), and is_hybrid is a
    # precomputed flag (is_extractor AND max tier present >= 2) so query sites don't need to parse
    # JSON to check it — mirrors the existing is_extractor precomputed-flag convention.
    _add_col("pp_char_planets", "product_chain TEXT")
    _add_col("pp_char_planets", "is_hybrid INTEGER DEFAULT 0")
    # Extractor head positions per ECU (JSON: [{"p0":type_id,"r":head_radius,"h":[[lat,lon],...]}], all
    # radians). Lets Setup Analysis flag colonies on the SAME planet whose heads overlap the same
    # resource hotspot (own or cross-character) — the "everyone parked on the one good spot" problem.
    # Only populated on a rescan after this shipped; older rows stay NULL (proximity check skips them).
    _add_col("pp_char_planets", "ext_heads TEXT")
    # When ESI will next regenerate this character's skills (Expires header) — lets a rescan
    # skip a re-fetch that's guaranteed to return the same cached data.
    _add_col("pp_characters", "skills_expires REAL")
    # Synthetic "dummy" characters (no ESI token / colonies) added manually so a player needn't
    # log every alt in. is_dummy=1; their character_id is negative to avoid colliding with real
    # EVE ids. They contribute planet slots + CCU only.
    _add_col("pp_characters", "is_dummy INTEGER DEFAULT 0")
    # A placeholder's DECLARED Industry job slots (0-11 each, 0 = doesn't do that activity).
    # Deliberately their own columns rather than writing implied levels into mass_production /
    # mass_reactions: the user is asked for slots, and slots do not round-trip through the skill
    # columns — the formula is 1 base + levels, so 0 slots is not expressible at all, and a fake
    # level would make a placeholder look like a scanned character to every other reader of those
    # columns (skill advisor, required-skills, the job-time basis).
    _add_col("pp_characters", "dummy_mfg_slots INTEGER DEFAULT 0")
    _add_col("pp_characters", "dummy_rx_slots INTEGER DEFAULT 0")
    # Which corp a character is in — needed to attribute a corp hangar (pp_corp_assets) to the
    # right account without counting an unrelated context's corp stock.
    _add_col("pp_characters", "corporation_id BIGINT")
    # Reactions-industry skills (see SKILL_IDS below) and alliance affiliation — the latter picks
    # the Reactions tool's moon-goo pricing source, group deal vs. open market (see app.groups.member_group).
    _add_col("pp_characters", "mass_reactions INTEGER DEFAULT 0")
    _add_col("pp_characters", "advanced_mass_reactions INTEGER DEFAULT 0")
    # Manufacturing job-slot skills (Industry planner) — same shape as the reaction ones above.
    _add_col("pp_characters", "mass_production INTEGER DEFAULT 0")
    _add_col("pp_characters", "advanced_mass_production INTEGER DEFAULT 0")
    # Industry job-time skills (Industry planner makespan).
    _add_col("pp_characters", "industry INTEGER DEFAULT 0")
    _add_col("pp_characters", "advanced_industry INTEGER DEFAULT 0")
    _add_col("pp_characters", "alliance_id INTEGER")
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
    # Mean head position ("lat,lon") for THIS program — heads are re-placed each reseat, so a moved
    # centroid between programs is a clear sign the player actually reseated (re-surveyed) rather than
    # just restarting in place. Lets Setup Analysis tell "reseated repeatedly, still marginal → the
    # planet's tapped, redeploy" from "hasn't really tried a fresh spot yet". NULL on pre-existing rows.
    _add_col("pp_colony_yield", "head_centroid TEXT")
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
    # Schema migrations for existing deployments (same commit-per-statement reasoning as _add_col above)
    for col, tbl in [("context_id", "pp_characters"), ("context_id", "pp_sessions")]:
        _add_col(tbl, f"{col} INTEGER")
    _add_col("pp_characters", "scopes TEXT DEFAULT ''")
    # When an UNATTENDED scan (the alert-driven rescan in app/notifications.py) last failed for a
    # reason that isn't a dead token — a timeout or a 5xx, which leave the refresh token in place
    # and so are invisible to `token_ok`. Without it, an ESI outage means every alerting colony is
    # retried on every 15-minute tick, which is the one way that feature could hammer the API for
    # no result. Cleared on the next success. Epoch seconds → double precision, per the rule in
    # CLAUDE.md; `_EPOCH_COLUMNS` in app/db.py carries the entry.
    _add_col("pp_characters", "scan_failed_at DOUBLE PRECISION")
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


def _heads_centroid(ext_heads: list | None) -> str | None:
    """Mean (lat,lon) of every extractor head across this colony's ECUs, as "lat,lon" — the anchor
    for detecting whether the heads MOVED between programs (a genuine reseat vs a same-spot restart)."""
    pts = [h for e in (ext_heads or []) for h in (e.get("h") or []) if h]
    if not pts:
        return None
    return f"{round(sum(p[0] for p in pts) / len(pts), 5)},{round(sum(p[1] for p in pts) / len(pts), 5)}"


_YIELD_KEEP = 10    # programs of yield history kept per colony (bounded storage)
def _record_yield_sample(con, character_id: int, planet_id: int, sim: dict | None, scan_ts: float,
                         head_centroid: str | None = None) -> None:
    """Log this colony's install-yield for the CURRENT program (deduped by install_ts), then prune to
    the last _YIELD_KEEP programs. One row per reseat/restart — the measured trend the burn-down uses.
    `head_centroid` ("lat,lon") records where the heads sat this program so a reseat (heads moved) can
    be told from a same-spot restart."""
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
            "(character_id, planet_id, install_ts, p0_type_id, peak_day, prog_days, scanned_ts, head_centroid) "
            "VALUES (?,?,?,?,?,?,?,?)"
            " ON CONFLICT (character_id, planet_id, install_ts) DO UPDATE SET"
            " p0_type_id=EXCLUDED.p0_type_id, peak_day=EXCLUDED.peak_day,"
            " prog_days=EXCLUDED.prog_days, scanned_ts=EXCLUDED.scanned_ts,"
            " head_centroid=COALESCE(EXCLUDED.head_centroid, pp_colony_yield.head_centroid)",
            (character_id, planet_id, float(install), p0, float(peak),
             sim.get("program_days"), scan_ts, head_centroid))
        # prune older programs beyond the cap (keep the newest _YIELD_KEEP by install_ts)
        con.execute(
            "DELETE FROM pp_colony_yield WHERE character_id=? AND planet_id=? AND install_ts NOT IN "
            "(SELECT install_ts FROM pp_colony_yield WHERE character_id=? AND planet_id=? "
            " ORDER BY install_ts DESC LIMIT ?)",
            (character_id, planet_id, character_id, planet_id, _YIELD_KEEP))
    except Exception:
        pass


class _AlreadyKnown(Exception):
    """Sentinel: this lookup's answer is already in the DB, so skip the request."""


def _fetch_planets(character_id: int, access_token: str, only_planet_id: int | None = None) -> dict:
    """Fetch planet list + pin details from ESI, store in pp_char_planets.

    Returns {"fetched": N, "skipped": N} (planets actually re-fetched vs. skipped because
    ESI's cache hadn't lapsed yet — see esi_cache_skip) so a caller can surface real numbers
    instead of digging through server logs (which aren't even configured to emit INFO here).

    `only_planet_id` scopes the scan to ONE planet (the per-planet 'rescan this planet' button):
    the delete-missing-planets purge is skipped (we're not looking at the others) and the ESI
    cache-skip is bypassed (the user explicitly asked to re-check this one)."""
    con = None   # closed in `finally` below, however this function exits — leaking a Postgres
                 # connection out of the (tiny, 8-per-pod) pool on any exception anywhere in this
                 # long function (many sequential ESI calls) previously required a full pod
                 # restart to recover, since the pool never got the connection back.
    _fetched, _skipped, _failed = 0, 0, 0
    try:
        resp = esi_http.get(f"characters/{character_id}/planets/", token=access_token, timeout=10)
        resp.raise_for_status()
        planet_list = resp.json()

        if only_planet_id is not None:
            planet_list = [p for p in planet_list if p.get("planet_id") == only_planet_id]

        # Resolve any new solar_system_id → name via ESI universe/names.
        # Only the ones we do not already have: a system's name is static, so asking again buys
        # nothing. This was an unconditional POST on every scan, which is a third of the request
        # cost of a single-colony rescan (the alert-driven path in app/notifications.py does one of
        # those per alert sent), spent re-learning names already in the table.
        sys_ids = list({p.get("solar_system_id") for p in planet_list if p.get("solar_system_id")})
        if sys_ids:
            try:
                _known_con = get_connection()
                try:
                    _qs = ",".join("?" for _ in sys_ids)
                    _known = {r["system_id"] for r in _known_con.execute(
                        f"SELECT system_id FROM solar_systems WHERE system_id IN ({_qs})",
                        tuple(sys_ids))}
                finally:
                    _known_con.close()
                sys_ids = [i for i in sys_ids if i not in _known]
            except Exception:
                pass                       # fail safe: ask ESI, exactly as before
        if sys_ids:
            try:
                nr = esi_http.post(
                    "universe/names/?datasource=tranquility", json=sys_ids, timeout=10,
                )
                nr.raise_for_status()
                ss_con = get_connection()
                try:
                    for item in nr.json():
                        if item.get("category") == "solar_system":
                            ss_con.execute("INSERT OR IGNORE INTO solar_systems VALUES (?,?)",
                                           (item["id"], item["name"]))
                    ss_con.commit()
                finally:
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
        # Purge only planets no longer owned — planets still present may be skip-eligible
        # below (cache_skip), so we can't blanket-delete before knowing which ones those are.
        # Skipped entirely for a single-planet rescan (we're not looking at the others).
        if only_planet_id is None:
            _current_ids = tuple(p["planet_id"] for p in planet_list)
            if _current_ids:
                _ph = ",".join("?" * len(_current_ids))
                con.execute(f"DELETE FROM pp_char_planets WHERE character_id=? AND planet_id NOT IN ({_ph})",
                            (character_id, *_current_ids))
            else:
                con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (character_id,))

        # Reuse the connection already open above rather than opening a second one via
        # feature_enabled() — this function already holds `con` for its whole (potentially
        # long, many-sequential-ESI-calls) duration, and a second concurrently-held connection
        # per in-flight rescan was doubling pool pressure under a real user burst. Fails safe
        # (don't skip) on any error — this is a speed optimization, not a correctness path.
        _skip_cached = False
        try:
            from app.features import ensure_features_table
            ensure_features_table()
            _row = con.execute("SELECT state FROM pp_features WHERE key='esi_cache_skip'").fetchone()
            _skip_cached = bool(_row) and _row["state"] == "public"
        except Exception:
            _skip_cached = False
        _cached_expiry = {
            r["planet_id"]: r["esi_expires"]
            for r in con.execute(
                "SELECT planet_id, esi_expires FROM pp_char_planets WHERE character_id=?",
                (character_id,),
            )
        } if (_skip_cached and only_planet_id is None) else {}
        _now = time.time()
        _known_planet_num = {
            r["planet_id"]: r["planet_num"]
            for r in con.execute(
                "SELECT planet_id, planet_num FROM pp_char_planets WHERE character_id=?",
                (character_id,),
            ) if r["planet_num"] is not None
        }

        _scan_ts = time.time()        # anchor for projecting buffer depletion between scans

        with esi_http.client() as client:
            for planet in planet_list:
                planet_id       = planet["planet_id"]

                # ESI's Expires header on the last fetch of THIS planet tells us when it will
                # next regenerate — before that, a re-fetch is guaranteed to return identical
                # data, so skip it entirely (fewer round trips = a faster rescan).
                _exp = _cached_expiry.get(planet_id)
                if _exp and _exp > _now:
                    _skipped += 1
                    continue

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
                esi_expires  = None             # when ESI will next regenerate it
                products: dict[int, str] = {}   # output type_id → name (factory pins)
                pads: dict[int, int] = {}       # stored item type_id → total amount
                ext_heads: list = []            # per-ECU head coords (for the same-hotspot proximity check)
                try:
                    pr = esi_http.get(
                        f"characters/{character_id}/planets/{planet_id}/",
                        client=client, token=access_token,
                    )
                    pr.raise_for_status()
                    _detail = pr.json()
                    # ESI caches PI data, so a reseat isn't reflected until ESI regenerates. Last-Modified
                    # is when THIS body was generated — surfacing its age explains a stale "expired".
                    esi_modified = _http_date_to_epoch(pr.headers.get("last-modified"))
                    esi_expires  = _http_date_to_epoch(pr.headers.get("expires"))
                    for pin in _detail.get("pins", []):
                        ext = pin.get("extractor_details")
                        if ext:
                            is_extractor = 1
                            p0_type_id   = ext.get("product_type_id")
                            p0_name      = P0_TYPE_NAMES.get(p0_type_id)
                            _heads = [[round(h["latitude"], 5), round(h["longitude"], 5)]
                                      for h in (ext.get("heads") or [])
                                      if h.get("latitude") is not None and h.get("longitude") is not None]
                            if _heads:
                                _e = {"p0": p0_type_id,
                                      "r": round(ext.get("head_radius") or 0.0, 5),
                                      "h": _heads}
                                # The ECU pin's own position centres the deployment's reachable area
                                # (heads reseat WITHIN reach of it) — the anchor for the range-overlap
                                # check, since two overlapping reachable areas keep competing however
                                # the heads are reseated.
                                if pin.get("latitude") is not None and pin.get("longitude") is not None:
                                    _e["c"] = [round(pin["latitude"], 5), round(pin["longitude"], 5)]
                                ext_heads.append(_e)
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
                    _detail = None

                # A detail read that failed writes NOTHING. The UPSERT below always ran, so a
                # timeout or a 5xx on this one endpoint overwrote a perfectly good colony with
                # `is_extractor=0` and NULLs for products, pads, sim state, storage and — the one
                # that compounds — `esi_expires`, while stamping a FRESH `scanned_at`. The planner,
                # the dashboard and Setup Analysis then read a blank colony rather than a stale one,
                # and with the expiry gone every caller believed it was due for a re-read. Losing
                # this scan is always better than destroying the last good one.
                if _detail is None:
                    _failed += 1
                    continue
                # Counted here, not before the read: `fetched` is reported to the user as
                # "planets re-scanned", and counting attempts made a scan that read nothing look
                # like a success on screen.
                _fetched += 1

                # Capture ALL tiers present (uncollapsed) before the highest-tier-only collapse
                # below discards the evidence — this is what lets a hybrid (extraction + chained
                # P1->P2+ factory) colony be detected without any new ESI call. Must run before
                # the collapse mutates `products` in place.
                product_chain = [
                    {"type_id": t, "name": n, "tier": _types.get(t, {}).get("pi_tier") or 0}
                    for t, n in products.items()
                ]
                product_chain_json = _json.dumps(product_chain) if product_chain else None
                is_hybrid = 1 if (is_extractor and any(pc["tier"] >= 2 for pc in product_chain)) else 0
                ext_heads_json = _json.dumps(ext_heads) if ext_heads else None

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
                # Drain state — per-imported-input consumption read off the planet's real factory
                # pins (constant quantity/cycle_time, no decay), so "when does this colony run dry"
                # is arithmetic rather than the modelled per-product average. The refill deadline,
                # the Up-next agenda and the factory_refill alert all answer from this.
                drain_json = None
                if _detail:
                    try:
                        from app.pi_sim import colony_drain_state
                        _drain = colony_drain_state(_detail, _pi)
                        drain_json = _json.dumps(_drain) if _drain else None
                    except Exception:
                        drain_json = None
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

                # Fetch planet name to derive in-system ordinal (e.g. "01B-88 VIII" → 8).
                # Skipped when we already worked it out: a planet's position in its system does not
                # change, so this is a once-per-planet lookup that was being repeated on every
                # scan — the other unconditional request the alert-driven rescan pays for.
                planet_num = _known_planet_num.get(planet_id)
                try:
                    if planet_num is not None:
                        raise _AlreadyKnown
                    pinfo = esi_http.get(f"universe/planets/{planet_id}/", client=client)
                    pinfo.raise_for_status()
                    pname = pinfo.json().get("name", "")
                    last_word = pname.strip().split()[-1] if pname.strip() else ""
                    planet_num = _roman_to_int(last_word)
                except _AlreadyKnown:
                    pass
                except Exception:
                    pass

                con.execute("""
                    INSERT INTO pp_char_planets
                        (character_id, planet_id, planet_type, solar_system_id,
                         upgrade_level, num_pins, is_extractor, p0_type_id, p0_name,
                         planet_num, products, pad_contents, pad_inputs, sim_state, drain, issues, scanned_at, checkpoint_at, storage, esi_modified, esi_expires, product_chain, is_hybrid, ext_heads)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT (character_id, planet_id) DO UPDATE SET
                      planet_type=EXCLUDED.planet_type, solar_system_id=EXCLUDED.solar_system_id,
                      upgrade_level=EXCLUDED.upgrade_level, num_pins=EXCLUDED.num_pins,
                      is_extractor=EXCLUDED.is_extractor, p0_type_id=EXCLUDED.p0_type_id,
                      p0_name=EXCLUDED.p0_name, planet_num=EXCLUDED.planet_num,
                      products=EXCLUDED.products, pad_contents=EXCLUDED.pad_contents,
                      pad_inputs=EXCLUDED.pad_inputs, sim_state=EXCLUDED.sim_state,
                      drain=EXCLUDED.drain, issues=EXCLUDED.issues, scanned_at=EXCLUDED.scanned_at,
                      checkpoint_at=EXCLUDED.checkpoint_at, storage=EXCLUDED.storage,
                      esi_modified=EXCLUDED.esi_modified, esi_expires=EXCLUDED.esi_expires,
                      product_chain=EXCLUDED.product_chain, is_hybrid=EXCLUDED.is_hybrid,
                      ext_heads=EXCLUDED.ext_heads
                """, (character_id, planet_id, planet_type, solar_system_id,
                      upgrade_level, num_pins, is_extractor, p0_type_id, p0_name,
                      planet_num, products_json, pads_json, pad_inputs_json, sim_state_json, drain_json, issues_json, _scan_ts, checkpoint_at, storage_json, esi_modified, esi_expires, product_chain_json, is_hybrid, ext_heads_json))

                if is_extractor and _sim:      # log a per-program yield sample for the trend/burn-down
                    _record_yield_sample(con, character_id, planet_id, _sim, _scan_ts,
                                         _heads_centroid(ext_heads))

        con.commit()
        if _skip_cached:
            log.info("planet scan char=%s fetched=%d skipped(cached)=%d failed=%d",
                     character_id, _fetched, _skipped, _failed)
        return {"fetched": _fetched, "skipped": _skipped, "failed": _failed}
    except Exception:
        return {"fetched": _fetched, "skipped": _skipped, "failed": _failed}
    finally:
        if con is not None:
            con.close()


# ── OAuth endpoints ───────────────────────────────────────────────────────────

@router.get("/auth/login")
def esi_login(wallet: int = 0, reactions: int = 0, market: int = 0, industry: int = 0,
              director: int = 0, pp_session: str = Cookie(default=None)):
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
    scope_enc = (
        # Director first: it is the superset of the superset, and it is only ever reached by
        # someone who explicitly asked to connect a character for corp hangars.
        DIRECTOR_SCOPES if director else
        REACTIONS_SCOPES if (market or reactions) else
        INDUSTRY_SCOPES if industry else
        WALLET_SCOPES if wallet else
        SCOPES
    ).replace(" ", "%20")
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

    # Exchange code for tokens. Deliberately NOT through esi_http: login.eveonline.com is the SSO
    # service, not ESI — it sends no error-limit headers, and inheriting ESI's backoff would block
    # logins for up to 90s whenever the ESI budget happened to be low. Only the User-Agent is shared.
    with httpx.Client(headers={"User-Agent": esi_http.USER_AGENT}) as client:
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
    alliance_id = _fetch_alliance_id(character_id)

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

    # Only overwrite skill columns when the ESI skills fetch actually returned data. A transient
    # ESI failure returns {} (see _fetch_skills) — writing that would zero a character's trained
    # skills and silently drop their reaction slots / PI skill bonuses until the next good rescan.
    # On a re-auth we therefore preserve the previously-stored skills; on a genuinely-new add with
    # a failed fetch there's nothing to preserve, so it defaults to 0 (corrected on next rescan).
    # This mirrors the `if skills:` guard the rescan path already uses (app.esi_data).
    if not skills:
        _SKILL_COLS = ("interplanetary_consolidation", "command_center_upgrades", "planetology",
                       "advanced_planetology", "mass_reactions", "advanced_mass_reactions",
                       "mass_production", "advanced_mass_production", "industry", "advanced_industry")
        prev = con.execute(
            f"SELECT {', '.join(_SKILL_COLS)} FROM pp_characters WHERE character_id=?",
            (character_id,),
        ).fetchone()
        if prev:
            skills = {c: prev[c] for c in _SKILL_COLS}

    con.execute("""
        INSERT INTO pp_characters
            (character_id, character_name, access_token, refresh_token, token_expiry,
             interplanetary_consolidation, command_center_upgrades, planetology,
             advanced_planetology, mass_reactions, advanced_mass_reactions,
             mass_production, advanced_mass_production, industry, advanced_industry, alliance_id,
             context_id, scopes)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT (character_id) DO UPDATE SET
          character_name=EXCLUDED.character_name, access_token=EXCLUDED.access_token,
          refresh_token=EXCLUDED.refresh_token, token_expiry=EXCLUDED.token_expiry,
          interplanetary_consolidation=EXCLUDED.interplanetary_consolidation,
          command_center_upgrades=EXCLUDED.command_center_upgrades,
          planetology=EXCLUDED.planetology, advanced_planetology=EXCLUDED.advanced_planetology,
          mass_reactions=EXCLUDED.mass_reactions,
          advanced_mass_reactions=EXCLUDED.advanced_mass_reactions,
          mass_production=EXCLUDED.mass_production,
          advanced_mass_production=EXCLUDED.advanced_mass_production,
          industry=EXCLUDED.industry, advanced_industry=EXCLUDED.advanced_industry,
          alliance_id=COALESCE(EXCLUDED.alliance_id, pp_characters.alliance_id),
          context_id=EXCLUDED.context_id, scopes=EXCLUDED.scopes
    """, (
        character_id, character_name, access_token, refresh_token, expiry,
        skills.get("interplanetary_consolidation", 0),
        skills.get("command_center_upgrades", 0),
        skills.get("planetology", 0),
        skills.get("advanced_planetology", 0),
        skills.get("mass_reactions", 0),
        skills.get("advanced_mass_reactions", 0),
        skills.get("mass_production", 0),
        skills.get("advanced_mass_production", 0),
        skills.get("industry", 0),
        skills.get("advanced_industry", 0),
        alliance_id,
        context_id, scopes_str,
    ))
    con.commit()
    con.close()
    _fetch_planets(character_id, access_token)
    cache_invalidate(charlist_key(context_id))

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


def _table_exists(con, table: str) -> bool:
    """Portable table probe, shared by the two deletion paths (disconnect a character, delete an
    account). Several tables they touch belong to modules (industry, reactions, markets) whose
    ensure_* may not have run in this process yet, and on Postgres a statement against a missing
    table aborts the WHOLE transaction — rolling back the deletes that already succeeded and
    failing the whole operation. Skipping absent tables is the difference between "cleaned
    everything that exists" and "cleaned nothing". app.db._pg_translate rewrites this
    sqlite_master probe into the information_schema equivalent."""
    return bool(con.execute(
        f"SELECT 1 FROM sqlite_master WHERE type='table' AND name='{table}'"
    ).fetchone())


# Per-character operational data, deleted wholesale when a character is disconnected. These are
# all "this character's own working state" — re-created by a rescan if the character is ever
# re-added. Every one is keyed by character_id alone (no context column), which is safe ONLY
# because we verify the character belongs to the caller's context first; without that check this
# list would let any logged-in user wipe any character's rows by guessing an id.
_CHAR_OWNED_TABLES = [
    "pp_char_planets",
    "pp_char_assets",
    "pp_char_blueprints",
    "pp_char_industry_jobs",
    "pp_char_manufacturing_jobs",
    "pp_char_formula_jobs",
    "pp_colony_flags",
    "pp_colony_yield",
    "pp_plan_config",
    "pp_reaction_assignments",
]

# Deliberately NOT deleted when a single CHARACTER is disconnected, and why:
#   pp_bugs                  — a support record for admins, not the character's data; it already
#                              denormalises character_name, so it stays readable once the row is gone.
#   pp_industry_completions  — the ACCOUNT's earnings ledger. character_id is provenance, not
#   pp_reaction_completions    ownership; deleting these would silently rewrite the account's
#                              historical profit, which is not what "disconnect a character" asks for.
# Deleting the whole ACCOUNT is the opposite case — see delete_account() below, where the account
# they belong to is itself going away and all three ARE cleared.

# Everything else keyed to a context_id, cleared when the whole account is deleted. Kept as an
# explicit list rather than "every table with a context_id column" so that adding a table is a
# deliberate decision about whether an account deletion should take it — a reflective sweep would
# silently start deleting a future table nobody considered.
_CONTEXT_OWNED_TABLES = [
    "pp_plan_snapshots", "pp_plan_baseline", "pp_profiles",
    "pp_alert_settings", "pp_notification_prefs", "pp_notification_settings", "pp_notification_log",
    "pp_market_config", "pp_asset_sources", "pp_asset_stock", "pp_source_sets",
    "pp_industry_settings", "pp_industry_orders", "pp_industry_shares", "pp_industry_completions",
    "pp_industry_manual_done", "pp_industry_sourced", "pp_industry_blueprints",
    "pp_account_reaction_settings", "pp_reaction_orders", "pp_reaction_completions",
    "pp_planet_submissions", "pp_oauth_pending",
]

# Context-scoped data NOT covered by the list above:
#   pp_locations               — global id→name cache for stations/structures, keyed by nothing but
#                                the location id. It holds no account's data (a station name is a
#                                property of New Eden), so there is nothing here to delete.
#   pp_baskets/pp_basket_items — handled separately (needs the basket ids to cascade).
#   pp_markets                 — keyed (owner_kind, owner_id); only the 'account' rows are ours.
#   pp_bugs                    — anonymised rather than deleted (see delete_account).
#   pp_shares, pp_inventory_shares — no owner column exists at all; a share is an opaque id with a
#                                payload, unattributable to the account that created it. Nothing to
#                                delete by context here, by construction.
#   pp_reaction_settings, pp_group_* — group-scoped, not account-scoped; shared with other members.


@router.delete("/api/me")
def delete_account(context_id: int = Depends(require_context)):
    """Permanently delete all data for the calling user's account.
    Runs in a single transaction; invalidates all sessions on success.

    This used to clear three per-character tables and four context tables, orphaning rows in
    roughly twenty others — on the endpoint whose entire promise is "delete all my data". The
    lists it works from (`_CHAR_OWNED_TABLES`, `_CONTEXT_OWNED_TABLES`) are shared with the
    per-character disconnect so the two can't drift; the comments above them record what is
    deliberately excluded and why.

    Unlike disconnecting a single character, the earnings ledgers and per-character work records
    DO go here — the account they belonged to is the thing being deleted.
    """
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in
                    con.execute("SELECT character_id FROM pp_characters WHERE context_id=?",
                                (context_id,)).fetchall()]
        if char_ids:
            ph = ",".join("?" * len(char_ids))
            for table in _CHAR_OWNED_TABLES:
                if _table_exists(con, table):
                    con.execute(f"DELETE FROM {table} WHERE character_id IN ({ph})", char_ids)

        for table in _CONTEXT_OWNED_TABLES:
            if _table_exists(con, table):
                con.execute(f"DELETE FROM {table} WHERE context_id=?", (context_id,))

        # Private baskets (context_id IS NOT NULL = owned by this context)
        basket_ids = [r["id"] for r in
                      con.execute("SELECT id FROM pp_baskets WHERE context_id=?",
                                  (context_id,)).fetchall()]
        if basket_ids:
            ph = ",".join("?" * len(basket_ids))
            con.execute(f"DELETE FROM pp_basket_items WHERE basket_id IN ({ph})", basket_ids)
        con.execute("DELETE FROM pp_baskets WHERE context_id=?", (context_id,))

        # Followed markets are keyed (owner_kind, owner_id) — delete only this account's own list,
        # never the group-level defaults it may have been seeded from (those belong to the group).
        if _table_exists(con, "pp_markets"):
            con.execute("DELETE FROM pp_markets WHERE owner_kind='account' AND owner_id=?",
                        (context_id,))

        # Bug reports are ANONYMISED, not deleted. The report is about the app, not the reporter —
        # admins still need open bugs triaged after someone leaves — but nothing identifying the
        # deleted account may survive, so the context/character link and the name all go.
        if _table_exists(con, "pp_bugs"):
            con.execute(
                "UPDATE pp_bugs SET context_id=NULL, character_id=NULL, character_name=? "
                "WHERE context_id=?",
                ("(deleted account)", context_id),
            )

        if char_ids:
            ph = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_characters WHERE character_id IN ({ph})", char_ids)

        con.execute("DELETE FROM pp_sessions WHERE context_id=?", (context_id,))
        con.execute("DELETE FROM pp_user_contexts WHERE id=?", (context_id,))
        con.commit()
    except Exception:
        con.rollback()
        con.close()
        log.exception("account deletion failed for context %s", context_id)
        raise HTTPException(status_code=500, detail="Deletion failed — no data was changed")

    con.close()
    _invalidate_context_sessions(context_id)
    # The character list is cached per context; a deleted account must not keep serving one.
    try:
        from app.cache import cache_invalidate, charlist_key
        cache_invalidate(charlist_key(context_id))
    except Exception:
        pass
    # Recorded LAST, after the account rows are gone. `_actor_name` therefore resolves to "" — which
    # is correct and is why the audit row stores a name rather than joining one: the context_id is
    # all that is left, and a row saying "account 412 deleted itself" is the useful record.
    from app.audit import record, ACCOUNT_SCOPE
    record("account.delete", scope=ACCOUNT_SCOPE, context_id=context_id,
           target=f"context {context_id}", affected=1,
           detail="account deleted every table keyed to it")
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
    if sess:
        names = _context_character_names(sess[1])
        admin_names = ADMIN_CHARACTERS | _db_admin_names()
        if any(n in admin_names for n in names):
            return sess[1]
        # A LOGGED-IN account reaching for an admin endpoint is the interesting case, and the only
        # one recorded: anonymous traffic probing the same paths is constant internet background
        # noise that would bury it. Recorded after the check has already failed — this never
        # decides anything, it only remembers.
        try:
            from app.audit import record_denied
            record_denied("access.denied.admin", context_id=sess[1],
                          target="admin endpoint",
                          detail="logged-in non-admin was refused an admin-only endpoint")
        except Exception:
            pass
    raise HTTPException(status_code=403, detail="Admin access required")


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
    fetch — for callers that need both (e.g. list_features), this avoids the duplicate
    session-lookup + character-name query that calling is_admin() and is_tester() separately
    would otherwise cost."""
    sess = _session_lookup(pp_session)
    if not sess:
        return False, False
    return admin_and_tester_status_for_context(sess[1])


def admin_and_tester_status_for_context(context_id: int | None) -> tuple[bool, bool]:
    """Same as admin_and_tester_status(), but for a caller that has already resolved
    context_id from its own session lookup (e.g. list_characters) — skips repeating it."""
    if not context_id:
        return False, False
    from app.cache import request_memo
    return request_memo(("admin_tester_status", int(context_id)),
                        lambda: _admin_and_tester_status_for_context_uncached(int(context_id)))


def _admin_and_tester_status_for_context_uncached(context_id: int) -> tuple[bool, bool]:
    """Read each role table once; character count must not multiply identical queries."""
    names = _context_character_names(context_id)
    admin_names = ADMIN_CHARACTERS | _db_admin_names()
    admin = any(n in admin_names for n in names)
    if admin:
        return True, True
    tester_names = _db_tester_names()
    tester = any(n in tester_names for n in names)
    return False, tester


# ── ESI helpers ───────────────────────────────────────────────────────────────

def _http_date_to_epoch(value: str | None) -> float | None:
    """Parse an HTTP-date header (Last-Modified / Expires) to a Unix epoch, or None."""
    if not value:
        return None
    try:
        from email.utils import parsedate_to_datetime
        return parsedate_to_datetime(value).timestamp()
    except Exception:
        return None


def _fetch_skills(character_id: int, access_token: str) -> dict[str, int]:
    try:
        resp = esi_http.get(f"characters/{character_id}/skills/", token=access_token, timeout=10)
        resp.raise_for_status()
        skill_data = resp.json()
        result: dict[str, int] = {}
        raw: dict[int, int] = {}
        for s in skill_data.get("skills", []):
            raw[s["skill_id"]] = s.get("trained_skill_level", 0)
            field = SKILL_IDS.get(s["skill_id"])
            if field:
                result[field] = s.get("trained_skill_level", 0)
        # ESI already returned the character's ENTIRE skill list here and we were keeping ~10 of
        # them; the Industry required-skills check needs the rest. Stored (and only stored) while
        # its feature flag is on — see app/industry/skills.py. Never allowed to fail the fetch:
        # this is a side-effect of a call whose real job is the PI skills above.
        try:
            from app.industry.skills import store_character_skills
            store_character_skills(character_id, raw)
        except Exception:
            log.exception("storing full skill list failed for %s", character_id)
        # Record when ESI will next regenerate this character's skills, so a rescan can skip
        # re-fetching skills it already knows are still cache-fresh (see esi_cache_skip).
        # try/finally is load-bearing here: a connection obtained but not close()'d never goes
        # back to the (small, 8-per-pod) Postgres pool — an exception between get_connection()
        # and close() permanently leaks it, and this whole function is wrapped in a blanket
        # except below that would otherwise swallow the leak silently.
        expires = _http_date_to_epoch(resp.headers.get("expires"))
        if expires:
            con = get_connection()
            try:
                con.execute("UPDATE pp_characters SET skills_expires=? WHERE character_id=?",
                            (expires, character_id))
                con.commit()
            finally:
                con.close()
        return result
    except Exception:
        return {}



def _fetch_alliance_id(character_id: int) -> int | None:
    """GET /characters/{id}/ is a PUBLIC ESI endpoint (no token needed) that returns alliance_id
    directly when the character's corp is in an alliance. Best-effort — a failure here must
    never block login/rescan, just leaves alliance_id unchanged."""
    try:
        resp = esi_http.get(f"characters/{character_id}/", timeout=10)
        resp.raise_for_status()
        return resp.json().get("alliance_id")
    except Exception:
        return None


def revoke_refresh_token(refresh_token: str | None) -> bool:
    """Best-effort: tell EVE SSO to invalidate this refresh token. Returns True if CCP accepted.

    Deleting our stored copy already stops THIS app from using the token, so nothing in the
    disconnect flow depends on this succeeding — but a token we merely forgot is still live at
    CCP until it expires, and "disconnect this character" should mean the grant is actually
    gone, not just invisible to us. Never raises: a disconnect must not fail because SSO is
    having a bad day, and the user's data is deleted either way."""
    if not refresh_token or not (CLIENT_ID and CLIENT_SECRET):
        return False
    try:
        with httpx.Client(headers={"User-Agent": esi_http.USER_AGENT}) as client:
            resp = client.post(
                EVE_REVOKE_URL,
                data={"token_type_hint": "refresh_token", "token": refresh_token},
                auth=(CLIENT_ID, CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=5,
            )
        return resp.status_code < 400
    except Exception:
        return False


def _refresh_token(character_id: int, refresh_token: str) -> str | None:
    """Exchange refresh token for new access token. Returns new access token or None.

    A 400 from EVE SSO here means the refresh token itself is dead (revoked, or rotated out by
    a use elsewhere — refresh tokens are single-use/rotating) — permanent, needs re-login. Any
    other failure (timeout, network, 5xx) is transient. These used to be indistinguishable: both
    just returned None and left the DB's refresh_token in place, so `token_ok` (which only checks
    whether a refresh_token is stored, not whether it still works) stayed green forever even for
    a permanently dead character — the red dot never caught up to reality."""
    try:
        # Plain httpx, not esi_http — SSO is not ESI (see the token exchange in esi_callback).
        with httpx.Client(headers={"User-Agent": esi_http.USER_AGENT}) as client:
            resp = client.post(
                EVE_TOKEN_URL,
                data={"grant_type": "refresh_token", "refresh_token": refresh_token},
                auth=(CLIENT_ID, CLIENT_SECRET),
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=10,
            )
        if resp.status_code == 400:
            con = get_connection()
            # EMPTY STRING, not NULL: the column is `TEXT NOT NULL DEFAULT ''` (see the CREATE
            # above), so writing NULL raised IntegrityError, was swallowed by this function's outer
            # `except Exception`, and left the dead token in place — which is precisely the bug the
            # docstring above says was fixed. It was not: `token_ok` stayed green for every
            # permanently dead character, and the red dot never caught up after all. Both readers
            # (`token_ok` in app/esi_data.py, the alert rescan's filter in app/notifications.py)
            # test falsiness, so '' and NULL mean the same thing to everything that asks.
            con.execute(
                "UPDATE pp_characters SET refresh_token='' WHERE character_id=?",
                (character_id,),
            )
            con.commit()
            con.close()
            return None
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
