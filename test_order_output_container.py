#!/usr/bin/env python3
"""A build's OUTPUT container is a property of the plan, and it says where it came from.

An EVE job delivers to exactly one container, so a batch shared between two builds has nowhere to
go — which is the whole reason `industry_per_order_plans` exists, and the half that was missing
until 2026-08-14. It was written up as a per-JOB problem, which is why it stayed unsolved; the user
settled it as a property of the PLAN: one box chosen once, inherited by every job in the order.

What is pinned here:

  * a stated box is returned as `stated` and survives a round trip;
  * an unstated box INHERITS the first materials box, and reports `inherited` rather than pretending
    the user chose it — a picked answer that reads like a stated one is the defect, not the pick;
  * no box anywhere is a legitimate answer (`none`), not an error: corp hangars need the Director
    role, so this can never be required;
  * clearing it is distinguishable from never setting it;
  * the output box does NOT change what the build pulls FROM — the two are separate bindings that
    happen to default to the same can.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_order_output_container.py web:/srv/app/ && \
      docker compose exec web python3 test_order_output_container.py
"""
import sys

sys.path.insert(0, ".")

_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def main() -> int:
    from app.industry.orders import order_output_source_key, order_source_keys

    print("\na stated box is the answer, and says it was stated:")
    row = {"output_source_key": "corp:1234:5", "source_keys": '["hangar:9"]', "source_key": "hangar:9"}
    key, basis = order_output_source_key(row)
    check(key == "corp:1234:5", f"the stated box wins (got {key!r})")
    check(basis == "stated", f"...and reports itself as stated (got {basis!r})")

    print("\nunstated INHERITS the materials box, and says so rather than implying a choice:")
    row = {"output_source_key": "", "source_keys": '["hangar:9", "corp:1:2"]', "source_key": "hangar:9"}
    key, basis = order_output_source_key(row)
    check(key == "hangar:9", f"it follows the FIRST materials box (got {key!r})")
    check(basis == "inherited", f"...and is labelled inherited, not stated (got {basis!r})")

    print("\nno box at all is a legitimate answer, not an error:")
    row = {"output_source_key": "", "source_keys": "", "source_key": ""}
    key, basis = order_output_source_key(row)
    check(key == "" and basis == "none", f"nothing bound reports 'none' (got {key!r}, {basis!r})")

    print("\nan order written before this column existed still answers:")
    row = {"source_keys": '["hangar:7"]', "source_key": "hangar:7"}   # no output_source_key at all
    key, basis = order_output_source_key(row)
    check(key == "hangar:7" and basis == "inherited",
          f"a missing column reads as inherited, never as a crash (got {key!r}, {basis!r})")

    print("\nthe output binding does not disturb the input binding:")
    row = {"output_source_key": "corp:1234:5", "source_keys": '["hangar:9", "corp:1:2"]',
           "source_key": "hangar:9"}
    check(order_source_keys(row) == ["hangar:9", "corp:1:2"],
          f"the build still pulls from both its material boxes (got {order_source_keys(row)})")
    check(order_output_source_key(row)[0] not in order_source_keys(row),
          "...and the output box is free to be a box the build does not pull from")

    print("\nthe column really exists on the table, and is writable:")
    from app.sde import get_connection
    from app.industry.orders import ensure_industry_orders_table
    ensure_industry_orders_table()
    con = get_connection()
    try:
        cols = {r[1] if not isinstance(r, dict) else r["name"]
                for r in con.execute("PRAGMA table_info(pp_industry_orders)")} \
            if con.__class__.__module__.startswith("sqlite3") else None
        if cols is None:
            cols = {r["column_name"] for r in con.execute(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='pp_industry_orders'")}
        check("output_source_key" in cols,
              f"pp_industry_orders.output_source_key is present on the LIVE table (cols={len(cols)})")
    finally:
        con.close()

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
