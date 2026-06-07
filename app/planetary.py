"""
Planetary Planning — planet database and allocation.
"""

import sqlite3
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.esi import require_context

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


@router.get("/api/planets")
def list_planets():
    ensure_tables()
    con = get_connection()
    cur = con.cursor()
    cols = ", ".join(["system", "planet_num", "planet_type", "constellation"] + P0_COLUMNS)
    cur.execute(f"SELECT {cols} FROM pp_planets ORDER BY system, planet_num")
    rows = [dict(r) for r in cur.fetchall()]
    con.close()
    return {"planets": rows, "p0_columns": P0_COLUMNS, "planet_p0_map": PLANET_P0_MAP}


@router.get("/api/pi-products")
def list_pi_products():
    con = get_connection()
    rows = con.execute(
        "SELECT type_id, name, pi_tier FROM types WHERE pi_tier IN (1,2,3,4) ORDER BY pi_tier, name"
    ).fetchall()
    con.close()
    return {"products": [{"type_id": r["type_id"], "name": r["name"], "tier": r["pi_tier"]} for r in rows]}


class LayoutRequest(BaseModel):
    type_id: int
    planet_type: str = "Barren"
    launchpads: Optional[int] = None   # None → per-tier default (3 factory / 1 extractor)
    count: Optional[int] = None         # production units on the planet; None → max that fits


@router.post("/api/layout")
def factory_layout(req: LayoutRequest):
    from app.layout import generate_layout
    try:
        return generate_layout(req.type_id, req.planet_type, req.launchpads, req.count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


def _safe_filename(name: str) -> str:
    name = name.replace("→", "to")
    return "".join(c if (c.isalnum() or c in " -_") else "_" for c in name).strip().replace(" ", "_")


@router.get("/api/layout/download")
def download_layout(type_id: int, planet_type: str = "Barren", launchpads: Optional[int] = None, count: int = 1):
    """Return a single planet template as a downloadable .json attachment."""
    import json
    from fastapi import Response
    from app.layout import generate_layout
    try:
        result = generate_layout(type_id, planet_type, launchpads, count)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    tmpl = result["planets"][0]["template"]
    fname = _safe_filename(result["planets"][0]["name"]) + ".json"
    return Response(content=json.dumps(tmpl), media_type="application/json",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.get("/api/layout/bundle")
def download_bundle(type_ids: str, expand: int = 0):
    """
    Return a ZIP of templates for one or more products (comma-separated `type_ids`).
    With expand=1, each product also includes the P0→P1 extractor templates for its
    whole chain (used by the planner's 'all templates' download). Launchpads default
    per tier (3 factory / 1 extractor).
    """
    import io, json, zipfile
    from fastapi import Response
    from app.layout import generate_layout, bundle_templates
    # each token is "id", "id:launchpads" or "id:launchpads:count"
    tokens = []
    for tok in type_ids.split(","):
        tok = tok.strip()
        if not tok:
            continue
        parts = tok.split(":")
        tid = int(parts[0])
        lp = int(parts[1]) if len(parts) > 1 and parts[1] else None
        cnt = int(parts[2]) if len(parts) > 2 and parts[2] else 1
        tokens.append((tid, lp, cnt))
    if not tokens:
        raise HTTPException(status_code=400, detail="no type_ids given")
    items: list[tuple] = []
    seen: set[str] = set()
    try:
        for tid, lp, cnt in tokens:
            if expand:
                pairs = bundle_templates(tid)
            else:
                r = generate_layout(tid, launchpads=lp, count=cnt)
                pairs = [(r["planets"][0]["name"], r["planets"][0]["template"])]
            for name, tmpl in pairs:
                if name not in seen:
                    seen.add(name)
                    items.append((name, tmpl))
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, tmpl in items:
            z.writestr(_safe_filename(name) + ".json", json.dumps(tmpl))
    buf.seek(0)
    return Response(content=buf.read(), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="pi_templates.zip"'})


class ImportRequest(BaseModel):
    text: str


@router.post("/api/planets/import")
def import_planets(req: ImportRequest, context_id: int = Depends(require_context)):
    ensure_tables()
    lines = [l for l in req.text.strip().splitlines() if l.strip()]
    if not lines:
        raise HTTPException(status_code=400, detail="No data provided")

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

    con = get_connection()
    cur = con.cursor()
    imported = skipped = 0
    errors: list[str] = []

    # system → constellation (auto-fill when the constellation column is absent)
    sysgeo: dict[str, str] = {}
    try:
        for r in con.execute("SELECT system, constellation FROM system_geo"):
            sysgeo[r["system"]] = r["constellation"]
    except sqlite3.Error:
        pass
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

        all_cols = ["system", "planet_num", "planet_type", "constellation"] + P0_COLUMNS
        placeholders = ", ".join("?" * len(all_cols))
        values = [system, planet_num, ptype_key, constel] + [p0_vals[c] for c in P0_COLUMNS]

        try:
            cur.execute(
                f"INSERT OR REPLACE INTO pp_planets ({', '.join(all_cols)}) VALUES ({placeholders})",
                values,
            )
            imported += 1
        except sqlite3.Error as e:
            errors.append(str(e))
            skipped += 1

    con.commit()
    con.close()
    return {"imported": imported, "skipped": skipped, "errors": errors[:10]}


@router.delete("/api/planets")
def clear_planets(context_id: int = Depends(require_context)):
    ensure_tables()
    con = get_connection()
    con.execute("DELETE FROM pp_planets")
    con.commit()
    con.close()
    return {"cleared": True}


@router.get("/api/constellations")
def list_constellations():
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
    except sqlite3.Error:
        pass
    con.close()
    return {"constellations": names, "regions": {n: region_of.get(n, "") for n in names}}
