from functools import lru_cache

from app.db import get_connection, ensure_once  # re-export: all app code imports these from here


@lru_cache(maxsize=1)
def load_pi_data() -> dict:
    """
    Load all PI schematics and types into memory.
    Returns:
      {
        "types":       {type_id: {"name": str, "pi_tier": int|None, "volume": float|None}},
        "schematics":  {output_type_id: {"schematic_id": int, "output_qty": int,
                                          "cycle_time": int,
                                          "inputs": [{"type_id": int, "quantity": int}]}},
        "name_to_id":  {lower_name: type_id},
      }
    """
    con = get_connection()

    types: dict[int, dict] = {}
    for row in con.execute("SELECT type_id, name, pi_tier, volume FROM types").fetchall():
        types[row["type_id"]] = {"name": row["name"], "pi_tier": row["pi_tier"], "volume": row["volume"]}

    schematics: dict[int, dict] = {}
    for row in con.execute(
        "SELECT schematic_id, output_type_id, output_qty, cycle_time FROM pi_schematics"
    ).fetchall():
        schematics[row["output_type_id"]] = {
            "schematic_id": row["schematic_id"],
            "output_qty": row["output_qty"],
            "cycle_time": row["cycle_time"],
            "inputs": [],
        }

    # Build a reverse map schematic_id -> output_type_id
    sch_id_to_out: dict[int, int] = {v["schematic_id"]: k for k, v in schematics.items()}
    for row in con.execute(
        "SELECT schematic_id, type_id, quantity FROM pi_schematic_inputs"
    ).fetchall():
        out_id = sch_id_to_out.get(row["schematic_id"])
        if out_id is not None:
            schematics[out_id]["inputs"].append({
                "type_id": row["type_id"],
                "quantity": row["quantity"],
            })

    con.close()

    # The SDE genuinely has duplicate-named rows for some types (e.g. every "Planet (Barren)"-style
    # pseudo-item exists at both a legacy low type_id [2016] and a reissued high one [56018]).
    # Building this from `SELECT ... FROM types` with no ORDER BY made which id won arbitrary
    # (Postgres row order isn't guaranteed). An earlier version of this fix preferred the HIGHEST
    # id, assuming the reissue was canonical — that was backwards: confirmed via a controlled
    # in-game test (identical PI-import template, only Pln changed from the legacy id to the
    # reissued one) that the EVE client's PI-import validator only accepts the LEGACY (lower) id
    # and rejects the reissued one as "not in the correct format". Sorting descending by type_id
    # so the LOWEST id wins on a name collision makes this deterministic AND correct.
    name_to_id = {v["name"].lower(): k for k, v in sorted(types.items(), reverse=True)}

    return {"types": types, "schematics": schematics, "name_to_id": name_to_id}
