"""Seed a STABLE test context for developing/verifying the dashboard + Setup Analysis features
(expansion suggestions, supply/demand, free-slot deploys) without depending on a live user's setup,
which can change underneath the test.

Creates context_id 999001 with real-looking (is_dummy=0) characters so the dashboard logic — which
filters out is_dummy planning placeholders — actually sees them:

  Setup: a Coolant (P2) operation in one system (TST-AA).
    - Test Alpha : 1 Coolant factory + 1 Water extractor
    - Test Bravo : 1 Electrolytes extractor (UNDER-supplied) + 1 Water extractor
    - Test Idle  : 0 colonies, 3 free planet slots  ← the spare capacity to grow into
  Electrolytes is deliberately the bottleneck, so the expansion suggestion should send the idle
  toon's free slots to Ionic-Solutions (Electrolytes) planets first.

Idempotent: wipes context 999001 first. Run inside the container:
  docker compose exec -T web python3 scripts/seed_test_fixture.py
"""
import json
import sqlite3
import sys

sys.path.insert(0, ".")
from app.sde import load_pi_data

CTX = 999001
SYS_ID = 99990001
SYS = "TST-AA"
CONST = "Test-Constellation"

pi = load_pi_data()
types = pi["types"]
NAME_TO_TID = {t.get("name"): tid for tid, t in types.items()}
COOLANT = NAME_TO_TID["Coolant"]
IONIC = NAME_TO_TID["Ionic Solutions"]       # P0 → Electrolytes
AQUEOUS = NAME_TO_TID["Aqueous Liquids"]     # P0 → Water
ELECTRO = NAME_TO_TID["Electrolytes"]
WATER = NAME_TO_TID["Water"]


def sim(out_tid, out_name, per_day):
    """Minimal sim_state — only the fields the supply read needs (outputs[].rate_sustained/rate)."""
    rate = per_day / 86400.0
    return json.dumps({
        "t0": 1_700_000_000, "expiry": 1_700_200_000, "install": 1_700_000_000,
        "program_days": 2.0, "peak_p0_day": per_day * 150,
        "outputs": [{"type_id": out_tid, "name": out_name, "tier": 1,
                     "base": 0.0, "rate": rate, "rate_sustained": rate, "batch": 0.0}],
    })


con = sqlite3.connect("data/sde.db")
cur = con.cursor()

# Wipe any prior fixture
cur.execute("DELETE FROM pp_char_planets WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (CTX,))
cur.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
cur.execute("DELETE FROM pp_planets WHERE solar_system_id=? OR system=?", (SYS_ID, SYS))
cur.execute("INSERT OR REPLACE INTO solar_systems (system_id, name) VALUES (?,?)", (SYS_ID, SYS))

# Planet DB for TST-AA: Storm planets grow Ionic Solutions (Electrolytes); Barren/Temperate grow Aqueous
# Liquids (Water). Leave plenty unoccupied so the idle toon has somewhere to deploy.
P0COLS = ("aqueous_liquids", "ionic_solutions")
planets = [
    (1, "Storm",     {"ionic_solutions": 88}),
    (2, "Storm",     {"ionic_solutions": 81}),
    (3, "Storm",     {"ionic_solutions": 74}),
    (4, "Barren",    {"aqueous_liquids": 92}),
    (5, "Temperate", {"aqueous_liquids": 85}),
    (6, "Barren",    {"aqueous_liquids": 78}),
    (7, "Storm",     {"ionic_solutions": 69}),
    (8, "Temperate", {"aqueous_liquids": 71}),
    (9, "Barren",    {"aqueous_liquids": 64}),
    (10, "Storm",    {"ionic_solutions": 66}),
    (11, "Temperate", {"aqueous_liquids": 60}),
    (12, "Barren",   {"aqueous_liquids": 58}),
]
for pn, ptype, dens in planets:
    cols = ", ".join(P0COLS)
    vals = ", ".join("?" for _ in P0COLS)
    cur.execute(
        f"INSERT INTO pp_planets (system, planet_num, planet_type, constellation, solar_system_id, {cols}) "
        f"VALUES (?,?,?,?,?,{vals})",
        (SYS, pn, ptype, CONST, SYS_ID, *(dens.get(c, 0) for c in P0COLS)))

# Characters (is_dummy=0 so the dashboard sees them). interplanetary_consolidation → max_planets-1.
chars = [
    (999100, "Test Alpha", 5),   # CCU5, IC5 → 6 planets
    (999101, "Test Bravo", 5),
    (999102, "Test Idle",  2),   # IC2 → 3 planets, none used
]
for cid, nm, ic in chars:
    cur.execute(
        "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
        "command_center_upgrades, context_id, is_dummy) VALUES (?,?,?,?,?,0)",
        (cid, nm, ic, 5, CTX))


def planet(cid, pn, ptype, is_ext, products=None, sim_state=None, p0_name=None, p0_tid=None):
    cur.execute(
        "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, solar_system_id, "
        "upgrade_level, num_pins, is_extractor, p0_type_id, p0_name, planet_num, products, sim_state) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, cid * 100 + pn, ptype, SYS_ID, 5, 10, is_ext, p0_tid, p0_name, pn,
         json.dumps(products) if products else None, sim_state))


# Test Alpha: 1 Coolant factory (planet 4 Barren) + 1 Water extractor (planet 5)
planet(999100, 4, "Barren", 0, products=[{"type_id": COOLANT, "name": "Coolant"}])
planet(999100, 5, "Temperate", 1, sim_state=sim(WATER, "Water", 9000), p0_name="Aqueous Liquids", p0_tid=AQUEOUS)
# Test Bravo: Electrolytes extractor (planet 1 Storm, UNDER-supplied) + Water extractor (planet 6)
planet(999101, 1, "Storm", 1, sim_state=sim(ELECTRO, "Electrolytes", 500), p0_name="Ionic Solutions", p0_tid=IONIC)
planet(999101, 6, "Barren", 1, sim_state=sim(WATER, "Water", 9000), p0_name="Aqueous Liquids", p0_tid=AQUEOUS)
# Test Idle: no colonies → 3 free slots.

con.commit()
con.close()
print(f"Seeded test context {CTX}: 3 chars, {len(planets)} Planet-DB rows in {SYS}.")
print("Electrolytes is the bottleneck (4,300/day supplied). Idle toon has 3 free slots.")
