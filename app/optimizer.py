"""
LP-based production optimizer.

Objective (two-part, single pass):
  1. Fill the supplied order as much as possible (high-priority weight M per unit).
  2. Use remaining inventory for any other producible items to minimise waste
     (weight = total inputs consumed per unit, so high-density items are preferred).

Both goals are combined into one LP so the solver finds the globally-optimal
split of shared resources between competing ordered items automatically.

Ordered items are capped at their order quantity; non-ordered items are uncapped.
Fractional LP solution is floored to integers before output.
"""

from app.pi import parse_inventory, resolve_inventory, compute_producible_costs


def _find_missing_inputs(
    output_type_id: int,
    target_tier: int,
    inventory: dict[int, int],
    schematics: dict[int, dict],
    types: dict[int, dict],
    _seen: set | None = None,
) -> list[str]:
    """Return names of inputs that are absent from inventory for this item's chain."""
    if _seen is None:
        _seen = set()
    if output_type_id in _seen:
        return []
    _seen.add(output_type_id)

    type_info = types.get(output_type_id)
    item_tier = type_info.get("pi_tier") if type_info else None

    if output_type_id in inventory and (item_tier is None or item_tier < target_tier):
        return []

    sch = schematics.get(output_type_id)
    if sch is None:
        name = type_info["name"] if type_info else str(output_type_id)
        return [name]

    missing: list[str] = []
    for inp in sch["inputs"]:
        missing.extend(_find_missing_inputs(
            inp["type_id"], target_tier, inventory, schematics, types, _seen
        ))
    return missing


def optimize_production(
    inventory_text: str,
    order_text: str,
    pi_data: dict,
) -> dict:
    # numpy/highspy are only ever needed here, so import lazily — keeps them off the cold-start
    # path for every request that isn't /api/optimize.
    import numpy as np
    import highspy

    types = pi_data["types"]
    schematics = pi_data["schematics"]
    name_to_id = pi_data["name_to_id"]

    # ── Parse inventory ────────────────────────────────────────────────────────
    raw_inv = parse_inventory(inventory_text)
    inventory, inv_unknown = resolve_inventory(raw_inv, name_to_id, types)

    if not inventory:
        return {
            "plan": [], "leftover": {}, "utilization": 0.0,
            "not_producible": [], "unknown_items": inv_unknown,
            "total_isk": 0.0,
        }

    # ── Parse order (optional) ─────────────────────────────────────────────────
    order: dict[int, int] = {}
    ord_unknown: list[str] = []
    if order_text and order_text.strip():
        raw_order = parse_inventory(order_text)
        order, ord_unknown = resolve_inventory(raw_order, name_to_id, types)

    # ── Build cost matrix ──────────────────────────────────────────────────────
    all_costs = compute_producible_costs(inventory, pi_data)

    # Items in order that can't be produced from current inventory, with missing inputs
    not_producible = []
    for tid in order:
        if tid not in all_costs and tid in types:
            tier = types[tid].get("pi_tier", 2)
            missing = sorted(set(_find_missing_inputs(tid, tier, inventory, schematics, types)))
            not_producible.append({"name": types[tid]["name"], "missing": missing})

    if not all_costs:
        return {
            "plan": [],
            "leftover": {types[tid]["name"]: qty for tid, qty in inventory.items() if tid in types},
            "utilization": 0.0,
            "not_producible": not_producible,
            "unknown_items": inv_unknown + ord_unknown,
            "total_isk": 0.0,
        }

    inv_ids = sorted(inventory.keys())
    out_ids = sorted(all_costs.keys())
    n_res, n_out = len(inv_ids), len(out_ids)

    b = np.array([float(inventory[tid]) for tid in inv_ids])

    # C[resource_row, output_col] = inputs consumed per output unit
    C = np.zeros((n_res, n_out))
    for col, out_id in enumerate(out_ids):
        for row, inv_id in enumerate(inv_ids):
            C[row, col] = all_costs[out_id].get(inv_id, 0.0)

    total_cost_per_unit = C.sum(axis=0)
    max_cost = float(total_cost_per_unit.max()) if total_cost_per_unit.max() > 0 else 1.0

    # Weights: ordered items get priority M >> utilization weight
    M = max_cost * 1e4
    weights = np.array([
        M if out_ids[col] in order else total_cost_per_unit[col]
        for col in range(n_out)
    ], dtype=float)

    # Bounds: ordered items capped at order qty, others uncapped
    bounds = [
        (0.0, float(order[out_ids[col]])) if out_ids[col] in order else (0.0, None)
        for col in range(n_out)
    ]

    # ── Solve LP ───────────────────────────────────────────────────────────────
    # Same HiGHS solver scipy.optimize.linprog(method="highs") delegates to internally, called
    # directly — highspy has no scipy/numpy-suite dependency chain, just numpy itself.
    h = highspy.Highs()
    h.silent()
    hvars = h.addVariables(
        n_out,
        lb=[lo for lo, _hi in bounds],
        ub=[(hi if hi is not None else highspy.kHighsInf) for _lo, hi in bounds],
    )
    h.maximize(sum(float(weights[col]) * hvars[col] for col in range(n_out) if weights[col] != 0))
    for row in range(n_res):
        terms = [float(C[row, col]) * hvars[col] for col in range(n_out) if C[row, col] != 0]
        if terms:
            h.addConstr(sum(terms) <= float(b[row]))
    h.run()
    status = h.getModelStatus()

    if status != highspy.HighsModelStatus.kOptimal:
        return {
            "plan": [],
            "leftover": {types[tid]["name"]: qty for tid, qty in inventory.items() if tid in types},
            "utilization": 0.0,
            "not_producible": not_producible,
            "unknown_items": inv_unknown + ord_unknown,
            "error": h.modelStatusToString(status),
            "total_isk": 0.0,
        }

    x = np.floor(np.maximum(np.array(h.getSolution().col_value), 0.0))

    # ── Build plan ─────────────────────────────────────────────────────────────
    plan = []
    for col, out_id in enumerate(out_ids):
        qty = int(x[col])
        order_qty = order.get(out_id, 0)
        if qty == 0 and order_qty == 0:
            continue

        type_info = types.get(out_id, {})
        tier = type_info.get("pi_tier")
        # Inputs sorted by cost descending — used for factory split display
        inputs = [
            {"name": types[inv_id]["name"], "per_unit": round(cpu, 4)}
            for inv_id, cpu in sorted(all_costs.get(out_id, {}).items(), key=lambda kv: -kv[1])
            if inv_id in types
        ]
        plan.append({
            "type_id": out_id,
            "name": type_info.get("name", str(out_id)),
            "tier": {2: "P2", 3: "P3", 4: "P4"}.get(tier, f"P{tier}"),
            "quantity": qty,
            "order_qty": order_qty or None,
            "fill_pct": round(min(100.0, qty / order_qty * 100), 1) if order_qty else None,
            "is_ordered": out_id in order,
            "sell_price": 0.0,
            "total_isk": 0.0,
            "inputs": inputs,
        })

    # Sort: ordered items first by fill_pct desc, then non-ordered by quantity desc
    plan.sort(key=lambda r: (not r["is_ordered"], -(r["fill_pct"] or 0), -r["quantity"]))

    # ── Leftover ───────────────────────────────────────────────────────────────
    consumed = C @ x
    leftover = {}
    for row, inv_id in enumerate(inv_ids):
        remaining = int(round(inventory[inv_id] - consumed[row]))
        if remaining > 0:
            leftover[types[inv_id]["name"]] = remaining

    utilization = consumed.sum() / b.sum() * 100 if b.sum() > 0 else 0.0

    return {
        "plan": plan,
        "leftover": leftover,
        "utilization": round(utilization, 1),
        "not_producible": not_producible,
        "unknown_items": inv_unknown + ord_unknown,
        "total_isk": 0.0,
    }
