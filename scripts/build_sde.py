#!/usr/bin/env python3
"""
Download the CCP SDE and populate PI schematic data into the app's database (Postgres in
production, SQLite in dev — via app.db.get_connection(), same as the rest of the app).

Tables built:
  types              - typeID, name, group_id, pi_tier (0=P0 raw, 1=P1, 2=P2, 3=P3, 4=P4, NULL=not PI), volume
  pi_schematics      - schematic_id, output_type_id, output_qty, cycle_time
  pi_schematic_inputs- schematic_id, type_id, quantity
  reactions          - reaction_id, output_type_id, output_qty, cycle_time (seconds; from
                        fsd/blueprints.yaml's activities.reaction, not planetSchematics)
  reaction_inputs    - reaction_id, type_id, quantity
  blueprints         - blueprint_type_id, product_type_id, output_qty, base_time, max_runs
                        (manufacturing recipes, from fsd/blueprints.yaml's activities.manufacturing
                        — same file as reactions, different activity block; drives the Industry
                        make-or-buy planner)
  blueprint_materials- blueprint_type_id, type_id, quantity
"""

import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from app.db import get_connection, add_columns, _IS_POSTGRES

# CSafeLoader (libyaml's C parser) is 5-10x faster than pure-Python SafeLoader — matters here
# since fsd/types.yaml is 50k+ entries and yaml.safe_load() is single-threaded (measured ~886s
# on a slow-clocked node vs ~88s on a faster one, for the SafeLoader path — CPU core COUNT/limit
# doesn't help a single-threaded parse). Falls back to SafeLoader if libyaml isn't bundled in
# whatever PyYAML wheel/build is installed, so this never hard-fails, just loses the speedup.
_YAML_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)

SDE_URL = "https://eve-static-data-export.s3-eu-west-1.amazonaws.com/tranquility/sde.zip"

# Arbitrary fixed key for the Postgres advisory lock guarding this build — only matters that
# every replica/process uses the same constant. Skipped entirely in SQLite dev (no concurrency).
_ADVISORY_LOCK_KEY = 918_273_645

_T0 = time.monotonic()


def _log(msg: str) -> None:
    """Timestamped progress line (elapsed since process start) so a live `kubectl logs -f`
    during first startup shows real progress instead of long silent gaps between phases."""
    print(f"[+{time.monotonic() - _T0:6.1f}s] {msg}", flush=True)


def download_sde(dest: Path) -> None:
    _log("Downloading SDE from CCP...")
    with urllib.request.urlopen(SDE_URL) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        with dest.open("wb") as fh:
            while chunk := resp.read(1024 * 1024):
                fh.write(chunk)
                downloaded += len(chunk)
                if total:
                    print(f"\r  {downloaded / 1e6:6.1f} / {total / 1e6:.1f} MB  ({downloaded/total*100:.0f}%)", end="", flush=True)
    print()
    _log(f"Downloaded {downloaded / 1e6:.1f} MB.")


def parse_yaml(zf: zipfile.ZipFile, member: str) -> dict:
    _log(f"Parsing {member} ...")
    t0 = time.monotonic()
    with zf.open(member) as fh:
        data = yaml.load(fh, Loader=_YAML_LOADER)
    _log(f"  {member}: {len(data):,} top-level entries ({time.monotonic() - t0:.1f}s)")
    return data


def compute_pi_tiers(schematics: list[dict]) -> dict[int, int]:
    """
    Topological tier assignment:
      - Items that are never the output of any schematic = tier 0 (raw, P0)
      - Items produced by schematics from tier-0 inputs only = tier 1 (P1)
      - Items produced by schematics from tier-1 inputs = tier 2 (P2), etc.
    """
    # output_type_id -> list of input type_ids for that schematic
    produced_by: dict[int, list[int]] = {}
    for sch in schematics:
        produced_by[sch["output_type_id"]] = [inp["type_id"] for inp in sch["inputs"]]

    memo: dict[int, int] = {}

    def tier(type_id: int) -> int:
        if type_id in memo:
            return memo[type_id]
        if type_id not in produced_by:
            memo[type_id] = 0
            return 0
        inputs = produced_by[type_id]
        if not inputs:
            memo[type_id] = 1
            return 1
        t = max(tier(i) for i in inputs) + 1
        memo[type_id] = t
        return t

    result: dict[int, int] = {}
    for out_id in produced_by:
        result[out_id] = tier(out_id)
    # Also assign tier 0 to items that appear only as inputs
    all_input_ids = {inp for sch in schematics for inp in produced_by.get(sch["output_type_id"], [])}
    for iid in all_input_ids:
        if iid not in result:
            result[iid] = tier(iid)
    return result


_DDL = [
    """
    CREATE TABLE IF NOT EXISTS types (
        type_id   INTEGER PRIMARY KEY,
        name      TEXT    NOT NULL,
        group_id  INTEGER NOT NULL DEFAULT 0,
        pi_tier   INTEGER,
        volume    REAL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_types_name ON types (name)",
    """
    CREATE TABLE IF NOT EXISTS pi_schematics (
        schematic_id   INTEGER PRIMARY KEY,
        output_type_id INTEGER NOT NULL,
        output_qty     INTEGER NOT NULL,
        cycle_time     INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_sch_output ON pi_schematics (output_type_id)",
    """
    CREATE TABLE IF NOT EXISTS pi_schematic_inputs (
        schematic_id INTEGER NOT NULL,
        type_id      INTEGER NOT NULL,
        quantity     INTEGER NOT NULL,
        PRIMARY KEY (schematic_id, type_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_pi_inputs_type ON pi_schematic_inputs (type_id)",
    # Reactions (moon-goo → reaction materials), sourced from fsd/blueprints.yaml entries
    # carrying an `activities.reaction` block — same shape as pi_schematics/pi_schematic_inputs
    # (every reaction formula has exactly one product, confirmed against the live SDE: 112
    # formulas, 0 with != 1 product), just from a different SDE file and a different time-field
    # name (`time`, not PI's `cycleTime`).
    """
    CREATE TABLE IF NOT EXISTS reactions (
        reaction_id    INTEGER PRIMARY KEY,
        output_type_id INTEGER NOT NULL,
        output_qty     INTEGER NOT NULL,
        cycle_time     INTEGER NOT NULL
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reactions_output ON reactions (output_type_id)",
    """
    CREATE TABLE IF NOT EXISTS reaction_inputs (
        reaction_id INTEGER NOT NULL,
        type_id     INTEGER NOT NULL,
        quantity    INTEGER NOT NULL,
        PRIMARY KEY (reaction_id, type_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_reaction_inputs_type ON reaction_inputs (type_id)",
]

# Manufacturing blueprints — blueprints.yaml entries carrying an `activities.manufacturing`
# block. Same file and shape as reactions (one product per recipe), but a different activity key
# and time field, plus `maxProductionLimit` (the per-BPC run cap the Industry planner's batch
# splitter needs). ME/TE are NOT stored here — they're per-player (a researched BPO or a BPC's
# fixed values), supplied at plan time by the Industry blueprint library. Kept in its own DDL list
# (not `_DDL`) so it can be backfilled onto an already-built DB without a full SDE rebuild — see
# _manufacturing_built() / the incremental path in main().
_MFG_DDL = [
    """
    CREATE TABLE IF NOT EXISTS blueprints (
        blueprint_type_id INTEGER PRIMARY KEY,
        product_type_id   INTEGER NOT NULL,
        output_qty        INTEGER NOT NULL,
        base_time         INTEGER NOT NULL,
        max_runs          INTEGER NOT NULL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_blueprints_product ON blueprints (product_type_id)",
    """
    CREATE TABLE IF NOT EXISTS blueprint_materials (
        blueprint_type_id INTEGER NOT NULL,
        type_id           INTEGER NOT NULL,
        quantity          INTEGER NOT NULL,
        PRIMARY KEY (blueprint_type_id, type_id)
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_bp_materials_type ON blueprint_materials (type_id)",
]


def parse_manufacturing(blueprints_yaml: dict) -> list[dict]:
    """Manufacturing recipes from blueprints.yaml: entries with an activities.manufacturing block.
    Same one-product-per-recipe shape as reactions; a handful have no materials (special items) and
    are skipped — they aren't buildable from inputs."""
    _log("Parsing manufacturing blueprints from blueprints.yaml...")
    out: list[dict] = []
    for bp_id, bp_attrs in blueprints_yaml.items():
        if not isinstance(bp_attrs, dict):
            continue
        mfg = (bp_attrs.get("activities") or {}).get("manufacturing")
        if not mfg:
            continue
        materials = mfg.get("materials") or []
        products = mfg.get("products") or []
        if len(products) != 1 or not materials:
            continue
        out.append({
            "blueprint_type_id": int(bp_id),
            "product_type_id": products[0]["typeID"],
            "output_qty": products[0]["quantity"],
            "base_time": mfg.get("time", 0) or 0,
            "max_runs": bp_attrs.get("maxProductionLimit", 0) or 0,
            "inputs": [{"type_id": m["typeID"], "quantity": m["quantity"]} for m in materials],
        })
    _log(f"Found {len(out)} manufacturing blueprints.")
    return out


def write_manufacturing(con, blueprints: list[dict]) -> None:
    """Create the blueprint tables (idempotent) and insert the parsed recipes. Own commit so the
    incremental backfill path is self-contained."""
    for stmt in _MFG_DDL:
        con.execute(stmt)
    for b in blueprints:
        con.execute(
            "INSERT INTO blueprints VALUES (?, ?, ?, ?, ?)",
            (b["blueprint_type_id"], b["product_type_id"], b["output_qty"],
             b["base_time"], b["max_runs"]),
        )
        for inp in b["inputs"]:
            con.execute(
                "INSERT INTO blueprint_materials VALUES (?, ?, ?)",
                (b["blueprint_type_id"], inp["type_id"], inp["quantity"]),
            )
    con.commit()


def _manufacturing_built(con) -> bool:
    """Whether the manufacturing tables exist AND are populated (mirrors _already_built's data
    presence check, so a crash mid-backfill re-runs)."""
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM blueprints").fetchone()
        return bool(row and row["n"] > 0)
    except Exception:
        return False


def _types_volume_built(con) -> bool:
    """Whether `types.volume` exists AND is populated.

    `volume` was added to the types DDL after the table already existed in deployed databases, and
    `_already_built` only checks that `types` has rows — so those installs skipped the build
    entirely and `CREATE TABLE IF NOT EXISTS` could never add the column. The result was a hard
    500 on every endpoint that reads it (`/api/pi-products`, `/api/baskets`, anything calling
    `load_pi_data`), permanently, with no way to self-heal. Found on the dev stack 2026-07-28.

    Same shape as `_manufacturing_built`: presence AND data, so a crash mid-backfill re-runs."""
    try:
        row = con.execute("SELECT COUNT(volume) AS n FROM types").fetchone()
        return bool(row and row["n"] > 0)
    except Exception:
        return False          # column missing entirely


def _backfill_type_volumes(con, type_data: dict) -> None:
    """Add `volume` if absent and populate it from types.yaml, leaving every other column alone."""
    add_columns(con, "types", "volume REAL")
    rows = []
    for type_id, attrs in type_data.items():
        vol = attrs.get("volume")
        if vol is not None:
            rows.append((vol, int(type_id)))
    _log(f"Backfilling volume for {len(rows)} types...")
    for i in range(0, len(rows), 1000):
        con.executemany("UPDATE types SET volume=? WHERE type_id=?", rows[i:i + 1000])
    con.commit()


def _already_built(con) -> bool:
    """Data-presence check (works identically on SQLite dev / Postgres prod via get_connection())
    instead of a file-exists check — a Postgres table has no file to check, and this also
    correctly treats 'table exists but empty' (a crash mid-build) as not-yet-built, unlike a bare
    existence check."""
    try:
        row = con.execute("SELECT COUNT(*) AS n FROM types").fetchone()
        return bool(row and row["n"] > 0)
    except Exception:
        return False


def build_db(con, type_data: dict, schematics_yaml: dict, blueprints_yaml: dict) -> None:
    # Parse types
    _log("Building types table...")
    type_rows: list[tuple] = []
    for type_id, attrs in type_data.items():
        name_obj = attrs.get("name", {})
        name = name_obj.get("en", "") if isinstance(name_obj, dict) else str(name_obj or "")
        if not name:
            continue
        group_id = attrs.get("groupID", 0) or 0
        volume = attrs.get("volume")
        type_rows.append((int(type_id), name, int(group_id), volume))

    # Parse PI schematics
    _log("Parsing planetSchematics...")
    schematics: list[dict] = []
    for sch_id, sch_attrs in schematics_yaml.items():
        if not isinstance(sch_attrs, dict):
            continue
        types_map = sch_attrs.get("types", {}) or {}
        output_type_id = None
        output_qty = 0
        inputs: list[dict] = []

        for type_id, type_info in types_map.items():
            tid = int(type_id)
            qty = type_info.get("quantity", 0) or 0
            is_input = type_info.get("isInput", False)
            if is_input:
                inputs.append({"type_id": tid, "quantity": qty})
            else:
                output_type_id = tid
                output_qty = qty

        if output_type_id is None or not inputs:
            continue

        schematics.append({
            "schematic_id": int(sch_id),
            "output_type_id": output_type_id,
            "output_qty": output_qty,
            "cycle_time": sch_attrs.get("cycleTime", 1800),
            "inputs": inputs,
        })

    _log(f"Found {len(schematics)} PI schematics.")

    # Compute PI tiers via topological sort
    pi_tiers = compute_pi_tiers(schematics)

    # Parse reactions: blueprints.yaml entries carrying an activities.reaction block. Every
    # formula has exactly one product (verified against the live SDE: 112 formulas, 0 with
    # != 1 product) so this is a scalar output_type_id/output_qty, same shape as pi_schematics.
    # Material counts vary 2-6 per formula (fuel blocks + named materials + bulk minerals all
    # land in the same `materials` list) — no fixed-shape assumption, just parse what's there.
    _log("Parsing reactions from blueprints.yaml...")
    reactions: list[dict] = []
    for bp_id, bp_attrs in blueprints_yaml.items():
        if not isinstance(bp_attrs, dict):
            continue
        reaction = (bp_attrs.get("activities") or {}).get("reaction")
        if not reaction:
            continue
        materials = reaction.get("materials") or []
        products = reaction.get("products") or []
        if len(products) != 1 or not materials:
            continue
        reactions.append({
            "reaction_id": int(bp_id),
            "output_type_id": products[0]["typeID"],
            "output_qty": products[0]["quantity"],
            "cycle_time": reaction.get("time", 0),
            "inputs": [{"type_id": m["typeID"], "quantity": m["quantity"]} for m in materials],
        })
    _log(f"Found {len(reactions)} reaction formulas.")

    blueprints = parse_manufacturing(blueprints_yaml)

    _log("Creating tables...")
    for stmt in _DDL:
        con.execute(stmt)
    con.commit()

    _log("Inserting rows...")
    rows_with_tier = [(tid, name, gid, pi_tiers.get(tid), vol) for (tid, name, gid, vol) in type_rows]
    for row in rows_with_tier:
        con.execute("INSERT INTO types VALUES (?, ?, ?, ?, ?)", row)

    for s in schematics:
        con.execute(
            "INSERT INTO pi_schematics VALUES (?, ?, ?, ?)",
            (s["schematic_id"], s["output_type_id"], s["output_qty"], s["cycle_time"]),
        )
        for inp in s["inputs"]:
            con.execute(
                "INSERT INTO pi_schematic_inputs VALUES (?, ?, ?)",
                (s["schematic_id"], inp["type_id"], inp["quantity"]),
            )

    for r in reactions:
        con.execute(
            "INSERT INTO reactions VALUES (?, ?, ?, ?)",
            (r["reaction_id"], r["output_type_id"], r["output_qty"], r["cycle_time"]),
        )
        for inp in r["inputs"]:
            con.execute(
                "INSERT INTO reaction_inputs VALUES (?, ?, ?)",
                (r["reaction_id"], inp["type_id"], inp["quantity"]),
            )

    con.commit()
    write_manufacturing(con, blueprints)

    # Summary
    tier_counts = {}
    for t in pi_tiers.values():
        tier_counts[t] = tier_counts.get(t, 0) + 1
    _log(f"Types: {len(type_rows):,}")
    _log(f"PI schematics: {len(schematics)}")
    _log(f"PI tier distribution: { {f'P{k}': v for k,v in sorted(tier_counts.items())} }")
    _log(f"Reactions: {len(reactions)}")
    _log(f"Manufacturing blueprints: {len(blueprints)}")


def main() -> None:
    con = get_connection()
    try:
        if _IS_POSTGRES:
            _log("Acquiring advisory lock (guards against multiple replicas building at once)...")
            con.execute("SELECT pg_advisory_lock(?)", (_ADVISORY_LOCK_KEY,))
        try:
            if _already_built(con):
                if _manufacturing_built(con) and _types_volume_built(con):
                    _log("SDE already built — skipping.")
                    return
                if not _types_volume_built(con):
                    # types.volume missing/empty on a DB built before the column existed. Parse
                    # ONLY types.yaml and backfill that one column — everything else stays put, so
                    # there's no SDE-less window for the running app.
                    _log("types.volume missing — backfilling from types.yaml only...")
                    tmp_path = Path(tempfile.mktemp(suffix=".zip"))
                    try:
                        download_sde(tmp_path)
                        with zipfile.ZipFile(tmp_path) as zf:
                            _backfill_type_volumes(con, parse_yaml(zf, "fsd/types.yaml"))
                        _log("Volume backfill complete.")
                    finally:
                        if tmp_path.exists():
                            tmp_path.unlink()
                if _manufacturing_built(con):
                    return
                # Types are present but the manufacturing tables are missing (an SDE built before
                # the Industry planner existed). Backfill ONLY those from blueprints.yaml — no need
                # to re-parse the 50k-entry types.yaml (~886s) — and leave everything else intact so
                # there's no SDE-less window for the running app.
                _log("Manufacturing blueprints missing — backfilling from blueprints.yaml only...")
                tmp_path = Path(tempfile.mktemp(suffix=".zip"))
                try:
                    download_sde(tmp_path)
                    with zipfile.ZipFile(tmp_path) as zf:
                        blueprints_yaml = parse_yaml(zf, "fsd/blueprints.yaml")
                        write_manufacturing(con, parse_manufacturing(blueprints_yaml))
                    _log("Manufacturing backfill complete.")
                finally:
                    if tmp_path.exists():
                        tmp_path.unlink()
                return

            tmp_path = Path(tempfile.mktemp(suffix=".zip"))
            try:
                download_sde(tmp_path)
                _log("Extracting YAML files...")
                with zipfile.ZipFile(tmp_path) as zf:
                    type_data = parse_yaml(zf, "fsd/types.yaml")
                    schematics_yaml = parse_yaml(zf, "fsd/planetSchematics.yaml")
                    blueprints_yaml = parse_yaml(zf, "fsd/blueprints.yaml")
                    build_db(con, type_data, schematics_yaml, blueprints_yaml)
            finally:
                if tmp_path.exists():
                    tmp_path.unlink()
        finally:
            if _IS_POSTGRES:
                con.execute("SELECT pg_advisory_unlock(?)", (_ADVISORY_LOCK_KEY,))
                con.commit()
    finally:
        con.close()
    _log("Done.")


if __name__ == "__main__":
    main()
