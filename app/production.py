"""Shared contracts for production tools; domain planning stays in its owning package.

Reactions and Manufacturing both describe character slot pools, but they do not reserve them the
same way. These helpers align the shape and the game-rule arithmetic without hiding that difference
behind a mode branch in either planner.
"""

MAX_ACTIVITY_SLOTS = 11


def skill_slot_count(primary_level: int | None, advanced_level: int | None) -> int:
    """EVE activity slots: one base plus both multiplier skills, clamped to the real maximum."""
    return min(MAX_ACTIVITY_SLOTS,
               1 + max(0, int(primary_level or 0)) + max(0, int(advanced_level or 0)))


def capacity_pool(total: int | float, available: int | float) -> dict:
    """Stable API shape for one activity pool, with impossible/stale inputs made harmless."""
    total_n = max(0, int(total or 0))
    available_n = max(0, min(total_n, int(available or 0)))
    return {"total": total_n, "available": available_n,
            "committed": total_n - available_n}


def capacity_contract(*, reservation_model: str, **pools: tuple[int, int]) -> dict:
    """Describe one or more pools and state what `available` means for this planner.

    `reserved` means a saved assignment consumes a slot before ESI sees the job (Reactions).
    `scheduled` means the plan may place later waves without reserving today's slot (Manufacturing).
    """
    if reservation_model not in {"reserved", "scheduled"}:
        raise ValueError("reservation_model must be 'reserved' or 'scheduled'")
    return {"reservation_model": reservation_model,
            "pools": {name: capacity_pool(*values) for name, values in pools.items()}}
