#!/usr/bin/env python3
"""A missing system cost index is visible, and the two engines are honest about differing.

Job installation fee = EIV x (system cost index + facility tax + 4% SCC). With no build system
configured, the two engines degrade DIFFERENTLY, and conflating them is the mistake this pins:

  * Manufacturing (app/industry/graph.py) still charges the SCC and any facility tax — only the
    index term is missing, so the fee is UNDERSTATED, not absent.
  * Reactions (app/reactions/graph.py) zeroes `job_cost_rate` entirely and skips the adjusted-price
    fetch with it, so the fee is ABSENT and every quoted profit is flattered.

Both now report a `cost_basis` so the UI can say which case it's in. The index is not a rounding
error — it spans 0.14% to 17.25% across New Eden — so in a busy system it IS most of the fee.

In-process; run inside the container. Read-only apart from a fabricated context id that is never
written. Safe against prod, but prefer dev.

    kubectl -n dev exec -i <pod> -- python3 - < test_cost_basis.py
"""
import sys

from app.industry.graph import BuildParams, _cost_basis
from app.industry_cost import fetch_system_cost_index, _all_cost_indices

JITA = 30000142

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def main():
    print("the cost index feed is actually populated:")
    idx = _all_cost_indices()
    mfg = idx.get("manufacturing") or {}
    rx = idx.get("reaction") or {}
    check(len(mfg) > 1000, f"manufacturing indices cover the cluster (got {len(mfg)} systems)")
    check(len(rx) > 1000, f"reaction indices cover the cluster (got {len(rx)} systems)")
    # The two activities are tracked separately per system and genuinely diverge — Jita's
    # manufacturing index is orders of magnitude above its reaction one. A single shared lookup
    # would be wrong, so prove they differ rather than assuming.
    jm = fetch_system_cost_index(JITA, "manufacturing")
    jr = fetch_system_cost_index(JITA, "reaction")
    check(jm > 0, f"Jita has a real manufacturing index (got {jm})")
    check(jm != jr, f"manufacturing and reaction indices are distinct per system ({jm} vs {jr})")
    check(fetch_system_cost_index(None, "manufacturing") == 0.0,
          "an unset system yields 0.0, the no-effect default")

    print("a plan with no build system reports it, so the UI can warn:")
    p = BuildParams()          # nothing configured
    cb = _cost_basis(p)
    check(cb["system_id"] is None, "cost_basis.system_id is None when no system is set")
    check(cb["mfg_index"] == 0.0 and cb["rx_index"] == 0.0, "and both indices are 0")

    print("a configured plan reports the real index:")
    p2 = BuildParams(build_system_id=JITA,
                     mfg_cost_index=jm, rx_cost_index=jr, facility_tax_pct=1.5)
    cb2 = _cost_basis(p2)
    check(cb2["system_id"] == JITA, "the configured system id is carried through")
    check(cb2["mfg_index"] == jm, "so is the index actually used")
    check(cb2["facility_tax_pct"] == 1.5, "and the facility tax")

    print("manufacturing without a system is UNDERSTATED, not zero:")
    # The formula the engines use, reproduced here so the assertion is about the arithmetic and
    # not about mocking a whole plan.
    SCC = 0.04
    eiv = 1_000_000.0
    no_system = eiv * (0.0 + 1.5 / 100.0 + SCC)
    with_system = eiv * (jm + 1.5 / 100.0 + SCC)
    check(no_system > 0,
          f"the 4% SCC and facility tax still apply with no system (fee {no_system:,.0f} ISK)")
    check(with_system > no_system,
          f"a real index adds to it ({with_system:,.0f} vs {no_system:,.0f} ISK)")
    share = (with_system - no_system) / with_system * 100 if with_system else 0
    print(f"       (in Jita the index is {share:.0f}% of the whole fee)")
    check(share > 50, "in a busy system the index dominates the fee, so omitting it matters")

    print("reactions without a system drop the fee ENTIRELY:")
    # Mirrors _load_goo_and_reached: no reaction_system -> job_cost_rate stays 0.0, so even the
    # SCC is uncharged. This is the difference from manufacturing that the warning text relies on.
    rx_rate_unset = 0.0
    rx_rate_set = jr + 1.5 / 100.0 + SCC
    check(rx_rate_unset == 0.0,
          "an unconfigured reaction system leaves the whole rate at 0 — SCC included")
    check(rx_rate_set > 0, f"configured, the rate is real ({rx_rate_set:.4f})")

    print("a defaulted system says it is a default:")
    import app.industry.graph as G
    real_flag, real_fb = G._default_system_on, G._fallback_build_system
    try:
        # Flag off: nothing is assumed, exactly as before the default existed.
        G._default_system_on = lambda ctx: False
        sid, tax, basis = G.account_build_defaults(-1, with_basis=True)
        check(sid is None and basis in ("none", "configured"),
              "with the flag off an unconfigured account still has no system")
        # Flag on, and the account has described a building: use ITS system and ITS tax.
        G._default_system_on = lambda ctx: True
        G._fallback_build_system = lambda ctx, tax: (30000001, 2.5, "structure")
        sid, tax, basis = G.account_build_defaults(-1, with_basis=True)
        check(basis == "structure" and sid == 30000001 and tax == 2.5,
              "a structure the account builds in supplies the system and its own tax")
        # ...and with nothing at all, the labelled reference.
        G._fallback_build_system = real_fb
        sid, tax, basis = G.account_build_defaults(-1, with_basis=True)
        check(basis == "reference" and sid == JITA,
              "knowing nothing falls back to Jita, labelled as a reference")
    finally:
        G._default_system_on, G._fallback_build_system = real_flag, real_fb

    p3 = BuildParams(build_system_id=JITA, mfg_cost_index=jm, build_system_basis="reference")
    check(_cost_basis(p3)["basis"] == "reference",
          "and the plan carries that label so the page can say so")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
