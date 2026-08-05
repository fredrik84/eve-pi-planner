"""
Solar-system typeahead (`GET /api/systems/search`, app/planetary.py `search_systems`).

The account-level "reaction / build system" setting is resolved by EXACT name and rejected if it
doesn't match, so a typo there is a silent no-op that leaves every job-installation fee estimate
light. The typeahead exists so the right spelling is one click away. What must stay true:

  * a query shorter than the minimum returns nothing (no full-table scan on a single keystroke),
  * matching is case-insensitive and substring-based (it must work the same on Postgres, whose
    LIKE is case-sensitive — hence the LOWER() on both sides),
  * a hit carries enough to disambiguate: constellation, region and security,
  * exact/prefix matches sort above longer names that merely contain the query,
  * the result count is capped,
  * the endpoint NEVER raises — the field it feeds is free text that validates on save, so an
    empty list has to be a working fallback, not an error the UI must handle.

Seeds real rows into `system_geo`/`constellations` and cleans up after itself, same approach as
test_page_access.py. Run inside the container:
    docker exec eve-pi-planner-web-1 python3 test_system_search.py
"""

import sys

sys.path.insert(0, ".")
from app.sde import get_connection  # noqa: E402
from app.planetary import search_systems  # noqa: E402

CONSTEL = "ZZ Test Constellation"
REGION = "ZZ Test Region"
# Long name first on purpose: it is a substring match for every query below, so it is the row
# that proves the ordering rules rather than the alphabet.
SYSTEMS = [
    ("ZZtestia Prime", CONSTEL, 0.87, 39900001),
    ("ZZtestia", CONSTEL, -0.31, 39900002),
    ("ZZtestib", CONSTEL, 0.0, 39900003),
]

_failures = []


def check(cond, msg):
    ok = bool(cond)
    print(f"  {'PASS' if ok else 'FAIL'}: {msg}")
    if not ok:
        _failures.append(msg)
    return ok


def _cleanup():
    con = get_connection()
    for name, *_ in SYSTEMS:
        con.execute("DELETE FROM system_geo WHERE system=?", (name,))
    con.execute("DELETE FROM constellations WHERE name=?", (CONSTEL,))
    con.commit()
    con.close()


def _seed():
    con = get_connection()
    con.execute("INSERT INTO constellations (name, region) VALUES (?,?)", (CONSTEL, REGION))
    for row in SYSTEMS:
        con.execute(
            "INSERT INTO system_geo (system, constellation, security, system_id) VALUES (?,?,?,?)",
            row,
        )
    con.commit()
    con.close()


def _names(res):
    return [r["system"] for r in res["results"]]


def main():
    print("System typeahead (/api/systems/search)")
    _cleanup()
    _seed()
    try:
        print("\nMinimum query length")
        for q in ("", " ", "Z", " z "):
            check(search_systems(q) == {"results": []}, f"query {q!r} returns nothing")

        print("\nMatching")
        res = search_systems("zztesti")
        check(set(_names(res)) == {s[0] for s in SYSTEMS},
              "lowercase query finds all three seeded systems (case-insensitive)")
        check(_names(search_systems("ZZTESTIA")) == ["ZZtestia", "ZZtestia Prime"],
              "uppercase query matches, exact/prefix before the longer name")
        check("ZZtestia Prime" in _names(search_systems("estia Pri")),
              "mid-name substring matches (not prefix-only)")
        check(search_systems("zz-no-such-system-zz")["results"] == [],
              "no match returns an empty list, not an error")

        print("\nEach hit disambiguates")
        hit = next(r for r in search_systems("zztesti")["results"] if r["system"] == "ZZtestia")
        check(hit["constellation"] == CONSTEL, "constellation returned")
        check(hit["region"] == REGION, "region returned (joined from the constellations table)")
        check(hit["security"] == -0.3, "security returned, rounded to one decimal")
        check(hit["system_id"] == 39900002, "solar_system_id returned")

        print("\nResult cap")
        # 'a' alone is under the minimum; a two-char query against real SDE data matches far more
        # than 25 systems, so this is a live check of the cap rather than a seeded one.
        wide = search_systems("an")["results"]
        check(len(wide) <= 25, f"broad query capped at 25 rows (got {len(wide)})")

        print("\nNever raises")
        try:
            bad = search_systems("%_'\";--")
            check(isinstance(bad.get("results"), list), "quote/wildcard input still returns a list")
        except Exception as e:  # noqa: BLE001
            check(False, f"SQL-ish input raised {e!r}")
    finally:
        _cleanup()

    print(f"\n{'FAILURES: ' + str(len(_failures)) if _failures else 'All checks passed.'}")
    for f in _failures:
        print(f"  - {f}")
    return 1 if _failures else 0


if __name__ == "__main__":
    sys.exit(main())
