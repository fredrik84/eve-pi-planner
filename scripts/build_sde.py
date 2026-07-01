#!/usr/bin/env python3
"""
Download the CCP SDE and populate PI schematic data into the app's database (Postgres in
production, SQLite in dev — via app.db.get_connection(), same as the rest of the app).

Tables built:
  types              - typeID, name, group_id, pi_tier (0=P0 raw, 1=P1, 2=P2, 3=P3, 4=P4, NULL=not PI)
  pi_schematics      - schematic_id, output_type_id, output_qty, cycle_time
  pi_schematic_inputs- schematic_id, type_id, quantity
"""

import sys
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

import yaml

sys.path.insert(0, ".")
from app.db import get_connection, _IS_POSTGRES

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
        data = yaml.safe_load(fh)
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
        pi_tier   INTEGER
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
]


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


def build_db(con, type_data: dict, schematics_yaml: dict) -> None:
    # Parse types
    _log("Building types table...")
    type_rows: list[tuple] = []
    for type_id, attrs in type_data.items():
        name_obj = attrs.get("name", {})
        name = name_obj.get("en", "") if isinstance(name_obj, dict) else str(name_obj or "")
        if not name:
            continue
        group_id = attrs.get("groupID", 0) or 0
        type_rows.append((int(type_id), name, int(group_id)))

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

    _log("Creating tables...")
    for stmt in _DDL:
        con.execute(stmt)
    con.commit()

    _log("Inserting rows...")
    rows_with_tier = [(tid, name, gid, pi_tiers.get(tid)) for (tid, name, gid) in type_rows]
    for row in rows_with_tier:
        con.execute("INSERT INTO types VALUES (?, ?, ?, ?)", row)

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

    con.commit()

    # Summary
    tier_counts = {}
    for t in pi_tiers.values():
        tier_counts[t] = tier_counts.get(t, 0) + 1
    _log(f"Types: {len(type_rows):,}")
    _log(f"PI schematics: {len(schematics)}")
    _log(f"PI tier distribution: { {f'P{k}': v for k,v in sorted(tier_counts.items())} }")


def main() -> None:
    con = get_connection()
    try:
        if _IS_POSTGRES:
            _log("Acquiring advisory lock (guards against multiple replicas building at once)...")
            con.execute("SELECT pg_advisory_lock(?)", (_ADVISORY_LOCK_KEY,))
        try:
            if _already_built(con):
                _log("SDE already built — skipping.")
                return

            tmp_path = Path(tempfile.mktemp(suffix=".zip"))
            try:
                download_sde(tmp_path)
                _log("Extracting YAML files...")
                with zipfile.ZipFile(tmp_path) as zf:
                    type_data = parse_yaml(zf, "fsd/types.yaml")
                    schematics_yaml = parse_yaml(zf, "fsd/planetSchematics.yaml")
                    build_db(con, type_data, schematics_yaml)
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
