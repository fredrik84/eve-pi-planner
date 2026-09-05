#!/usr/bin/env python3
"""Running reaction jobs have consumed their materials and must leave the shopping list."""
import sys

sys.path.insert(0, ".")


def main():
    from app.reactions.graph import _rows_still_needing_materials

    rows = [
        {"character_id": 1, "type_id": 10, "runs": 120},
        {"character_id": 1, "type_id": 10, "runs": 120},
        {"character_id": 1, "type_id": 20, "runs": 100},
        {"character_id": 2, "type_id": 10, "runs": 120},
    ]
    jobs = {
        1: [
            {"product_type_id": 10, "runs": 120, "status": "active"},
            {"product_type_id": 99, "runs": 1, "status": "delivered"},
        ],
        2: [{"product_type_id": 10, "runs": 120, "status": "paused"}],
    }
    pending = _rows_still_needing_materials(rows, jobs)
    assert [(r["character_id"], r["type_id"]) for r in pending] == [(1, 10), (1, 20)]
    print("  ok   one live job covers exactly one same-character/product plan row")
    print("  ok   active and paused jobs are excluded; delivered jobs are not")

    # A manual mark belongs to the newest matching chain that existed at click time. The next
    # recurring cycle may use the same character/product/stage but was not bought or consumed yet.
    overlap = [
        {"character_id": 1, "type_id": 10, "runs": 120, "tier_order": 0,
         "created_at": 100.0, "last_completed_at": None},
        {"character_id": 1, "type_id": 10, "runs": 120, "tier_order": 0,
         "created_at": 200.0, "last_completed_at": None},
        {"character_id": 1, "type_id": 20, "runs": 50, "tier_order": 1,
         "created_at": 100.0, "last_completed_at": 150.0},
    ]
    marks = [{"character_id": 1, "type_id": 10, "tier_order": 0, "jobs": -1,
              "state": "done", "marked_at": 150.0}]
    pending = _rows_still_needing_materials(overlap, {}, marks)
    assert [(r["type_id"], r["created_at"]) for r in pending] == [(10, 200.0)]
    print("  ok   consumed/completed old-cycle rows leave the list without hiding the new cycle")
    print("all checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
