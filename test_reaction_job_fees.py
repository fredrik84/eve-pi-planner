#!/usr/bin/env python3
"""Job install fees are quoted for an account that never typed a system in.

The defect: `reaction_system` is a free-text field, and with it blank `job_cost_rate` stayed 0, so
install fees fell out of every estimate and the app reported the hole in a settings note instead of
filling it. That was survivable while "cost" meant the shopping list. It stopped being survivable on
2026-08-14, when profit began to be netted against a FULL cost base — a missing fee is then not an
absent line, it is profit the account does not have.

**What is pinned here is the invariant, not the number.** The fee depends on a live cost index and
on which structure the account has on file, so no assertion below hard-codes ISK. What must hold:

  * with the inference off and nothing typed, the fee is ZERO and the basis says `none` — the old
    behaviour survives untouched behind the flag (CLAUDE.md rule 2);
  * with it on and nothing typed, a system IS resolved and the basis names where it came from;
  * a typed system always wins, and reads as `configured` — an inference never overrides a
    statement;
  * whatever the basis, Reactions and Industry resolve the SAME system, because two services
    quoting one job at two fees is the defect this reuses one resolver to avoid.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_reaction_job_fees.py web:/srv/app/ && \
      docker compose exec web python3 /srv/app/test_reaction_job_fees.py
"""
import sys

sys.path.insert(0, ".")

_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _set_flag(key: str, state: str | None) -> None:
    """Force a flag's state for this test, or clear the row. Never asserts a default."""
    from app.db import get_connection
    with get_connection() as con:
        if state is None:
            con.execute("DELETE FROM pp_features WHERE key=?", (key,))
        else:
            con.execute(
                "INSERT INTO pp_features (key, state) VALUES (?,?) "
                "ON CONFLICT(key) DO UPDATE SET state=excluded.state", (key, state))
        con.commit()
    try:
        from app.features import _invalidate_feature_cache
        _invalidate_feature_cache()
    except Exception:
        pass


def main() -> int:
    from app.reactions.graph import _reaction_fee_system
    from app.industry.graph import account_build_defaults

    ctx = 1
    prior = {}
    for k in ("industry_default_build_system", "reactions_default_system"):
        try:
            from app.db import get_connection
            with get_connection() as con:
                r = con.execute("SELECT state FROM pp_features WHERE key=?", (k,)).fetchone()
            prior[k] = r["state"] if r else None
        except Exception:
            prior[k] = None

    try:
        print("\nwith the inference OFF, an unset system still means no fee — the old behaviour:")
        _set_flag("industry_default_build_system", "admin")
        _set_flag("reactions_default_system", "admin")
        sid, tax, basis = _reaction_fee_system(ctx, {"reaction_system": None, "facility_tax_pct": 0.0})
        check(sid is None, f"no system is resolved, so no fee is charged (got {sid})")
        check(basis == "none", f"...and the basis says so rather than implying a real answer (got '{basis}')")
        check(tax == 0.0, f"no facility tax either (got {tax})")

        print("\nwith it ON, the fee is worked out and the account is told from what:")
        _set_flag("reactions_default_system", "public")
        sid, tax, basis = _reaction_fee_system(ctx, {"reaction_system": None, "facility_tax_pct": 0.0})
        check(sid is not None, f"a system IS resolved with nothing typed in (got {sid})")
        check(basis in ("structure", "reference"),
              f"...and the basis names where it came from, never a bare number (got '{basis}')")

        print("\n...and the REACTIONS flag alone is enough — no Manufacturing flag required:")
        _set_flag("industry_default_build_system", "admin")
        sid2, _, basis2 = _reaction_fee_system(ctx, {"reaction_system": None, "facility_tax_pct": 0.0})
        check(sid2 == sid and basis2 == basis,
              f"the same answer on the Reactions flag alone (got {sid2}, '{basis2}')")

        print("\na typed system always wins — an inference never overrides a statement:")
        sid3, tax3, basis3 = _reaction_fee_system(ctx, {"reaction_system": "Jita", "facility_tax_pct": 0.05})
        check(basis3 == "configured", f"the basis reads as configured (got '{basis3}')")
        check(sid3 is not None, "and it resolves to a real system")
        check(abs(tax3 - 0.05) < 1e-9, f"the account's own facility tax is used, not a structure's (got {tax3})")

        print("\nwith BOTH inferences on, the two services resolve the SAME system:")
        _set_flag("reactions_default_system", "public")
        _set_flag("industry_default_build_system", "public")
        r_sid, _, _ = _reaction_fee_system(ctx, {"reaction_system": None, "facility_tax_pct": 0.0})
        i_sid, _, i_basis = account_build_defaults(ctx, with_basis=True)
        check(r_sid == i_sid,
              f"one job, one fee — they cannot diverge ({r_sid} vs {i_sid})")
        check(i_basis in ("structure", "reference"), f"on the same basis (got '{i_basis}')")

        print("\n...but the REACTIONS flag alone must not move an INDUSTRY quote:")
        # These are rollout-ladder flags, not per-account opt-ins. `_default_system_on` briefly
        # ORed the Reactions flag in, which would have let the Reactions rung start charging
        # inferred job fees on every Industry user's build — a money-moving change behind a switch
        # that never mentions Industry. Reactions reaches the same helper through its own gate.
        _set_flag("industry_default_build_system", "admin")
        _set_flag("reactions_default_system", "public")
        r_sid2, _, r_basis2 = _reaction_fee_system(ctx, {"reaction_system": None, "facility_tax_pct": 0.0})
        i_sid2, _, i_basis2 = account_build_defaults(ctx, with_basis=True)
        check(r_sid2 is not None, f"Reactions still infers on its own flag (got {r_sid2})")
        check(i_sid2 is None and i_basis2 == "none",
              f"...while Industry stays exactly where it was (got {i_sid2}, '{i_basis2}')")
    finally:
        for k, v in prior.items():
            _set_flag(k, v)

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
