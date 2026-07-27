"""Industry build options, persisted per account.

The facility, the saving threshold, the speed shortcut and "build everything" shape every number the
planner produces — and they lived only in the browser, travelling as request fields. Any plan run
WITHOUT a browser therefore ran with library defaults, and quietly disagreed with what the user was
looking at. That produced a run of the same bug in different clothes: the start-now checklist naming
a job the plan scheduled last, and a customer's share link quoting an ETA days off the builder's own
screen (defaults mean no facility time bonus and a 3% threshold that buys components the user builds).

So the options are stored per context and applied in `prepare_plan_inputs` — the one place every plan
path passes through. A request that explicitly sets a field still wins (the live UI has to be able to
tweak a knob without saving it first); anything the caller didn't set falls back to the account's
saved value, and only then to the library default.
"""
from __future__ import annotations

import time

from fastapi import Depends
from pydantic import BaseModel

from app.db import get_connection
from app.sde import ensure_once
from app.esi import require_context

from app.industry._router import router


@ensure_once
def ensure_industry_settings_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_settings (
                context_id          INTEGER PRIMARY KEY,
                struct_material_pct REAL,
                struct_time_pct     REAL,
                prioritize_speed    INTEGER,
                marginal_pct        REAL,
                force_build         INTEGER,
                margin_pct          REAL,
                facility_id         TEXT,
                updated_at          REAL
            )
        """)
        # Added after the table shipped; additive ALTER is this codebase's migration convention.
        try:
            con.execute("ALTER TABLE pp_industry_settings ADD COLUMN margin_pct REAL")
        except Exception:
            pass
        con.commit()
    finally:
        con.close()


class IndustrySettings(BaseModel):
    """What the plan form holds. `facility_id` is the UI's preset key — stored so the control can be
    restored, never used by the engine, which only takes the ME/TE percentages it resolves to."""
    struct_material_pct: float | None = None
    struct_time_pct: float | None = None
    prioritize_speed: bool | None = None
    marginal_pct: float | None = None
    force_build: bool | None = None
    margin_pct: float | None = None      # markup over net cost when quoting a customer
    facility_id: str | None = None


def get_settings(context_id: int) -> dict:
    ensure_industry_settings_table()
    con = get_connection()
    try:
        row = con.execute("SELECT * FROM pp_industry_settings WHERE context_id=?",
                          (context_id,)).fetchone()
        return dict(row) if row else {}
    finally:
        con.close()


def apply_account_build_options(context_id: int, opts):
    """Fill in every build option the caller didn't explicitly set from the account's saved ones.

    `model_fields_set` is the whole trick: a pydantic default and an explicitly-sent value are
    otherwise indistinguishable, and treating a default as a choice would let a bare request silently
    override what the user saved — which is the bug this module exists to end.
    """
    saved = get_settings(context_id)
    if not saved:
        return opts
    sent = getattr(opts, "model_fields_set", set())
    update = {}
    for field in ("struct_material_pct", "struct_time_pct", "marginal_pct", "margin_pct"):
        if field not in sent and saved.get(field) is not None:
            update[field] = float(saved[field])
    for field in ("prioritize_speed", "force_build"):
        if field not in sent and saved.get(field) is not None:
            update[field] = bool(saved[field])
    return opts.model_copy(update=update) if update else opts


@router.get("/api/industry/settings")
def read_industry_settings(ctx: int = Depends(require_context)):
    s = get_settings(ctx)
    s.pop("context_id", None)
    return {"settings": s}


@router.put("/api/industry/settings")
def write_industry_settings(req: IndustrySettings, ctx: int = Depends(require_context)):
    """Save the plan form's options. Sent whenever a knob moves, so a plan run on the user's behalf
    later — a share link, a checklist — uses the settings they actually build with."""
    ensure_industry_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, struct_material_pct, struct_time_pct, "
            "prioritize_speed, marginal_pct, force_build, margin_pct, facility_id, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "struct_material_pct=excluded.struct_material_pct, struct_time_pct=excluded.struct_time_pct, "
            "prioritize_speed=excluded.prioritize_speed, marginal_pct=excluded.marginal_pct, "
            "force_build=excluded.force_build, margin_pct=excluded.margin_pct, "
            "facility_id=excluded.facility_id, "
            "updated_at=excluded.updated_at",
            (ctx, req.struct_material_pct, req.struct_time_pct,
             None if req.prioritize_speed is None else int(req.prioritize_speed),
             req.marginal_pct,
             None if req.force_build is None else int(req.force_build),
             req.margin_pct,
             (req.facility_id or "")[:40], time.time()))
        con.commit()
    finally:
        con.close()
    return {"ok": True}
