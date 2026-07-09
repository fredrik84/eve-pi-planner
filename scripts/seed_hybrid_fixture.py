"""Seed STABLE test contexts for verifying /api/my-setup-plan's "Current setup" derivation —
both the hybrid-colony exclusion fix and the combined-entry feature built on top of it.

Built after the Aiden Lorrent bug report (2026-07-09): a player whose factories were ALL hybrid
colonies saw Setup Analysis report ~0% fed on almost everything, because my-setup-plan was
including hybrid colonies' self-consumed P1 needs as account-wide demand, while the supply side
(_producersOf in analysis.js) correctly excludes hybrid colonies' own output as non-exportable —
demand the supply accounting could structurally never satisfy. Fixed by excluding is_hybrid=1
planets from my-setup-plan entirely (they get their own analysis via _hybridReseatSection).

Then, from a SocomTedd report the same day: a player with multiple deployed products had to
switch the Refill tool between each product's "Current setup" plan one at a time, and two
products sharing a P1 input had no shared bookkeeping between their separate views (risk of
double-allocating a pasted stack). Fixed by adding one more synthetic "All factories combined"
entry whenever an account has 2+ distinct current products — same flat shape as any other plan,
consumption summed per P1 across all of them.

Creates THREE separate test contexts (each representing an independent player account — reusing
TST-BB's already-seeded planets/system from the 999001 dashboard fixture is safe since colony
data is scoped per-character, not per-planet):
  999002 / Test Dana  : ONLY a hybrid colony (extraction + chained Mechanical Parts factory) —
                        mirrors Aiden's all-hybrid setup. derive_setup_plans(999002) must be EMPTY.
  999003 / Test Erin  : a hybrid colony (Mechanical Parts, self-consumed) PLUS a genuine standalone
                        factory (Coolant) — proves a real standalone factory still surfaces
                        normally even when hybrid colonies are also present on the same account.
  999004 / Test Fiona : TWO genuine standalone factories (Mechanical Parts, Enriched Uranium) that
                        both consume Precious Metals — mirrors SocomTedd's report. Must produce 3
                        plans: the two individual ones plus a combined entry whose Precious Metals
                        consumption is the SUM of both.

Idempotent: wipes all three contexts first. Run inside the container:
  docker compose exec -T web python3 scripts/seed_hybrid_fixture.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection
from app.sde import load_pi_data

CTX_HYBRID_ONLY = 999002
CTX_MIXED = 999003
CTX_COMBINED = 999004
pi = load_pi_data()
types = pi["types"]
NAME = {t.get("name"): tid for tid, t in types.items()}

con = get_connection()
cur = con.cursor()

for ctx in (CTX_HYBRID_ONLY, CTX_MIXED, CTX_COMBINED):
    cur.execute("DELETE FROM pp_char_planets WHERE character_id IN (SELECT character_id FROM pp_characters WHERE context_id=?)", (ctx,))
    cur.execute("DELETE FROM pp_characters WHERE context_id=?", (ctx,))


def add_char(cid, nm, ctx, ic=5, ccu=5):
    cur.execute(
        "INSERT INTO pp_characters (character_id, character_name, interplanetary_consolidation, "
        "command_center_upgrades, context_id, is_dummy) VALUES (?,?,?,?,?,0)", (cid, nm, ic, ccu, ctx))


def add_colony(cid, sid, pn, ptype, is_ext, product=None, out_name=None, per_day=None, is_hybrid=0):
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
        "upgrade_level, num_pins, is_extractor, p0_name, planet_num, products, sim_state, is_hybrid) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (cid, cid * 100 + pn, ptype, sid, 5, 10, is_ext, out_name, pn,
         json.dumps(prods) if prods else None, sim, is_hybrid))


TST_BB = 99990002  # reuses scripts/seed_test_fixture.py's already-seeded system/planets

add_char(999200, "Test Dana", CTX_HYBRID_ONLY)
add_char(999201, "Test Erin", CTX_MIXED)
add_char(999202, "Test Fiona", CTX_COMBINED)

# Test Dana (999002): ONLY a hybrid colony — extraction + chained Mechanical Parts factory on one
# planet. Mirrors Aiden's all-hybrid setup exactly. Planet number reused from TST-BB is fine —
# colony rows are scoped per-character, not per-planet, so this doesn't collide with 999001/999003.
add_colony(999200, TST_BB, 1, "Lava", 1, product="Mechanical Parts",
           out_name="Reactive Metals", per_day=9000, is_hybrid=1)

# Test Erin (999003): a hybrid colony (same shape as Dana's) PLUS a genuine standalone factory
# (Coolant, is_extractor=0) — proves the standalone one still surfaces normally when a hybrid
# colony is also present on the same account.
add_colony(999201, TST_BB, 2, "Lava", 1, product="Mechanical Parts",
           out_name="Reactive Metals", per_day=9000, is_hybrid=1)
add_colony(999201, TST_BB, 3, "Barren", 0, product="Coolant")

# Test Fiona (999004): two standalone factories that both consume Precious Metals (Mechanical
# Parts ← Reactive Metals + Precious Metals; Enriched Uranium ← Toxic Metals + Precious Metals).
add_colony(999202, TST_BB, 4, "Barren", 0, product="Mechanical Parts")
add_colony(999202, TST_BB, 5, "Barren", 0, product="Enriched Uranium")

con.commit()
con.close()
print(f"Seeded hybrid/combined test contexts: {CTX_HYBRID_ONLY} (Test Dana, hybrid-only), "
      f"{CTX_MIXED} (Test Erin, hybrid + standalone Coolant factory), "
      f"{CTX_COMBINED} (Test Fiona, two standalone factories sharing Precious Metals).")
