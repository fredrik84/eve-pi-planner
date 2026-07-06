"""
Planetary Planning — planet database and allocation.
"""

import logging
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.cache import cache_get_json, cache_set_json, cache_invalidate
from app.esi import require_context, is_admin, require_admin
from app.notifiers import notify_admin_discord

log = logging.getLogger(__name__)
router = APIRouter()

# P0 resources per planet type, sorted alphabetically.
# Each planet type yields exactly FIVE P0 resources (verified vs EVE University's
# Planetary Commodities table). Each type's 5-set is unique, so a planet's resources
# identify its type unambiguously. Names sorted alphabetically (used for positional import).
PLANET_P0_MAP: dict[str, list[str]] = {
    "Barren":    ["Aqueous Liquids", "Base Metals",    "Carbon Compounds",   "Microorganisms",   "Noble Metals"],
    "Gas":       ["Aqueous Liquids", "Base Metals",    "Ionic Solutions",    "Noble Gas",        "Reactive Gas"],
    "Ice":       ["Aqueous Liquids", "Heavy Metals",   "Microorganisms",     "Noble Gas",        "Planktic Colonies"],
    "Lava":      ["Base Metals",     "Felsic Magma",   "Heavy Metals",       "Non-CS Crystals",  "Suspended Plasma"],
    "Oceanic":   ["Aqueous Liquids", "Carbon Compounds","Complex Organisms", "Microorganisms",   "Planktic Colonies"],
    "Plasma":    ["Base Metals",     "Heavy Metals",   "Noble Metals",       "Non-CS Crystals",  "Suspended Plasma"],
    "Storm":     ["Aqueous Liquids", "Base Metals",    "Ionic Solutions",    "Noble Gas",        "Suspended Plasma"],
    "Temperate": ["Aqueous Liquids", "Autotrophs",     "Carbon Compounds",   "Complex Organisms","Microorganisms"],
}

P0_COLUMNS = [
    "aqueous_liquids", "autotrophs", "base_metals", "carbon_compounds",
    "complex_organisms", "felsic_magma", "heavy_metals", "ionic_solutions",
    "micro_organisms", "noble_gas", "noble_metals", "non_cs_crystals",
    "planktic_colonies", "reactive_gas", "suspended_plasma",
]

# Display name → DB column (handles capitalisation & spacing variants)
_NAME_TO_COL: dict[str, str] = {
    "aqueous liquids":   "aqueous_liquids",
    "aqueous liquid":    "aqueous_liquids",   # singular variant (some exports)
    "autotrophs":        "autotrophs",
    "autotroph":         "autotrophs",
    "base metals":       "base_metals",
    "carbon compounds":  "carbon_compounds",
    "complex organisms": "complex_organisms",
    "felsic magma":      "felsic_magma",
    "heavy metals":      "heavy_metals",
    "ionic solutions":   "ionic_solutions",
    "microorganisms":    "micro_organisms",
    "micro organisms":   "micro_organisms",
    "noble gas":         "noble_gas",
    "noble metals":      "noble_metals",
    "non-cs crystals":   "non_cs_crystals",
    "non cs crystals":   "non_cs_crystals",
    "planktic colonies": "planktic_colonies",
    "reactive gas":      "reactive_gas",
    "suspended plasma":  "suspended_plasma",
}

# Reverse: db col → display name
_COL_TO_NAME: dict[str, str] = {v: k.title() for k, v in _NAME_TO_COL.items() if k == k}


def _p0_col(header: str) -> Optional[str]:
    return _NAME_TO_COL.get(header.strip().lower())


@ensure_once
def ensure_tables():
    p0_ddl = "\n".join(f"    {col} INTEGER NOT NULL DEFAULT 0," for col in P0_COLUMNS)
    con = get_connection()
    con.execute(f"""
        CREATE TABLE IF NOT EXISTS pp_planets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            system TEXT NOT NULL,
            planet_num INTEGER NOT NULL,
            planet_type TEXT NOT NULL,
            constellation TEXT NOT NULL,
            solar_system_id INTEGER,
            {p0_ddl}
            UNIQUE(system, planet_num)
        )
    """)
    # Pending planet-data contributions from non-admins, held for admin review.
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_planet_submissions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            context_id INTEGER NOT NULL,
            submitter_name TEXT NOT NULL DEFAULT '',
            raw_text TEXT NOT NULL,
            planet_count INTEGER NOT NULL DEFAULT 0,
            status TEXT NOT NULL DEFAULT 'pending',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP,
            reviewed_at TEXT
        )
    """)
    con.commit()
    # Add solar_system_id if upgrading from older schema
    try:
        con.execute("ALTER TABLE pp_planets ADD COLUMN solar_system_id INTEGER")
    except Exception:
        pass
    # Populate from solar_systems table (created by SDE build)
    try:
        con.execute("""
            UPDATE pp_planets SET solar_system_id = (
                SELECT system_id FROM solar_systems
                WHERE name = pp_planets.system COLLATE NOCASE
                LIMIT 1
            )
            WHERE solar_system_id IS NULL
        """)
    except Exception:
        pass
    con.commit()
    con.close()


# Planet DB is a single global, shared table (no context_id) written only by admin
# import/approve/clear — a good fit for a single global cache entry serving every visitor,
# invalidated on those writes rather than a bare TTL. No-ops (falls through to the DB) wherever
# REDIS_URL isn't set, same as the /api/characters cache.
_PLANETS_CACHE_KEY = "planetdb:planets"
_CONSTELLATIONS_CACHE_KEY = "planetdb:constellations"


def _invalidate_planetdb_cache():
    cache_invalidate(_PLANETS_CACHE_KEY)
    cache_invalidate(_CONSTELLATIONS_CACHE_KEY)


@router.get("/api/planets")
def list_planets():
    cached = cache_get_json(_PLANETS_CACHE_KEY)
    if cached is not None:
        return cached
    ensure_tables()
    con = get_connection()
    cur = con.cursor()
    cols = ", ".join(["system", "planet_num", "planet_type", "constellation"] + P0_COLUMNS)
    cur.execute(f"SELECT {cols} FROM pp_planets ORDER BY system, planet_num")
    rows = [dict(r) for r in cur.fetchall()]

    # Pooled real-world measured yield (see app/yield_stats.py) — supplementary, sparse, never
    # overwrites the static columns above. Attached only where it exists (system, planet_num, p0).
    from app.yield_stats import ensure_yield_avg_table
    ensure_yield_avg_table()
    measured: dict[tuple[str, int], dict[str, dict]] = {}
    for r in con.execute(
        "SELECT system, planet_num, p0_name, measured_pct, sample_count FROM pp_planet_yield_avg"
    ):
        measured.setdefault((r["system"], r["planet_num"]), {})[r["p0_name"]] = {
            "pct": round(r["measured_pct"]), "n": r["sample_count"],
        }
    con.close()
    for row in rows:
        m = measured.get((row["system"], row["planet_num"]))
        if m:
            row["measured"] = m

    result = {"planets": rows, "p0_columns": P0_COLUMNS, "planet_p0_map": PLANET_P0_MAP}
    cache_set_json(_PLANETS_CACHE_KEY, result, ttl=3600)
    return result


@router.get("/api/pi-products")
def list_pi_products():
    pi_data = load_pi_data()
    products = [
        {"type_id": tid, "name": t["name"], "tier": t["pi_tier"]}
        for tid, t in pi_data["types"].items()
        if t["pi_tier"] in (1, 2, 3, 4)
    ]
    products.sort(key=lambda p: (p["tier"], p["name"]))
    return {"products": products}


class LayoutRequest(BaseModel):
    type_id: int
    planet_type: str = "Barren"
    launchpads: Optional[int] = None   # None → per-tier default (3 factory / 1 extractor)
    count: Optional[int] = None         # production units on the planet; None → max that fits
    cc_level: Optional[int] = None      # command-centre level 1–5; None → CMD_CTR_LEVEL (5)
    no_storage: bool = False            # extractor: buffer in launchpad, no storage hub (frees PG)


@router.post("/api/layout")
def factory_layout(req: LayoutRequest):
    from app.layout import generate_layout
    try:
        return generate_layout(req.type_id, req.planet_type, req.launchpads, req.count,
                               cc_level=req.cc_level, no_storage=req.no_storage)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _safe_filename(name: str) -> str:
    name = name.replace("→", "to")
    return "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip().replace(" ", "_")


@router.get("/api/layout/download")
def download_layout(type_id: int, planet_type: str = "Barren", launchpads: Optional[int] = None,
                    count: int = 1, cc_level: Optional[int] = None, no_storage: int = 0):
    """Return a single planet template as a downloadable .json attachment."""
    import json
    from fastapi import Response
    from app.layout import generate_layout
    try:
        result = generate_layout(type_id, planet_type, launchpads, count, cc_level=cc_level,
                                 no_storage=bool(no_storage))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tmpl = result["planets"][0]["template"]
    fname = _safe_filename(result["planets"][0]["name"]) + ".json"
    return Response(content=json.dumps(tmpl), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/api/layout/bundle")
def download_bundle(type_ids: str = "", expand: int = 0, splits: str = "", no_storage: int = 0):
    """
    Return a ZIP of templates for one or more products (comma-separated `type_ids`).
    Each token is `id[:launchpads[:count[:cc[:planet_type]]]]` — the optional `cc`
    (command-centre level 1–5) and `planet_type` scale the factory template to where it
    actually lands (a lower CC fits fewer facilities; a larger planet → longer links →
    more grid). With expand=1, each product also includes the P0→P1 extractor templates
    for its whole chain. Launchpads default per tier (3 factory / 1 extractor).

    `splits` is a comma-list of two-ECU extractor tokens `p1a:p1b:headsA:headsB[:cc[:ptype]]`
    (from a split-extraction plan) — each becomes one importable split planet template.
    """
    import io, json, zipfile
    from fastapi import Response
    from app.layout import generate_layout, bundle_templates, generate_split_extractor_layout
    tokens = []
    for tok in type_ids.split(","):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split(":")
        tid = int(parts[0])
        lp = int(parts[1]) if len(parts) > 1 and parts[1] else None
        # Blank count → None so generate_layout packs the MAX facilities that fit (a full
        # factory). Defaulting to 1 here produced single-facility "corrupted" templates.
        cnt = int(parts[2]) if len(parts) > 2 and parts[2] else None
        cc = int(parts[3]) if len(parts) > 3 and parts[3] else None
        ptype = parts[4] if len(parts) > 4 and parts[4] else "Barren"
        tokens.append((tid, lp, cnt, cc, ptype))
    split_tokens = []
    for tok in splits.split(","):
        tok = tok.strip()
        if not tok:
            continue
        p = tok.split(":")
        if len(p) < 4:
            continue
        split_tokens.append((
            int(p[0]), int(p[1]), int(p[2]), int(p[3]),
            int(p[4]) if len(p) > 4 and p[4] else None,
            p[5] if len(p) > 5 and p[5] else "Barren",
        ))
    if not tokens and not split_tokens:
        raise HTTPException(status_code=400, detail="no type_ids or splits given")
    items: list[tuple] = []
    seen: set[str] = set()
    try:
        for tid, lp, cnt, cc, ptype in tokens:
            if expand:
                # Scale the whole chain to the host CC: the factory takes the chosen planet
                # type, each P0→P1 extractor keeps the planet type its P0 grows on.
                pairs = bundle_templates(tid, cc_level=cc, planet_type=ptype, no_storage=bool(no_storage))
            else:
                r = generate_layout(tid, planet_type=ptype, launchpads=lp, count=cnt, cc_level=cc,
                                    no_storage=bool(no_storage))
                pairs = [(r["planets"][0]["name"], r["planets"][0]["template"])]
            for name, tmpl in pairs:
                # Disambiguate per (planet type, CC) variants so they don't collide in the zip. Key on
                # the template's planet-type id (Pln), not the name — factory names no longer carry the
                # type, but a Barren vs Temperate factory is a genuinely different template.
                key = f"{name}|{tmpl.get('Pln')}|CC{tmpl.get('CmdCtrLv', 5)}"
                if key not in seen:
                    seen.add(key)
                    items.append((name, tmpl))
        for p1a, p1b, ha, hb, cc, ptype in split_tokens:
            r = generate_split_extractor_layout(p1a, p1b, heads_a=ha, heads_b=hb,
                                                planet_type=ptype, cc_level=cc)
            name, tmpl = r["planets"][0]["name"], r["planets"][0]["template"]
            key = f"{name}|{tmpl.get('Pln')}|CC{tmpl.get('CmdCtrLv', 5)}"
            if key not in seen:
                seen.add(key)
                items.append((name, tmpl))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        used: set[str] = set()
        for name, tmpl in items:
            fname = f"{_safe_filename(name)}_CC{tmpl.get('CmdCtrLv', 5)}"
            # Guard against any residual filename collision.
            base, n = fname, 2
            while fname in used:
                fname = f"{base}_{n}"; n += 1
            used.add(fname)
            z.writestr(fname + ".json", json.dumps(tmpl))
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="pi_templates.zip"'})


class ImportRequest(BaseModel):
    text: str


def _parse_planet_rows(text: str, con) -> tuple[list[dict], int, list[str]]:
    """Parse pasted spreadsheet text into planet rows WITHOUT writing anything.
    Returns (rows, skipped, errors); each row is a dict ready for _write_planet_rows.
    `con` is read-only here (only for the optional system_geo constellation map)."""
    rows: list[dict] = []
    lines = [l for l in text.strip().splitlines() if l.strip()]
    if not lines:
        return rows, 0, []

    # ── Detect format ────────────────────────────────────────────────────────
    # Split first line; if it contains recognisable P0 header names → column-map mode.
    # Otherwise → positional mode (6 compact values per row keyed by planet type).
    sep = "\t" if "\t" in lines[0] else ","
    first_parts = [p.strip() for p in lines[0].split(sep)]

    p0_header_map: dict[str, int] = {}   # db_col → column_index  (column-map mode)
    structural_map: dict[str, int] = {}  # "system"/"planet_num"/etc → column_index

    STRUCT_KEYS = {
        "system": "system", "planet": "planet_num", "planet_num": "planet_num",
        "type": "planet_type", "planet_type": "planet_type", "constellation": "constellation",
    }

    for i, h in enumerate(first_parts):
        key = h.lower()
        if key in STRUCT_KEYS:
            structural_map[STRUCT_KEYS[key]] = i
        p0c = _p0_col(h)
        if p0c:
            p0_header_map[p0c] = i

    use_column_map = bool(p0_header_map)  # True if header row had P0 column names
    data_start = 1 if (use_column_map or "system" in structural_map) else 0

    skipped = 0
    errors: list[str] = []

    # system → constellation (auto-fill when the constellation column is absent)
    sysgeo: dict[str, str] = {}
    try:
        for r in con.execute("SELECT system, constellation FROM system_geo"):
            sysgeo[r["system"]] = r["constellation"]
    except Exception:
        # system_geo is populated by scripts/populate_geo.py — legitimately absent in a fresh
        # dev checkout that hasn't run it yet, but in production the Dockerfile's boot chain
        # guarantees this table exists before traffic is served, so a failure here is a real
        # regression, not a routine routing quirk. Log it instead of swallowing silently.
        log.exception("system_geo lookup failed (constellation auto-fill degraded to none)")
    # planet type ↔ its P0 column set (infer type from which P0 columns are present)
    type_p0_sets = {t: frozenset(c for c in (_p0_col(n) for n in names) if c)
                    for t, names in PLANET_P0_MAP.items()}
    p0set_to_type = {s: t for t, s in type_p0_sets.items()}

    for line in lines[data_start:]:
        parts = [p.strip() for p in line.split(sep)]

        def _col(key: str, default: str = "") -> str:
            if key in structural_map:
                idx = structural_map[key]
            elif not use_column_map:        # positional fallback only without a header row
                idx = {"system": 0, "planet_num": 1, "planet_type": 2, "constellation": 3}.get(key, -1)
            else:
                return default              # column-map mode: missing header → blank (derive it)
            return parts[idx].strip() if 0 <= idx < len(parts) else default

        system    = _col("system")
        praw      = _col("planet_num")
        ptype_raw = _col("planet_type")
        # Constellation: explicit column, else derived from the system name.
        constel   = _col("constellation") or sysgeo.get(system, "")

        if not system:
            skipped += 1
            continue
        try:
            planet_num = int(praw)
        except ValueError:
            skipped += 1
            continue

        # Planet type: explicit ("Scorched Barren" → "Barren"), else inferred from
        # which P0 columns the row fills (each planet type has a unique P0 set).
        ptype_key = None
        if ptype_raw:
            ptype_key = next((k for k in PLANET_P0_MAP if ptype_raw.lower().endswith(k.lower())), None)
        elif use_column_map:
            present = frozenset(db_col for db_col, idx in p0_header_map.items()
                                if idx < len(parts) and parts[idx].strip())
            ptype_key = p0set_to_type.get(present)   # exact 6-P0 match → unique
            if ptype_key is None and present:
                cands = sorted(t for t, s in type_p0_sets.items() if present <= s)
                if len(cands) == 1:
                    ptype_key = cands[0]
                elif len(cands) >= 2:
                    # Ambiguous: this planet reports only 5 of its 6 P0s and those 5 are
                    # shared by two types (e.g. Lava/Plasma, Gas/Storm). Best-guess the
                    # first and warn — the missing P0 is blank (unusable) either way, so
                    # extraction planning is unaffected; only the type label is a guess.
                    ptype_key = cands[0]
                    errors.append(f"{system} P{praw}: ambiguous type ({'/'.join(cands)}) "
                                  f"— assumed {ptype_key}; add a Type column to disambiguate")
        if ptype_key is None:
            errors.append(f"Could not determine planet type at {system} P{praw or '?'}"
                          f"{' — ' + repr(ptype_raw) if ptype_raw else ''}")
            skipped += 1
            continue

        p0_vals: dict[str, int] = {col: 0 for col in P0_COLUMNS}

        if use_column_map:
            # Use header positions — works with sparse 15-column spreadsheet format
            for db_col, col_idx in p0_header_map.items():
                if col_idx < len(parts) and parts[col_idx]:
                    try:
                        p0_vals[db_col] = round(float(parts[col_idx]))
                    except ValueError:
                        pass
        else:
            # Positional: 6 compact values in alphabetical order for this planet type
            p0_names = PLANET_P0_MAP[ptype_key]
            for i, p0_name in enumerate(p0_names):
                col_idx = 4 + i
                db_col = _p0_col(p0_name)
                if db_col and col_idx < len(parts) and parts[col_idx]:
                    try:
                        p0_vals[db_col] = round(float(parts[col_idx]))
                    except ValueError:
                        pass

        # Drop any P0 value that doesn't grow on this planet type (e.g. a Noble Gas reading on a Plasma
        # planet — usually a "Noble Gas"/"Noble Metals" column mix-up). Keeps contradictory data out of
        # the DB; the planner's per-P0 planet-type filter is the backstop if any still slips through.
        valid_cols = {_p0_col(n) for n in PLANET_P0_MAP.get(ptype_key, [])}
        stray = [c for c in list(p0_vals) if c not in valid_cols and p0_vals.get(c)]
        for c in stray:
            p0_vals.pop(c, None)
        if stray:
            errors.append(f"{system} P{praw}: dropped {', '.join(_COL_TO_NAME.get(c, c) for c in stray)}"
                          f" — doesn't occur on {ptype_key} planets")

        row = {"system": system, "planet_num": planet_num,
               "planet_type": ptype_key, "constellation": constel}
        row.update(p0_vals)
        rows.append(row)

    return rows, skipped, errors


def _write_planet_rows(con, rows: list[dict]) -> tuple[int, int]:
    """Upsert parsed planet rows into pp_planets, **merging** rather than overwriting: a new
    planet is inserted, an existing one is updated only where the incoming value is non-blank.
    A 0/blank P0 cell keeps the current value (so a sparse paste can't wipe good data); a blank
    constellation keeps the current one. Caller is responsible for authorisation.
    Returns (imported, skipped) — imported counts rows inserted *or* updated."""
    cur = con.cursor()
    all_cols = ["system", "planet_num", "planet_type", "constellation"] + P0_COLUMNS
    placeholders = ", ".join("?" * len(all_cols))
    set_parts = [
        "planet_type = excluded.planet_type",
        "constellation = CASE WHEN excluded.constellation != '' "
        "THEN excluded.constellation ELSE pp_planets.constellation END",
    ] + [
        f"{c} = CASE WHEN excluded.{c} != 0 THEN excluded.{c} ELSE pp_planets.{c} END"
        for c in P0_COLUMNS
    ]
    sql = (f"INSERT INTO pp_planets ({', '.join(all_cols)}) VALUES ({placeholders}) "
           f"ON CONFLICT(system, planet_num) DO UPDATE SET {', '.join(set_parts)}")
    imported = skipped = 0
    for row in rows:
        values = [row["system"], row["planet_num"], row["planet_type"], row["constellation"]] \
                 + [row.get(c, 0) for c in P0_COLUMNS]
        try:
            cur.execute(sql, values)
            imported += 1
        except Exception:
            skipped += 1
    return imported, skipped


def _submitter_name(con, context_id: int) -> str:
    r = con.execute(
        "SELECT character_name FROM pp_characters WHERE context_id=? LIMIT 1", (context_id,)
    ).fetchone()
    return (r["character_name"] if r else "") or ""


@router.post("/api/planets/import")
def import_planets(req: ImportRequest,
                   pp_session: str = Cookie(default=None),
                   context_id: int = Depends(require_context)):
    """Admins write straight to the shared Planet DB. Everyone else's paste is held in
    pp_planet_submissions for an admin to review — nothing touches the live DB until then."""
    ensure_tables()
    con = get_connection()
    rows, skipped, errors = _parse_planet_rows(req.text, con)
    if not rows and skipped == 0 and not errors:
        con.close()
        raise HTTPException(status_code=400, detail="No data provided")

    if is_admin(pp_session):
        imported, wskip = _write_planet_rows(con, rows)
        con.commit()
        con.close()
        _invalidate_planetdb_cache()
        return {"queued": False, "imported": imported,
                "skipped": skipped + wskip, "errors": errors[:10]}

    # Non-admin → queue for review. Store the original paste; it's re-parsed on approval.
    submitter = _submitter_name(con, context_id)
    if rows:
        con.execute(
            "INSERT INTO pp_planet_submissions (context_id, submitter_name, raw_text, "
            "planet_count, status) VALUES (?,?,?,?, 'pending')",
            (context_id, submitter, req.text, len(rows)),
        )
        con.commit()
        notify_admin_discord("New planet submission", f"{len(rows)} planet(s) submitted by {submitter} — pending review in Admin → Planet submissions")
    con.close()
    return {"queued": True, "submitted": len(rows), "skipped": skipped, "errors": errors[:10]}


@router.get("/api/planet-submissions")
def list_planet_submissions(status: str = "pending", _: int = Depends(require_admin)):
    """Admin: list planet-data submissions awaiting review, each with a parsed preview
    of its planets flagged new vs overwrite (against the current live Planet DB)."""
    ensure_tables()
    con = get_connection()
    subs = con.execute(
        "SELECT id, submitter_name, raw_text, planet_count, status, created_at "
        "FROM pp_planet_submissions WHERE status=? ORDER BY created_at DESC, id DESC",
        (status,),
    ).fetchall()
    existing = {(r["system"], r["planet_num"]) for r in
                con.execute("SELECT system, planet_num FROM pp_planets")}
    out = []
    for s in subs:
        rows, _skip, _err = _parse_planet_rows(s["raw_text"], con)
        planets = [{
            "system": r["system"], "planet_num": r["planet_num"],
            "planet_type": r["planet_type"],
            "exists": (r["system"], r["planet_num"]) in existing,
        } for r in rows]
        out.append({
            "id": s["id"], "submitter_name": s["submitter_name"],
            "planet_count": s["planet_count"], "status": s["status"],
            "created_at": s["created_at"], "planets": planets,
        })
    con.close()
    return {"submissions": out}


@router.post("/api/planet-submissions/{sub_id}/approve")
def approve_planet_submission(sub_id: int, _: int = Depends(require_admin)):
    """Admin: apply a submission to the live Planet DB (insert new / overwrite existing)."""
    ensure_tables()
    con = get_connection()
    s = con.execute(
        "SELECT raw_text FROM pp_planet_submissions WHERE id=? AND status='pending'", (sub_id,)
    ).fetchone()
    if not s:
        con.close()
        raise HTTPException(status_code=404, detail="Submission not found or already reviewed")
    rows, skipped, errors = _parse_planet_rows(s["raw_text"], con)
    imported, wskip = _write_planet_rows(con, rows)
    con.execute(
        "UPDATE pp_planet_submissions SET status='approved', reviewed_at=CURRENT_TIMESTAMP WHERE id=?",
        (sub_id,),
    )
    con.commit()
    con.close()
    _invalidate_planetdb_cache()
    return {"approved": True, "imported": imported, "skipped": skipped + wskip, "errors": errors[:10]}


@router.post("/api/planet-submissions/{sub_id}/reject")
def reject_planet_submission(sub_id: int, _: int = Depends(require_admin)):
    """Admin: discard a submission without touching the Planet DB."""
    ensure_tables()
    con = get_connection()
    n = con.execute(
        "UPDATE pp_planet_submissions SET status='rejected', reviewed_at=CURRENT_TIMESTAMP "
        "WHERE id=? AND status='pending'",
        (sub_id,),
    ).rowcount
    con.commit()
    con.close()
    if not n:
        raise HTTPException(status_code=404, detail="Submission not found or already reviewed")
    return {"rejected": True}


@router.delete("/api/planets")
def clear_planets(context_id: int = Depends(require_context)):
    ensure_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_planets")
    con.commit()
    con.close()
    _invalidate_planetdb_cache()
    return {"cleared": True}


@router.get("/api/constellations")
def list_constellations():
    cached = cache_get_json(_CONSTELLATIONS_CACHE_KEY)
    if cached is not None:
        return cached
    ensure_tables()
    con = get_connection()
    names = [r["constellation"] for r in con.execute(
        "SELECT DISTINCT constellation FROM pp_planets WHERE constellation != '' ORDER BY constellation"
    ).fetchall()]
    # constellation→region map (the `constellations` table is optional — populate via
    # scripts/populate_geo.py). Missing rows just have no region.
    region_of: dict[str, str] = {}
    try:
        for r in con.execute("SELECT name, region FROM constellations"):
            region_of[r["name"]] = r["region"]
    except Exception:
        # See the matching comment in _parse_planet_rows — legitimately absent only in a fresh
        # dev checkout that hasn't run scripts/populate_geo.py yet.
        log.exception("constellations lookup failed (region grouping degraded to none)")
    con.close()
    result = {"constellations": names, "regions": {n: region_of.get(n, "") for n in names}}
    cache_set_json(_CONSTELLATIONS_CACHE_KEY, result, ttl=3600)
    return result
