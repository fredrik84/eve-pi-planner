#!/usr/bin/env python3
"""
Download the CCP SDE and build a SQLite database containing PI schematic data.

Tables built:
  types              - typeID, name, group_id, pi_tier (0=P0 raw, 1=P1, 2=P2, 3=P3, 4=P4, NULL=not PI)
  pi_schematics      - schematic_id, output_type_id, output_qty, cycle_time
  pi_schematic_inputs- schematic_id, type_id, quantity
"""

import io
import sqlite3
import urllib.request
import zipfile
from pathlib import Path

import yaml

SDE_URL = "https://eve-static-data-export.s3-eu-west-1.amazonaws.com/tranquility/sde.zip"
DB_PATH = Path("data/sde.db")


def download_sde() -> io.BytesIO:
    print(f"Downloading SDE from CCP...")
    buf = io.BytesIO()
    with urllib.request.urlopen(SDE_URL) as resp:
        total = int(resp.headers.get("Content-Length", 0))
        downloaded = 0
        while chunk := resp.read(1024 * 1024):
            buf.write(chunk)
            downloaded += len(chunk)
            if total:
                print(f"\r  {downloaded / 1e6:6.1f} / {total / 1e6:.1f} MB  ({downloaded/total*100:.0f}%)", end="", flush=True)
    print(f"\nDownloaded {downloaded / 1e6:.1f} MB.")
    buf.seek(0)
    return buf


def parse_yaml(zf: zipfile.ZipFile, member: str) -> dict:
    print(f"  Parsing {member} ...")
    with zf.open(member) as fh:
        return yaml.safe_load(fh)


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


def parse_universe(zf: zipfile.ZipFile) -> tuple[dict[int, str], dict[int, set[int]]]:
    """
    Walk fsd/universe/eve/**/**/solarsystem.staticdata to extract:
      - {system_id: system_name}
      - {system_id: {adjacent_system_id, ...}}  (via stargates)
    Returns (names, adjacency).
    """
    print("  Parsing universe system adjacency…")
    # First pass: collect all system IDs and their stargate→destination mappings
    gate_to_system: dict[int, int] = {}   # stargate_id → system_id that owns it
    gate_destinations: dict[int, int] = {} # stargate_id → destination_stargate_id
    system_names: dict[int, str] = {}

    universe_prefix = "fsd/universe/eve/"
    for member in zf.namelist():
        if not member.startswith(universe_prefix):
            continue
        if not member.endswith("solarsystem.staticdata"):
            continue
        # Path: fsd/universe/eve/{region}/{constellation}/{system}/solarsystem.staticdata
        parts = member[len(universe_prefix):].split("/")
        if len(parts) < 4:
            continue
        system_name = parts[2]
        try:
            with zf.open(member) as fh:
                data = yaml.safe_load(fh)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        sys_id = data.get("solarSystemID")
        if not sys_id:
            continue
        system_names[sys_id] = system_name
        for gate_id, gate_data in (data.get("stargates") or {}).items():
            dest = gate_data.get("destination") if isinstance(gate_data, dict) else None
            gate_to_system[int(gate_id)] = sys_id
            if dest:
                gate_destinations[int(gate_id)] = int(dest)

    # Second pass: build adjacency
    adjacency: dict[int, set[int]] = {}
    for gate_id, dest_gate_id in gate_destinations.items():
        src_sys = gate_to_system.get(gate_id)
        dst_sys = gate_to_system.get(dest_gate_id)
        if src_sys and dst_sys and src_sys != dst_sys:
            adjacency.setdefault(src_sys, set()).add(dst_sys)
            adjacency.setdefault(dst_sys, set()).add(src_sys)

    print(f"  Universe: {len(system_names):,} systems, {sum(len(v) for v in adjacency.values())//2:,} stargates")
    return system_names, adjacency


def build_db(type_data: dict, schematics_yaml: dict, zf: zipfile.ZipFile | None = None) -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    if DB_PATH.exists():
        DB_PATH.unlink()

    # Parse types
    print("  Building types table...")
    type_rows: list[tuple] = []
    type_name_map: dict[int, str] = {}
    for type_id, attrs in type_data.items():
        name_obj = attrs.get("name", {})
        name = name_obj.get("en", "") if isinstance(name_obj, dict) else str(name_obj or "")
        if not name:
            continue
        group_id = attrs.get("groupID", 0) or 0
        type_name_map[int(type_id)] = name
        type_rows.append((int(type_id), name, int(group_id)))

    # Parse PI schematics
    print("  Parsing planetSchematics...")
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

    print(f"  Found {len(schematics)} PI schematics.")

    # Compute PI tiers via topological sort
    pi_tiers = compute_pi_tiers(schematics)
    all_pi_type_ids = set(pi_tiers.keys())

    # Build DB
    con = sqlite3.connect(str(DB_PATH))
    cur = con.cursor()
    cur.executescript("""
        CREATE TABLE types (
            type_id   INTEGER PRIMARY KEY,
            name      TEXT    NOT NULL,
            group_id  INTEGER NOT NULL DEFAULT 0,
            pi_tier   INTEGER
        );
        CREATE INDEX idx_types_name ON types(name COLLATE NOCASE);

        CREATE TABLE pi_schematics (
            schematic_id   INTEGER PRIMARY KEY,
            output_type_id INTEGER NOT NULL,
            output_qty     INTEGER NOT NULL,
            cycle_time     INTEGER NOT NULL
        );
        CREATE INDEX idx_pi_sch_output ON pi_schematics(output_type_id);

        CREATE TABLE pi_schematic_inputs (
            schematic_id INTEGER NOT NULL,
            type_id      INTEGER NOT NULL,
            quantity     INTEGER NOT NULL,
            PRIMARY KEY (schematic_id, type_id)
        );
        CREATE INDEX idx_pi_inputs_type ON pi_schematic_inputs(type_id);

        CREATE TABLE solar_systems (
            system_id   INTEGER PRIMARY KEY,
            name        TEXT NOT NULL
        );
        CREATE INDEX idx_ss_name ON solar_systems(name COLLATE NOCASE);

        CREATE TABLE system_jumps (
            from_id INTEGER NOT NULL,
            to_id   INTEGER NOT NULL,
            PRIMARY KEY (from_id, to_id)
        );
        CREATE INDEX idx_sj_from ON system_jumps(from_id);
    """)

    # Insert types with pi_tier
    rows_with_tier = []
    for (tid, name, gid) in type_rows:
        t = pi_tiers.get(tid)
        rows_with_tier.append((tid, name, gid, t))
    cur.executemany("INSERT INTO types VALUES (?, ?, ?, ?)", rows_with_tier)

    # Insert schematics
    sch_rows = [(s["schematic_id"], s["output_type_id"], s["output_qty"], s["cycle_time"]) for s in schematics]
    cur.executemany("INSERT INTO pi_schematics VALUES (?, ?, ?, ?)", sch_rows)

    inp_rows = []
    for s in schematics:
        for inp in s["inputs"]:
            inp_rows.append((s["schematic_id"], inp["type_id"], inp["quantity"]))
    cur.executemany("INSERT INTO pi_schematic_inputs VALUES (?, ?, ?)", inp_rows)

    # Insert universe adjacency if available
    if zf is not None:
        system_names, adjacency = parse_universe(zf)
        cur.executemany("INSERT OR IGNORE INTO solar_systems VALUES (?,?)", system_names.items())
        jump_rows = [(src, dst) for src, dsts in adjacency.items() for dst in dsts]
        cur.executemany("INSERT OR IGNORE INTO system_jumps VALUES (?,?)", jump_rows)

    con.commit()
    con.close()

    # Summary
    tier_counts = {}
    for t in pi_tiers.values():
        tier_counts[t] = tier_counts.get(t, 0) + 1
    print(f"  Types: {len(type_rows):,}")
    print(f"  PI schematics: {len(schematics)}")
    print(f"  PI tier distribution: { {f'P{k}': v for k,v in sorted(tier_counts.items())} }")
    print(f"Database written to {DB_PATH}")


def main() -> None:
    if DB_PATH.exists():
        print(f"SDE already built at {DB_PATH} — skipping. (Delete to force refresh.)")
        return
    buf = download_sde()
    print("Extracting YAML files...")
    with zipfile.ZipFile(buf) as zf:
        type_data = parse_yaml(zf, "fsd/types.yaml")
        schematics_yaml = parse_yaml(zf, "fsd/planetSchematics.yaml")
        build_db(type_data, schematics_yaml, zf)
    print("Done.")


if __name__ == "__main__":
    main()
