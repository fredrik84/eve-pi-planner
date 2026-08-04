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

import json
import time

from fastapi import Depends
from pydantic import BaseModel

from app.db import get_connection
from app.sde import ensure_once, add_columns
from app.esi import require_context, require_admin

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
        add_columns(con, "pp_industry_settings", "margin_pct REAL",
                    # The account's "always buy these" list, a JSON id array. Stored here rather
                    # than per order because it's a standing way of operating, not a decision about
                    # one build — an order overrides it with force_build_ids.
                    "never_build_ids TEXT",
                    # Has the first-run setup screen been completed? Mirrors pp_markets.onboarded.
                    # Per ACCOUNT, not per browser: "I've set this up" is a fact about the account,
                    # and a localStorage flag would re-ask on every new device and forget on a
                    # cache clear.
                    "onboarded INTEGER",
                    # When the tab last tried to refresh a stale cache on its own. Stamped on the
                    # ATTEMPT, not the result — see app/industry/freshness.py.
                    "auto_refreshed_at REAL",
                    # The stock source the last build was pointed at, so the next one can default to
                    # it instead of asking again. Per ACCOUNT and not derived from the orders table:
                    # it has to survive that order being finished and cleared.
                    "last_source_key TEXT")
        # Anyone who already has saved build options has plainly used this tab before, so they must
        # not be handed a first-run screen. Safe to re-run on a restart precisely because a user who
        # has NOT been through setup owns no settings row: the frontend only seeds one once the
        # wizard is done (see the guard on _indSaveSettings in onIndustryTabOpen).
        con.execute("UPDATE pp_industry_settings SET onboarded = 1 "
                    "WHERE onboarded IS NULL AND updated_at IS NOT NULL")
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
        d = dict(row) if row else {}
    finally:
        con.close()
    d["never_build_ids"] = _parse_ids(d.get("never_build_ids"))
    d["onboarded"] = bool(d.get("onboarded"))
    return d


def _parse_ids(value) -> list[int]:
    """The blacklist column holds a JSON array; '' / NULL / anything unparseable means no list."""
    try:
        return [int(v) for v in json.loads(value or "[]")]
    except Exception:
        return []


def get_blacklist(context_id: int) -> list[int]:
    return get_settings(context_id).get("never_build_ids") or []


def set_blacklist(context_id: int, type_ids: list[int]) -> list[int]:
    """Replace the account's always-buy list. Written through its own path rather than the settings
    PUT: that one is a debounced save of the whole plan form, so a knob moving would carry a stale
    (or absent) blacklist along with it and quietly undo an edit made elsewhere on the page."""
    ensure_industry_settings_table()
    ids = sorted({int(t) for t in type_ids})
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, never_build_ids, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "never_build_ids=excluded.never_build_ids, updated_at=excluded.updated_at",
            (context_id, json.dumps(ids), time.time()))
        con.commit()
    finally:
        con.close()
    return ids


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
    # WHICH facility, not just its percentages: per-job routing needs to know whether the selected
    # facility is one of the account's own structures. A plan run without a browser (share link,
    # checklist) must route the same way the user's screen did.
    if "facility_id" not in sent and saved.get("facility_id"):
        update["facility_id"] = str(saved["facility_id"])
    # The blacklist is a standing rule, so it applies to every plan path — the checklist and the
    # customer's share link included — not only to plans a browser asks for.
    if "never_build_ids" not in sent and saved.get("never_build_ids"):
        update["never_build_ids"] = list(saved["never_build_ids"])
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


@router.post("/api/industry/onboarding/complete")
def complete_onboarding(ctx: int = Depends(require_context)):
    """Mark the first-run setup screen done, so it never blocks the tab again.

    Written on its own rather than as a field on the settings PUT for the same reason the blacklist
    is: that PUT is a debounced save of the plan form, and a knob moving must not be able to reset
    (or set) whether the account has been through setup.
    """
    ensure_industry_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, onboarded, updated_at) VALUES (?,1,?) "
            "ON CONFLICT(context_id) DO UPDATE SET onboarded=1, updated_at=excluded.updated_at",
            (ctx, time.time()))
        con.commit()
    finally:
        con.close()
    return {"onboarded": True}


@router.post("/api/industry/onboarding/reset")
def reset_onboarding(ctx: int = Depends(require_admin)):
    """Replay the first-run setup screen on YOUR OWN account. Admin-only, and deliberately not a
    tool for resetting anyone else's: the flag is backfilled from having saved build options, so
    nobody who has used the tab can see that screen again, which leaves no way to check the thing
    every new user meets first.

    Writes 0 rather than NULL on purpose — the backfill in `ensure_industry_settings_table` claims
    NULL rows, so a NULL here would be silently undone on the next pod restart.
    """
    ensure_industry_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, onboarded, updated_at) VALUES (?,0,?) "
            "ON CONFLICT(context_id) DO UPDATE SET onboarded=0, updated_at=excluded.updated_at",
            (ctx, time.time()))
        con.commit()
    finally:
        con.close()
    return {"onboarded": False}


# ── Always-buy blacklist ──────────────────────────────────────────────────────────────────────

def _named(context_id: int) -> dict:
    """The blacklist with names attached — an id list is unreadable, and this is a list the user
    edits by hand."""
    ids = get_blacklist(context_id)
    names = {}
    if ids:
        con = get_connection()
        try:
            names = {r["type_id"]: r["name"] for r in con.execute(
                f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                tuple(ids))}
        finally:
            con.close()
    return {"items": [{"type_id": t, "name": names.get(t, str(t))} for t in ids]}


class BlacklistEdit(BaseModel):
    type_id: int
    add: bool = True


@router.get("/api/industry/blacklist")
def read_blacklist(ctx: int = Depends(require_context)):
    """Components the account always buys instead of building."""
    return _named(ctx)


@router.post("/api/industry/blacklist")
def edit_blacklist(req: BlacklistEdit, ctx: int = Depends(require_context)):
    """Add or remove one item. One item at a time rather than a whole-list PUT: the list is edited
    from wherever the item is on screen (a shopping row, a job chip), never as a form."""
    ids = set(get_blacklist(ctx))
    ids.add(int(req.type_id)) if req.add else ids.discard(int(req.type_id))
    set_blacklist(ctx, sorted(ids))
    return _named(ctx)
