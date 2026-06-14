import sqlite3
from pathlib import Path
from functools import lru_cache

DB_PATH = Path("data/sde.db")


def get_connection() -> sqlite3.Connection:
    con = sqlite3.connect(str(DB_PATH))
    con.row_factory = sqlite3.Row
    # WAL lets readers run concurrently with a single writer; busy_timeout makes a
    # contended write wait instead of failing instantly with "database is locked";
    # synchronous=NORMAL is safe under WAL and much faster on writes.
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=5000")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


@lru_cache(maxsize=1)
def load_pi_data() -> dict:
    """
    Load all PI schematics and types into memory.
    Returns:
      {
        "types":       {type_id: {"name": str, "pi_tier": int|None}},
        "schematics":  {output_type_id: {"schematic_id": int, "output_qty": int,
                                          "cycle_time": int,
                                          "inputs": [{"type_id": int, "quantity": int}]}},
        "name_to_id":  {lower_name: type_id},
      }
    """
    con = get_connection()
    cur = con.cursor()

    types: dict[int, dict] = {}
    cur.execute("SELECT type_id, name, pi_tier FROM types")
    for row in cur.fetchall():
        types[row["type_id"]] = {"name": row["name"], "pi_tier": row["pi_tier"]}

    schematics: dict[int, dict] = {}
    cur.execute("SELECT schematic_id, output_type_id, output_qty, cycle_time FROM pi_schematics")
    for row in cur.fetchall():
        schematics[row["output_type_id"]] = {
            "schematic_id": row["schematic_id"],
            "output_qty": row["output_qty"],
            "cycle_time": row["cycle_time"],
            "inputs": [],
        }

    cur.execute("SELECT schematic_id, type_id, quantity FROM pi_schematic_inputs")
    # Build a reverse map schematic_id -> output_type_id
    sch_id_to_out: dict[int, int] = {v["schematic_id"]: k for k, v in schematics.items()}
    for row in cur.fetchall():
        out_id = sch_id_to_out.get(row["schematic_id"])
        if out_id is not None:
            schematics[out_id]["inputs"].append({
                "type_id": row["type_id"],
                "quantity": row["quantity"],
            })

    con.close()

    name_to_id = {v["name"].lower(): k for k, v in types.items()}

    return {"types": types, "schematics": schematics, "name_to_id": name_to_id}
