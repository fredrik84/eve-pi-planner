"""
Pure unit tests for planner.reseat_redeploy_events — dating the last reseat and last redeploy from a
colony's per-program head-centroid trail.

Classification of each program-to-program centroid move:
  • d <= _RESEAT_MOVE          → same-spot restart (heads didn't move)
  • _RESEAT_MOVE < d <= reach  → reseat (heads shuffled within the ECU's reachable area)
  • d > reach                  → redeploy (command centre moved beyond extraction range)

reach comes from the colony's CURRENT ext_heads (max footprint radius). With no current head data
reach is unknown, so a far jump can't be told from a reseat and every move counts as a reseat.

Runs in-process (no server) — but must run INSIDE the container so `app` imports resolve:
    docker compose exec -T web python3 test_reseat_events.py
"""

import sys

from app.planner import reseat_redeploy_events, _colony_reach


def check(cond, msg):
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return cond


# A single ECU whose heads sit within ~0.05 rad of the centre → reach ≈ 0.05 (+0 head_radius).
# Moves of 0.02 are reseats (within reach); a 0.30 jump is a redeploy (beyond reach).
NEAR = [{"p0": 1, "r": 0.0, "c": [1.0, 0.5], "h": [[1.05, 0.5], [0.95, 0.5], [1.0, 0.55]]}]


def test_reach():
    print("\n== reach from ext_heads ==")
    r = _colony_reach(NEAR)
    ok = check(r is not None and 0.04 < r < 0.06, f"reach ~0.05 from a compact ECU (got {r})")
    ok &= check(_colony_reach(None) is None, "no ext_heads → reach None")
    ok &= check(_colony_reach([]) is None, "empty ext_heads → reach None")
    return ok


def test_reseat_only():
    print("\n== reseats within reach ==")
    # Three programs, each shifted 0.02 rad (> _RESEAT_MOVE 0.01, < reach 0.05) → all reseats.
    samples = [(100, "1.00,0.50"), (200, "1.02,0.50"), (300, "1.04,0.50")]
    ev = reseat_redeploy_events(samples, NEAR)
    ok = check(ev["reseat_at"] == 300, f"last reseat dated to the newest move (got {ev['reseat_at']})")
    ok &= check(ev["redeploy_at"] is None, "no redeploy when every move stays within reach")
    return ok


def test_redeploy():
    print("\n== a jump beyond reach is a redeploy ==")
    # reseat at 200 (0.02), redeploy at 300 (0.30 > reach), reseat again at 400 (0.02).
    samples = [(100, "1.00,0.50"), (200, "1.02,0.50"), (300, "1.32,0.50"), (400, "1.34,0.50")]
    ev = reseat_redeploy_events(samples, NEAR)
    ok = check(ev["redeploy_at"] == 300, f"redeploy dated to the far jump (got {ev['redeploy_at']})")
    ok &= check(ev["reseat_at"] == 400, f"last reseat is the newest within-reach move (got {ev['reseat_at']})")
    return ok


def test_restart_ignored():
    print("\n== same-spot restart is not a move ==")
    # Identical centroids across programs (a plain restart) → neither dated.
    samples = [(100, "1.00,0.50"), (200, "1.00,0.50"), (300, "1.005,0.50")]  # 0.005 < _RESEAT_MOVE
    ev = reseat_redeploy_events(samples, NEAR)
    ok = check(ev["reseat_at"] is None and ev["redeploy_at"] is None, "sub-threshold shuffles ignored")
    return ok


def test_no_reach_all_reseats():
    print("\n== unknown reach → far jumps still count as reseats, never redeploys ==")
    # No current ext_heads (older/factory colony): a 0.30 jump can't be proven a redeploy.
    samples = [(100, "1.00,0.50"), (200, "1.30,0.50")]
    ev = reseat_redeploy_events(samples, None)
    ok = check(ev["reseat_at"] == 200, f"move dated as a reseat (got {ev['reseat_at']})")
    ok &= check(ev["redeploy_at"] is None, "redeploy stays undated without a reach yardstick")
    return ok


def test_degenerate():
    print("\n== too little / missing data ==")
    ok = check(reseat_redeploy_events([], NEAR) == {"reseat_at": None, "redeploy_at": None}, "empty history")
    ok &= check(reseat_redeploy_events([(100, "1.0,0.5")], NEAR)["reseat_at"] is None, "one program → no transition")
    # Missing centroids are skipped, not crashed on.
    ev = reseat_redeploy_events([(100, None), (200, "1.0,0.5"), (300, "1.03,0.5")], NEAR)
    ok &= check(ev["reseat_at"] == 300, "a null centroid is skipped, later real moves still dated")
    return ok


def main():
    results = [test_reach(), test_reseat_only(), test_redeploy(), test_restart_ignored(),
               test_no_reach_all_reseats(), test_degenerate()]
    print("\n" + "=" * 50)
    if all(results):
        print("  ALL TESTS PASSED")
        sys.exit(0)
    print(f"  {results.count(False)} TEST(S) FAILED")
    sys.exit(1)


if __name__ == "__main__":
    main()
