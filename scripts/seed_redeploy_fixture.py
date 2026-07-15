"""Seed STABLE test contexts for /api/redeploy-candidates — the two Setup Analysis "redeploy the
CC elsewhere" signals (own-character planet collisions + depleting deposits).

Creates two independent test accounts:
  999005 / collisions : two characters (Gale, Hana) with several extractor colonies —
    - planet 900001: BOTH extract the SAME P0 (shared_resource=True, the strong signal)
    - planet 900002: both extract, but DIFFERENT P0s (shared_resource=False)
    - planet 900003: only Gale (solo — must NOT be flagged)
    - planet 900004: Gale extracts, Hana runs a FACTORY there (not an extractor — must NOT be
      a collision; depletion/collision are extraction-only concerns)
  999006 / depletion : one character (Ivan), several extractor colonies with different yield
    histories in pp_colony_yield (peak_day per program), exercising the downtrend detector:
    - 900010: clean 6-program downtrend            → flagged
    - 900011: steady-low (flat, thin planet)       → NOT flagged (no downtrend)
    - 900012: declining but only 3 programs (<min)  → NOT flagged (too few samples)
    - 900013: an up-trend (recovering)             → NOT flagged
    - 900014: downtrend with ONE blip up (tolerated) → flagged
    - 900015: two blips up (over the tolerance)     → NOT flagged

Idempotent: wipes both contexts first. Run inside the container:
  docker compose exec -T web python3 scripts/seed_redeploy_fixture.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection

CTX_COLLISION = 999005
CTX_DEPLETION = 999006

# Real P0 type_ids (only their distinctness matters for shared-resource detection).
AQUEOUS = 2268
BASE_METALS = 2267
NOBLE_METALS = 2270
REACTIVE_GAS = 2310

con = get_connection()
cur = con.cursor()

for ctx in (CTX_COLLISION, CTX_DEPLETION):
    cur.execute("DELETE FROM pp_char_planets WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (ctx,))
    cur.execute("DELETE FROM pp_colony_yield WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (ctx,))
    cur.execute("DELETE FROM pp_characters WHERE context_id=?", (ctx,))


def add_char(cid, nm, ctx):
    cur.execute(
        "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
        "command_center_upgrades, context_id, is_dummy) VALUES (?,?,5,5,?,0)", (cid, nm, ctx))


def add_colony(cid, planet_id, pn, ptype, is_ext, p0_tid=None, p0_name=None, product=None):
    prods = [{"type_id": 9832, "name": product}] if product else None
    cur.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, solar_system_id, "
        "upgrade_level, num_pins, is_extractor, p0_type_id, p0_name, planet_num, products) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, planet_id, ptype, 30000142, 5, 10, is_ext, p0_tid, p0_name, pn,
         json.dumps(prods) if prods else None))


def add_yield_history(cid, planet_id, p0_tid, peaks, ts0=1_700_000_000):
    """One pp_colony_yield row per program (oldest first), install_ts stepped a day apart."""
    for i, peak in enumerate(peaks):
        cur.execute(
            "INSERT INTO pp_colony_yield (character_id, planet_id, install_ts, p0_type_id, "
            "peak_day, prog_days, scanned_ts) VALUES (?,?,?,?,?,?,?)",
            (cid, planet_id, ts0 + i * 86400, p0_tid, float(peak), 5.0, ts0 + i * 86400))


# ── 999005: own-character collisions ─────────────────────────────────────────
GALE, HANA = 999210, 999211
add_char(GALE, "Test Gale", CTX_COLLISION)
add_char(HANA, "Test Hana", CTX_COLLISION)
# planet 900001 — both extract the SAME P0 → shared_resource True
add_colony(GALE, 900001, 1, "Barren", 1, p0_tid=AQUEOUS, p0_name="Aqueous Liquids")
add_colony(HANA, 900001, 1, "Barren", 1, p0_tid=AQUEOUS, p0_name="Aqueous Liquids")
# planet 900002 — both extract, DIFFERENT P0s → shared_resource False
add_colony(GALE, 900002, 2, "Barren", 1, p0_tid=BASE_METALS, p0_name="Base Metals")
add_colony(HANA, 900002, 2, "Barren", 1, p0_tid=NOBLE_METALS, p0_name="Noble Metals")
# planet 900003 — solo (only Gale) → NOT a collision
add_colony(GALE, 900003, 3, "Barren", 1, p0_tid=REACTIVE_GAS, p0_name="Reactive Gas")
# planet 900004 — Gale extracts, Hana runs a factory → NOT a collision (only 1 extractor)
add_colony(GALE, 900004, 4, "Barren", 1, p0_tid=AQUEOUS, p0_name="Aqueous Liquids")
add_colony(HANA, 900004, 4, "Barren", 0, product="Coolant")

# ── 999006: depleting deposits ───────────────────────────────────────────────
IVAN = 999220
add_char(IVAN, "Test Ivan", CTX_DEPLETION)
_depl = [
    (900010, AQUEOUS, "Aqueous Liquids", [40000, 38000, 35000, 32000, 29000, 26000]),  # clean downtrend → flag
    (900011, BASE_METALS, "Base Metals", [20000, 20000, 19500, 20000, 19800, 20000]),  # flat → no flag
    (900012, NOBLE_METALS, "Noble Metals", [40000, 30000, 20000]),                     # only 3 → no flag
    (900013, REACTIVE_GAS, "Reactive Gas", [26000, 29000, 32000, 35000, 38000, 40000]),# up-trend → no flag
    (900014, AQUEOUS, "Aqueous Liquids", [40000, 37000, 38000, 34000, 30000, 27000]),  # 1 blip up → flag
    (900015, BASE_METALS, "Base Metals", [40000, 42000, 36000, 38000, 33000, 30000]),  # 2 blips up → no flag
]
for pn, (pid, p0, name, peaks) in enumerate(_depl, start=1):
    add_colony(IVAN, pid, pn, "Barren", 1, p0_tid=p0, p0_name=name)
    add_yield_history(IVAN, pid, p0, peaks)

con.commit()
con.close()
print(f"Seeded redeploy test contexts: {CTX_COLLISION} (collisions: Gale + Hana), "
      f"{CTX_DEPLETION} (depletion: Ivan).")
