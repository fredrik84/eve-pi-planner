"""The SDE recipe graphs and their cache, the EVE material formula, and reachability."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

# ── SDE recipe graph loaders ──────────────────────────────────────────────────────────────────

# The two recipe graphs are STATIC SDE data — ~4,800 blueprints and every material row — and they
# were rebuilt from scratch on every plan call: 68ms locally, more against Postgres, and the
# manufacturing page runs several plans per load. They're read-only to every consumer (verified: no
# caller mutates a recipe), so one process-level copy serves all of them. The TTL is only a backstop
# for an SDE rebuild under a long-lived process; a deploy restarts the pod anyway.
_GRAPH_CACHE: dict[str, tuple[float, dict]] = {}
_GRAPH_TTL = 900.0


def _cached_graph(key: str, con, loader) -> dict[int, dict]:
    import time as _t
    hit = _GRAPH_CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < _GRAPH_TTL:
        return hit[1]
    graph = loader(con)
    _GRAPH_CACHE[key] = (now, graph)
    return graph


def clear_graph_cache():
    """Drop the cached recipe graphs — for an SDE rebuild that has to take effect immediately."""
    _GRAPH_CACHE.clear()


def load_manufacturing_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`. `_load_manufacturing_graph` does the real work."""
    return _cached_graph("mfg", con, _load_manufacturing_graph)


def load_reaction_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`."""
    return _cached_graph("rx", con, _load_reaction_graph)


def _load_manufacturing_graph(con) -> dict[int, dict]:
    """product_type_id -> {blueprint_type_id, output_qty, base_time, max_runs, inputs}. If a
    product is (rarely) made by more than one blueprint, keep the lowest blueprint id so the graph
    is deterministic."""
    mats: dict[int, list[dict]] = {}
    for r in con.execute("SELECT blueprint_type_id, type_id, quantity FROM blueprint_materials"):
        mats.setdefault(r["blueprint_type_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute(
        "SELECT blueprint_type_id, product_type_id, output_qty, base_time, max_runs FROM blueprints"
    ):
        prod = r["product_type_id"]
        if prod in graph and graph[prod]["blueprint_type_id"] <= r["blueprint_type_id"]:
            continue
        graph[prod] = {
            "blueprint_type_id": r["blueprint_type_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["base_time"] or 0,
            "max_runs": r["max_runs"] or 0,
            "inputs": mats.get(r["blueprint_type_id"], []),
        }
    return graph


def _load_reaction_graph(con) -> dict[int, dict]:
    """product_type_id -> {reaction_id, output_qty, base_time, inputs}. Same tables app/reactions
    reads; loaded directly here (no cross-package import) to keep the make-or-buy core standalone.
    First formula wins on the rare double-output product."""
    inputs: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        prod = r["output_type_id"]
        if prod in graph:
            continue
        graph[prod] = {
            "reaction_id": r["reaction_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["cycle_time"] or 0,
            "inputs": inputs.get(r["reaction_id"], []),
        }
    return graph


# ── EVE material / time formulas ──────────────────────────────────────────────────────────────

def effective_material_qty(base_qty: int, runs: int, me_pct: float, bonus_mult: float) -> int:
    """CCP's per-job material formula: max(runs, ceil(round(baseQty·runs·(1−ME/100)·bonus, 2))).
    The max(runs, …) floor means a material never drops below 1 per run however good the ME."""
    if base_qty <= 0 or runs <= 0:
        return 0
    q = base_qty * runs * (1 - me_pct / 100.0) * bonus_mult
    return max(runs, math.ceil(round(q, 2)))


def _producer(type_id: int, mfg: dict, rx: dict) -> tuple[str | None, dict | None]:
    if type_id in mfg:
        return "manufacturing", mfg[type_id]
    if type_id in rx:
        return "reaction", rx[type_id]
    return None, None


def collect_reachable(target: int, mfg: dict, rx: dict) -> set[int]:
    """Every type_id reachable from the target through the producer graph (target + all recipe
    inputs, recursively), cycle-guarded — the set to price in one market call."""
    seen: set[int] = set()

    def walk(tid: int):
        if tid in seen:
            return
        seen.add(tid)
        _, recipe = _producer(tid, mfg, rx)
        if recipe:
            for inp in recipe["inputs"]:
                walk(inp["type_id"])

    walk(target)
    return seen
