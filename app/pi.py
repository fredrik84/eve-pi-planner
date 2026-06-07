"""
PI production chain calculator.

Supports mixed-tier inventories (P1 + P2 + P3). Higher-tier items in inventory
are used directly as inputs rather than being re-derived from P1.

Results are grouped by absolute PI tier (P2/P3/P4) and calculated independently
per output item (as if all available resources are dedicated to that one product).
"""

import re
from typing import Optional


def parse_inventory(text: str) -> dict[str, int]:
    """
    Parse EVE inventory paste into {item_name: quantity}.

    Handles two column orderings from the EVE UI:
      Format A: Name | Quantity | Category | ...  (quantity is 2nd column)
      Format B: Name | Category | Quantity         (category is 2nd column)

    Quantities may use spaces as thousands separators (European locale).
    """
    inventory: dict[str, int] = {}
    for line in text.strip().splitlines():
        line = line.strip()
        if not line:
            continue

        if "\t" in line:
            parts = line.split("\t")
        else:
            parts = re.split(r" {2,}", line)

        if len(parts) < 2:
            # Fallback for single-space separator: treat the trailing number as qty
            m = re.match(r'^(.+?)\s+(\d[\d,\s]*)\s*$', line)
            if m:
                parts = [m.group(1).strip(), m.group(2).strip()]
            else:
                continue

        name = parts[0].strip()
        col1 = parts[1].strip()

        # Detect format: if the 2nd column is purely numeric (with optional
        # space/comma thousands separators) it's Format A; otherwise Format B.
        if re.match(r"^[\d\s,]+$", col1) and re.search(r"\d", col1):
            qty_str = col1                          # Format A
        elif len(parts) >= 3:
            qty_str = parts[2].strip()              # Format B
        else:
            continue

        qty_clean = re.sub(r"[^\d]", "", qty_str)
        if not qty_clean:
            continue
        try:
            qty = int(qty_clean)
            if qty > 0 and name:
                inventory[name] = inventory.get(name, 0) + qty
        except ValueError:
            continue

    return inventory


def resolve_inventory(
    raw: dict[str, int],
    name_to_id: dict[str, int],
    types: dict[int, dict],
) -> tuple[dict[int, int], list[str]]:
    resolved: dict[int, int] = {}
    unknown: list[str] = []
    for name, qty in raw.items():
        tid = name_to_id.get(name.lower())
        if tid is None:
            unknown.append(name)
        else:
            resolved[tid] = resolved.get(tid, 0) + qty
    return resolved, unknown


def _cost_per_output_unit(
    output_type_id: int,
    target_tier: int,
    schematics: dict[int, dict],
    types: dict[int, dict],
    inventory: dict[int, int],
    _cache: dict,
) -> Optional[dict[int, float]]:
    """
    Cost of one unit of output_type_id expressed as inventory items.

    Recursion stops at any inventory item whose PI tier < target_tier, meaning
    we consume it directly rather than tracing it back to lower-tier inputs.

    This correctly handles mixed-tier inventories:
      - P2 output (target_tier=2): stops at P1 items in inventory
      - P3 output (target_tier=3): stops at P2 (or P1) items in inventory
      - P4 output (target_tier=4): stops at P3 (or P2 or P1) items in inventory

    Returns {inventory_type_id: qty_per_output_unit}, or None if not producible.
    Cache key is (output_type_id, target_tier) so the same item can be costed
    differently depending on which tier's production we're computing.
    """
    key = (output_type_id, target_tier)
    if key in _cache:
        return _cache[key]

    type_info = types.get(output_type_id)
    if type_info is None:
        _cache[key] = None
        return None

    item_tier = type_info.get("pi_tier")

    # Base case: item is in inventory and is a lower tier than what we're targeting.
    # Stops at whatever tier is actually in inventory (P1, P2, or P3), so a mixed
    # inventory is used correctly — P2 items on hand are consumed directly rather
    # than re-derived from P1.
    if output_type_id in inventory and (item_tier is None or item_tier < target_tier):
        result = {output_type_id: 1.0}
        _cache[key] = result
        return result

    sch = schematics.get(output_type_id)
    if sch is None:
        _cache[key] = None
        return None

    output_qty = sch["output_qty"]
    if output_qty <= 0:
        _cache[key] = None
        return None

    cost: dict[int, float] = {}
    for inp in sch["inputs"]:
        inp_cost = _cost_per_output_unit(
            inp["type_id"], target_tier, schematics, types, inventory, _cache
        )
        if inp_cost is None:
            _cache[key] = None
            return None
        for base_id, unit_cost in inp_cost.items():
            cost[base_id] = cost.get(base_id, 0.0) + unit_cost * inp["quantity"] / output_qty

    if not cost:
        _cache[key] = None
        return None

    _cache[key] = cost
    return cost


def compute_producible_costs(
    inventory: dict[int, int],
    pi_data: dict,
) -> dict[int, dict[int, float]]:
    """
    Returns {output_type_id: {input_type_id: cost_per_unit}} for every P2/P3/P4
    item that can be fully produced from the given inventory.
    """
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    _cache: dict = {}
    costs: dict[int, dict[int, float]] = {}

    for output_type_id, type_info in types.items():
        tier = type_info.get("pi_tier")
        if tier not in (2, 3, 4):
            continue
        cost = _cost_per_output_unit(output_type_id, tier, schematics, types, inventory, _cache)
        if cost is None:
            continue
        if not all(base_id in inventory for base_id in cost):
            continue
        costs[output_type_id] = cost

    return costs


def calculate_production(
    inventory_text: str,
    pi_data: dict,
) -> dict:
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    name_to_id = pi_data["name_to_id"]

    raw_inv = parse_inventory(inventory_text)
    inventory, unknown = resolve_inventory(raw_inv, name_to_id, types)

    if not inventory:
        return {
            "inventory": raw_inv,
            "unknown_items": unknown,
            "results": {"p2": [], "p3": [], "p4": []},
        }

    _cache: dict = {}
    results: dict[str, list] = {"p2": [], "p3": [], "p4": []}
    tier_label = {2: "p2", 3: "p3", 4: "p4"}

    for output_type_id, type_info in types.items():
        tier = type_info.get("pi_tier")
        label = tier_label.get(tier)
        if label is None:
            continue

        cost = _cost_per_output_unit(
            output_type_id, tier, schematics, types, inventory, _cache
        )
        if cost is None:
            continue

        # All required base resources must be in inventory
        if not all(base_id in inventory for base_id in cost):
            continue

        # Max output = bottleneck across all required base resources
        max_output = float("inf")
        limiting_id = None
        for base_id, per_unit in cost.items():
            if per_unit <= 0:
                continue
            can_make = inventory[base_id] / per_unit
            if can_make < max_output:
                max_output = can_make
                limiting_id = base_id

        if max_output == float("inf") or max_output <= 0:
            continue

        max_output_int = int(max_output)
        if max_output_int <= 0:
            continue

        input_costs = []
        for base_id, per_unit in sorted(cost.items(), key=lambda x: -x[1]):
            base_name = types[base_id]["name"] if base_id in types else str(base_id)
            input_costs.append({
                "name": base_name,
                "per_unit": round(per_unit, 4),
                "total_used": int(per_unit * max_output_int),
                "available": inventory[base_id],
            })

        limiting_name = types[limiting_id]["name"] if limiting_id and limiting_id in types else ""

        results[label].append({
            "type_id": output_type_id,
            "name": type_info["name"],
            "max_output": max_output_int,
            "limiting_input": limiting_name,
            "p1_costs": input_costs,
        })

    for key in results:
        results[key].sort(key=lambda x: -x["max_output"])

    inv_display = {types[tid]["name"]: qty for tid, qty in inventory.items() if tid in types}

    return {
        "inventory": inv_display,
        "unknown_items": unknown,
        "results": results,
    }
