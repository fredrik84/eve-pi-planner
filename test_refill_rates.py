"""
Per-factory refill rates: the numbers "refill to a deadline" (TODO §21) is computed from.

The deadline split asks one question of every factory — how much P1 does THIS planet burn per day,
and in what step does it eat it — and the plan-total consumption figure cannot answer it once a
combined plan sums two products that share a P1. So each factory carries its own `units_per_day`
and `units_per_run` on `p1_inputs`. These pin both.

Parts 1 and 2 run in-process (the SDE, plus a seeded throwaway account) — inside the container:

    docker exec eve-pi-planner-web-1 python3 test_refill_rates.py

Part 3 checks the SAME invariant on a real account's deployed factories (the numbers a player is
actually shown) — needs a live server with DEBUG_PI + DEBUG_CONTEXT_ID set:

    python3 test_refill_rates.py --url https://eveindustry.net

Both halves FAIL rather than pass quietly when they find nothing to assert against.
"""

import argparse
import json
import sys
import urllib.request

sys.path.insert(0, ".")

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


# ── Part 1: the draw-down step, from the SDE ──────────────────────────────────
def test_batch_sizes():
    from app.planner import _compute_p1_fracs, _p1_batch_sizes
    from app.sde import load_pi_data

    pi = load_pi_data()
    types, schematics = pi["types"], pi["schematics"]
    by_tier = {t: [tid for tid, ty in types.items() if ty.get("pi_tier") == t and tid in schematics]
               for t in (2, 3, 4)}

    print("\nevery P1 in a product's chain has a batch size, at every tier:")
    for tier in (2, 3, 4):
        bad = []
        for tid in by_tier[tier]:
            fracs = _compute_p1_fracs(tid, pi)
            batch = _p1_batch_sizes(tid, pi)
            if not fracs:
                continue
            if set(batch) != set(fracs) or any(b <= 0 for b in batch.values()):
                bad.append(types[tid].get("name", tid))
        check(not bad, f"P{tier}: every traced P1 got a positive batch "
                       f"({len(by_tier[tier])} products{'' if not bad else ' — missing: ' + ', '.join(bad[:3])})")

    print("\nthe batch is the quantity the BASIC factory eats, not the final schematic's:")
    # P1→P2 is the first on-planet step whatever the final tier, so a P4's P1 batches must equal
    # the batches of the P2s in its chain — this is what makes rounding a P4 refill to "whole runs"
    # mean anything. A regression that took the final schematic's input quantity instead would
    # hand back a P3/P4 quantity here.
    for tid in by_tier[4][:1]:
        batch = _p1_batch_sizes(tid, pi)
        for p1, qty in batch.items():
            direct = [inp["quantity"] for out_tid, s in schematics.items() for inp in s["inputs"]
                      if inp["type_id"] == p1 and types.get(out_tid, {}).get("pi_tier") == 2]
            check(bool(direct) and qty in direct,
                  f"{types[tid]['name']} / {types[p1]['name']}: batch {qty} is a P1→P2 input quantity")
        break   # one P4 is enough to pin the shape; the loop above covers coverage

    print("\na P2's own batch is simply its schematic's input quantity:")
    tid = by_tier[2][0]
    sch = pi["schematics"][tid]
    want = {i["type_id"]: i["quantity"] for i in sch["inputs"]
            if types.get(i["type_id"], {}).get("pi_tier") == 1}
    check(_p1_batch_sizes(tid, pi) == want,
          f"{types[tid]['name']}: {json.dumps({str(k): v for k, v in want.items()})}")


# ── Part 2: per-factory rates sum to the plan total ───────────────────────────
# The point of putting units_per_day on each factory is that the plan total can no longer be
# divided back out. It still has to ADD back up, or the deadline split and the refill cadence the
# rest of the app quotes describe different colonies.
def _check_plans(plans, where):
    checked = 0
    for p in plans:
        facs = [f for f in p.get("factories", []) if f.get("p1_inputs")]
        if not facs:
            continue
        name = p.get("name", "?")
        check(all(i.get("units_per_day") is not None for f in facs for i in f["p1_inputs"]),
              f"{where} {name}: every factory input carries its own units_per_day")
        check(all(i.get("units_per_run") for f in facs for i in f["p1_inputs"]),
              f"{where} {name}: ...and the run size it eats them in")
        per_fac = {}
        for f in facs:
            for i in f["p1_inputs"]:
                per_fac[i["p1_type_id"]] = per_fac.get(i["p1_type_id"], 0) + (i.get("units_per_day") or 0)
        for c in p.get("consumption", []):
            got, want = per_fac.get(c["p1_type_id"], 0), c["units_per_day"]
            check(abs(got - want) <= len(facs),
                  f"{where} {name} / {c['p1_name']}: per-factory rates sum to the plan total "
                  f"({got} vs {want}, ±{len(facs)} rounding)")
            checked += 1
    return checked


FAKE_CTX = 778101
FAKE_CID = 991101


def _seed_factories(product_counts):
    """A throwaway account running `n` factory planets of each given product. Several factories of
    ONE product is the case that matters: with one factory each, per-factory and plan-total are the
    same number and nothing is being tested."""
    from app.sde import get_connection
    con = get_connection()
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET context_id=excluded.context_id",
                (FAKE_CID, "Refill Rates Fixture", FAKE_CTX))
    pid = 88810000
    for tid, name, n in product_counts:
        for i in range(n):
            pid += 1
            con.execute(
                "INSERT INTO pp_char_planets (character_id, planet_id, planet_type, planet_num, "
                "is_extractor, products, pad_inputs) VALUES (?,?,?,?,?,?,?)",
                (FAKE_CID, pid, "Barren", i + 1, 0,
                 json.dumps([{"type_id": tid, "name": name}]), "[]"))
    con.commit()
    con.close()


def _cleanup():
    from app.sde import get_connection
    con = get_connection()
    con.execute("DELETE FROM pp_char_planets WHERE character_id=?", (FAKE_CID,))
    con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
    con.commit()
    con.close()


def test_setup_plans():
    """The real derivation, over a seeded account with several factories per product."""
    from app.planner_advisor import derive_setup_plans
    from app.sde import load_pi_data

    pi = load_pi_data()
    types = pi["types"]
    # Two products that SHARE a P1, so the combined entry sums consumption across them — the case
    # a plan total and a share cannot be divided back into one factory's rate.
    from app.planner import _compute_p1_fracs
    p2s = [tid for tid, t in types.items() if t.get("pi_tier") == 2 and tid in pi["schematics"]]
    pair = None
    for a in p2s:
        for b in p2s:
            if a < b and set(_compute_p1_fracs(a, pi)) & set(_compute_p1_fracs(b, pi)):
                pair = (a, b)
                break
        if pair:
            break
    products = [(pair[0], types[pair[0]]["name"], 3), (pair[1], types[pair[1]]["name"], 2)]
    print(f"\nper-factory rates add up to the plan total "
          f"({products[0][1]} ×3 + {products[1][1]} ×2, sharing a P1):")
    _cleanup()
    try:
        _seed_factories(products)
        plans = derive_setup_plans(FAKE_CTX)
        checked = _check_plans(plans, "fixture:")
        combined = [p for p in plans if p.get("tier") == 99]
        check(bool(combined), "the combined multi-product entry is derived (the shared-P1 case)")
        # A green run that asserted nothing is the failure mode this test exists to avoid.
        check(checked > 0, f"{checked} P1 totals checked")
    finally:
        _cleanup()


# ── Part 3: the same invariant on a live account's real factories ─────────────
def test_live(url):
    print(f"\nlive per-factory rates from {url}/api/my-setup-plan:")
    try:
        with urllib.request.urlopen(f"{url}/api/my-setup-plan", timeout=60) as r:
            plans = json.loads(r.read()).get("plans", [])
    except Exception as e:
        check(False, f"could not reach the endpoint ({e}) — needs DEBUG_PI + DEBUG_CONTEXT_ID")
        return
    if not _check_plans(plans, "live"):
        check(False, "NOT CHECKED — the debug context has no deployed factory planets, so the "
                     "live half asserted nothing; point --url at an account that runs factories")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="run the live half against this server instead of the in-process half")
    args = ap.parse_args()
    if args.url:
        test_live(args.url.rstrip("/"))
    else:
        test_batch_sizes()
        test_setup_plans()
    print("\n" + ("FAILED: " + "; ".join(FAILS) if FAILS else "all checks passed"))
    sys.exit(1 if FAILS else 0)
