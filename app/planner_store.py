"""Persistence for everything the PI planner stores on a user's behalf — the CRUD half of what
used to be one 4,400-line app/planner.py.

Five self-contained groups, none of which the planning algorithm calls into (verified: they
reference nothing else in planner.py, which is why they could move as a unit):

    plan-config   per-character roles for a product (planet/extractor limits, CCU override)
    pp-shares     server-stored shared plans behind the /s/<id> links
    profiles      saved plan INPUTS, re-runnable
    snapshots     saved plan OUTPUTS (the factory→P1 distribution the refill tool splits stacks by)
    colony-flags  per-colony "this one is tapped out" marks used by the redeploy advice

They live here so planner.py is the planning ALGORITHM and little else. This module owns its own
APIRouter, mounted by app.main alongside the planner's; planner.py imports the few names it still
needs (ensure_share_table, _flagged_colonies) from here, never the other way round — keeping the
dependency one-directional and cycle-free.
"""

import json as _json
import os as _os
import secrets as _secrets
import time as _time

from fastapi import APIRouter, Body, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, add_columns
from app.esi import require_context, session_context_id, PI_CHAR_SQL, natural_name_key
from app.planner_models import (
    SaveConfigRequest, PlanShareSave, ProfileSave, PlanSnapshotSave,
)
from app.planner_serialization import (
    _anonymize_share_payload, _fleet_fingerprint, _plan_staleness,
    _norm_dist_mode, _norm_split_mode,
)

router = APIRouter()

# ── DB setup ──────────────────────────────────────────────────────────────────

@ensure_once
def ensure_plan_tables():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_plan_config (
            character_id    INTEGER NOT NULL,
            product_type_id INTEGER NOT NULL,
            planet_limit    INTEGER,
            factory_only    INTEGER DEFAULT 0,
            extractor_limit INTEGER,
            PRIMARY KEY (character_id, product_type_id)
        )
    """)
    con.commit()
    add_columns(con, "pp_plan_config",
                "extractor_limit INTEGER",
                "ccu INTEGER")   # per-character command-centre override (NULL = use the ESI value)
    con.execute("""
        UPDATE pp_plan_config SET extractor_limit = 0
        WHERE factory_only = 1 AND extractor_limit IS NULL
    """)
    con.commit()
    con.close()


# ── Config endpoints ──────────────────────────────────────────────────────────

@router.get("/api/plan-config/{type_id}")
def get_plan_config(type_id: int, context_id: int = Depends(require_context)):
    ensure_plan_tables()
    con = get_connection()
    char_rows = con.execute(f"""
        SELECT character_id, character_name,
               1 + interplanetary_consolidation AS max_planets,
               command_center_upgrades AS esi_ccu
        FROM pp_characters WHERE context_id=?
              {PI_CHAR_SQL}
    """, (context_id,)).fetchall()
    char_rows = sorted(char_rows, key=lambda r: natural_name_key(r["character_name"]))
    saved = {
        r["character_id"]: dict(r)
        for r in con.execute(
            "SELECT character_id, planet_limit, extractor_limit, ccu FROM pp_plan_config WHERE product_type_id=?",
            (type_id,),
        ).fetchall()
    }
    con.close()
    return {"configs": [
        {
            "character_id":    c["character_id"],
            "character_name":  c["character_name"],
            "max_planets":     c["max_planets"],
            "esi_ccu":         c["esi_ccu"],
            "planet_limit":    saved.get(c["character_id"], {}).get("planet_limit"),
            "extractor_limit": saved.get(c["character_id"], {}).get("extractor_limit"),
            "ccu":             saved.get(c["character_id"], {}).get("ccu"),
        }
        for c in char_rows
    ]}


@router.post("/api/plan-config/{type_id}")
def save_plan_config(type_id: int, req: SaveConfigRequest):
    ensure_plan_tables()
    con = get_connection()
    for e in req.configs:
        if e.planet_limit is None and e.extractor_limit is None and e.ccu is None:
            con.execute(
                "DELETE FROM pp_plan_config WHERE character_id=? AND product_type_id=?",
                (e.character_id, type_id),
            )
        else:
            con.execute("""
                INSERT INTO pp_plan_config
                    (character_id, product_type_id, planet_limit, extractor_limit, factory_only, ccu)
                VALUES (?,?,?,?,?,?)
                ON CONFLICT (character_id, product_type_id) DO UPDATE SET
                  planet_limit=EXCLUDED.planet_limit, extractor_limit=EXCLUDED.extractor_limit,
                  factory_only=EXCLUDED.factory_only, ccu=EXCLUDED.ccu
            """, (
                e.character_id, type_id, e.planet_limit, e.extractor_limit,
                1 if e.extractor_limit == 0 else 0, e.ccu,
            ))
    con.commit()
    con.close()
    return {"ok": True}

# ── Profiles / shares ─────────────────────────────────────────────────────────


@ensure_once
def ensure_share_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_shares (
            id            TEXT PRIMARY KEY,
            payload       TEXT NOT NULL,
            created_at    TEXT DEFAULT CURRENT_TIMESTAMP,
            last_accessed TEXT
        )
    """)
    # Migration: add last_accessed to existing tables that predate this column.
    cols = {r["name"] for r in con.execute("PRAGMA table_info(pp_shares)")}
    if "last_accessed" not in cols:
        con.execute("ALTER TABLE pp_shares ADD COLUMN last_accessed TEXT")
    con.commit()
    con.close()


@router.post("/api/pp-shares")
def save_plan_share(req: PlanShareSave):
    ensure_share_table()
    share_id = _secrets.token_urlsafe(6)
    payload = _anonymize_share_payload(req.payload) if req.anonymize else req.payload
    con = get_connection()
    con.execute("INSERT INTO pp_shares (id, payload) VALUES (?, ?)",
                (share_id, _json.dumps(payload)))
    con.commit()
    con.close()
    return {"id": share_id}


@router.get("/api/pp-shares/{share_id}")
def load_plan_share(share_id: str):
    ensure_share_table()
    con = get_connection()
    row = con.execute("SELECT payload FROM pp_shares WHERE id=?", (share_id,)).fetchone()
    if not row:
        con.close()
        raise HTTPException(status_code=404, detail="Share not found")
    try:
        con.execute(
            "UPDATE pp_shares SET last_accessed=datetime('now') WHERE id=?",
            (share_id,),
        )
        con.commit()
    except Exception:
        pass
    con.close()
    return {"payload": _json.loads(row["payload"])}


@ensure_once
def ensure_profile_tables():
    con = get_connection()
    cols = [r["name"] for r in con.execute("PRAGMA table_info(pp_profiles)").fetchall()]
    if not cols:
        # Fresh DB — create directly, no old data to migrate.
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_profiles (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id        INTEGER NOT NULL DEFAULT 1,
                name              TEXT    NOT NULL,
                type_id           INTEGER NOT NULL,
                type_name         TEXT    NOT NULL DEFAULT '',
                factories         INTEGER NOT NULL DEFAULT 15,
                preferred_systems INTEGER NOT NULL DEFAULT 1,
                constellations    TEXT    NOT NULL DEFAULT '[]',
                use_existing      INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(context_id, name)
            )
        """)
        con.commit()
    elif "context_id" not in cols:
        # Old schema without context_id — migrate via rename.
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_profiles_new (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id        INTEGER NOT NULL DEFAULT 1,
                name              TEXT    NOT NULL,
                type_id           INTEGER NOT NULL,
                type_name         TEXT    NOT NULL DEFAULT '',
                factories         INTEGER NOT NULL DEFAULT 15,
                preferred_systems INTEGER NOT NULL DEFAULT 1,
                constellations    TEXT    NOT NULL DEFAULT '[]',
                use_existing      INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(context_id, name)
            )
        """)
        con.commit()
        try:
            con.execute("""
                INSERT INTO pp_profiles_new
                    (id, context_id, name, type_id, type_name, factories,
                     preferred_systems, constellations, created_at)
                SELECT id, 1, name, type_id, type_name, factories,
                       preferred_systems, constellations, created_at
                FROM pp_profiles
            """)
            con.execute("DROP TABLE pp_profiles")
            con.commit()
        except Exception:
            pass
        con.execute("ALTER TABLE pp_profiles_new RENAME TO pp_profiles")
        con.commit()
    else:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_profiles (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id        INTEGER NOT NULL DEFAULT 1,
                name              TEXT    NOT NULL,
                type_id           INTEGER NOT NULL,
                type_name         TEXT    NOT NULL DEFAULT '',
                factories         INTEGER NOT NULL DEFAULT 15,
                preferred_systems INTEGER NOT NULL DEFAULT 1,
                constellations    TEXT    NOT NULL DEFAULT '[]',
                use_existing      INTEGER NOT NULL DEFAULT 1,
                created_at        TEXT    DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(context_id, name)
            )
        """)
        con.commit()
        for col, defval in [
            ("use_existing",           "INTEGER NOT NULL DEFAULT 1"),
            ("factory_system",         "TEXT NOT NULL DEFAULT ''"),
            ("overproduction_pct",     "INTEGER NOT NULL DEFAULT 20"),
            ("factory_output_per_hour","REAL"),
            ("factory_character_ids",  "TEXT NOT NULL DEFAULT '[]'"),
            ("max_jumps",              "INTEGER NOT NULL DEFAULT 1"),
            ("factory_planet_types",   "TEXT NOT NULL DEFAULT '[]'"),
            ("split_mode",             "TEXT NOT NULL DEFAULT 'off'"),
            ("distribution_mode",      "TEXT NOT NULL DEFAULT 'stability'"),
            ("min_density_pct",        "INTEGER NOT NULL DEFAULT 0"),
            ("fleet_json",             "TEXT"),   # {char_id: [ccu, ic]} at save → staleness flag
        ]:
            if col not in cols:
                add_columns(con, "pp_profiles", f"{col} {defval}")
    # Fleet skill baseline captured when a plan was last saved (one row per context). Lets the
    # dashboard flag "characters trained up since you planned" without storing skill history.
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_plan_baseline (
            context_id  INTEGER PRIMARY KEY,
            skills_json TEXT,        -- {char_id: [ccu, interplanetary_consolidation]} at save time
            plan_name   TEXT,
            saved_at    REAL
        )
    """)
    con.commit()
    con.close()


@router.get("/api/profiles")
def list_profiles(pp_session: str = Cookie(default=None)):
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"profiles": []}
    ensure_profile_tables()
    con = get_connection()
    rows = con.execute(
        "SELECT id, name, type_id, type_name, overproduction_pct, preferred_systems, "
        "constellations, use_existing, factory_system, factory_output_per_hour, factory_character_ids, max_jumps, "
        "factory_planet_types, split_mode, distribution_mode, min_density_pct, fleet_json "
        "FROM pp_profiles WHERE context_id=? ORDER BY name",
        (context_id,),
    ).fetchall()
    current = _fleet_fingerprint(con, context_id)
    con.close()
    out = []
    for r in rows:
        d = dict(r)
        try:
            saved_fleet = _json.loads(d.pop("fleet_json") or "null") or {}
        except Exception:
            saved_fleet = {}
        d.update({
            "constellations":          _json.loads(r["constellations"] or "[]"),
            "use_existing":            bool(r["use_existing"]),
            "factory_system":          r["factory_system"] or "",
            "factory_output_per_hour": r["factory_output_per_hour"],
            "factory_character_ids":   _json.loads(r["factory_character_ids"] or "[]"),
            "factory_planet_types":    _json.loads(r["factory_planet_types"] or "[]"),
            "split_mode":              r["split_mode"] or "off",
            "distribution_mode":       r["distribution_mode"] or "stability",
            "min_density_pct":         r["min_density_pct"] or 0,
            **_plan_staleness(saved_fleet, current),
        })
        out.append(d)
    return {"profiles": out}


@router.post("/api/profiles")
def save_profile(req: ProfileSave, context_id: int = Depends(require_context)):
    ensure_profile_tables()
    con = get_connection()
    fleet = _fleet_fingerprint(con, context_id)   # the fleet+skills this plan was built against
    con.execute("""
        INSERT INTO pp_profiles
            (context_id, name, type_id, type_name, overproduction_pct, preferred_systems,
             constellations, use_existing, factory_system, factory_output_per_hour,
             factory_character_ids, max_jumps, factory_planet_types, split_mode, distribution_mode,
             min_density_pct, fleet_json)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT (context_id, name) DO UPDATE SET
          type_id=EXCLUDED.type_id, type_name=EXCLUDED.type_name,
          overproduction_pct=EXCLUDED.overproduction_pct,
          preferred_systems=EXCLUDED.preferred_systems, constellations=EXCLUDED.constellations,
          use_existing=EXCLUDED.use_existing, factory_system=EXCLUDED.factory_system,
          factory_output_per_hour=EXCLUDED.factory_output_per_hour,
          factory_character_ids=EXCLUDED.factory_character_ids, max_jumps=EXCLUDED.max_jumps,
          factory_planet_types=EXCLUDED.factory_planet_types, split_mode=EXCLUDED.split_mode,
          distribution_mode=EXCLUDED.distribution_mode, min_density_pct=EXCLUDED.min_density_pct,
          fleet_json=EXCLUDED.fleet_json
    """, (context_id, req.name, req.type_id, req.type_name, req.overproduction_pct,
          req.preferred_systems, _json.dumps(req.constellations),
          1 if req.use_existing else 0, req.factory_system or "",
          req.factory_output_per_hour, _json.dumps(req.factory_character_ids), req.max_jumps,
          _json.dumps(req.factory_planet_types), _norm_split_mode(req.split_mode),
          _norm_dist_mode(req.distribution_mode), max(0, min(100, int(req.min_density_pct or 0))),
          _json.dumps(fleet)))
    # Same fingerprint feeds the dashboard "trained up since you planned" nudge (per-context baseline).
    con.execute(
        "INSERT INTO pp_plan_baseline (context_id, skills_json, plan_name, saved_at) VALUES (?,?,?,?)"
        " ON CONFLICT (context_id) DO UPDATE SET skills_json=EXCLUDED.skills_json,"
        " plan_name=EXCLUDED.plan_name, saved_at=EXCLUDED.saved_at",
        (context_id, _json.dumps(fleet), req.name, _time.time()))
    con.commit()
    con.close()
    return {"ok": True}


@router.delete("/api/profiles/{profile_id}")
def delete_profile(profile_id: int, context_id: int = Depends(require_context)):
    ensure_profile_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_profiles WHERE id=? AND context_id=?", (profile_id, context_id))
    con.commit()
    con.close()
    return {"ok": True}

# ── Saved plan snapshots (for the PI Planner refill distribution tool) ─────────
# A snapshot is the computed plan's factory→P1 distribution, stored per context so a player
# can split stacks into their factories without re-running the wizard. Stored server-side
# (cross-device) and named; the frontend also keeps a localStorage copy of the last build.

@ensure_once
def ensure_plan_snapshot_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_plan_snapshots (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id INTEGER NOT NULL,
            name       TEXT NOT NULL,
            snapshot   TEXT NOT NULL,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(context_id, name)
        )
    """)
    con.commit()
    con.close()


@router.post("/api/plan-snapshots")
def save_plan_snapshot(req: PlanSnapshotSave, context_id: int = Depends(require_context)):
    from datetime import datetime, timezone
    name = (req.name or "").strip()[:80]
    if not name:
        raise HTTPException(status_code=400, detail="Plan name is required")
    ensure_plan_snapshot_table()
    con = get_connection()
    snap = dict(req.snapshot) if isinstance(req.snapshot, dict) else {"value": req.snapshot}
    snap["fleet"] = _fleet_fingerprint(con, context_id)   # fleet+skills this plan was built against → stale flag
    con.execute(
        "INSERT INTO pp_plan_snapshots (context_id, name, snapshot, created_at) "
        "VALUES (?,?,?,?)"
        " ON CONFLICT (context_id, name) DO UPDATE SET"
        " snapshot=EXCLUDED.snapshot, created_at=EXCLUDED.created_at",
        (context_id, name, _json.dumps(snap), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.get("/api/plan-snapshots")
def list_plan_snapshots(pp_session: str = Cookie(default=None)):
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"snapshots": []}
    ensure_plan_snapshot_table()
    con = get_connection()
    rows = con.execute(
        "SELECT id, name, snapshot, created_at FROM pp_plan_snapshots WHERE context_id=? ORDER BY name",
        (context_id,),
    ).fetchall()
    current = _fleet_fingerprint(con, context_id)
    con.close()
    out = []
    for r in rows:
        try:
            snap = _json.loads(r["snapshot"])
        except Exception:
            snap = {}
        out.append({"id": r["id"], "name": r["name"], "created_at": r["created_at"],
                    "factories": snap.get("factories", []),
                    "consumption": snap.get("consumption", {}),
                    "products_per_day": snap.get("products_per_day"),
                    "isk_per_day": snap.get("isk_per_day"),
                    "unit_label": snap.get("unit_label", "units"),
                    "factory_refill_hours": snap.get("factory_refill_hours"),
                    "factories_count": snap.get("factories_count"),
                    "has_payload": bool(snap.get("payload")),
                    **_plan_staleness(snap.get("fleet") or {}, current)})
    return {"snapshots": out}


@router.get("/api/plan-snapshots/{snap_id}")
def get_plan_snapshot(snap_id: int, pp_session: str = Cookie(default=None)):
    """Return one snapshot's stored full-plan payload (for reopening the whole plan view).
    Kept out of the list response so listing stays light."""
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"payload": None}
    ensure_plan_snapshot_table()
    con = get_connection()
    row = con.execute(
        "SELECT snapshot FROM pp_plan_snapshots WHERE id=? AND context_id=?",
        (snap_id, context_id),
    ).fetchone()
    con.close()
    if not row:
        return {"payload": None}
    try:
        snap = _json.loads(row["snapshot"])
    except Exception:
        snap = {}
    return {"payload": snap.get("payload")}


@router.delete("/api/plan-snapshots/{snap_id}")
def delete_plan_snapshot(snap_id: int, context_id: int = Depends(require_context)):
    ensure_plan_snapshot_table()
    con = get_connection()
    con.execute("DELETE FROM pp_plan_snapshots WHERE id=? AND context_id=?", (snap_id, context_id))
    con.commit()
    con.close()
    return {"ok": True}

@ensure_once
def ensure_colony_flags_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_colony_flags (
            context_id   INTEGER NOT NULL,
            character_id INTEGER NOT NULL,
            planet_id    INTEGER NOT NULL,
            created_at   REAL,
            PRIMARY KEY (context_id, character_id, planet_id)
        )
    """)
    con.commit()
    con.close()


def _flagged_colonies(context_id: int) -> set:
    """(character_id, planet_id) pairs the user has manually marked 'a reseat can't reach the target
    here' — a manual reseat-exhausted mark, so we stop suggesting a reseat and treat it as a redeploy."""
    ensure_colony_flags_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, planet_id FROM pp_colony_flags WHERE context_id=?", (context_id,)
        ).fetchall()
    finally:
        con.close()
    return {(r["character_id"], r["planet_id"]) for r in rows}


@router.get("/api/colony-flags")
def get_colony_flags(context_id: int = Depends(require_context)):
    """The caller's manually-flagged 'reseat can't reach target' colonies (own account only)."""
    return {"flags": [[cid, pid] for cid, pid in sorted(_flagged_colonies(context_id))]}


@router.post("/api/colony-flags")
def set_colony_flag(body: dict = Body(...), context_id: int = Depends(require_context)):
    """Toggle the 'reseat can't reach target' flag on one of the caller's OWN colonies."""
    try:
        character_id = int(body["character_id"]); planet_id = int(body["planet_id"])
    except (KeyError, TypeError, ValueError):
        return {"error": "character_id and planet_id required"}
    flagged = bool(body.get("flagged"))
    ensure_colony_flags_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?", (character_id, context_id)
        ).fetchone()
        if not owner:
            return {"error": "not your character"}
        if flagged:
            con.execute(
                "INSERT INTO pp_colony_flags (context_id, character_id, planet_id, created_at) VALUES (?,?,?,?) "
                "ON CONFLICT (context_id, character_id, planet_id) DO NOTHING",
                (context_id, character_id, planet_id, _time.time()))
        else:
            con.execute("DELETE FROM pp_colony_flags WHERE context_id=? AND character_id=? AND planet_id=?",
                        (context_id, character_id, planet_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True, "flagged": flagged}
