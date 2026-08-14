#!/usr/bin/env python3
"""Two orders cannot both plan jobs off the same print.

A blueprint is an item and it is LOCKED while a job runs on it. Planned as one batch that was
already respected; planned per order (`industry_per_order_plans`) each order was built on its own
and saw the whole holding, so two orders each planned up to `prints` concurrent jobs off the SAME
original — 2f-residual #2.

**What is pinned is the claim arithmetic, not a whole schedule.** `_less_claimed` is the single
point where an earlier order's claim reaches the print cap, and these are its contract:

  * with nothing claimed the count is returned untouched — the aggregated plan, which never sets
    `prints_claimed`, must be byte-for-byte what it always was;
  * a claim reduces what is left;
  * it FLOORS AT 1 on purpose: an order with no print left still has to plan its jobs, and emitting
    zero would be a plan that cannot be executed rather than one that is merely optimistic. This is
    the known residue — it bounds the over-booking, it does not model a print being handed on when
    a job ends.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_print_locking.py web:/srv/app/ && \
      docker compose exec web python3 test_print_locking.py
"""
import sys

sys.path.insert(0, ".")

_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


class _P:
    """Just enough of BuildParams for the helper under test."""
    def __init__(self, claimed=None):
        if claimed is not None:
            self.prints_claimed = claimed


def main() -> int:
    from app.industry.schedule import _less_claimed

    print("\nthe aggregated plan is untouched — nothing claimed, nothing subtracted:")
    check(_less_claimed(_P(), 4, 7) == 7, "no attribute at all leaves the count alone")
    check(_less_claimed(_P({}), 4, 7) == 7, "an empty claim map leaves it alone")
    check(_less_claimed(_P({99: 3}), 4, 7) == 7, "a claim on a DIFFERENT type leaves it alone")

    print("\nan earlier order's claim reduces what is left:")
    check(_less_claimed(_P({4: 2}), 4, 7) == 5, "7 prints, 2 claimed -> 5 (got %d)"
          % _less_claimed(_P({4: 2}), 4, 7))
    check(_less_claimed(_P({4: 6}), 4, 7) == 1, "7 prints, 6 claimed -> 1")

    print("\n...and floors at 1 rather than emitting a plan that cannot run:")
    check(_less_claimed(_P({4: 7}), 4, 7) == 1, "every print claimed still leaves 1")
    check(_less_claimed(_P({4: 99}), 4, 7) == 1, "over-claiming cannot go below 1 or negative")
    check(_less_claimed(_P({4: 1}), 4, 1) == 1, "one print, one claim -> still 1 (the known residue)")

    print("\nthe single-print case is the one that motivated this:")
    before = 1          # what an order used to see with one owned BPO
    after = _less_claimed(_P({4: 1}), 4, 1)
    check(after <= before, "a second order never sees MORE than the first did")

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
