"""What the account can actually REACT — and what it has to acquire before it can.

The rule this module changes, deliberately and in one place: **absent evidence never serialises
work**. A product nobody has said anything about stays uncapped everywhere else in this package
(`formula_concurrency_caps`, `_assigned_slot_capacity`, `_print_limits`) precisely so the tool
never refuses work the player can really do. That is right while the app genuinely does not know.

It stops being right once the user has stated their whole library. A real account declared ~238
formulas across 41 products, ordered Reinforced Carbon Fiber, and was told to react Carbon Fiber —
a sub-reaction whose formula it does not hold — because "not mentioned" was read as "unknown"
rather than "not owned". Once the library is complete, **absence IS knowledge**.

Two things follow, and they are the whole design:

* **Completeness is inferred from a PASTE, never from a toggle.** Pasting an industry window is a
  complete statement about what that window shows, the same reasoning `formula_print_floor` already
  applies when it lets a paste win outright over observed jobs. So: at least one pasted batch, and
  it has to contain formulas. No knob (CLAUDE.md rule 3) — a "my library is complete" checkbox is
  exactly the kind of manual config the paste already answers.
* **Report what to ACQUIRE; substitute nothing.** This module never removes a step, re-plans a
  chain, or flips a reaction to "buy it instead". It answers one question — *which formulas does
  this plan need that you do not hold* — in the shape `app/industry`'s `missing_blueprints` already
  uses: what, how many runs, what a contract costs. Like that report, nothing here is in any
  shopping list and nothing here is in any cost total. A plan that quietly swapped a step for a
  market buy would be making the player's decision for them with their ISK.

**The sharp edge, and why the unresolved-name plumbing exists.** Making absence load-bearing means
a formula whose NAME fails to resolve becomes indistinguishable from one the user does not own — so
a CCP rename turns into a confident, wrong "go buy this". That is not hypothetical: a client copy
carried `Fullerides Reaction Formula` where the SDE has `Fulleride Reaction Formula` (fixed in
`ee633be` with a product-name fallback). So every report this module produces carries the pasted
names that resolved to nothing (`paste_unresolved_names`), and the UI must show them beside the
finding rather than in a status line that scrolls away.
"""

from __future__ import annotations

from fastapi import Depends
from pydantic import BaseModel

from app.sde import get_connection
from app.esi import require_context
from app.reactions._router import router
from app.reactions._flags import flag_on
from app.reactions.graph import request_memo

FEATURE_KEY = "reactions_missing_formulas"


def _feature_on(context_id: int) -> bool:
    """Gated: this changes what the wizard, a manual assign and an order all say you can run, so it
    rolls out rather than lands (CLAUDE.md rule 2). Off ⇒ every report here is empty ⇒ the three
    surfaces read exactly as they did before."""
    return flag_on(FEATURE_KEY, context_id)


def _reaction_index() -> tuple[dict[int, int], dict[int, str]]:
    """({product_type_id: reaction_id}, {type_id: name}) for every reaction this SDE knows.

    `reaction_id` IS the formula item's type_id — the thing a contract lists and the thing
    `pp_asset_stock` counts — which is why it is also what the price lookup asks for.
    """
    con = get_connection()
    try:
        rx = {int(r["output_type_id"]): int(r["reaction_id"])
              for r in con.execute("SELECT reaction_id, output_type_id FROM reactions")}
    except Exception:
        return {}, {}                 # a manufacturing-only SDE knows no formulas at all
    try:
        ids = set(rx) | set(rx.values())
        names: dict[int, str] = {}
        ordered = sorted(ids)
        for i in range(0, len(ordered), 400):
            chunk = ordered[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(f"SELECT type_id, name FROM types WHERE type_id IN ({marks})",
                                 tuple(chunk)):
                names[int(r["type_id"])] = r["name"]
    except Exception:
        names = {}
    finally:
        con.close()
    return rx, names


def held_formula_products(context_id: int) -> set[int]:
    """Reaction products this account holds a formula for, on ANY of the evidence this app has.

    Deliberately the union of every source rather than the declaration alone — a formula seen in a
    corp scan, in a pasted hangar or on a running job is one the player can install with, whether
    or not they got round to declaring it. The set is used to say what is MISSING, so a wide
    reading is the safe direction here: the cost of counting a formula the user does not really
    have is a step they cannot run (which is the status quo), while the cost of missing one they do
    have is telling them to buy something already in their hangar.
    """
    def _build():
        try:
            from app.industry.blueprints import formula_print_floor, owned_blueprints
            owned = owned_blueprints(context_id)
            floor = formula_print_floor(context_id, owned)
        except Exception:
            return set()              # evidence unavailable is evidence absent — see library_state
        rx, _ = _reaction_index()
        return (set(owned) | set(floor)) & set(rx)

    # Reads every character's blueprint JSON, the asset table and every observed job — and an order
    # report asks for it twice over (here and in `formula_concurrency_caps`). Once per request.
    return set(request_memo(("held_formulas", context_id), _build))


def library_state(context_id: int) -> dict:
    """Memoised per request — see `_library_state`."""
    return request_memo(("library_state", context_id), lambda: _library_state(context_id))


def _library_state(context_id: int) -> dict:
    """Whether this account's formula library can be read as COMPLETE, and the caveats on it.

    `{complete, formulas_declared, batches, unresolved: [{name, batch_name}]}`.

    Complete means: the hand-declaration feature is on, and at least one PASTED batch (batch <> '',
    so never a row typed in one at a time) names at least one reaction formula. A paste is a
    statement about a whole window; typing in three formulas is a statement about three formulas.
    """
    out = {"complete": False, "formulas_declared": 0, "batches": 0, "unresolved": []}
    try:
        from app.industry.blueprints import (
            ensure_manual_blueprints_table, paste_unresolved_names, _manual_enabled)
        if not _manual_enabled(context_id):
            return out
        ensure_manual_blueprints_table()
        out["unresolved"] = paste_unresolved_names(context_id)
    except Exception:
        return out
    rx, _ = _reaction_index()
    if not rx:
        return out
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT DISTINCT COALESCE(batch,'') AS batch, type_id FROM pp_industry_blueprints "
            "WHERE context_id=? AND COALESCE(batch,'') <> ''", (context_id,)).fetchall()
    except Exception:
        return out
    finally:
        con.close()
    formulas = {int(r["type_id"]) for r in rows if int(r["type_id"]) in rx}
    out["formulas_declared"] = len(formulas)
    out["batches"] = len({r["batch"] for r in rows})
    out["complete"] = bool(formulas)
    return out


def missing_formulas(context_id: int, wanted: dict[int, int] | None,
                     names: dict[int, str] | None = None,
                     jobs: dict[int, int] | None = None) -> dict:
    """The report every surface renders: `{complete, formulas: [...], unresolved: [...]}`.

    `wanted` is {product_type_id: runs the plan wants} — every step of the plan, chain tiers
    included, since a chain tier is exactly the case that started this. A row carries the product,
    the runs asked of it, and the FORMULA's own type_id so the caller can price it
    (`/api/reactions/formula-prices`).

    `jobs` is {product_type_id: jobs the plan runs at once}, and it is **how many copies of the
    formula to buy** — a formula is a physical item locked into the reactor for the job's duration
    (the same fact `formula_concurrency_caps` enforces), so four parallel Carbon Fiber jobs need
    four formulas. Without it a row says what to buy and leaves "how many" to be worked out from a
    slot grid.

    Empty — not absent — when the flag is off or the library is not complete, so a caller never has
    to ask two questions to know whether to render anything. **Nothing here is a cost**: the
    formulas are what you must go and get, and pricing them into a plan total would quietly charge
    the player for a purchase they may already own, or may decide not to make.
    """
    state = library_state(context_id)
    report = {"complete": state["complete"], "formulas": [],
              "unresolved": state["unresolved"], "formulas_declared": state["formulas_declared"]}
    if not wanted or not state["complete"] or not _feature_on(context_id):
        return report
    rx, rx_names = _reaction_index()
    held = held_formula_products(context_id)
    names = names or {}
    rows = []
    for tid, runs in wanted.items():
        tid = int(tid)
        if tid not in rx or tid in held:
            continue                  # not a reaction at all, or a formula they hold
        rows.append({
            "type_id": tid,
            "name": names.get(tid) or rx_names.get(tid) or str(tid),
            "runs_needed": int(runs or 0),
            "formulas_needed": max(1, int((jobs or {}).get(tid, 1) or 1)),
            "formula_type_id": rx[tid],
            "formula_name": rx_names.get(rx[tid]) or f"#{rx[tid]}",
        })
    report["formulas"] = sorted(rows, key=lambda r: r["name"])
    return report


def jobs_from_sequence(steps) -> dict[int, int]:
    """{type_id: jobs run at once} from the same step lists `wanted_from_sequence` reads. A step
    that does not say (an older client, or a plan row, which IS one job) counts as one."""
    jobs: dict[int, int] = {}
    for s in steps or []:
        tid = s.get("type_id") if isinstance(s, dict) else getattr(s, "type_id", None)
        if not tid:
            continue
        n = s.get("job_count") if isinstance(s, dict) else getattr(s, "job_count", None)
        jobs[int(tid)] = jobs.get(int(tid), 0) + max(1, int(n or 1))
    return jobs


def wanted_from_sequence(steps) -> dict[int, int]:
    """{type_id: runs} from any of this package's step lists — a suggestion's chain_tiers plus its
    own product, an order's sequence, a manual assign's preview. One helper because three callers
    building the same dict three ways is how a chain tier gets left out of exactly one of them,
    which is the bug this whole module exists for.
    """
    wanted: dict[int, int] = {}
    for s in steps or []:
        tid = s.get("type_id") if isinstance(s, dict) else getattr(s, "type_id", None)
        if not tid:
            continue
        runs = s.get("runs") if isinstance(s, dict) else getattr(s, "runs", 0)
        wanted[int(tid)] = wanted.get(int(tid), 0) + max(0, int(runs or 0))
    return wanted


class MissingFormulaRequest(BaseModel):
    """One or more planned steps: `[{type_id, runs}]`. The manual-assign modal's shape, but nothing
    about it is manual-assign specific — it is "here is a plan, what am I short of"."""
    items: list[dict] = []


@router.post("/api/reactions/missing-formulas")
def reactions_missing_formulas(req: MissingFormulaRequest,
                               context_id: int = Depends(require_context)):
    """What a proposed set of reaction steps needs and this account does not hold.

    Generic on purpose (CLAUDE.md rule 4): the manual-assign modal asks about one product and its
    chain, but the endpoint knows nothing about manual assigns. The wizard and the customer-order
    report answer the same question inline, off the same helper, because they are already computing
    the step list server-side and a second round trip would only add a way for the two to disagree.
    """
    return missing_formulas(context_id, wanted_from_sequence(req.items))


@router.get("/api/reactions/formula-prices")
def reactions_formula_prices(type_ids: str = "", context_id: int = Depends(require_context)):
    """MARKET prices for the FORMULAS of the given reaction products (comma-separated product
    type_ids).

    **The market, not the public-contract index** — reported from use 2026-08-08: *"Formulas is
    only bought on the market and not via contract."* This priced them off the same contract scan
    Industry uses for ship and module blueprints, which is right for those (a BPC with runs/ME/TE
    on it only exists on contract) and wrong for a reaction formula, which is an item with a sell
    order like any other. A player quoted a contract price for something they buy off the market
    is being told the wrong number to budget with.

    A formula has no row in the `blueprints` table (its "blueprint" is the reaction itself), so this
    maps product -> `reactions.reaction_id` and prices that type through the account's own followed
    markets (`resolve_market_data`, same resolver the shopping list spends). Reported, never folded
    into a plan cost — the formulas are what you must go and get, and charging the plan for them
    would bill a player for a purchase they may already own or decide not to make.
    """
    ids = [int(x) for x in type_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return {"prices": {}}
    rx, _ = _reaction_index()
    formula_of = {tid: rx[tid] for tid in ids if tid in rx}
    if not formula_of:
        return {"prices": {}}
    try:
        from app.markets import resolve_market_data
        by_formula = resolve_market_data(context_id, sorted(set(formula_of.values())))
    except Exception:
        return {"prices": {}}
    out = {}
    for tid, f in formula_of.items():
        m = by_formula.get(f) or {}
        if m.get("sell_price"):
            out[str(tid)] = {"sell_price": m["sell_price"], "source": m.get("source") or "Jita",
                             "volume": m.get("sell_volume")}
    return {"prices": out}
