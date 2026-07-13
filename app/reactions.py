"""
Moon-goo reaction profitability ranking — see app/moon_goo.py for the (now group-scoped, see
app.groups) alliance price sheet this reads from. Not part of the PI planner's extractor/factory
distribution algorithm — a separate, unrelated read-only advisory tool.

Starting from whatever's priced (a caller's own group's price sheet, if any, compared against
the open market — see _load_goo_and_reached), walks the reaction graph forward (Simple ->
Composite, any depth) to find every reachable product, and for each computes: cost to make a
run at the achievable quantity (ME-adjusted), value at Jita (both instant-sell/buy and
sell-order/ask, with order-book depth alongside so the caller can judge liquidity), and
shipping+collateral cost to get it there. Ranks by profit but returns every dimension (steps,
profit/m3, volume) un-collapsed — "advice, not a tool": the comparison happens client-side,
this doesn't pick a single winner.

This evaluates each candidate chain IN ISOLATION (as if unlimited supply went to that one
product) — it does not account for competing chains sharing the same raw materials. That
cross-product allocation is what _suggest_reactions' knapsack does, further down.
"""
import json as _json
import math
import time as _time
from datetime import datetime, timezone

import httpx
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.market import fetch_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.cache import cache_get_json, cache_set_json, cache_invalidate, charlist_key
from app.esi import require_context, ESI_BASE, _get_valid_token
from app.groups import member_group, is_group_manager

router = APIRouter()

# Standup L-Set Reactor Efficiency I (T1) — the rig actually fitted, confirmed via EVE Ref.
# -2% material / -20% time base, x1.1 in null/WH space. Only the material figure matters here.
REACTION_ME_REDUCTION = 0.022

# Group-configurable shipping/collateral rates — these are alliance-wide assumptions (courier
# rates, insurance terms) that vary per alliance ("I doubt everyone has the same values"), so
# each group gets its own row; anyone not in a priced group falls back to a shared global-default
# row. Import (buying materials in from Jita) has no collateral — that's a self-haul/no-3rd-party-
# courier assumption; only the export leg (shipping the reacted product OUT to sell) uses a
# courier and needs collateral declared. Confirmed with the user: moon goo itself has zero
# import cost (picked up at/near the reaction site), only non-goo purchased inputs (fuel
# blocks etc.) pay the import rate.
_RXS_DEFAULTS = {"import_isk_per_m3": 1200.0, "export_isk_per_m3": 1200.0, "export_collateral_pct": 0.005,
                  # No default reaction system on purpose — an unset system means job installation
                  # cost is skipped entirely (0 ISK effect), matching pre-feature behavior, rather
                  # than silently assuming some arbitrary system's cost index applies to you.
                  "reaction_system": None, "facility_tax_pct": 0.0,
                  # Real reaction job duration in-game is shorter than raw SDE cycle_time — reactor
                  # efficiency rigs, skills, and structure/security bonuses all reduce it, and unlike
                  # material consumption (REACTION_ME_REDUCTION, a single well-known T1 rig figure)
                  # there's no one fixed number: it depends on the player's actual fit and where they
                  # react, which we have NO way to detect (ESI only reports facility for a job
                  # that's already installed, not one we're still planning, and a corp hangar
                  # reachable from every character isn't tied to one structure anyway — confirmed
                  # with the user 2026-07-13). So this is a manual figure, same pattern as
                  # reaction_system/facility_tax_pct: 0% default (today's un-corrected behavior)
                  # until a player sets their own observed reduction.
                  "time_efficiency_pct": 0.0}
_GLOBAL_SETTINGS_GROUP_ID = 0  # sentinel "no group" row — kept as a real (non-NULL) value since
# a nullable PRIMARY KEY doesn't behave consistently across SQLite and Postgres.


@ensure_once
def ensure_reaction_settings_table():
    con = get_connection()
    try:
        table_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pp_reaction_settings'"
        ).fetchone()
        migrate_row = None
        if table_exists:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(pp_reaction_settings)")}
            if "group_id" not in cols:
                # Old single-global-row shape (id INTEGER PRIMARY KEY, always id=1). SQLite can't
                # ALTER a PRIMARY KEY in place, so rebuild — preserve the already-configured rate
                # as BOTH the new global-default row AND the bootstrap (B0SS) group's own row, so
                # nothing silently resets to the hardcoded defaults after the group split.
                migrate_row = con.execute(
                    "SELECT import_isk_per_m3, export_isk_per_m3, export_collateral_pct "
                    "FROM pp_reaction_settings WHERE id=1"
                ).fetchone()
                con.execute("DROP TABLE pp_reaction_settings")
                con.commit()
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_settings (
                group_id               INTEGER PRIMARY KEY,
                import_isk_per_m3      REAL NOT NULL DEFAULT 1200,
                export_isk_per_m3      REAL NOT NULL DEFAULT 1200,
                export_collateral_pct  REAL NOT NULL DEFAULT 0.005
            )
        """)
        con.commit()
        for coldef in ("reaction_system TEXT", "facility_tax_pct REAL NOT NULL DEFAULT 0",
                       "time_efficiency_pct REAL NOT NULL DEFAULT 0"):
            try:
                con.execute(f"ALTER TABLE pp_reaction_settings ADD COLUMN {coldef}")
                con.commit()
            except Exception:
                pass
        if migrate_row:
            from app.groups import bootstrap_group_id
            for gid in (_GLOBAL_SETTINGS_GROUP_ID, bootstrap_group_id()):
                con.execute(
                    "INSERT INTO pp_reaction_settings (group_id, import_isk_per_m3, export_isk_per_m3, export_collateral_pct) "
                    "VALUES (?,?,?,?) ON CONFLICT (group_id) DO NOTHING",
                    (gid, migrate_row["import_isk_per_m3"], migrate_row["export_isk_per_m3"],
                     migrate_row["export_collateral_pct"]),
                )
            con.commit()
    finally:
        con.close()


def get_reaction_settings(group_id: int | None = None) -> dict:
    """Raw shipping/collateral settings for a SPECIFIC group (or the shared global default when
    group_id is None/not in any priced group) — the saved row if a manager has customized it,
    else the hardcoded defaults above. This is the group's own configured rate, unaware of any
    individual member's personal override — use effective_reaction_settings(context_id) for
    actual pricing math. Nothing changes in behavior until someone edits these (same convention
    as app.alert_settings)."""
    ensure_reaction_settings_table()
    gid = group_id if group_id is not None else _GLOBAL_SETTINGS_GROUP_ID
    con = get_connection()
    try:
        row = con.execute(
            "SELECT import_isk_per_m3, export_isk_per_m3, export_collateral_pct, "
            "reaction_system, facility_tax_pct, time_efficiency_pct FROM pp_reaction_settings WHERE group_id=?", (gid,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return dict(_RXS_DEFAULTS)
    return {
        "import_isk_per_m3": row["import_isk_per_m3"],
        "export_isk_per_m3": row["export_isk_per_m3"],
        "export_collateral_pct": row["export_collateral_pct"],
        "reaction_system": row["reaction_system"],
        "facility_tax_pct": row["facility_tax_pct"] or 0.0,
        "time_efficiency_pct": row["time_efficiency_pct"] or 0.0,
    }


@ensure_once
def ensure_account_reaction_settings_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_account_reaction_settings (
            context_id             INTEGER PRIMARY KEY,
            import_isk_per_m3      REAL NOT NULL,
            export_isk_per_m3      REAL NOT NULL,
            export_collateral_pct  REAL NOT NULL
        )
    """)
    con.commit()
    for coldef in ("reaction_system TEXT", "facility_tax_pct REAL NOT NULL DEFAULT 0",
                   "time_efficiency_pct REAL NOT NULL DEFAULT 0"):
        try:
            con.execute(f"ALTER TABLE pp_account_reaction_settings ADD COLUMN {coldef}")
            con.commit()
        except Exception:
            pass
    con.close()


def _account_reaction_settings_override(context_id: int) -> dict | None:
    ensure_account_reaction_settings_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT import_isk_per_m3, export_isk_per_m3, export_collateral_pct, "
            "reaction_system, facility_tax_pct, time_efficiency_pct FROM pp_account_reaction_settings WHERE context_id=?", (context_id,)
        ).fetchone()
    finally:
        con.close()
    if not row:
        return None
    return {
        "import_isk_per_m3": row["import_isk_per_m3"],
        "export_isk_per_m3": row["export_isk_per_m3"],
        "export_collateral_pct": row["export_collateral_pct"],
        "reaction_system": row["reaction_system"],
        "facility_tax_pct": row["facility_tax_pct"] or 0.0,
        "time_efficiency_pct": row["time_efficiency_pct"] or 0.0,
    }


def effective_reaction_settings(context_id: int) -> dict:
    """The shipping/collateral rate actually used to price a specific account's reactions,
    resolved personal override -> group default -> global default. JF/import costs genuinely
    vary account to account even within one alliance (different home system, different courier
    arrangement), so a group's rate is only a starting point, not a mandate — a player can
    override it for themselves in Settings without affecting anyone else in their group."""
    override = _account_reaction_settings_override(context_id)
    if override:
        return override
    group = member_group(context_id)
    return get_reaction_settings(group["id"] if group else None)


class ReactionSettingsUpdate(BaseModel):
    import_isk_per_m3: float
    export_isk_per_m3: float
    export_collateral_pct: float
    # Both optional/nullable — see _RXS_DEFAULTS: an unset reaction_system means job
    # installation cost is skipped entirely rather than guessed.
    reaction_system: str | None = None
    facility_tax_pct: float = 0.0
    # Fraction 0-1 (e.g. 0.532 for 53.2%) — see _RXS_DEFAULTS for why this can't be auto-detected.
    time_efficiency_pct: float = 0.0


@router.get("/api/reactions/settings")
def api_get_reaction_settings(ctx: int = Depends(require_context)):
    """Always the CALLER's own group's settings (or the global default) — a manager only ever
    needs to see/edit their own group's rate, never someone else's."""
    group = member_group(ctx)
    return get_reaction_settings(group["id"] if group else None)


def _resolve_system_id(name: str | None) -> int | None:
    """System name -> real solar_system_id via system_geo (populated by scripts/populate_geo.py
    from Fuzzworks — see its system_id backfill). Case-insensitive exact match; None/blank in,
    None out (that's the valid "not set" state, not an error)."""
    if not name or not name.strip():
        return None
    con = get_connection()
    try:
        row = con.execute(
            "SELECT system_id FROM system_geo WHERE system = ? COLLATE NOCASE", (name.strip(),)
        ).fetchone()
    finally:
        con.close()
    return row["system_id"] if row and row["system_id"] else None


def _validate_reaction_system(name: str | None) -> None:
    if name and name.strip() and _resolve_system_id(name) is None:
        raise HTTPException(status_code=400, detail=f'Unrecognized solar system "{name}"')


@router.put("/api/reactions/settings")
def api_update_reaction_settings(req: ReactionSettingsUpdate, ctx: int = Depends(require_context)):
    group = member_group(ctx)
    if not group:
        raise HTTPException(status_code=403, detail="You're not a member of any priced group")
    if not is_group_manager(ctx, group["id"]):
        raise HTTPException(status_code=403, detail="Only a manager of your group can edit its reaction settings")
    _validate_reaction_system(req.reaction_system)
    ensure_reaction_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_reaction_settings (group_id, import_isk_per_m3, export_isk_per_m3, "
            "export_collateral_pct, reaction_system, facility_tax_pct, time_efficiency_pct) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT (group_id) DO UPDATE SET "
            "import_isk_per_m3=excluded.import_isk_per_m3, export_isk_per_m3=excluded.export_isk_per_m3, "
            "export_collateral_pct=excluded.export_collateral_pct, reaction_system=excluded.reaction_system, "
            "facility_tax_pct=excluded.facility_tax_pct, time_efficiency_pct=excluded.time_efficiency_pct",
            (group["id"], req.import_isk_per_m3, req.export_isk_per_m3, req.export_collateral_pct,
             (req.reaction_system or "").strip() or None, req.facility_tax_pct, req.time_efficiency_pct),
        )
        con.commit()
    finally:
        con.close()
    return get_reaction_settings(group["id"])


@router.get("/api/reactions/account-settings")
def api_get_account_reaction_settings(ctx: int = Depends(require_context)):
    """The caller's personal shipping-cost override, if any, plus the group/global default it
    falls back to otherwise — the Settings UI shows both so a user can see what rate they're
    actually getting and whether it's their own override or an inherited default."""
    override = _account_reaction_settings_override(ctx)
    group = member_group(ctx)
    default = get_reaction_settings(group["id"] if group else None)
    return {"override": override, "default": default, "effective": override or default}


@router.put("/api/reactions/account-settings")
def api_update_account_reaction_settings(req: ReactionSettingsUpdate, ctx: int = Depends(require_context)):
    _validate_reaction_system(req.reaction_system)
    ensure_account_reaction_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_account_reaction_settings (context_id, import_isk_per_m3, export_isk_per_m3, "
            "export_collateral_pct, reaction_system, facility_tax_pct, time_efficiency_pct) "
            "VALUES (?,?,?,?,?,?,?) ON CONFLICT (context_id) DO UPDATE SET "
            "import_isk_per_m3=excluded.import_isk_per_m3, export_isk_per_m3=excluded.export_isk_per_m3, "
            "export_collateral_pct=excluded.export_collateral_pct, reaction_system=excluded.reaction_system, "
            "facility_tax_pct=excluded.facility_tax_pct, time_efficiency_pct=excluded.time_efficiency_pct",
            (ctx, req.import_isk_per_m3, req.export_isk_per_m3, req.export_collateral_pct,
             (req.reaction_system or "").strip() or None, req.facility_tax_pct, req.time_efficiency_pct),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/account-settings")
def api_reset_account_reaction_settings(ctx: int = Depends(require_context)):
    """Revert to the group/global default by removing the personal override."""
    ensure_account_reaction_settings_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_account_reaction_settings WHERE context_id=?", (ctx,))
        con.commit()
    finally:
        con.close()
    group = member_group(ctx)
    return get_reaction_settings(group["id"] if group else None)


def _load_reaction_graph(con, time_efficiency_pct: float = 0.0) -> tuple[dict[int, list[dict]], dict[int, list[dict]]]:
    """Returns (reactions_by_output, inputs_by_reaction): reactions_by_output maps a product
    type_id to the list of reaction formulas that can produce it (usually 1, occasionally 2 —
    e.g. a small no-fuel batch vs. a large fuel-block-consuming batch of the same conversion;
    both are kept as separate candidate paths rather than picking one). Each formula dict is
    {reaction_id, output_qty, cycle_time, inputs: [{type_id, quantity}]}.

    `cycle_time` here is the EFFECTIVE per-run duration (raw SDE value reduced by
    time_efficiency_pct — see _RXS_DEFAULTS), applied once at the source so every downstream
    consumer of a formula's cycle_time (chain-tier duration math, the Suggest wizard's cadence/
    completion estimates, the customer-order time estimate, and the frontend's own
    cycle_time-based runtime preview) gets the corrected figure automatically without each of
    them needing its own settings-aware logic. 0.0 (the default) reproduces the raw SDE value —
    unaware/non-context callers (e.g. list_reaction_fuel_blocks, which only needs formula
    shape, not timing) are unaffected."""
    inputs_by_reaction: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs_by_reaction.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})

    reactions_by_output: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        formula = {
            "reaction_id": r["reaction_id"], "output_qty": r["output_qty"],
            "cycle_time": (r["cycle_time"] or 0) * (1 - time_efficiency_pct),
            "inputs": inputs_by_reaction.get(r["reaction_id"], []),
        }
        reactions_by_output.setdefault(r["output_type_id"], []).append(formula)
    return reactions_by_output, inputs_by_reaction


_PURCHASABLE_MAX_QTY = 1_000_000_000  # a large-but-finite stand-in for "unlimited supply" for a
# single LEAF material — kept finite (not literal infinity) so arithmetic on it (cost = qty *
# unit_cost etc.) stays well-defined. On its own this would compound into absurd numbers through
# a multi-tier chain (each tier's max_qty is the previous tier's max_qty ÷ its own consumption ×
# its own output_qty, so a big number in only gets bigger going up the chain) —
# _UNLIMITED_RUNS_CAP below re-applies a sane ceiling at EVERY tier, not just the raw leaf.
_UNLIMITED_RUNS_CAP = 5_000  # a generous but sane ceiling on any one reaction's own run count —
# comfortably above what a real "Suggest reactions" run would ever actually use (cadence/ISK
# capping there is always much smaller in practice), but keeps the RAW opportunity table (which
# isn't cadence-capped) from showing nonsensical hundred-million-unit / trillion-ISK "opportunities."


def _resolve_reachable(goo: dict[int, dict], purchasable: dict[int, float],
                        reactions_by_output: dict[int, list[dict]],
                        job_cost_rate: float = 0.0, adjusted_prices: dict[int, float] | None = None
                        ) -> dict[int, dict]:
    """Fixed-point expansion from available leaf materials through the reaction graph. Returns
    {type_id: node} for every reachable node (goo, market-bought inputs, AND every reaction
    product reachable at any depth), where node carries:
      unit_cost      - ISK to produce one unit, rolled down to raw goo/market cost (ME-adjusted)
      max_qty        - max units producible this tier, capped by _UNLIMITED_RUNS_CAP
      reaction_count - distinct reaction runs needed in the subtree (the "work" proxy)
      via            - None for raw goo/purchasable, else the {reaction_id, ...} formula used
      source         - LEAF nodes only ("via" is None): "group" or "market", whichever priced
                       this specific unit_cost — see the cheaper-wins note below. Absent on
                       reaction-product nodes (never directly "bought", always reacted).
      alt_cost/      - LEAF nodes only: the LOSING price and its source, when both a group sheet
      alt_source       price and a market price were available for this material (None/None if
                       only one source had it at all) — lets a caller show the real ISK/unit
                       difference between the two, not just whichever one silently won.
      job_cost       - ISK of reaction JOB INSTALLATION fees to produce one unit, rolled up the
                       same way unit_cost is (0 for leaves — buying/harvesting installs no job).
                       Real EVE formula per job: EIV x (system cost index + facility tax rate),
                       EIV = sum of that job's own consumed materials valued at CCP's published
                       adjusted price (see app.industry_cost) — NOT the market/group unit_cost
                       used everywhere else. job_cost_rate is that (cost index + tax) sum,
                       precomputed once by the caller; 0.0 (the default) means job cost is
                       skipped entirely — see effective_reaction_settings.
    A reaction only becomes reachable once every one of its inputs is already reachable —
    same "expand until no more nodes unlock" shape as build_sde.py's compute_pi_tiers, just
    walked forward from available inputs instead of backward from a fixed target.

    Both `goo` (a group member's below-market alliance price sheet — see app.groups) and
    `purchasable` (Fuzzworks market price + import shipping — the only source for a non-member's
    moon materials, and for every reaction's non-goo inputs like fuel blocks regardless of group)
    are treated as UNLIMITED supply. This was a deliberate 2026-07-12 decision: a group's price
    sheet stock figures aren't a trustworthy enough signal to hard-cap on (no visibility into how
    often any given sheet is actually maintained — a stale zero used to silently hide a real
    opportunity, and a stale nonzero gave false confidence either way), so only price is trusted,
    not quantity.

    When a material is priced in BOTH dicts (a group member's own sheet AND the open market), the
    CHEAPER of the two wins automatically — so a group member is never stuck trusting a possibly-
    stale sheet price when the market rate is actually better; no manual override needed, the
    math just picks the cheaper source the same way it already picks the cheaper of two reaction
    formulas below."""
    adjusted_prices = adjusted_prices or {}
    # alt_cost/alt_source: the LOSING price, kept alongside the winning one whenever a leaf has
    # both a group sheet price and a market price available — lets a caller (the shopping list)
    # show the real ISK/unit difference between the two, not just whichever one won silently.
    reached: dict[int, dict] = {}
    for tid, g in goo.items():
        if g["sell_price"] > 0:
            reached[tid] = {"unit_cost": g["sell_price"], "max_qty": _PURCHASABLE_MAX_QTY,
                             "reaction_count": 0, "via": None, "source": "group", "job_cost": 0.0,
                             "alt_cost": None, "alt_source": None}
    for tid, buy_price in purchasable.items():
        if buy_price <= 0:
            continue
        if tid not in reached:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "via": None, "job_cost": 0.0, "alt_cost": None, "alt_source": None}
        elif buy_price < reached[tid]["unit_cost"]:
            reached[tid] = {"unit_cost": buy_price, "max_qty": _PURCHASABLE_MAX_QTY, "source": "market",
                             "reaction_count": 0, "via": None, "job_cost": 0.0,
                             "alt_cost": reached[tid]["unit_cost"], "alt_source": "group"}
        else:
            reached[tid]["alt_cost"] = buy_price
            reached[tid]["alt_source"] = "market"

    changed = True
    while changed:
        changed = False
        for output_id, formulas in reactions_by_output.items():
            best = None
            for f in formulas:
                if not f["inputs"] or any(inp["type_id"] not in reached for inp in f["inputs"]):
                    continue
                # ME reduces material CONSUMED per run, so it doesn't change unit_cost's
                # normalization directly — it scales down how much of each input a run needs.
                eff_qty = {inp["type_id"]: inp["quantity"] * (1 - REACTION_ME_REDUCTION)
                           for inp in f["inputs"]}
                runs = min(reached[tid]["max_qty"] / q for tid, q in eff_qty.items())
                if runs <= 0:
                    continue
                # Every leaf is unlimited-supply (see docstring) — always re-cap here so a
                # multi-tier chain can't compound the leaf sentinel into an absurd number
                # (each tier's max_qty feeds the next tier's own runs calculation).
                runs = min(runs, _UNLIMITED_RUNS_CAP)
                cost_per_run = sum(q * reached[tid]["unit_cost"] for tid, q in eff_qty.items())
                # This formula's OWN job-install fee (EIV of what IT consumes, valued at CCP's
                # adjusted prices) plus whatever job fees are already embedded in its inputs
                # (a reacted input's job_cost already rolled up its own subtree the same way) —
                # exactly mirrors cost_per_run's roll-up above, just for job fees instead of
                # material ISK. 0 when job_cost_rate is 0 (no reaction system configured).
                own_eiv_per_run = sum(q * adjusted_prices.get(tid, 0.0) for tid, q in eff_qty.items())
                job_cost_per_run = own_eiv_per_run * job_cost_rate + \
                    sum(q * reached[tid]["job_cost"] for tid, q in eff_qty.items())
                reaction_count = 1 + sum(reached[inp["type_id"]]["reaction_count"] for inp in f["inputs"])
                candidate = {
                    "unit_cost": cost_per_run / f["output_qty"],
                    "job_cost": job_cost_per_run / f["output_qty"],
                    "max_qty": int(runs) * f["output_qty"],
                    "reaction_count": reaction_count,
                    # Actual reaction-job cycles of THIS specific formula needed to hit max_qty
                    # (distinct from reaction_count, which is chain DEPTH/distinct-formula count,
                    # not run count) — this is what the wizard's "steps budget" (confirmed by the
                    # user: total reaction runs, not chain complexity) actually constrains.
                    # Deliberately counts only the top-level formula's own cycles, not upstream
                    # feeder reactions' cycles too — a documented simplification, not a full
                    # multi-level rollup (see Phase 3c plan notes).
                    "top_level_runs": int(runs),
                    "cycle_time": f["cycle_time"],
                    "via": {"reaction_id": f["reaction_id"], "cycle_time": f["cycle_time"],
                            "output_qty": f["output_qty"], "inputs": f["inputs"]},
                }
                # Prefer whichever formula yields the lower TOTAL landed cost — material cost
                # AND its own job-install fee — not just unit_cost alone, so a configured
                # reaction system's job cost can actually shift which recipe/path looks cheaper
                # ("least work most profitable" should already have picked the cheap path).
                if best is None or (candidate["unit_cost"] + candidate["job_cost"]) < (best["unit_cost"] + best["job_cost"]):
                    best = candidate
            if best is not None and (output_id not in reached or
                                      (best["unit_cost"] + best["job_cost"]) < (reached[output_id]["unit_cost"] + reached[output_id]["job_cost"])):
                reached[output_id] = best
                changed = True

    return reached


def _load_goo_and_reached(context_id: int, allowed_material_ids: set[int] | None = None):
    """Shared setup for both the profitability table and the shopping-list export: the alliance
    goo stock, the reaction graph, and the fixed-point `reached` expansion (see
    _resolve_reachable) from that goo through to every producible product. Returns
    (goo, reached, reactions_by_output, inputs_by_reaction, types) or None if there's no priced
    material to start from at all.

    A member of a priced group gets that group's below-market pp_moon_goo_prices sheet price for
    their moon materials AS WELL AS the open-market price (+ import shipping) — _resolve_reachable
    picks whichever is cheaper automatically, so a group member is never stuck trusting a
    stale/high sheet price over a better market rate (2026-07-12: the sheet's stock figures were
    dropped as a hard cap for the same reason — no reliable signal for how well-maintained it
    actually is; price is trusted, quantity isn't). Everyone else only ever sees the market
    price — no group deal to fall back to. The Reactions feature itself is open to any logged-in
    user (require_context) — group membership only changes which price(s) a material is costed
    at, not who can see the tool."""
    group = member_group(context_id)
    settings = effective_reaction_settings(context_id)

    con = get_connection()
    try:
        # The reference catalog of "known moon materials" is the UNION across every group's
        # sheet (not just the caller's own) — this is what makes a type_id count as a "moon
        # material" reachable via the market-priced path at all for a non-member; a group's
        # OWN priced rows (queried separately below) are what actually get the below-market
        # sheet price.
        all_goo_rows = con.execute("SELECT DISTINCT type_id FROM pp_moon_goo_prices").fetchall()
        own_goo_rows = (con.execute(
            "SELECT type_id, sell_price FROM pp_moon_goo_prices WHERE group_id=?", (group["id"],)
        ).fetchall() if group else [])
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con, settings.get("time_efficiency_pct", 0.0))
    finally:
        con.close()

    # Advanced filter: restrict which raw moon materials are actually available to this player
    # (e.g. a Gas type their group doesn't stock, or they simply can't reliably buy) — any
    # chain that would need an excluded material is never reachable in the first place. None/
    # empty = no restriction (every priced material is assumed available, the original behavior).
    moon_material_ids = {r["type_id"] for r in all_goo_rows}
    if allowed_material_ids:
        moon_material_ids &= set(allowed_material_ids)
    if not moon_material_ids:
        return None  # no group has priced any moon material at all — nothing to react from

    goo = {r["type_id"]: {"sell_price": r["sell_price"]} for r in own_goo_rows if r["type_id"] in moon_material_ids}

    pi = load_pi_data()
    types = pi["types"]

    # Every reaction input that isn't itself a reaction product (fuel blocks, and other named
    # materials most formulas need alongside moon goo) PLUS every moon material, regardless of
    # group — a group member's material needs a market price to compare against their sheet
    # price (see _resolve_reachable), and a non-member has no other price source at all.
    # Priced at what it costs to instantly ACQUIRE it (the order book's sell_price — market_data
    # field names are from the order book's perspective, buying costs the sell price) PLUS the
    # configured import shipping cost to haul it in (no collateral on the import leg — that's a
    # self-haul assumption, only the export leg uses a courier). Unlimited supply assumed.
    #
    # Deliberately EXCLUDES anything that is itself a reaction product (has its own entry in
    # reactions_by_output, e.g. Ferrofluid, Carbon Polymers — Simple/T1-tier intermediates) even
    # when buying it outright happens to be marginally cheaper in raw ISK than reacting it from
    # goo. Without this exclusion, "make chains" is misleading: a "chain-depth-2" suggestion
    # could actually just be one purchased intermediate + one real reaction, not a genuine
    # goo-to-final-product chain — and the bought intermediate's own market depth was never
    # checked by the liquidity filter (which only looks at the FINAL product), so its assumed
    # "instant, unlimited" availability is a much shakier assumption than for a true raw/
    # manufactured leaf (fuel blocks etc., which have no reaction formula at all and stay
    # purchasable). Only real reaction products get this treatment; true leaves are unaffected.
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    purchasable_ids = {tid for tid in all_input_ids if tid not in reactions_by_output} | moon_material_ids
    # Same "uncheck what you can't reliably get" filter as moon goo, applied to the racial fuel
    # blocks too — each reaction formula needs one SPECIFIC fuel block (fixed by its SDE recipe),
    # but a player's real access to each racial variant varies independently (e.g. cheap local
    # Oxygen production, no reliable Hydrogen supply) — a chain needing an excluded fuel block
    # becomes unreachable, same as an excluded moon material.
    if allowed_material_ids:
        fuel_block_ids = set(_fuel_block_ids(inputs_by_reaction, reactions_by_output, types))
        purchasable_ids -= (fuel_block_ids - set(allowed_material_ids))
    purchasable_ids = list(purchasable_ids)
    purchasable_market = fetch_market_data(purchasable_ids)
    purchasable = {
        tid: m["sell_price"] + settings["import_isk_per_m3"] * (types.get(tid, {}).get("volume") or 0.0)
        for tid, m in purchasable_market.items()
    }

    # Job installation cost: EIV x (system cost index + facility tax) — see _resolve_reachable's
    # job_cost roll-up. Both are 0 (no effect, current behavior preserved) unless the caller's
    # effective settings actually name a reaction system — an unconfigured account never pays
    # the extra ESI round-trip for adjusted prices either.
    reaction_system = settings.get("reaction_system")
    job_cost_rate = 0.0
    adjusted_prices: dict[int, float] = {}
    if reaction_system:
        cost_index = fetch_system_cost_index(_resolve_system_id(reaction_system))
        job_cost_rate = cost_index + (settings.get("facility_tax_pct") or 0.0)
        if job_cost_rate > 0:
            adjusted_prices = fetch_adjusted_prices(list(all_input_ids))

    reached = _resolve_reachable(goo, purchasable, reactions_by_output, job_cost_rate, adjusted_prices)
    return goo, reached, reactions_by_output, inputs_by_reaction, types


_OPPS_CACHE_TTL = 90  # short-lived Redis cache — this is the single most expensive computation
# in the Reactions tab (full reaction-graph walk + a market fetch per candidate + job-cost ESI
# lookups when a reaction system is configured), and it was being recomputed on every dashboard
# refresh (tab open, every assign/cancel, every settings save) even though nothing upstream
# usually changed between them. 90s is short enough that a real price/settings change becomes
# visible again quickly with no explicit invalidation needed — same "TTL is enough" convention
# as app.market's own 15-minute price cache, just shorter since this result is more request-
# specific (per context_id + material filter) and therefore has a much smaller cache-hit pool.


def _build_opportunities(context_id: int, allowed_material_ids: set[int] | None = None) -> list[dict]:
    cache_key = "rx:opps:%d:%s" % (
        context_id,
        ",".join(str(t) for t in sorted(allowed_material_ids)) if allowed_material_ids else "all",
    )
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    result = _build_opportunities_uncached(context_id, allowed_material_ids)
    cache_set_json(cache_key, result, ttl=_OPPS_CACHE_TTL)
    return result


def _build_opportunities_uncached(context_id: int, allowed_material_ids: set[int] | None = None) -> list[dict]:
    loaded = _load_goo_and_reached(context_id, allowed_material_ids)
    if loaded is None:
        return []
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    settings = effective_reaction_settings(context_id)

    # Only reaction PRODUCTS are candidates — shipping raw unreacted goo isn't what this tool
    # is for (that's just the input side).
    candidate_ids = [tid for tid, node in reached.items() if node["reaction_count"] > 0 and node["max_qty"] > 0]
    if not candidate_ids:
        return []

    market = fetch_market_data(candidate_ids)

    opportunities = []
    for tid in candidate_ids:
        node = reached[tid]
        type_info = types.get(tid, {})
        vol = type_info.get("volume") or 0.0
        m = market.get(tid)
        if not m:
            continue  # no live market data for this product — can't price it, skip rather than guess

        qty = node["max_qty"]
        input_cost = qty * node["unit_cost"]
        job_cost = qty * node.get("job_cost", 0.0)
        ship_volume = qty * vol
        shipping_cost = ship_volume * settings["export_isk_per_m3"]
        # Collateral is a transport cost (courier contract), charged regardless of how the
        # cargo is later sold — declared consistently against Jita sell (the standard
        # freight-collateral reference value), not whichever sell method ends up chosen.
        collateral_cost = qty * m["sell_price"] * settings["export_collateral_pct"]
        instant_value = qty * m["buy_price"]
        order_value = qty * m["sell_price"]
        fixed_costs = input_cost + shipping_cost + collateral_cost + job_cost

        # Intermediate reactions this product's own formula needs BEFORE the top-level reaction
        # can even start (e.g. goo -> Ferrofluid -> this product) — computed once here at this
        # opportunity's own max batch (top_level_runs) so a caller (the manual-assign modal, the
        # suggestion flow) can linearly scale runs down to whatever smaller batch it actually
        # wants, the same way every other field on this dict already scales. Empty for a
        # single-tier product (the common case) — via is only set on reaction-product nodes.
        chain_tiers = []
        if node.get("via"):
            tier_runs: dict[int, dict] = {}
            _explode_chain_tiers(node["via"]["inputs"], node["top_level_runs"], reached, tier_runs)
            ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
            for tier_tid, info in ordered:
                chain_tiers.append({
                    "type_id": tier_tid, "name": types.get(tier_tid, {}).get("name", str(tier_tid)),
                    "runs": info["runs"], "cycle_time": info["cycle_time"], "output_qty": info["output_qty"],
                })

        opportunities.append({
            "type_id": tid,
            "name": type_info.get("name", str(tid)),
            "steps": node["reaction_count"],
            "top_level_runs": node["top_level_runs"],
            "cycle_time": node["cycle_time"],
            "output_qty": qty,
            "input_cost": round(input_cost, 2),
            "job_cost": round(job_cost, 2),
            "shipping_volume_m3": round(ship_volume, 2),
            "shipping_cost": round(shipping_cost, 2),
            "collateral_cost": round(collateral_cost, 2),
            "instant_sell_value": round(instant_value, 2),
            "sell_order_value": round(order_value, 2),
            "net_profit_instant": round(instant_value - fixed_costs, 2),
            "net_profit_order": round(order_value - fixed_costs, 2),
            "profit_per_m3_instant": round((instant_value - fixed_costs) / ship_volume, 2) if ship_volume > 0 else None,
            "buy_volume": m["buy_volume"],
            "sell_volume": m["sell_volume"],
            "chain_tiers": chain_tiers,
        })

    opportunities.sort(key=lambda o: -o["net_profit_instant"])
    return opportunities


@router.get("/api/reactions/opportunities")
def reactions_opportunities(context_id: int = Depends(require_context)):
    return {"opportunities": _build_opportunities(context_id)}


def _fuel_block_ids(inputs_by_reaction: dict, reactions_by_output: dict, types: dict) -> dict[int, str]:
    """The racial fuel-block variants (Hydrogen/Helium/Nitrogen/Oxygen) used as reaction inputs
    — a second, distinct category from moon goo in the advanced material-availability filter.
    Each formula's required fuel block is fixed by its SDE recipe, but a player's REAL access to
    each racial variant can differ a lot (e.g. cheap local Oxygen production, but no reliable
    Hydrogen supply) — same idea as "a Gas type your group doesn't stock" already applies to
    moon goo, just for the other big recurring purchasable input. Detected dynamically (name
    match + actually used as a reaction input, not itself a reaction product) rather than
    hardcoded IDs, so it stays correct if the SDE's exact set ever changes."""
    all_input_ids = {inp["type_id"] for inputs in inputs_by_reaction.values() for inp in inputs}
    return {
        tid: types.get(tid, {}).get("name", str(tid))
        for tid in all_input_ids
        if tid not in reactions_by_output and "Fuel Block" in (types.get(tid, {}).get("name") or "")
    }


@router.get("/api/reactions/fuel-blocks")
def list_reaction_fuel_blocks(ctx: int = Depends(require_context)):
    con = get_connection()
    try:
        reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
    finally:
        con.close()
    types = load_pi_data()["types"]
    ids = _fuel_block_ids(inputs_by_reaction, reactions_by_output, types)
    rows = sorted(({"type_id": tid, "name": name} for tid, name in ids.items()), key=lambda r: r["name"])
    return {"fuel_blocks": rows}


def _explode_shopping_list(type_id: int, units_needed: float, reached: dict, out: dict[int, float]):
    """Recursively break `units_needed` units of `type_id` down to raw moon goo / purchasable
    leaf materials, accumulating into `out`. A leaf (node["via"] is None) just needs that many
    units directly; a reaction product needs ceil(units_needed / its output_qty) actual reaction
    cycles, which in turn consume its own ME-adjusted inputs — same graph _resolve_reachable
    already built, walked back down instead of the forward fixed-point expansion."""
    node = reached.get(type_id)
    if not node or node["via"] is None:
        out[type_id] = out.get(type_id, 0.0) + units_needed
        return
    formula = node["via"]
    reaction_runs = math.ceil(units_needed / formula["output_qty"])
    for inp in formula["inputs"]:
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * reaction_runs
        _explode_shopping_list(inp["type_id"], eff_qty, reached, out)


def _explode_chain_tiers(formula_inputs: list[dict], runs: int, reached: dict, tiers: dict[int, dict]):
    """For `runs` cycles of a formula needing `formula_inputs`, finds every INTERMEDIATE reaction
    product among those inputs (recursively — a real chain can be several tiers deep, e.g. goo ->
    Ferrofluid -> Nonlinear Metamaterials) and accumulates how many of ITS OWN reaction cycles are
    needed to keep the tier above it supplied, into `tiers` (type_id -> {runs, cycle_time,
    output_qty}). Deliberately excludes the TOP-level formula itself — that's the caller's own
    suggestion, already tracked separately, not an "extra" tier. Without this, a suggestion for a
    multi-tier product only ever told the player to install the FINAL reaction, silently assuming
    they'd already have the intermediate on hand — which since the "force real chains" fix
    (intermediates are never just bought) is never actually true."""
    for inp in formula_inputs:
        inp_node = reached.get(inp["type_id"])
        if not inp_node or inp_node["via"] is None:
            continue  # raw goo or a genuine purchasable leaf — nothing to react
        eff_qty = inp["quantity"] * (1 - REACTION_ME_REDUCTION) * runs
        formula = inp_node["via"]
        inp_runs = math.ceil(eff_qty / formula["output_qty"])
        tid = inp["type_id"]
        if tid not in tiers:
            tiers[tid] = {"runs": 0, "cycle_time": formula["cycle_time"], "output_qty": formula["output_qty"]}
        tiers[tid]["runs"] += inp_runs
        _explode_chain_tiers(formula["inputs"], inp_runs, reached, tiers)  # this tier may itself be multi-level


def _materials_report(totals: dict[int, float], reached: dict, types: dict) -> list[dict]:
    """Turns a leaf-level {type_id: qty} total (as built by _explode_shopping_list) into the
    display row shape both the pending-assignments shopping list and a customer order's own
    materials report use — kept as one function so the two callers can't drift apart.

    "source" reflects which price actually won for THIS specific leaf (see _resolve_reachable's
    cheaper-wins note) — not just "is this material categorically moon goo." A group's own
    sheet can lose to a better market rate on any given material, and a shopping-list entry
    must follow whichever source is actually cheapest right now, or it'd send the player to
    buy from the alliance when the market is the better (or for a non-member, the only) option.
    unit_cost is the SAME per-unit price _resolve_reachable already settled on for this leaf
    (whichever source won) — surfaced so the player has a concrete expected price to check the
    actual alliance/market quote against, not just a bare quantity. alt_unit_cost/alt_source
    is the LOSING price (if both a group sheet price and a market price were available for
    this material) — the actual ISK/unit gap between them, not just an implicit "cheaper won."
    Market prices always include the configured import shipping cost per m3 already baked in
    (see the "market_data field names..." note in _load_goo_and_reached above) — never a bare
    Jita quote."""
    materials = [
        {
            "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
            "quantity": math.ceil(qty), "source": reached.get(tid, {}).get("source", "market"),
            "unit_cost": round(reached.get(tid, {}).get("unit_cost", 0.0), 2),
            "alt_unit_cost": (round(reached[tid]["alt_cost"], 2)
                               if tid in reached and reached[tid].get("alt_cost") is not None else None),
            "alt_source": reached.get(tid, {}).get("alt_source"),
            "volume_m3": round(math.ceil(qty) * (types.get(tid, {}).get("volume") or 0.0), 2),
        }
        for tid, qty in totals.items()
    ]
    materials.sort(key=lambda m: (m["source"] != "group", m["name"]))
    return materials


@router.get("/api/reactions/shopping-list")
def reactions_shopping_list(context_id: int = Depends(require_context)):
    """Total raw materials needed across every one of the caller's pending SPECULATIVE-PROFIT
    assignments (see assign_reaction) — moon goo AND any purchased materials (fuel blocks etc.),
    summed and broken down to the same leaf level the profitability table prices from. Meant to
    be copied straight into a Jita multibuy tool (Janice) or the alliance's goo buy channel.

    Deliberately EXCLUDES order-linked assignments (order_id IS NOT NULL) — a customer order
    already has its own materials report scoped to that specific order (GET
    /api/reactions/orders/{id}, sized off the order's own target_qty, not whatever a partial
    batch has been assigned so far), so folding it into this general list would both double-count
    against the order's own report and mix a client's specific requirement into an unrelated
    general shopping run."""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if not char_ids:
            return {"materials": []}
        placeholders = ",".join("?" * len(char_ids))
        assignments = con.execute(
            f"SELECT type_id, runs FROM pp_reaction_assignments "
            f"WHERE character_id IN ({placeholders}) AND order_id IS NULL",
            char_ids,
        ).fetchall()
    finally:
        con.close()

    if not assignments:
        return {"materials": []}

    loaded = _load_goo_and_reached(context_id)
    if loaded is None:
        return {"materials": []}
    goo, reached, _, _, types = loaded

    totals: dict[int, float] = {}
    for a in assignments:
        node = reached.get(a["type_id"])
        if not node or node["via"] is None:
            continue  # shouldn't happen (assignments are always reaction products), skip defensively
        top_units = a["runs"] * node["via"]["output_qty"]
        _explode_shopping_list(a["type_id"], top_units, reached, totals)

    return {"materials": _materials_report(totals, reached, types)}


# ── Personal reaction-job tracking (opt-in scope, see app.esi.INDUSTRY_JOBS_SCOPES) ────────────
# Cache-at-fetch, not live-fetch-on-every-page-load (same shape as app.pi_sim's colony state):
# ESI already reports start_date/end_date directly for a job, so there's no decay/rate math to
# simulate forward — just cache the raw filtered job list with a fetched_at timestamp, refreshed
# on demand (a "Refresh" button, same UX as the existing planet rescan) rather than polling.

@ensure_once
def ensure_industry_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_industry_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


_structure_name_cache: dict[int, str] = {}  # structure names don't change — cache for process lifetime


def _resolve_structure_name(structure_id: int, access_token: str) -> str:
    if structure_id in _structure_name_cache:
        return _structure_name_cache[structure_id]
    name = f"Structure #{structure_id}"
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/universe/structures/{structure_id}/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            data = resp.json()
        name = data.get("name") or name
    except Exception:
        pass  # best-effort — an unresolvable structure just shows its raw ID, never blocks the fetch
    _structure_name_cache[structure_id] = name
    return name


# EVE industry activity id for Reactions is 9 (Manufacturing=1, ME research=4, …). Verified
# against live corp/character industry-jobs responses — a reaction-heavy corp's jobs are all
# activity 9, and there is no activity 11 at all. An earlier value of 11 here was wrong but never
# caught, because the jobs table was never populated (the refresh was unwired) so the filter never
# actually ran against real data.
REACTION_ACTIVITY_ID = 9


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Fetch this character's reaction jobs (activity_id 9) from ESI, resolving each distinct
    facility to a readable name. Best-effort: returns [] on any failure rather than raising —
    a refresh failing for one character must not block the others."""
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{ESI_BASE}/characters/{character_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
                timeout=10,
            )
            resp.raise_for_status()
            jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == REACTION_ACTIVITY_ID]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


def fetch_corp_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """This character's reaction jobs installed FOR CORPORATION (a shared corp hangar/reactor,
    not the character's personal jobs) — a real, confirmed gap: these never appear via the
    per-character endpoint fetch_industry_jobs uses, only via the corp one, and only when the
    character holds Factory_Manager/Director. Best-effort like the rest of this module: any
    failure (missing role, no corp, network) returns [] rather than raising — one character's
    corp-jobs lookup failing must never block their own personal jobs or any other character's
    refresh. Filtered to `installer_id == character_id` — this reads the WHOLE corp's job queue
    over ESI, but only this specific character's own installs are what a "my jobs" view should
    show, not every corpmate's."""
    try:
        with httpx.Client(timeout=10) as client:
            pub = client.get(f"{ESI_BASE}/characters/{character_id}/").json()
            corp_id = pub.get("corporation_id")
            if not corp_id:
                return []
            resp = client.get(
                f"{ESI_BASE}/corporations/{corp_id}/industry/jobs/",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            resp.raise_for_status()
            jobs = resp.json()
    except Exception:
        return []

    reaction_jobs = [j for j in jobs if j.get("activity_id") == REACTION_ACTIVITY_ID and j.get("installer_id") == character_id]
    for j in reaction_jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return reaction_jobs


# ESI's industry-jobs endpoint caches ~5 min; a plain tab-open refresh must not re-hit ESI more
# often than that (both to respect CCP's cache and to keep the Reactions tab snappy). The manual
# "Refresh jobs" button passes force=1 to bypass this and pull immediately.
_JOBS_CACHE_TTL = 300


@router.post("/api/reactions/jobs/refresh")
def refresh_industry_jobs(force: int = 0, context_id: int = Depends(require_context)):
    """Refresh the caller's own characters' cached reaction-job list from ESI — only characters
    that have actually granted the industry-jobs scope (opted in via ?reactions=1 login) are
    fetched; others are silently skipped, not an error, since most PI-planner accounts never
    opt into this. Called on Reactions tab-open (respecting ESI's ~5min cache via _JOBS_CACHE_TTL)
    and by the manual "Refresh jobs" button (force=1, bypasses the staleness guard)."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        # Last-fetch time per character, read up front (one connection, closed before the ESI loop
        # below) so the staleness guard doesn't re-hit ESI for a character refreshed seconds ago.
        fetched_at_by_char = {r["character_id"]: r["fetched_at"] for r in con.execute(
            "SELECT character_id, fetched_at FROM pp_char_industry_jobs"
        )}
    finally:
        con.close()

    now = _time.time()
    refreshed = 0
    skipped = 0
    for c in chars:
        scopes = c["scopes"] or ""
        if "read_character_jobs" not in scopes:
            continue
        prev = fetched_at_by_char.get(c["character_id"])
        if not force and prev is not None and (now - prev) < _JOBS_CACHE_TTL:
            skipped += 1  # still within ESI's own cache window — a re-fetch would return the same data
            continue
        token = _get_valid_token(c["character_id"])
        if not token:
            continue
        jobs = fetch_industry_jobs(c["character_id"], token)
        # Only characters that re-authorised after the corp-jobs scope was added carry it —
        # already-connected characters keep working (personal jobs only) until they reconnect,
        # no forced re-auth. job_id is unique across BOTH endpoints (it's ESI's own job
        # identifier), so a plain concat can't double-count even in the (shouldn't-happen) case
        # of a job appearing in both responses.
        if "read_corporation_jobs" in scopes:
            jobs = jobs + fetch_corp_industry_jobs(c["character_id"], token)
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(jobs), _time.time()),
            )
            con.commit()
        finally:
            con.close()
        refreshed += 1
    # The Characters tab shows each toon's running reaction jobs (see list_characters), served
    # from the same Redis-cached charlist payload — so a refresh that actually pulled new jobs must
    # bust that cache or the tab keeps showing stale/absent jobs until the next colony rescan.
    if refreshed:
        cache_invalidate(charlist_key(context_id))
    return {"ok": True, "characters_refreshed": refreshed, "characters_skipped": skipped}


def reaction_slots(character_row: dict) -> int:
    """1 base slot + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at
    the game's real max of 11 (5+5+1)."""
    return min(11, 1 + (character_row.get("mass_reactions") or 0) + (character_row.get("advanced_mass_reactions") or 0))


@ensure_once
def ensure_reaction_assignments_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_assignments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                runs         INTEGER NOT NULL,
                input_cost   REAL NOT NULL,
                reward       REAL NOT NULL,
                created_at   REAL NOT NULL
            )
        """)
        con.commit()
        # tier_order: 0 = the deepest intermediate reaction (react first — a real chain, e.g.
        # goo -> Ferrofluid -> Nonlinear Metamaterials, needs the intermediate done before the
        # top-level reaction can even start), ascending up to the top-level product itself
        # (highest number in the group = react last). Existing single-tier assignments default
        # to 0, unaffected — additive migration, matches this codebase's convention.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN tier_order INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except Exception:
            pass
        # order_id: tags every row (top-level AND its chain-tier rows) created on behalf of a
        # fixed-unit customer order (see ensure_reaction_orders_table below) — NULL for every
        # assignment created the normal way (manual-assign, suggest-and-assign), no behavior
        # change there. Lets the dashboard label which slots are committed to a client job.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN order_id INTEGER")
            con.commit()
        except Exception:
            pass
    finally:
        con.close()


# ── Fixed-unit customer orders ──────────────────────────────────────────────────────────────
# A different framing from the day-cadence/profit-maximizing wizard above: sometimes another
# player asks for a FIXED number of finished units (a one-off job), not an ongoing weekly
# routine. An order is persistent (tracked across sessions, not a one-shot calculator) and
# committing to it occupies real reaction slots the same way the suggestion/manual-assign flow
# does — see _allocate_and_insert below, which reuses the exact slot-spreading logic
# _suggest_reactions already has.

@ensure_once
def ensure_reaction_orders_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id     INTEGER NOT NULL,
                type_id        INTEGER NOT NULL,
                name           TEXT NOT NULL,
                target_qty     REAL NOT NULL,
                top_level_runs INTEGER NOT NULL,
                assigned_runs  INTEGER NOT NULL DEFAULT 0,
                client_name    TEXT,
                notes          TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                created_at     REAL NOT NULL
            )
        """)
        con.commit()
    finally:
        con.close()


def _insert_assignment_rows(con, character_id: int, type_id: int, name: str, runs: float,
                             job_count: int, input_cost: float, reward: float, tier_order: int,
                             now: float, order_id: int | None = None) -> None:
    """One product's worth of a job commitment, split into `job_count` separate assignment rows
    (one per actual in-game job install — see assign_reaction's own docstring for why). Shared by
    assign_reaction (order_id always None there — no behavior change) and the customer-order
    assign flow (_allocate_and_insert, order_id set) so the row-insertion shape can't drift
    between the two callers."""
    job_count = max(1, job_count)
    runs_per_job = math.ceil(runs / job_count)
    for _ in range(job_count):
        con.execute(
            "INSERT INTO pp_reaction_assignments "
            "(character_id, type_id, name, runs, input_cost, reward, created_at, tier_order, order_id) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (character_id, type_id, name, runs_per_job, input_cost / job_count, reward / job_count,
             now, tier_order, order_id),
        )


class ChainTier(BaseModel):
    type_id: int
    name: str
    runs: int
    job_count: int = 1


class AssignRequest(BaseModel):
    character_id: int
    type_id: int
    name: str
    runs: int  # total runs across all jobs for this suggestion
    job_count: int = 1  # how many separate in-game job installs this splits into (one per slot)
    input_cost: float
    reward: float
    # Intermediate reactions this product's own formula needs (see _explode_chain_tiers in
    # _suggest_reactions), deepest-first — each becomes its own set of assignment rows the
    # player must install and let finish BEFORE the top-level reaction above can even start.
    chain_tiers: list[ChainTier] = []


@router.post("/api/reactions/assign")
def assign_reaction(req: AssignRequest, context_id: int = Depends(require_context)):
    """Commit a suggested (character, product) pairing as standing "go do this" instructions —
    surfaced on the dashboard until ESI confirms a matching job is actually running, at which
    point it's auto-cleared (see get_industry_jobs). A suggestion sized to use multiple reaction
    slots at once (job_count > 1, e.g. a big batch that needs several parallel jobs to finish
    within the chosen cadence) becomes that many SEPARATE assignment rows — one per actual
    in-game job install — so the dashboard shows the real number of slots this occupies, not one
    square standing in for several.

    Any chain_tiers (intermediate reactions this product's own formula needs, e.g. goo ->
    Ferrofluid -> this product — see _explode_chain_tiers) get their own assignment rows too,
    tagged with a LOWER tier_order so the dashboard can show them as "react this first." Their
    input_cost/reward are recorded as 0 — the full chain's cost/profit is already rolled up into
    the top-level row (unit_cost is computed recursively down to raw goo), so giving the
    intermediate rows their own nonzero values would double-count it if anything ever sums
    pp_reaction_assignments financially. (Expected output value is priced LIVE off current
    market data in get_industry_jobs, not stored here — see that function's own notes on why.)"""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")

        now = _time.time()
        for tier_order, tier in enumerate(req.chain_tiers):
            _insert_assignment_rows(con, req.character_id, tier.type_id, tier.name, tier.runs,
                                     tier.job_count, 0.0, 0.0, tier_order, now)

        top_tier_order = len(req.chain_tiers)
        _insert_assignment_rows(con, req.character_id, req.type_id, req.name, req.runs,
                                 req.job_count, req.input_cost, req.reward, top_tier_order, now)
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign/{assignment_id}")
def unassign_reaction(assignment_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT a.id FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE a.id=? AND c.context_id=?",
            (assignment_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Assignment not found")
        con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (assignment_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/reactions/assign")
def unassign_all_reactions(context_id: int = Depends(require_context)):
    """Clear every pending assignment across all of the caller's characters in one go —
    "Clear all" on the dashboard, for starting a fresh suggestion set without hand-cancelling
    each pending slot one at a time."""
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE character_id IN ({placeholders})", char_ids)
            con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.get("/api/reactions/jobs")
def get_industry_jobs(context_id: int = Depends(require_context)):
    """Personal reaction-job status for the Reactions wizard's dashboard page: currently
    running jobs (from the last refresh), a capacity summary (free slots right now, across
    every character that's opted into tracking), the per-character opt-in breakdown so the UI
    can offer to connect any character that hasn't opted in yet, and any standing "assigned but
    not yet actually running" instructions (see assign_reaction) — a context can hold several
    characters (an account's own alts, or characters from separate EVE accounts logged into the
    same session), and each authorises the tracking scope independently."""
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    # Fetched BEFORE opening the main connection below, not inside that try block — this opens
    # its OWN connection internally (member_group/get_reaction_settings/account override), and
    # holding two connections open at once per request is exactly what exhausted the pool under
    # concurrency (see app.db._pg_pool's queue.Queue: bounded at pool size, get() waits up to 15s
    # then raises) — a real production incident on 2026-07-13 traced to this exact pattern taking
    # down unrelated endpoints (Dashboard, Setup Analysis) once the pool was saturated. Never hold
    # a second get_connection() open while a first one from the same request is still live.
    time_eff = effective_reaction_settings(context_id).get("time_efficiency_pct", 0.0)
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json, fetched_at FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        assignments: dict[int, list] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT id, character_id, type_id, name, runs, input_cost, reward, tier_order, order_id "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) ORDER BY tier_order", char_ids,
            ):
                assignments.setdefault(r["character_id"], []).append(dict(r))
        # Client-order labels for any pending row committed via a customer order (see
        # _allocate_and_insert) — so the dashboard can show "Order: <client>" on those slots
        # instead of just the product name, distinguishing client-committed jobs from
        # speculative-profit ones at a glance.
        order_ids = list({a["order_id"] for rows in assignments.values() for a in rows if a.get("order_id")})
        order_labels: dict[int, str] = {}
        if order_ids:
            placeholders_o = ",".join("?" * len(order_ids))
            for r in con.execute(
                f"SELECT id, client_name FROM pp_reaction_orders WHERE id IN ({placeholders_o})", order_ids,
            ):
                order_labels[r["id"]] = r["client_name"] or f"Order #{r['id']}"
        # output_type_id -> cycle hours, so a stored assignment (which only keeps `runs`, not its
        # own formula) can be turned into a real duration for the profit/day normalization below —
        # PI's headline number is already a rate (value_per_day), so Reactions' should be too.
        # Reduced by time_eff (fetched above, before this connection was opened) — this query
        # bypasses _load_reaction_graph (which applies the same correction for the opportunity/
        # suggestion/order paths), so the reduction has to be applied here too or a pending
        # assignment's reported profit/day would understate itself using the slower raw SDE time.
        cycle_hours_by_type = {r["output_type_id"]: (r["cycle_time"] or 0) * (1 - time_eff) / 3600.0
                                for r in con.execute("SELECT output_type_id, cycle_time FROM reactions")}
        # Same idea, output units per run — needed to turn a pending row's `runs` into an actual
        # output quantity for the live output-value estimate below.
        output_qty_by_type = {r["output_type_id"]: r["output_qty"]
                               for r in con.execute("SELECT output_type_id, output_qty FROM reactions")}
    finally:
        con.close()

    # Expected output value is priced LIVE off today's market, not stored at assign-time — a
    # stored snapshot would need retroactive backfilling for every row created before this
    # existed (impossible — no way to know a past market price) and would go stale for older
    # rows anyway as prices move. One bulk fetch across every distinct assigned type_id, same
    # pattern _build_opportunities already uses.
    all_assigned_type_ids = list({r["type_id"] for rows in assignments.values() for r in rows})
    market_by_type = fetch_market_data(all_assigned_type_ids) if all_assigned_type_ids else {}

    now = _time.time()
    running: list[dict] = []
    characters: list[dict] = []
    total_slots = 0
    used_slots = 0
    tracked_any = False
    pending_isk_committed = pending_net_profit = pending_net_profit_per_day = 0.0
    pending_output_value = 0.0
    for c in chars:
        opted_in = "read_character_jobs" in (c["scopes"] or "")
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False, "slots": slots})
            continue
        tracked_any = True
        total_slots += slots
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        active = [j for j in jobs if j.get("status") in ("active", "paused", "ready")]
        used_slots += len(active)
        # Count-aware, not just a set of type_ids present — a big batch can be split into
        # several separate pending assignment rows for the SAME product (one per job slot), so
        # only as many of them may be cleared as there are actually-running jobs of that type;
        # naive set-membership would wrongly clear every pending row for a product the moment
        # just ONE of its several intended jobs gets installed.
        running_type_counts: dict[int, int] = {}
        for j in active:
            tid = j.get("product_type_id")
            running_type_counts[tid] = running_type_counts.get(tid, 0) + 1

        pending = []
        for a in assignments.get(c["character_id"], []):
            if running_type_counts.get(a["type_id"], 0) > 0:
                # A live ESI job of this product is running — this planned slot is TEMPORARILY
                # covered by it, so it's hidden from the "to install" list (the running-job square
                # already occupies that slot in the loadout). It is NOT deleted: the plan is a
                # persistent loadout, so when the job finishes and drops out of ESI this row
                # reappears as "to install" again. (Was a destructive DELETE — that silently wiped
                # the plan the moment a job started, so slots vanished on refresh and never came
                # back; see the count-aware matching note above for why this is per-job, not
                # per-type.)
                running_type_counts[a["type_id"]] -= 1
            else:
                # Chain-tier rows (intermediate reactions) have no output value worth counting
                # here — their product is consumed by the next tier up, not sold; only the
                # top-level row's own output is the thing you'd actually sell. assign_reaction
                # always stores chain-tier rows with input_cost=reward=0.0 (the whole chain's
                # cost/profit is already rolled into the top-level row), so that's also a
                # reliable signal to skip pricing them here — matches the same convention that
                # already lets input_cost/reward be summed flatly without filtering by tier.
                # Real market data required otherwise (no live price = no guess), same
                # "skip rather than guess" rule _build_opportunities already follows.
                is_chain_tier = a["input_cost"] == 0 and a["reward"] == 0
                m = market_by_type.get(a["type_id"])
                out_qty_per_run = output_qty_by_type.get(a["type_id"], 0.0)
                row_output_value = (a["runs"] * out_qty_per_run * m["buy_price"]) if (m and out_qty_per_run and not is_chain_tier) else 0.0
                pending.append({
                    "assignment_id": a["id"], "type_id": a["type_id"], "name": a["name"], "runs": a["runs"],
                    "tier_order": a["tier_order"], "input_cost": a["input_cost"], "reward": a["reward"],
                    "output_value": round(row_output_value, 2),
                    "order_id": a.get("order_id"), "order_label": order_labels.get(a.get("order_id")),
                })
                # Intermediate-tier rows are stored with input_cost/reward=0 (the full chain's
                # cost/profit already lives on the top-level row — see assign_reaction), so
                # summing every pending row never double-counts a multi-tier chain.
                pending_isk_committed += a["input_cost"]
                pending_net_profit += a["reward"]
                pending_output_value += row_output_value
                # Per-day rate for this specific job: its own real duration (runs × the
                # product's own cycle time), not a shared cadence — once committed, an
                # assignment's completion time is a fact, not a planning-time target.
                if a["reward"] > 0:
                    duration_hours = a["runs"] * cycle_hours_by_type.get(a["type_id"], 0)
                    if duration_hours > 0:
                        pending_net_profit_per_day += a["reward"] / (duration_hours / 24)
        used_slots += len(pending)

        characters.append({
            "character_id": c["character_id"], "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active) - len(pending)),
            "pending": pending,
        })
        for j in active:
            end = j.get("end_date")
            hours_left = None
            if end:
                try:
                    end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                    hours_left = round((end_ts - now) / 3600.0, 1)
                except Exception:
                    pass
            running.append({
                "character_name": c["character_name"],
                "product_type_id": j.get("product_type_id"),
                "runs": j.get("runs"),
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
            })

    return {
        "tracked": tracked_any,
        "characters": characters,
        "running": sorted(running, key=lambda r: r["hours_left"] if r["hours_left"] is not None else 1e9),
        "total_slots": total_slots,
        "free_slots": max(0, total_slots - used_slots),
        "pending_isk_committed": round(pending_isk_committed, 2),
        "pending_net_profit": round(pending_net_profit, 2),
        "pending_net_profit_per_day": round(pending_net_profit_per_day, 2),
        "pending_output_value": round(pending_output_value, 2),
    }


# ── Wizard suggestion engine ────────────────────────────────────────────────────────────────
# Two stages, not one monolithic LP: WHAT to run (a knapsack — genuinely an LP's job) and WHO
# runs it (bin-packing onto real characters/slots — not naturally an LP, and keeping it a
# separate greedy step means each stage is small enough to hand-verify on its own).

_MIN_LIQUIDITY = 1000  # order-book depth (both sides) a candidate must clear to be suggested —
# fixed heuristic, not a UI knob, per "use liquidity as a selection filter, don't show it".
_CANDIDATE_POOL_SIZE = 30  # how many of the liquidity-filtered opportunities feed the knapsack


def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs AND
    minus already-pending assignments from a previous suggestion the player hasn't installed
    yet) — only characters that have opted into job tracking count, since we can't know a
    non-tracked character's current load. A fresh "Suggest reactions" run must not double-book
    slots a prior suggestion already claimed but hasn't been confirmed as running by ESI yet;
    mirrors get_industry_jobs' slot math, which does the same running+pending subtraction."""
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        pending_counts: dict[int, int] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT character_id, COUNT(*) AS n FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders}) GROUP BY character_id", char_ids,
            ):
                pending_counts[r["character_id"]] = r["n"]
    finally:
        con.close()

    result = []
    for c in chars:
        if "read_character_jobs" not in (c["scopes"] or ""):
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        used += pending_counts.get(c["character_id"], 0)
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
        })
    return result


def _suggest_reactions(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                        material_ids: set[int] | None = None) -> dict:
    opportunities = _build_opportunities(context_id, allowed_material_ids=material_ids)
    # Needed again in stage 2 to walk each chosen candidate's own formula tree for chain_tiers
    # (the intermediate reactions a multi-tier product needs before its own reaction can even
    # start) — cheap to recompute (fetch_market_data's own cache absorbs the repeat cost).
    _loaded = _load_goo_and_reached(context_id, material_ids)
    reached = _loaded[1] if _loaded else {}
    types = _loaded[4] if _loaded else {}
    candidates = [o for o in opportunities
                  if o["buy_volume"] >= _MIN_LIQUIDITY and o["sell_volume"] >= _MIN_LIQUIDITY
                  and o["top_level_runs"] > 0 and o["net_profit_instant"] > 0
                  and o["steps"] <= max_chain_depth]
    empty = {"suggestions": [], "totals": {
        "isk_committed": 0.0, "isk_budget": isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
        "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"}}
    if not candidates:
        return empty

    # Cap each candidate's usable batch size so a huge, cheap-per-unit chain doesn't get most of
    # the ISK budget allocated to a run count that could never actually finish within the
    # player's chosen cadence (e.g. "weekly") even using every free reaction slot at once — the
    # cap uses the single best character's free-slot count as an upper bound (stage 2 below
    # clamps further to whichever character an assignment actually lands on). Cost/output/profit
    # scale down linearly with runs (unit cost and unit price don't change with batch size).
    chars_for_cap = _character_capacities(context_id)
    max_slots_available = max((c["free_slots"] for c in chars_for_cap), default=0) or 1
    capped = []
    for o in candidates:
        cycle_hours = o["cycle_time"] / 3600.0 if o["cycle_time"] else 0
        if cycle_hours <= 0:
            continue
        max_runs_in_cadence = int(max_slots_available * cadence_hours / cycle_hours)
        if max_runs_in_cadence <= 0:
            continue
        if max_runs_in_cadence >= o["top_level_runs"]:
            capped.append(o)
            continue
        scale = max_runs_in_cadence / o["top_level_runs"]
        c2 = dict(o)
        c2["top_level_runs"] = max_runs_in_cadence
        c2["output_qty"] = o["output_qty"] * scale
        c2["input_cost"] = o["input_cost"] * scale
        c2["net_profit_instant"] = o["net_profit_instant"] * scale
        c2["shipping_volume_m3"] = o["shipping_volume_m3"] * scale
        c2["instant_sell_value"] = o["instant_sell_value"] * scale
        capped.append(c2)
    candidates = capped
    if not candidates:
        return empty

    # Rank by profit per step (the "least work most profitable" ordering) before truncating to
    # a small pool — keeps the LP tiny regardless of how many opportunities Phase 2 finds.
    candidates.sort(key=lambda o: -(o["net_profit_instant"] / o["top_level_runs"]))
    candidates = candidates[:_CANDIDATE_POOL_SIZE]

    import highspy  # lazy: only ever needed here, keeps it off the cold-start path (matches app.optimizer)
    n = len(candidates)
    h = highspy.Highs()
    h.silent()
    # x_i in [0,1]: what fraction of candidate i's (cadence-capped) max achievable batch to
    # actually run — a continuous relaxation, not a strict per-unit integer knapsack, since with
    # only ISK as a resource constraint the LP optimum is naturally at-or-near integer anyway (at
    # most one variable fractional at the ISK cap), and this stays small/fast and easy to
    # hand-verify, matching this codebase's existing app.optimizer approach.
    hvars = h.addVariables(n, lb=[0.0] * n, ub=[1.0] * n)
    h.maximize(sum(float(c["net_profit_instant"]) * hvars[i] for i, c in enumerate(candidates)))
    h.addConstr(sum(float(c["input_cost"]) * hvars[i] for i, c in enumerate(candidates)) <= float(isk_budget))
    # Real reaction slots are ALSO a shared, limited resource across every chosen candidate —
    # the per-candidate cadence cap above only checked each one against the single BEST
    # character's slots in isolation, so the LP could (and did, in a real reported case) fund
    # several products that each look individually cadence-feasible but together demand more
    # slots than actually exist across the account; stage 2's real bin-packing then has no
    # choice but to badly overshoot the chosen cadence on whichever gets scheduled last (a real
    # instance: two suggestions landing at 24d/10d runtimes against a much shorter cadence).
    # Uses the continuous (non-ceiled) slot-demand — runs × cycle_hours ÷ cadence_hours — so it
    # stays linear in x_i; the ceil() rounding that can nudge stage 2's actual slot count up by
    # a fraction per suggestion is a minor, expected difference, not the systemic multi-week
    # overshoot this constraint fixes.
    total_free_slots = sum(c["free_slots"] for c in chars_for_cap)
    slot_demand = [c["top_level_runs"] * (c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0) / cadence_hours
                   for c in candidates]
    h.addConstr(sum(slot_demand[i] * hvars[i] for i in range(n)) <= float(total_free_slots))
    h.run()
    if h.getModelStatus() != highspy.HighsModelStatus.kOptimal:
        return empty

    x = h.getSolution().col_value
    chosen = [(c, xi) for c, xi in zip(candidates, x) if xi > 1e-6]
    chosen.sort(key=lambda cx: -(cx[0]["net_profit_instant"] * cx[1]))
    chosen = chosen[:10]  # the wizard shows up to 10 concrete suggestions
    if not chosen:
        return empty

    # Stage 2 below allocates real slots in ASCENDING ideal-slot-need order (smallest first),
    # not this profit order — letting the biggest, most profit-heavy candidate go first would
    # let it greedily claim its ENTIRE ideal slot count, leaving only rounding scraps for
    # smaller candidates; a small candidate losing even 1 slot to ceil() rounding can lose HALF
    # its allocation (a real reported case: needed 2 slots, got 1, runtime nearly doubled),
    # while a big candidate absorbing that same 1-slot shortfall barely moves its own
    # percentage. Smallest-need-first minimizes the worst-case overshoot across the whole set.
    # `suggestions` is re-sorted back to this original profit order before being returned, so
    # display order is unaffected — only the internal allocation order changes.
    def _ideal_slots_for(c, xi):
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        return max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

    alloc_order = sorted(chosen, key=lambda cx: _ideal_slots_for(cx[0], cx[1]))

    # Stage 2: allocate real reaction slots to each chosen product, all targeting completion
    # within roughly one cadence period — NOT a queue over unbounded future time (the old model),
    # since everything here is sized to finish around the same ~cadence window. Each suggestion
    # claims `slots_used` of a character's free slots (a one-time budget for this cadence period,
    # not something that frees up mid-period) — using MORE slots for a bigger batch so it still
    # finishes on time, rather than trickling one run at a time through a single slot for weeks.
    # `job_count`/`runs_per_job` are what the player actually installs in-game (one job install
    # per slot); `runs` is just the total for display.
    chars = _character_capacities(context_id)
    remaining_slots = {c["character_id"]: c["free_slots"] for c in chars if c["free_slots"] > 0}
    char_names = {c["character_id"]: c["character_name"] for c in chars}
    touched_chars: set[int] = set()

    suggestions = []
    isk_committed = net_profit = total_output_value = total_output_m3 = 0.0
    max_completion_hours = 0.0
    for c, xi in alloc_order:
        runs_needed = max(1, round(c["top_level_runs"] * xi))
        cycle_hours = c["cycle_time"] / 3600.0 if c["cycle_time"] else 1.0
        ideal_slots = max(1, math.ceil(runs_needed * cycle_hours / cadence_hours)) if cadence_hours > 0 else runs_needed

        available = [cid for cid, free in remaining_slots.items() if free > 0]
        if not available:
            continue  # no character has any reaction slots left at all — this suggestion can't be scheduled
        # Prefer consolidating onto an already-used character (fewer characters touched overall)
        # as long as it still has room; otherwise open a fresh one with the most free slots.
        touched_with_room = [cid for cid in touched_chars if remaining_slots.get(cid, 0) > 0]
        pick_id = max(touched_with_room, key=lambda cid: remaining_slots[cid]) if touched_with_room \
            else max(available, key=lambda cid: remaining_slots[cid])

        slots_used = min(ideal_slots, remaining_slots[pick_id])
        remaining_slots[pick_id] -= slots_used
        touched_chars.add(pick_id)

        # Stage 1's cadence cap sized every candidate assuming it COULD land on the single best
        # character's free-slot count — but only one candidate ever actually can. Once it's
        # known here which REAL character (and how many of ITS slots) this suggestion landed
        # on, downscale runs_needed (and everything computed from it below, via xi) to what
        # those specific slots can really finish within cadence, instead of keeping the full
        # run count and letting real duration balloon past what was asked for (a real reported
        # case: multiple suggestions each independently sized for a "best" character that only
        # one of them could actually get, landing at 11d4h against a much shorter cadence).
        if slots_used < ideal_slots:
            achievable_runs = max(1, int(slots_used * cadence_hours / cycle_hours))
            xi *= min(1.0, achievable_runs / runs_needed)
            runs_needed = achievable_runs

        runs_per_job = math.ceil(runs_needed / slots_used)
        duration_hours = (runs_needed / slots_used) * cycle_hours
        max_completion_hours = max(max_completion_hours, duration_hours)

        # Chain tiers: any INTERMEDIATE reaction this product's own formula needs (e.g.
        # goo -> Ferrofluid -> this product) — each is a SEPARATE job the player must install
        # and let finish BEFORE the top-level reaction can even start, since the "force real
        # chains" fix means an intermediate is never just bought pre-made. Slots for these come
        # from the SAME character (one suggestion, one character does the whole chain — simpler
        # than spreading it), taken out of whatever's left after the top tier's own allocation.
        chain_tiers = []
        top_via = reached.get(c["type_id"], {}).get("via")
        if top_via:
            tier_runs: dict[int, dict] = {}
            _explode_chain_tiers(top_via["inputs"], runs_needed, reached, tier_runs)
            # Deepest (closest to raw goo) first — the one the player must react first.
            ordered = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
            for tid, info in ordered:
                t_cycle_hours = info["cycle_time"] / 3600.0 if info["cycle_time"] else 1.0
                t_ideal_slots = max(1, math.ceil(info["runs"] * t_cycle_hours / cadence_hours)) if cadence_hours > 0 else info["runs"]
                t_slots_used = max(1, min(t_ideal_slots, remaining_slots.get(pick_id, 0)))
                remaining_slots[pick_id] = remaining_slots.get(pick_id, 0) - t_slots_used
                chain_tiers.append({
                    "type_id": tid, "name": types.get(tid, {}).get("name", str(tid)),
                    "runs": info["runs"],
                    "job_count": t_slots_used,
                    "runs_per_job": math.ceil(info["runs"] / t_slots_used),
                })

        cost = c["input_cost"] * xi
        reward = c["net_profit_instant"] * xi
        output_qty = c["output_qty"] * xi
        output_value = c["instant_sell_value"] * xi
        output_m3 = c["shipping_volume_m3"] * xi
        isk_committed += cost
        net_profit += reward
        total_output_value += output_value
        total_output_m3 += output_m3

        # How much MORE this specific product could use if it were ISK-funded all the way to
        # actually filling its claimed slots for the whole cadence window, instead of finishing
        # early and leaving them idle until the next check-in. Bounded by `top_level_runs` (the
        # true cadence/stock-capped max for this candidate) so this never suggests spending ISK
        # on more than could physically be produced.
        max_runs_per_job_for_cadence = math.floor(cadence_hours / cycle_hours) if cycle_hours > 0 else runs_per_job
        aligned_runs = min(slots_used * max_runs_per_job_for_cadence, c["top_level_runs"])
        align_extra_runs = max(0, aligned_runs - runs_needed)
        align_ratio = aligned_runs / c["top_level_runs"]
        align_extra_isk = round(align_extra_runs * (c["input_cost"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0
        align_extra_reward = round(align_extra_runs * (c["net_profit_instant"] / c["top_level_runs"]), 2) if align_extra_runs > 0 else 0.0

        # Profit normalized to ISK/day, matching how the PI planner already reports value_per_day
        # — divided by the CADENCE window (not this suggestion's own, possibly-shorter runtime),
        # since a batch that finishes early just leaves its claimed slots idle until the next
        # cadence check-in; the cadence-normalized rate is the honest "average ISK/day this
        # delivers" including that idle time (the align hint above already targets closing this
        # exact gap by suggesting more spend to fill the whole window).
        profit_per_day = round(reward / (cadence_hours / 24), 2) if cadence_hours > 0 else None

        suggestions.append({
            "type_id": c["type_id"], "name": c["name"],
            "runs": runs_needed,
            "job_count": slots_used,
            "runs_per_job": runs_per_job,
            "input_cost": round(cost, 2),
            "reward": round(reward, 2),
            "profit_per_day": profit_per_day,
            "output_qty": round(output_qty, 1),
            "output_value": round(output_value, 2),
            "output_m3": round(output_m3, 1),
            "runtime_hours": round(duration_hours, 1),
            "align_extra_isk": align_extra_isk,
            "align_extra_reward": align_extra_reward,
            # Absolute (not delta) values for applying the alignment in one click — the frontend
            # swaps a suggestion's displayed fields to these wholesale rather than re-running the
            # whole optimizer, so clicking "align" only ever changes THIS product, nothing else.
            "aligned_runs": aligned_runs,
            "aligned_runs_per_job": max_runs_per_job_for_cadence,
            "aligned_input_cost": round(c["input_cost"] * align_ratio, 2),
            "aligned_reward": round(c["net_profit_instant"] * align_ratio, 2),
            "aligned_profit_per_day": round(c["net_profit_instant"] * align_ratio / (cadence_hours / 24), 2) if cadence_hours > 0 else None,
            "aligned_output_qty": round(c["output_qty"] * align_ratio, 1),
            "aligned_output_value": round(c["instant_sell_value"] * align_ratio, 2),
            "aligned_output_m3": round(c["shipping_volume_m3"] * align_ratio, 1),
            "assigned_character": char_names.get(pick_id, "?"),
            "assigned_character_id": pick_id,
            "chain_tiers": chain_tiers,
        })

    # Built in allocation order (smallest slot-need first, see alloc_order above) — restore
    # profit-descending order for display, matching what the LP itself ranked as most valuable.
    suggestions.sort(key=lambda s: -s["reward"])

    # "isk" = spent (near enough) the whole budget; "neither" = ran out of profitable, liquid,
    # within-chain-depth/cadence candidates before using it all — raising the ISK budget further
    # won't help, there's nothing more suitable to spend it on right now.
    binding = "isk" if isk_committed >= 0.97 * isk_budget else "neither"

    return {
        "suggestions": suggestions,
        "totals": {
            "isk_committed": round(isk_committed, 2),
            "isk_budget": isk_budget,
            "net_profit": round(net_profit, 2),
            "net_profit_per_day": round(net_profit / (cadence_hours / 24), 2) if cadence_hours > 0 and suggestions else None,
            "output_value": round(total_output_value, 2),
            "output_m3": round(total_output_m3, 1),
            "characters_used": len(touched_chars),
            "completion_hours": round(max_completion_hours, 1) if suggestions else None,
            "binding": binding,
        },
    }


class SuggestRequest(BaseModel):
    isk_budget: float
    max_chain_depth: int = 2
    cadence_hours: float = 168.0  # default weekly — how long you want a batch to run before checking back in
    material_ids: list[int] | None = None  # None/empty = no restriction, every priced material usable


_BUDGET_SENSITIVITY_STEP = 0.10  # "what if you raised your ISK budget by 10%?"


def _build_advisor(context_id: int, isk_budget: float, max_chain_depth: int, cadence_hours: float,
                    material_ids: set[int] | None, current_profit: float, current_profit_per_day: float | None,
                    current_binding: str, suggestions: list[dict]) -> dict:
    """Cheap, easily-computable "how could this be better" hints — not a full analysis, just the
    obvious low-effort wins: whether a bit more ISK would actually buy meaningfully more profit
    right now (vs. there being nothing left worth spending it on within the current chain-depth/
    cadence/material limits), per-product cadence-alignment gaps (a suggestion that finishes
    early and leaves its claimed slots idle for the rest of the cadence window, for want of a bit
    more ISK to keep them running), and which excluded fuel blocks would be worth allowing back
    in. Deliberately does NOT suggest skill training — unlike every other hint here, training a
    reaction skill takes real days/weeks in-game, not something this session can act on, so it
    read as permanent background noise rather than a real "low-effort win" (explicit user
    feedback)."""
    # Budget sensitivity: only worth suggesting "raise your ISK budget" when ISK is actually the
    # thing holding this back right now (current_binding == "isk") — if the current run already
    # left ISK unspent ("neither"), the real limit is something else (chain depth, cadence,
    # material filter, or simply no more profitable/liquid candidates), and more ISK wouldn't
    # help; recommending it anyway would be confusing/wrong advice.
    budget_hint = None
    if current_binding == "isk" and current_profit > 0:
        bigger = _suggest_reactions(context_id, isk_budget * (1 + _BUDGET_SENSITIVITY_STEP),
                                     max_chain_depth, cadence_hours, material_ids)
        extra_profit = bigger["totals"]["net_profit"] - current_profit
        if extra_profit > current_profit * 0.01:
            budget_hint = {
                "extra_isk": round(isk_budget * _BUDGET_SENSITIVITY_STEP, 2),
                "extra_profit": round(extra_profit, 2),
            }

    # Per-product cadence-alignment gaps (see the align_extra_isk/align_extra_reward computed
    # alongside each suggestion in _suggest_reactions) — worth a mention only when it's a
    # meaningful amount of profit, not a rounding-sized sliver.
    align_hints = [
        {"name": s["name"], "extra_isk": s["align_extra_isk"], "extra_reward": s["align_extra_reward"]}
        for s in suggestions if s.get("align_extra_isk", 0) > 0 and s["align_extra_reward"] > current_profit * 0.01
    ]

    # Fuel-block breadth: if the caller restricted which racial fuel blocks to use (the advanced
    # material filter, e.g. "only Oxygen — that's my cheap local one"), quantify what re-adding
    # each EXCLUDED one would actually be worth, in the same ISK/day terms the rest of this tool
    # already reports profit in — turns "I locked myself to one fuel block" from a guess into a
    # real number ("+12% ISK/day if you also used Hydrogen Fuel Block") the player can weigh
    # against how much of a hassle sourcing that second variant actually is for them.
    fuel_block_hints = []
    if material_ids is not None and current_profit_per_day:
        con = get_connection()
        try:
            reactions_by_output, inputs_by_reaction = _load_reaction_graph(con)
        finally:
            con.close()
        all_fuel_blocks = _fuel_block_ids(inputs_by_reaction, reactions_by_output, load_pi_data()["types"])
        excluded = {tid: name for tid, name in all_fuel_blocks.items() if tid not in material_ids}
        for tid, name in excluded.items():
            widened = _suggest_reactions(context_id, isk_budget, max_chain_depth, cadence_hours,
                                          material_ids | {tid})
            widened_per_day = widened["totals"].get("net_profit_per_day") or 0.0
            extra_per_day = widened_per_day - current_profit_per_day
            if extra_per_day > current_profit_per_day * 0.01:
                fuel_block_hints.append({
                    "type_id": tid, "name": name,
                    "extra_isk_per_day": round(extra_per_day, 2),
                    "extra_pct": round(100 * extra_per_day / current_profit_per_day, 1),
                })
        fuel_block_hints.sort(key=lambda h: -h["extra_isk_per_day"])

    return {"budget_hint": budget_hint, "align_hints": align_hints, "fuel_block_hints": fuel_block_hints}


@router.post("/api/reactions/suggest")
def suggest_reactions(req: SuggestRequest, context_id: int = Depends(require_context)):
    if req.isk_budget <= 0 or req.max_chain_depth <= 0 or req.cadence_hours <= 0:
        return {"suggestions": [], "totals": {
            "isk_committed": 0.0, "isk_budget": req.isk_budget, "net_profit": 0.0, "net_profit_per_day": None,
            "output_value": 0.0, "output_m3": 0.0, "characters_used": 0, "completion_hours": None, "binding": "neither"},
            "advisor": {"budget_hint": None, "align_hints": [], "fuel_block_hints": []}}
    material_ids = set(req.material_ids) if req.material_ids else None
    result = _suggest_reactions(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours, material_ids)
    result["advisor"] = _build_advisor(context_id, req.isk_budget, req.max_chain_depth, req.cadence_hours,
                                        material_ids, result["totals"]["net_profit"],
                                        result["totals"].get("net_profit_per_day"), result["totals"]["binding"],
                                        result["suggestions"])
    return result


# ── Customer orders: committing a fixed order to real reaction slots ───────────────────────────

def _allocate_and_insert(context_id: int, type_id: int, name: str, node: dict, reached: dict,
                          types: dict, runs_needed: int, order_id: int) -> dict:
    """Commits `runs_needed` top-level runs (plus any intermediate chain-tier reactions the
    formula needs) onto ONE character with enough free reaction slots right now — deliberately
    single-character, not spread across several like _suggest_reactions' stage 2: there's no
    per-job runs cap in this app's model (assign_reaction already lets one job carry an
    arbitrary run count), so a whole batch always fits in one job once a character has a free
    slot for it, and an intermediate reaction's output has to be physically on the same
    character as the job that consumes it anyway (same rule the manual-assign modal already
    enforces). Repeated "assign next batch" calls naturally land on different characters over
    time as each one's slots fill up, since this always re-reads free slots fresh."""
    chars = [c for c in _character_capacities(context_id) if c["free_slots"] > 0]
    if not chars or runs_needed <= 0:
        return {"runs_assigned": 0, "characters": []}
    chars.sort(key=lambda c: -c["free_slots"])

    formula = node.get("via")
    tier_runs: dict[int, dict] = {}
    if formula:
        _explode_chain_tiers(formula["inputs"], runs_needed, reached, tier_runs)
    ordered_tiers = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
    chain_job_slots = len(ordered_tiers)

    pick = next((c for c in chars if c["free_slots"] >= chain_job_slots + 1), None)
    if pick is None:
        if chain_job_slots > 0:
            return {"runs_assigned": 0, "characters": [], "error":
                     f"Needs {chain_job_slots} intermediate reaction job slot(s) plus 1 for the product "
                     f"itself, all on one character — none of your tracked characters has that much free "
                     f"right now. Free up slots, or assign a smaller batch."}
        pick = chars[0]

    now = _time.time()
    con = get_connection()
    try:
        for tier_order, (tid, info) in enumerate(ordered_tiers):
            tname = types.get(tid, {}).get("name", str(tid))
            _insert_assignment_rows(con, pick["character_id"], tid, tname, info["runs"], 1,
                                     0.0, 0.0, tier_order, now, order_id)
        unit_cost = node.get("unit_cost", 0.0) + node.get("job_cost", 0.0)
        _insert_assignment_rows(con, pick["character_id"], type_id, name, runs_needed, 1,
                                 unit_cost * runs_needed, 0.0, len(ordered_tiers), now, order_id)
        con.commit()
    finally:
        con.close()
    return {"runs_assigned": runs_needed,
            "characters": [{"character_id": pick["character_id"], "character_name": pick["character_name"],
                             "runs": runs_needed}]}


def _order_report(context_id: int, order: dict) -> dict:
    """Materials/cost/time report for a customer order — recomputed LIVE against current prices
    and the order's OWN stored `top_level_runs`/`target_qty` (a fixed order doesn't rescale with
    market conditions the way the day-cadence opportunity table does). No markup is applied to
    cost — the user decides what to actually charge the client; this only reports what it costs
    to produce."""
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(order["type_id"]) if loaded else None
    if not node or node.get("via") is None:
        return {
            "materials": [], "chain_tiers": [],
            "cost": {"material_cost": None, "job_cost": None, "total_cost": None, "cost_per_unit": None},
            "time": {"tiers": [], "free_slots_now": 0, "estimated_hours": None, "caveat": None},
            "stale": True,
        }
    goo, reached, reactions_by_output, inputs_by_reaction, types = loaded
    formula = node["via"]
    output_qty = formula["output_qty"]
    top_level_runs = order["top_level_runs"]
    target_qty = order["target_qty"]

    material_cost = top_level_runs * output_qty * node["unit_cost"]
    job_cost = top_level_runs * output_qty * node.get("job_cost", 0.0)
    total_cost = material_cost + job_cost
    cost = {
        "material_cost": round(material_cost, 2), "job_cost": round(job_cost, 2),
        "total_cost": round(total_cost, 2),
        "cost_per_unit": round(total_cost / target_qty, 2) if target_qty else 0.0,
    }

    totals: dict[int, float] = {}
    _explode_shopping_list(order["type_id"], target_qty, reached, totals)
    materials = _materials_report(totals, reached, types)

    tier_runs: dict[int, dict] = {}
    _explode_chain_tiers(formula["inputs"], top_level_runs, reached, tier_runs)
    ordered_tiers = sorted(tier_runs.items(), key=lambda kv: reached.get(kv[0], {}).get("reaction_count", 0))
    chain_tiers = [
        {"type_id": tid, "name": types.get(tid, {}).get("name", str(tid)), "runs": info["runs"],
         "cycle_time": info["cycle_time"], "output_qty": info["output_qty"]}
        for tid, info in ordered_tiers
    ]

    # Time estimate: chain tiers must finish before the tier above can even start (sequential,
    # not parallel-with-each-other), so durations ADD across tiers — within a single tier, spread
    # its own runs across however many free slots you have right now. An honest approximation,
    # not a guarantee (see the caveat text) — matches this tool's "advice, not a tool" convention
    # rather than presenting false precision.
    free_slots_now = sum(c["free_slots"] for c in _character_capacities(context_id))
    sequence = chain_tiers + [{"type_id": order["type_id"], "name": order["name"], "runs": top_level_runs,
                                "cycle_time": node.get("cycle_time")}]
    estimated_hours = 0.0
    for tier in sequence:
        cycle_hours = (tier["cycle_time"] or 3600) / 3600.0
        jobs_used = min(free_slots_now, tier["runs"]) or 1
        estimated_hours += math.ceil(tier["runs"] / jobs_used) * cycle_hours

    time_report = {
        "tiers": sequence, "free_slots_now": free_slots_now, "estimated_hours": round(estimated_hours, 1),
        "caveat": "Assumes your current free reaction slots stay free until each tier finishes, run in "
                  "sequence (each intermediate tier must finish before the next starts) — a rough "
                  "estimate, not a guarantee.",
    }

    return {"materials": materials, "chain_tiers": chain_tiers, "cost": cost, "time": time_report, "stale": False}


class OrderCreateRequest(BaseModel):
    type_id: int
    target_qty: float
    client_name: str | None = None
    notes: str | None = None


@router.post("/api/reactions/orders")
def create_reaction_order(req: OrderCreateRequest, context_id: int = Depends(require_context)):
    if req.target_qty <= 0:
        raise HTTPException(status_code=400, detail="Target quantity must be positive")
    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(req.type_id) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=404, detail="Not a reachable reaction product right now")
    types = loaded[4]
    name = types.get(req.type_id, {}).get("name", str(req.type_id))
    output_qty = node["via"]["output_qty"]
    top_level_runs = max(1, math.ceil(req.target_qty / output_qty))

    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order_id = con.execute(
            "INSERT INTO pp_reaction_orders (context_id, type_id, name, target_qty, top_level_runs, "
            "assigned_runs, client_name, notes, status, created_at) VALUES (?,?,?,?,?,0,?,?,'open',?) "
            "RETURNING id",
            (context_id, req.type_id, name, req.target_qty, top_level_runs,
             (req.client_name or "").strip() or None, (req.notes or "").strip() or None, _time.time()),
        ).fetchone()[0]
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order, **_order_report(context_id, order)}


@router.get("/api/reactions/orders")
def list_reaction_orders(context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, type_id, name, target_qty, top_level_runs, assigned_runs, client_name, notes, "
            "status, created_at FROM pp_reaction_orders WHERE context_id=? "
            "ORDER BY CASE WHEN status='open' THEN 0 ELSE 1 END, created_at DESC",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return {"orders": [dict(r) for r in rows]}


def _get_order_or_404(con, order_id: int, context_id: int) -> dict:
    row = con.execute(
        "SELECT * FROM pp_reaction_orders WHERE id=? AND context_id=?", (order_id, context_id)
    ).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Order not found")
    return dict(row)


@router.get("/api/reactions/orders/{order_id}")
def get_reaction_order(order_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
    finally:
        con.close()
    return {"order": order, **_order_report(context_id, order)}


class OrderAssignRequest(BaseModel):
    runs: int | None = None  # None = assign everything still remaining


@router.post("/api/reactions/orders/{order_id}/assign")
def assign_reaction_order(order_id: int, req: OrderAssignRequest, context_id: int = Depends(require_context)):
    """Commits the next batch of this order's remaining runs to a real reaction slot — see
    _allocate_and_insert. Occupies slots the same way the suggestion/manual-assign flow does;
    `assigned_runs` is monotonic (never decreases here) since committing a slot is a real
    action taken, distinct from the order's own status (still `open` until the player marks it
    delivered — see set_reaction_order_status)."""
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
    finally:
        con.close()
    if order["status"] != "open":
        raise HTTPException(status_code=400, detail="This order isn't open")
    remaining = order["top_level_runs"] - order["assigned_runs"]
    if remaining <= 0:
        raise HTTPException(status_code=400, detail="Every run for this order has already been assigned")
    runs_to_assign = min(req.runs, remaining) if req.runs else remaining
    if runs_to_assign <= 0:
        raise HTTPException(status_code=400, detail="Nothing to assign")

    loaded = _load_goo_and_reached(context_id)
    node = loaded[1].get(order["type_id"]) if loaded else None
    if not node or node.get("via") is None:
        raise HTTPException(status_code=400, detail="This product isn't reachable right now — check priced materials")
    reached, types = loaded[1], loaded[4]

    result = _allocate_and_insert(context_id, order["type_id"], order["name"], node, reached, types,
                                   runs_to_assign, order_id)
    if result["runs_assigned"] <= 0:
        raise HTTPException(status_code=400, detail=result.get("error") or "No free reaction slots right now")

    con = get_connection()
    try:
        con.execute("UPDATE pp_reaction_orders SET assigned_runs = assigned_runs + ? WHERE id=?",
                     (result["runs_assigned"], order_id))
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order, "runs_assigned": result["runs_assigned"], "characters": result["characters"]}


class OrderStatusRequest(BaseModel):
    status: str  # 'completed' or 'cancelled'


@router.post("/api/reactions/orders/{order_id}/status")
def set_reaction_order_status(order_id: int, req: OrderStatusRequest, context_id: int = Depends(require_context)):
    """Manual override — "I delivered the goods to the client" / "the client backed out". This
    tool has no way to know a real reaction job finished or the goods actually changed hands, so
    completion is always a deliberate player action, never inferred."""
    if req.status not in ("completed", "cancelled"):
        raise HTTPException(status_code=400, detail="status must be 'completed' or 'cancelled'")
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        _get_order_or_404(con, order_id, context_id)
        con.execute("UPDATE pp_reaction_orders SET status=? WHERE id=?", (req.status, order_id))
        con.commit()
        order = dict(con.execute("SELECT * FROM pp_reaction_orders WHERE id=?", (order_id,)).fetchone())
    finally:
        con.close()
    return {"order": order}


@router.delete("/api/reactions/orders/{order_id}")
def delete_reaction_order(order_id: int, context_id: int = Depends(require_context)):
    """Only when nothing's been committed yet (assigned_runs == 0) — once real reaction slots
    have been claimed for this order, cancel it instead so the assignment history linked via
    order_id never dangles."""
    ensure_reaction_orders_table()
    con = get_connection()
    try:
        order = _get_order_or_404(con, order_id, context_id)
        if order["assigned_runs"] > 0:
            raise HTTPException(status_code=400,
                                 detail="Runs have already been assigned to this order — cancel it instead of deleting")
        con.execute("DELETE FROM pp_reaction_orders WHERE id=?", (order_id,))
        con.commit()
    finally:
        con.close()
    return {"ok": True}
