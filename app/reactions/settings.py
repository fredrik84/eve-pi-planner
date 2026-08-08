"""Group + per-account reaction pricing settings (shipping/collateral rates, reaction system for
job-cost, facility tax, time efficiency). The base layer of the reactions package — depends only
on the DB, ESI auth, and group membership, never on the graph/jobs/orders submodules, so it can
be imported first with no cycle. effective_reaction_settings() is the resolver everything else
prices with (personal override -> group default -> global default)."""
import time as _time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.esi import require_context
from app.groups import member_group, is_group_manager

from app.reactions._router import router

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


_SETTINGS_CACHE: dict[int, tuple[float, dict]] = {}
_SETTINGS_TTL = 30.0  # seconds — bounds staleness of a group-default edit (manager action, rare)


def _invalidate_reaction_settings_cache(context_id: int | None = None) -> None:
    """Drop one account's memoized settings (personal-override edit) or the whole cache (a group
    default changed, which affects every member and can't be enumerated cheaply)."""
    if context_id is None:
        _SETTINGS_CACHE.clear()
    else:
        _SETTINGS_CACHE.pop(context_id, None)


def effective_reaction_settings(context_id: int) -> dict:
    """The shipping/collateral rate actually used to price a specific account's reactions,
    resolved personal override -> group default -> global default. JF/import costs genuinely
    vary account to account even within one alliance (different home system, different courier
    arrangement), so a group's rate is only a starting point, not a mandate — a player can
    override it for themselves in Settings without affecting anyone else in their group.

    Memoized per process for a few seconds keyed on context_id: one suggest/opportunity request
    resolves this several times (every _load_goo_and_reached, the value math, the job-cost math)
    and each miss is 2-3 DB round-trips. The account-settings endpoints invalidate the caller's
    entry on edit; the group-default case is bounded by _SETTINGS_TTL. Returns a fresh copy so a
    caller can't mutate the cached dict."""
    now = _time.monotonic()
    hit = _SETTINGS_CACHE.get(context_id)
    if hit and now - hit[0] < _SETTINGS_TTL:
        return dict(hit[1])
    override = _account_reaction_settings_override(context_id)
    if override:
        result = override
    else:
        group = member_group(context_id)
        result = get_reaction_settings(group["id"] if group else None)
    _SETTINGS_CACHE[context_id] = (now, result)
    return dict(result)


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
    _invalidate_reaction_settings_cache()  # a group default affects every member — clear all
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
    _invalidate_reaction_settings_cache(ctx)
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
    _invalidate_reaction_settings_cache(ctx)
    group = member_group(ctx)
    return get_reaction_settings(group["id"] if group else None)


# ── How long ONE reaction job should run (`pp_reaction_job_target`) ────────────────────────────
#
# The one judgement the levelling pass cannot make for the player: how long they want to leave a
# batch cooking before they come back. Everything else about a job layout follows from the chains
# and the reactors, but "125 runs a job, so about a fortnight" versus "two days" is a preference
# about logging in, not a fact about the plan.
#
# Deliberately NOT part of the shipping/pricing settings above: those are a GROUP's rates, set by
# a manager for everyone in it, and nobody's alliance should be choosing their login cadence. Its
# own table, its own endpoint, per context.
#
# Two ways to say the same thing, because players think in both:
#   * `days`  — every job runs about this long, whatever the product. Run counts differ per
#               product (a 6-hour reaction gets half the runs of a 3-hour one) and a stage's jobs
#               land together, which is what makes one login collect the lot.
#   * `runs`  — every job carries about this run count, whatever the product. The number is the
#               same everywhere on the plan; the durations are not.
#
# `auto` is the default and means "no opinion": the pass keeps its own rule of never running a job
# longer than the longest one already planned in that stage. It matters that this is the default —
# a plan built before this existed must not have its jobs resized by a number nobody chose.
_JOB_TARGET_MODES = ("auto", "days", "runs")
# What one reaction run takes for 94 of the 112 reactions in the SDE (the rest are 6 hours). Only
# used to state a `runs` target as an approximate window for the wizard's cadence, which needs one
# number of hours and cannot have a per-product one.
_STANDARD_CYCLE_HOURS = 3.0


@ensure_once
def ensure_job_target_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_job_target (
                context_id INTEGER PRIMARY KEY,
                mode       TEXT NOT NULL DEFAULT 'auto',
                value      REAL NOT NULL DEFAULT 0
            )
        """)
        con.commit()
    finally:
        con.close()


def get_job_target(context_id: int) -> dict:
    """`{mode, value, hours, runs}` for one account. `hours`/`runs` are the resolved target, either
    of which is None when the mode does not fix it: a `days` target fixes the hours and leaves the
    run count to the product's cycle time, a `runs` target does the opposite, and `auto` fixes
    neither."""
    ensure_job_target_table()
    con = get_connection()
    try:
        row = con.execute("SELECT mode, value FROM pp_reaction_job_target WHERE context_id=?",
                          (context_id,)).fetchone()
    except Exception:
        return {"mode": "auto", "value": 0.0, "hours": None, "runs": None}
    finally:
        con.close()
    mode = (row["mode"] if row else "auto") or "auto"
    value = float(row["value"] or 0) if row else 0.0
    if mode not in _JOB_TARGET_MODES or value <= 0:
        mode, value = "auto", 0.0
    return {
        "mode": mode, "value": value,
        "hours": value * 24.0 if mode == "days" else None,
        "runs": int(value) if mode == "runs" else None,
        # What the wizard's cadence dropdown needs: one window in hours. A `runs` target has no
        # exact one (the products differ), so it is stated at the standard cycle and labelled as
        # approximate rather than silently presented as exact.
        "cadence_hours": (value * 24.0 if mode == "days"
                          else (value * _STANDARD_CYCLE_HOURS if mode == "runs" else None)),
    }


class JobTargetUpdate(BaseModel):
    mode: str = "auto"
    value: float = 0.0


@router.get("/api/reactions/job-target")
def api_get_job_target(ctx: int = Depends(require_context)):
    return get_job_target(ctx)


@router.put("/api/reactions/job-target")
def api_set_job_target(req: JobTargetUpdate, ctx: int = Depends(require_context)):
    mode = (req.mode or "auto").strip().lower()
    if mode not in _JOB_TARGET_MODES:
        raise HTTPException(400, f"mode must be one of {', '.join(_JOB_TARGET_MODES)}")
    value = 0.0 if mode == "auto" else float(req.value or 0)
    if mode != "auto" and value <= 0:
        raise HTTPException(400, "a job length has to be more than zero")
    # A run count is typed into the industry window, so it is a whole number; days can be a half.
    if mode == "runs":
        value = float(int(value))
    ensure_job_target_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_reaction_job_target (context_id, mode, value) VALUES (?,?,?) "
            "ON CONFLICT (context_id) DO UPDATE SET mode=excluded.mode, value=excluded.value",
            (ctx, mode, value))
        con.commit()
    finally:
        con.close()
    return get_job_target(ctx)
