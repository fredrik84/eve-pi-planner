"""Seed STABLE test contexts for /api/redeploy-candidates — the two Setup Analysis extraction
signals (same-hotspot head overlap + depleting deposits).

Creates two independent test accounts:
  999005 / proximity : two characters (Gale, Hana) with extractor colonies sharing planets —
    - planet 900001: both pull AQUEOUS, heads on the SAME spot (overlapping discs)   → flagged
    - planet 900002: both pull AQUEOUS, heads FAR apart (no disc overlap)             → NOT flagged
    - planet 900005: heads on the same spot but DIFFERENT P0s (own deposits)          → NOT flagged
    - planet 900003: solo Gale (only one colony)                                       → NOT flagged
    - planet 900004: Gale extracts, Hana runs a FACTORY (not an extractor)             → NOT flagged
  999006 / depletion : one character (Ivan), extractor colonies with different yield histories in
    pp_colony_yield (peak_day per program), exercising the downtrend detector:
    - 900010: clean 6-program downtrend             → flagged
    - 900011: steady-low (flat, thin planet)        → NOT flagged (no downtrend)
    - 900012: declining but only 3 programs (<min)  → NOT flagged (too few samples)
    - 900013: an up-trend (recovering)              → NOT flagged
    - 900014: downtrend with ONE blip up (tolerated)→ flagged
    - 900015: two blips up (over the tolerance)     → NOT flagged

Idempotent: wipes both contexts first. Run inside the container:
  docker compose exec -T web python3 scripts/seed_redeploy_fixture.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection
from app.esi import ensure_char_tables

ensure_char_tables()   # ensure the ext_heads column exists before seeding

CTX_PROXIMITY = 999005
CTX_DEPLETION = 999006

# Real P0 type_ids (only their distinctness matters).
AQUEOUS = 2268
BASE_METALS = 2267
NOBLE_METALS = 2270
REACTIVE_GAS = 2310

con = get_connection()
cur = con.cursor()

for ctx in (CTX_PROXIMITY, CTX_DEPLETION):
    cur.execute("DELETE FROM pp_char_planets WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (ctx,))
    cur.execute("DELETE FROM pp_colony_yield WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (ctx,))
    cur.execute("DELETE FROM pp_characters WHERE context_id=?", (ctx,))


def add_char(cid, nm, ctx):
    cur.execute(
        "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
        "command_center_upgrades, context_id, is_dummy) VALUES (?,?,5,5,?,0)", (cid, nm, ctx))


def add_colony(cid, planet_id, pn, ptype, is_ext, p0_tid=None, p0_name=None, product=None, heads=None):
    """heads = [{"p0":tid,"r":radius,"h":[[lat,lon],...]}] (an ext_heads list) or None."""
    prods = [{"type_id": 9832, "name": product}] if product else None
    cur.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, solar_system_id, "
        "upgrade_level, num_pins, is_extractor, p0_type_id, p0_name, planet_num, products, ext_heads) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, planet_id, ptype, 30000142, 5, 10, is_ext, p0_tid, p0_name, pn,
         json.dumps(prods) if prods else None, json.dumps(heads) if heads else None))


def add_yield_history(cid, planet_id, p0_tid, peaks, ts0=1_700_000_000):
    for i, peak in enumerate(peaks):
        cur.execute(
            "INSERT INTO pp_colony_yield (character_id, planet_id, install_ts, p0_type_id, "
            "peak_day, prog_days, scanned_ts) VALUES (?,?,?,?,?,?,?)",
            (cid, planet_id, ts0 + i * 86400, p0_tid, float(peak), 5.0, ts0 + i * 86400))


R = 0.02  # head_radius (radians) — two heads within 2R (0.04) of each other overlap
def _ecu(p0, lat, lon):
    return [{"p0": p0, "r": R, "h": [[lat, lon]]}]


# ── 999005: same-hotspot proximity ───────────────────────────────────────────
GALE, HANA = 999210, 999211
add_char(GALE, "Test Gale", CTX_PROXIMITY)
add_char(HANA, "Test Hana", CTX_PROXIMITY)
# planet 900001 — same P0, heads on the SAME point → overlap → flagged
add_colony(GALE, 900001, 1, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 0.50))
add_colony(HANA, 900001, 1, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 0.50))
# planet 900002 — same P0 but heads FAR apart (Δlon ~2 rad) → no overlap → NOT flagged
add_colony(GALE, 900002, 2, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 0.50))
add_colony(HANA, 900002, 2, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 2.50))
# planet 900005 — heads on the same point but DIFFERENT P0s → different deposits → NOT flagged
add_colony(GALE, 900005, 5, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 0.50))
add_colony(HANA, 900005, 5, "Barren", 1, BASE_METALS, "Base Metals", heads=_ecu(BASE_METALS, 1.00, 0.50))
# planet 900003 — solo (only Gale) → NOT flagged
add_colony(GALE, 900003, 3, "Barren", 1, REACTIVE_GAS, "Reactive Gas", heads=_ecu(REACTIVE_GAS, 1.00, 0.50))
# planet 900004 — Gale extracts, Hana runs a factory → the factory isn't an extractor → NOT flagged
add_colony(GALE, 900004, 4, "Barren", 1, AQUEOUS, "Aqueous Liquids", heads=_ecu(AQUEOUS, 1.00, 0.50))
add_colony(HANA, 900004, 4, "Barren", 0, product="Coolant")

# ── 999006: depleting deposits ───────────────────────────────────────────────
IVAN = 999220
add_char(IVAN, "Test Ivan", CTX_DEPLETION)
_depl = [
    (900010, AQUEOUS, "Aqueous Liquids", [40000, 38000, 35000, 32000, 29000, 26000]),   # clean downtrend → flag
    (900011, BASE_METALS, "Base Metals", [20000, 20000, 19500, 20000, 19800, 20000]),   # flat → no flag
    (900012, NOBLE_METALS, "Noble Metals", [40000, 30000, 20000]),                      # only 3 → no flag
    (900013, REACTIVE_GAS, "Reactive Gas", [26000, 29000, 32000, 35000, 38000, 40000]), # up-trend → no flag
    (900014, AQUEOUS, "Aqueous Liquids", [40000, 37000, 38000, 34000, 30000, 27000]),   # 1 blip up → flag
    (900015, BASE_METALS, "Base Metals", [40000, 42000, 36000, 38000, 33000, 30000]),   # 2 blips up → no flag
]
for pn, (pid, p0, name, peaks) in enumerate(_depl, start=1):
    add_colony(IVAN, pid, pn, "Barren", 1, p0, name)
    add_yield_history(IVAN, pid, p0, peaks)

con.commit()
con.close()
print(f"Seeded redeploy test contexts: {CTX_PROXIMITY} (proximity: Gale + Hana), "
      f"{CTX_DEPLETION} (depletion: Ivan).")
