"""Seed a STABLE test context for developing/verifying the dashboard + Setup Analysis features
(expansion suggestions, supply/demand, free-slot deploys, the 'plan for this product' dropdown)
without depending on a live user's setup, which can change underneath the test.

Creates context_id 999001 with real-looking (is_dummy=0) characters so the dashboard logic — which
filters out is_dummy planning placeholders — actually sees them. TWO products, so the spare-capacity
card shows the product dropdown:

  TST-AA — Coolant (P2 ← Electrolytes + Water)
    Test Alpha : Coolant factory + Water extractor
    Test Bravo : Electrolytes extractor (UNDER-supplied) + Water extractor
  TST-BB — Mechanical Parts (P2 ← Reactive Metals + Precious Metals)
    Test Charlie : Mechanical Parts factory + Reactive Metals extractor (UNDER-supplied) + Precious Metals extractor
  Test Idle : 0 colonies, 3 free planet slots ← the spare capacity (can grow either product)

Each product has a deliberate bottleneck so the expansion cards send spare slots to the short input
first, then add factories. Idempotent: wipes context 999001 first. Run inside the container:
  docker compose exec -T web python3 scripts/seed_test_fixture.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection
from app.sde import load_pi_data

CTX = 999001
pi = load_pi_data()
types = pi["types"]
NAME = {t.get("name"): tid for tid, t in types.items()}

con = get_connection()
cur = con.cursor()

# Wipe any prior fixture
cur.execute("DELETE FROM pp_char_planets WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (CTX,))
cur.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
for sid in (99990001, 99990002):
    cur.execute("DELETE FROM pp_planets WHERE solar_system_id=?", (sid,))


def add_system(sid, name):
    # OR IGNORE, not OR REPLACE (app.db._pg_translate only supports the former) — fine here since
    # the name for a given fixture system_id never actually changes between re-runs.
    cur.execute("INSERT OR IGNORE INTO solar_systems (system_id, name) VALUES (?,?)", (sid, name))


def add_planet(sid, sysname, pn, ptype, dens):
    cols = list(dens.keys())
    cur.execute(
        f"INSERT INTO pp_planets (system, planet_num, planet_type, constellation, solar_system_id, "
        f"{', '.join(cols)}) VALUES (?,?,?,?,?,{', '.join('?' for _ in cols)})",
        (sysname, pn, ptype, "Test-Const", sid, *(dens[c] for c in cols)))


def add_char(cid, nm, ic, ccu=5):
    cur.execute(
        "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
        "command_center_upgrades, context_id, is_dummy) VALUES (?,?,?,?,?,0)", (cid, nm, ic, ccu, CTX))


def add_colony(cid, sid, pn, ptype, is_ext, product=None, out_name=None, per_day=None):
    sim = None
    if is_ext:
        rate = (per_day or 0) / 86400.0
        out_tid = NAME[out_name]
        sim = json.dumps({"t0": 1_700_000_000, "expiry": 1_700_200_000, "install": 1_700_000_000,
                          "program_days": 2.0, "peak_p0_day": (per_day or 0) * 150,
                          "outputs": [{"type_id": out_tid, "name": out_name, "tier": 1, "base": 0.0,
                                       "rate": rate, "rate_sustained": rate, "batch": 0.0}]})
    prods = [{"type_id": NAME[product], "name": product}] if product else None
    cur.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, solar_system_id, "
        "upgrade_level, num_pins, is_extractor, p0_name, planet_num, products, sim_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        (cid, cid * 100 + pn, ptype, sid, 5, 10, is_ext, out_name, pn,
         json.dumps(prods) if prods else None, sim))


# ── TST-AA: Coolant (Electrolytes ← Ionic Solutions / Storm, Water ← Aqueous Liquids / Barren-Temp) ──
add_system(99990001, "TST-AA")
for pn, t, d in [(1, "Storm", {"ionic_solutions": 88}), (2, "Storm", {"ionic_solutions": 81}),
                 (3, "Storm", {"ionic_solutions": 74}), (7, "Storm", {"ionic_solutions": 69}),
                 (4, "Barren", {"aqueous_liquids": 92}), (5, "Temperate", {"aqueous_liquids": 85}),
                 (6, "Barren", {"aqueous_liquids": 78}), (8, "Temperate", {"aqueous_liquids": 71}),
                 (9, "Barren", {"aqueous_liquids": 64}), (11, "Temperate", {"aqueous_liquids": 60})]:
    add_planet(99990001, "TST-AA", pn, t, d)

# ── TST-BB: Mechanical Parts (Reactive Metals ← Base Metals / Lava, Precious Metals ← Noble Metals / Plasma) ──
add_system(99990002, "TST-BB")
for pn, t, d in [(1, "Lava", {"base_metals": 90}), (2, "Lava", {"base_metals": 82}),
                 (3, "Lava", {"base_metals": 75}), (4, "Plasma", {"noble_metals": 88}),
                 (5, "Plasma", {"noble_metals": 80}), (6, "Barren", {"aqueous_liquids": 50}),
                 (7, "Barren", {"aqueous_liquids": 48}), (8, "Barren", {"aqueous_liquids": 46})]:
    add_planet(99990002, "TST-BB", pn, t, d)

add_char(999100, "Test Alpha", 5)
add_char(999101, "Test Bravo", 5)
add_char(999102, "Test Idle", 2, ccu=3)  # IC2 → 3 planets, none used → spare capacity. CCU3 verifies we
#                                          do NOT over-warn: the fixture's P2 factories fit 3 LP at CC3, so
#                                          no CCU warning should appear (only P4 like RCM is cramped at CC3).
add_char(999103, "Test Charlie", 5)

# Coolant op (TST-AA): Electrolytes deliberately short (500/day) vs Water (9000/day).
add_colony(999100, 99990001, 4, "Barren", 0, product="Coolant")
add_colony(999100, 99990001, 5, "Temperate", 1, out_name="Water", per_day=9000)
add_colony(999101, 99990001, 1, "Storm", 1, out_name="Electrolytes", per_day=500)
add_colony(999101, 99990001, 6, "Barren", 1, out_name="Water", per_day=9000)

# Mechanical Parts op (TST-BB): Reactive Metals deliberately short (600/day) vs Precious Metals (9000/day).
add_colony(999103, 99990002, 6, "Barren", 0, product="Mechanical Parts")
add_colony(999103, 99990002, 1, "Lava", 1, out_name="Reactive Metals", per_day=600)
add_colony(999103, 99990002, 4, "Plasma", 1, out_name="Precious Metals", per_day=9000)

con.commit()
con.close()
print(f"Seeded test context {CTX}: 2 products (Coolant @ TST-AA, Mechanical Parts @ TST-BB), 1 idle toon.")
print("Electrolytes and Reactive Metals are the bottlenecks. The card should show a product dropdown.")
