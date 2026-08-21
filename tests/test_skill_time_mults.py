#!/usr/bin/env python3
"""Job-time multipliers use the account's REAL skills wherever we can prove we have them.

`account_industry_time_mults` used to collapse to `if ind == 0 and adv == 0: ind = adv = 5`, which
silently upgraded an untrained account to V/V and quoted it a build ~47% faster than it can do.

The subtlety, and the reason this test exists: a 0 in `industry`/`advanced_industry` has two
meanings. Those columns were added to `pp_characters` at different times, so a character last
scanned before a given migration reads 0 for that column while its older columns are populated —
stale, not untrained. Prod proved this the hard way on 2026-08-01: two accounts show Mass
Production V alongside Industry 0, which the game does not permit (the SDE lists Industry III as a
prerequisite of Mass Production). So neither the ESI skills scope NOR a populated neighbouring
column proves anything about a particular column.

The only sound signal is a `pp_char_skills` row set — the full ESI skill list, which records
absence as well as presence. Everything else falls back to V/V and SAYS it is assuming. Both
directions are asserted below.

In-process; run inside the container against a NON-PROD database. Seeds characters under a
fabricated context id and removes them in a finally.

    kubectl -n dev exec -i <pod> -- python3 - < tests/test_skill_time_mults.py
"""
import sys

try: import _bootstrap  # noqa: F401
except ModuleNotFoundError: from tests import _bootstrap  # noqa: F401
from app.db import get_connection
from app.industry.graph import account_industry_time_mults

CTX = -98763
SCOPE = "esi-skills.read_skills.v1"
INDUSTRY_SKILL_ID = 3380

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _seed(con, cid, name, industry, advanced, mass_production=0, full_scan=False):
    """A character, and optionally the authoritative full skill list that makes it trustworthy."""
    con.execute(
        "INSERT INTO pp_characters (character_id, character_name, context_id, scopes, "
        "industry, advanced_industry, mass_production) VALUES (?,?,?,?,?,?,?)",
        (cid, name, CTX, SCOPE, industry, advanced, mass_production))
    if full_scan:
        # One row is enough: its PRESENCE is what marks the character as fully scanned.
        con.execute("INSERT INTO pp_char_skills (character_id, skill_type_id, level) VALUES (?,?,?)",
                    (cid, INDUSTRY_SKILL_ID, industry))


def _reset(con):
    con.execute("DELETE FROM pp_char_skills WHERE character_id <= -9200 AND character_id >= -9299")
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def main():
    from app.industry.skills import ensure_char_skills_table
    ensure_char_skills_table()
    con = get_connection()
    try:
        _reset(con)

        print("no authoritative skill list anywhere → assume V/V, and admit it:")
        _seed(con, -9201, "NeverScanned", 0, 0)
        con.commit()
        mfg, rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "assumed", f"basis is 'assumed' (got {basis})")
        check(abs(mfg - 0.68) < 1e-9, f"manufacturing falls back to 0.68 (got {mfg})")
        check(abs(rx - 0.85) < 1e-9, f"reactions fall back to 0.85 (got {rx})")

        print("a STALE zero is not believed, even next to a populated column:")
        _reset(con)
        # Exactly the shape prod showed: Mass Production V with Industry 0 — impossible in game,
        # so the 0 is a column added after this character's last scan, not a fact about the pilot.
        _seed(con, -9210, "StaleColumns", 0, 0, mass_production=5)
        con.commit()
        mfg, rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "assumed",
              f"a populated neighbour column proves nothing about Industry (got {basis})")
        check(abs(mfg - 0.68) < 1e-9,
              f"so these accounts keep their existing timings, not a 47% jump (got {mfg})")

        print("a fully scanned character IS believed, zeros included:")
        _reset(con)
        _seed(con, -9202, "ScannedUntrained", 0, 0, full_scan=True)
        con.commit()
        mfg, rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "real", f"basis is 'real' once the full skill list exists (got {basis})")
        check(abs(mfg - 1.0) < 1e-9, f"genuinely untrained means NO time reduction (got {mfg})")
        check(abs(rx - 1.0) < 1e-9, f"reactions likewise (got {rx})")

        print("real partial training is used as-is:")
        _reset(con)
        _seed(con, -9203, "Partial", 3, 2, full_scan=True)
        con.commit()
        mfg, rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "real", "basis is 'real'")
        check(abs(mfg - (1 - 0.04 * 3) * (1 - 0.03 * 2)) < 1e-9,
              f"manufacturing = (1-.04*3)*(1-.03*2) (got {mfg:.4f})")
        check(abs(rx - (1 - 0.03 * 2)) < 1e-9, f"reactions use Advanced Industry only (got {rx:.4f})")

        print("the account's BEST character wins, per skill:")
        _reset(con)
        _seed(con, -9204, "GoodIndustry", 5, 0, full_scan=True)
        _seed(con, -9205, "GoodAdvanced", 0, 4, full_scan=True)
        con.commit()
        mfg, _rx, _b = account_industry_time_mults(CTX, with_basis=True)
        check(abs(mfg - (1 - 0.04 * 5) * (1 - 0.03 * 4)) < 1e-9,
              f"it takes the max of EACH skill across characters (got {mfg:.4f})")

        print("an unscanned character can't drag down a scanned one:")
        _reset(con)
        _seed(con, -9206, "Scanned", 5, 5, full_scan=True)
        _seed(con, -9207, "Unscanned", 0, 0)
        con.commit()
        mfg, _rx, basis = account_industry_time_mults(CTX, with_basis=True)
        check(basis == "real", "basis stays 'real' when at least one character is fully scanned")
        check(abs(mfg - 0.68) < 1e-9, f"the scanned V/V character sets the multiplier (got {mfg})")

        print("the plain 2-tuple call still works for existing callers:")
        res = account_industry_time_mults(CTX)
        check(isinstance(res, tuple) and len(res) == 2,
              f"without with_basis it returns exactly (mfg, rx) (got {res})")
    finally:
        _reset(con)
        con.close()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
