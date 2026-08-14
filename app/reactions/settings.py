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
                  # efficiency rigs, skills, and structure/security bonuses all reduce it.
                  #
                  # This used to be a hand-typed number defaulting to 0%, on the grounds that the
                  # bonus could not be detected. **That is no longer true in this codebase.**
                  # `reaction_time_mult_for` (app/reactions/jobs.py) reads the real ratio off ESI job
                  # durations — measured, persisted, and falling back to the account's skills alone
                  # when nothing has ever been observed. Leaving the typed 0% default in place meant
                  # every duration and ETA the player reads was quoted off a clock running ~2.14x
                  # slow (measured multiplier 0.4680) while the leveller, which DID read the
                  # measurement, sized the same jobs off the real one. Two clocks, one plan.
                  #
                  # So 0 no longer means "no bonus" — it means **not overridden**, and
                  # `effective_reaction_settings` derives the real figure. A non-zero value is an
                  # explicit override and always wins (CLAUDE.md rule 3: no knob for a computable
                  # number; rule 5: live data trumps unless reliably derivable — both point here).
                  # Kept as 0-means-unset rather than NULL because the column ships
                  # `REAL NOT NULL DEFAULT 0` on two tables and SQLite cannot drop a NOT NULL; a
                  # deliberate "exactly 0%" override is indistinguishable from the un-set state and
                  # is also the one value the measurement can never legitimately be.
                  "time_efficiency_pct": 0.0}
_GLOBAL_SETTINGS_GROUP_ID = 0  # sentinel "no group" row — kept as a real (non-NULL) value since
# a nullable PRIMARY KEY doesn't behave consistently across SQLite and Postgres.

# The group table and the per-account override table carry the SAME six settings columns — one is
# keyed by group_id, the other by context_id, and nothing else about them differs. These four
# helpers are what both sides share; keep them in step when a setting is added.
_RXS_COLS = ("import_isk_per_m3, export_isk_per_m3, export_collateral_pct, "
             "reaction_system, facility_tax_pct, time_efficiency_pct")
# Columns added after the tables first shipped — applied to either table on startup.
_RXS_LATE_COLS = ("reaction_system TEXT", "facility_tax_pct REAL NOT NULL DEFAULT 0",
                  "time_efficiency_pct REAL NOT NULL DEFAULT 0")


def _add_settings_columns(con, table: str) -> None:
    """Best-effort ALTER for the late columns — already-there raises and is the normal case."""
    for coldef in _RXS_LATE_COLS:
        try:
            con.execute(f"ALTER TABLE {table} ADD COLUMN {coldef}")
            con.commit()
        except Exception:
            pass


def _settings_row(row) -> dict:
    """Row -> settings dict. The two tax/efficiency columns coalesce because rows written before
    those columns existed carry NULL, and the pricing math wants a number.

    `time_efficiency_pct` comes back RAW here — the stored override, 0 when unset. Only
    `effective_reaction_settings` derives the measured figure; the settings FORMS must keep seeing
    what was actually typed, or a derived value would be written straight back as an override the
    next time anyone pressed Save."""
    return {
        "import_isk_per_m3": row["import_isk_per_m3"],
        "export_isk_per_m3": row["export_isk_per_m3"],
        "export_collateral_pct": row["export_collateral_pct"],
        "reaction_system": row["reaction_system"],
        "facility_tax_pct": row["facility_tax_pct"] or 0.0,
        "time_efficiency_pct": row["time_efficiency_pct"] or 0.0,
    }


def _read_settings(table: str, key_col: str, key_val) -> dict | None:
    """Caller ensures the table first — which `ensure_*` applies is the caller's business."""
    con = get_connection()
    try:
        row = con.execute(
            f"SELECT {_RXS_COLS} FROM {table} WHERE {key_col}=?", (key_val,)
        ).fetchone()
    finally:
        con.close()
    return _settings_row(row) if row else None


def _upsert_settings(table: str, key_col: str, key_val, req: "ReactionSettingsUpdate") -> None:
    con = get_connection()
    try:
        con.execute(
            f"INSERT INTO {table} ({key_col}, import_isk_per_m3, export_isk_per_m3, "
            "export_collateral_pct, reaction_system, facility_tax_pct, time_efficiency_pct) "
            f"VALUES (?,?,?,?,?,?,?) ON CONFLICT ({key_col}) DO UPDATE SET "
            "import_isk_per_m3=excluded.import_isk_per_m3, export_isk_per_m3=excluded.export_isk_per_m3, "
            "export_collateral_pct=excluded.export_collateral_pct, reaction_system=excluded.reaction_system, "
            "facility_tax_pct=excluded.facility_tax_pct, time_efficiency_pct=excluded.time_efficiency_pct",
            (key_val, req.import_isk_per_m3, req.export_isk_per_m3, req.export_collateral_pct,
             (req.reaction_system or "").strip() or None, req.facility_tax_pct, req.time_efficiency_pct),
        )
        con.commit()
    finally:
        con.close()


def _group_defaults(context_id: int) -> dict:
    """The settings the caller inherits when they have no personal override: their group's row, or
    the global-default row when they're in no priced group."""
    group = member_group(context_id)
    return get_reaction_settings(group["id"] if group else None)


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
        _add_settings_columns(con, "pp_reaction_settings")
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
    return _read_settings("pp_reaction_settings", "group_id", gid) or dict(_RXS_DEFAULTS)


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
    _add_settings_columns(con, "pp_account_reaction_settings")
    con.close()


def _account_reaction_settings_override(context_id: int) -> dict | None:
    ensure_account_reaction_settings_table()
    return _read_settings("pp_account_reaction_settings", "context_id", context_id)


_SETTINGS_CACHE: dict[int, tuple[float, dict]] = {}
_SETTINGS_TTL = 30.0  # seconds — bounds staleness of a group-default edit (manager action, rare)


def _invalidate_reaction_settings_cache(context_id: int | None = None) -> None:
    """Drop one account's memoized settings (personal-override edit) or the whole cache (a group
    default changed, which affects every member and can't be enumerated cheaply)."""
    if context_id is None:
        _SETTINGS_CACHE.clear()
    else:
        _SETTINGS_CACHE.pop(context_id, None)


_DERIVING: set[int] = set()


def derived_time_efficiency(context_id: int) -> tuple[float, str]:
    """The account's REAL reaction time efficiency as a fraction 0-1, and where it came from.

    `reaction_time_mult_for` returns what a reaction job really takes as a fraction of its raw SDE
    cycle time — measured from ESI job durations where the account has ever reacted, the skills
    multiplier alone where it has not. Time efficiency is the other half of that: `1 - mult`.

    Returns ("measured" | "skills" | "none", ...) so the UI can say whether the number is a
    measurement or an estimate that will tighten after the first real job (there is no honest way
    to present a guess as a fact — see the repair spec's 3e).

    Imported lazily: `app.reactions.settings` is the base layer of the package and must not import
    the jobs module at module scope. Failure is never fatal — a settings read that cannot reach the
    DB falls back to "no derivation", which is the old behaviour."""
    # The cache entry is written only AFTER this returns, so anything reached from here that asks
    # for settings again would recurse forever. Nothing does today; the guard is what keeps that
    # true when someone adds a settings read to the measurement path.
    if context_id in _DERIVING:
        return 0.0, "none"
    _DERIVING.add(context_id)
    try:
        from app.reactions.jobs import _reaction_time_mult, _reaction_skill_mult
        measured = _reaction_time_mult(context_id, _derive=False)
        if measured and 0.0 < measured < 1.0:
            return 1.0 - float(measured), "measured"
        skills = _reaction_skill_mult(context_id)
        if skills and 0.0 < skills < 1.0:
            return 1.0 - float(skills), "skills"
    except Exception:
        pass
    finally:
        _DERIVING.discard(context_id)
    return 0.0, "none"


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
    caller can't mutate the cached dict.

    **`time_efficiency_pct` is DERIVED here** unless the account (or its group) typed an explicit
    override. See _RXS_DEFAULTS for why: the bonus is measurable from ESI job durations, so the
    clock every user-facing duration and ETA is quoted off is now the same one the leveller already
    sized jobs against. `time_efficiency_source` says which of measured/skills/override/none it is,
    so a surface can admit that an unmeasured account's figure is an estimate. The memoization
    above is what keeps the measurement's cost bounded — it is a scan of the account's cached ESI
    job JSON, paid at most once per _SETTINGS_TTL per account, not once per priced material."""
    now = _time.monotonic()
    hit = _SETTINGS_CACHE.get(context_id)
    if hit and now - hit[0] < _SETTINGS_TTL:
        return dict(hit[1])
    result = _account_reaction_settings_override(context_id) or _group_defaults(context_id)
    result = dict(result)
    override = float(result.get("time_efficiency_pct") or 0.0)
    if override > 0.0:
        result["time_efficiency_source"] = "override"
    else:
        result["time_efficiency_pct"], result["time_efficiency_source"] = derived_time_efficiency(context_id)
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
    # Fraction 0-1 (e.g. 0.532 for 53.2%), and an OVERRIDE only: 0 means "use the measured figure"
    # (see _RXS_DEFAULTS and effective_reaction_settings), not "no reactor bonus".
    time_efficiency_pct: float = 0.0


@router.get("/api/reactions/settings")
def api_get_reaction_settings(ctx: int = Depends(require_context)):
    """Always the CALLER's own group's settings (or the global default) — a manager only ever
    needs to see/edit their own group's rate, never someone else's.

    Carries the caller's own DERIVED time efficiency alongside the stored group value for the same
    reason the account endpoint does: the typed field is an override, and the form has to say what
    the app uses when it is left unset."""
    out = dict(_group_defaults(ctx))
    te_pct, te_source = derived_time_efficiency(ctx)
    out["derived_time_efficiency_pct"] = te_pct
    out["time_efficiency_source"] = te_source
    return out


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
    _upsert_settings("pp_reaction_settings", "group_id", group["id"], req)
    _invalidate_reaction_settings_cache()  # a group default affects every member — clear all
    return get_reaction_settings(group["id"])


@router.get("/api/reactions/account-settings")
def api_get_account_reaction_settings(ctx: int = Depends(require_context)):
    """The caller's personal shipping-cost override, if any, plus the group/global default it
    falls back to otherwise — the Settings UI shows both so a user can see what rate they're
    actually getting and whether it's their own override or an inherited default."""
    override = _account_reaction_settings_override(ctx)
    default = _group_defaults(ctx)
    # ...and the DERIVED time efficiency, so the form can say what the app is really using when the
    # typed field is left at 0 — the knob is an override now, not the source of truth, and a form
    # that shows a blank 0 next to a plan sized off 53.2% is exactly the two-clocks confusion this
    # replaced. `source` distinguishes a measurement from the skills-only estimate.
    te_pct, te_source = derived_time_efficiency(ctx)
    return {"override": override, "default": default, "effective": override or default,
            "derived_time_efficiency_pct": te_pct, "time_efficiency_source": te_source}


@router.put("/api/reactions/account-settings")
def api_update_account_reaction_settings(req: ReactionSettingsUpdate, ctx: int = Depends(require_context)):
    _validate_reaction_system(req.reaction_system)
    ensure_account_reaction_settings_table()
    _upsert_settings("pp_account_reaction_settings", "context_id", ctx, req)
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
    return _group_defaults(ctx)
