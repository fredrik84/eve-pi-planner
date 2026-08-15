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

    round_trip()
    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


def round_trip() -> None:
    """The WRITE path, through the real endpoint functions.

    The helper above is pure and was green while the write path was broken: a 2026-08-14 edit nested
    `source_keys=?` inside `if req.output_source_key is not None:`, so every PATCH that bound
    containers WITHOUT also sending an output box silently dropped the whole set and never set
    `sources_owned` — the core mechanic of per-plan sources, returning 200 the whole time. Testing
    the helper alone is what let that ship, so this exercises create and update for real.
    """
    from app.industry.orders import (OrderCreate, OrderUpdate, create_order, update_order,
                                     ensure_industry_orders_table)
    from app.sde import get_connection
    ensure_industry_orders_table()
    ctx = 424242                      # a context of its own, so nothing real is touched
    # Any buildable type will do — the binding is what is under test, not the recipe. Looked up
    # rather than hardcoded so a different SDE cannot turn this into a false failure.
    tid = None
    # The same two tables `create_order` validates against, so this can never skip for a reason
    # the endpoint would not also have.
    with get_connection() as con:
        r = con.execute("SELECT product_type_id AS t FROM blueprints "
                        "UNION SELECT output_type_id AS t FROM reactions LIMIT 1").fetchone()
        if r:
            tid = int(dict(r)["t"])
    check(tid is not None, "a buildable type exists to test the write path with")
    if tid is None:
        return
    made = create_order(OrderCreate(product_type_id=tid, quantity=1,
                                    source_keys=["hangar:a", "corp:b:1"],
                                    output_source_key="corp:out:9"), ctx=ctx)
    oid = made["id"]
    try:
        print("\ncreate stores BOTH bindings, not one at the cost of the other:")
        check(made.get("output_source_key") == "corp:out:9",
              f"the output box is persisted by create (got {made.get('output_source_key')!r})")
        check(made.get("source_keys") == ["hangar:a", "corp:b:1"],
              f"...and the material boxes survive alongside it (got {made.get('source_keys')})")

        print("\nTHE REGRESSION: binding containers WITHOUT an output box must not drop them:")
        up = update_order(oid, OrderUpdate(source_keys=["hangar:x", "hangar:y"]), ctx=ctx)
        check(up.get("source_keys") == ["hangar:x", "hangar:y"],
              f"the new set is stored (got {up.get('source_keys')})")
        with get_connection() as con:
            row = dict(con.execute(
                "SELECT source_keys, sources_owned, output_source_key FROM pp_industry_orders "
                "WHERE id=?", (oid,)).fetchone())
        check((row.get("source_keys") or "") not in ("", "null"),
              f"...and really reached the column, not just the response (got {row.get('source_keys')!r})")
        check(int(row.get("sources_owned") or 0) == 1,
              f"...and the plan took OWNERSHIP of its stock (sources_owned={row.get('sources_owned')})")
        check((row.get("output_source_key") or "") == "corp:out:9",
              "...while leaving the output box it never mentioned alone")

        print("\nand setting only the output box must not disturb the material boxes:")
        up = update_order(oid, OrderUpdate(output_source_key="corp:other:2"), ctx=ctx)
        check(up.get("output_source_key") == "corp:other:2", "the output box is updated")
        check(up.get("source_keys") == ["hangar:x", "hangar:y"],
              f"...and the material set is untouched (got {up.get('source_keys')})")

        print("\nOWNERSHIP is set by the UPDATE itself, not inherited from create:")
        # An order created WITH source_keys already has sources_owned=1, so asserting it after an
        # update proves nothing — removing the `sources_owned=?` write from update_order left the
        # first version of this test green. This order is created bare, so only the update can set it.
        bare = create_order(OrderCreate(product_type_id=tid, quantity=1), ctx=ctx)
        bid = bare["id"]
        with get_connection() as con:
            r0 = dict(con.execute("SELECT sources_owned FROM pp_industry_orders WHERE id=?",
                                  (bid,)).fetchone())
        check(int(r0.get("sources_owned") or 0) == 0, "a bare order owns nothing to begin with")
        update_order(bid, OrderUpdate(source_keys=["hangar:z"]), ctx=ctx)
        with get_connection() as con:
            r1 = dict(con.execute("SELECT sources_owned, source_keys FROM pp_industry_orders "
                                  "WHERE id=?", (bid,)).fetchone())
        check(int(r1.get("sources_owned") or 0) == 1,
              f"binding a box through UPDATE takes ownership (got {r1.get('sources_owned')})")
        update_order(bid, OrderUpdate(source_keys=[]), ctx=ctx)
        with get_connection() as con:
            r2 = dict(con.execute("SELECT sources_owned FROM pp_industry_orders WHERE id=?",
                                  (bid,)).fetchone())
        check(int(r2.get("sources_owned") or 0) == 0,
              f"...and clearing every box hands it back to the account pool (got {r2.get('sources_owned')})")

        print("\nthe output box resolves to a NAME, not a swallowed ImportError:")
        # `output_source_name` was null on every order for a day because its import named the wrong
        # module and a bare `except` ate the error. Nothing asserted it, so nothing noticed.
        named = update_order(bid, OrderUpdate(output_source_key="hangar:z"), ctx=ctx)
        check("output_source_name" in named,
              "the field is present on the payload at all")
        # Resolve the import the way the CODE writes it, not the way this test would like it. A
        # bare `except` around that import means a wrong module is silent — `output_source_name` was
        # null on every order for a day — so asserting "it imports cleanly from somewhere" proves
        # nothing. This reads the actual import statement and checks that module really defines it.
        import ast as _ast
        import importlib as _il
        _src = open("app/industry/orders.py", encoding="utf-8").read()
        _mods = [n.module for n in _ast.walk(_ast.parse(_src))
                 if isinstance(n, _ast.ImportFrom)
                 and any(a.name == "source_name" for a in n.names)]
        check(bool(_mods), f"orders.py imports source_name from somewhere (got {_mods})")
        for _m in _mods:
            _ok = False
            try:
                _ok = hasattr(_il.import_module(_m), "source_name")
            except Exception:
                _ok = False
            check(_ok, f"...and `{_m}` actually defines it — a wrong module here is swallowed silently")

        print("\nclearing is distinguishable from never setting:")
        up = update_order(oid, OrderUpdate(output_source_key=""), ctx=ctx)
        check(up.get("output_source_basis") in ("inherited", "none"),
              f"cleared falls back rather than staying stated (got {up.get('output_source_basis')!r})")
    finally:
        with get_connection() as con:
            con.execute("DELETE FROM pp_industry_orders WHERE context_id=?", (ctx,))
            con.commit()


if __name__ == "__main__":
    sys.exit(main())
