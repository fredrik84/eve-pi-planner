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

from fastapi import Depends, HTTPException
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
                    # …and its mirror: build THESE whatever the shortcuts say. One type can only be
                    # in one of the two — see `set_component_rules`, which is the only writer of
                    # both and keeps them disjoint.
                    "always_build_ids TEXT",
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
                    "last_source_key TEXT",
                    # Does this account run reactions, and which kinds — a JSON object
                    # {"build_reactions": bool, "buy_categories": [category key, …]}. Per ACCOUNT
                    # for the same reason the blacklist is: it's a standing way of operating, not a
                    # decision about one build. An order overrides it with build_reactions.
                    "reaction_policy TEXT",
                    # …and the whole remembered SET, a JSON array. A build gathered from a reaction
                    # can and a manufacturing can answers the same question with the same two boxes
                    # every time; `last_source_key` keeps the first of them so nothing that only
                    # knows the single field changes.
                    "last_source_keys TEXT",
                    # Plan each queued order on its OWN instead of aggregating the queue into one
                    # shared batch. A standing way of operating (a builder either runs a container
                    # per build or does not), so it lives here rather than on an order — see
                    # `plan_queue_per_order`. NULL/0 = the aggregated plan, which is the default and
                    # what every account planned with before this existed.
                    "per_order_plans INTEGER",
                    # The longest a single REACTION job may run, in DAYS. Days because that is the
                    # unit the ceiling is thought in — "never spend more than 2-3 days on a reaction
                    # job" — and a reaction batch is a multi-day object; hours would put a builder
                    # in front of a box wanting 72. Stored as REAL so half a day is sayable.
                    # NULL/0 = no ceiling, which is what every account planned with before this
                    # existed. Manufacturing has no equivalent on purpose: splitting a manufacturing
                    # batch spends blueprint COPIES, which cost ISK, while a reaction formula is
                    # durable and reused by every later build — so this half can stand alone.
                    "max_reaction_job_days REAL",
                    # Where a whole rig FAMILY is built, overriding the routing's inference — a JSON
                    # object {family key: "s:<pp_markets row id>"}. Per ACCOUNT and stored here with
                    # the other standing options rather than on an order: "capitals are built in the
                    # Sotiyo" is how this builder operates, not a decision about one hull.
                    # Referenced by the pp_markets ROW id, which is exactly the key routing already
                    # speaks (`BuildSite.key`, `facility_id`), not by location_id: a hand-described
                    # structure has a synthetic negative location, two rows can name one location,
                    # and a row that is deleted takes its id with it — so a stale pin resolves to no
                    # candidate and falls back, instead of silently matching a different building.
                    "build_pins TEXT")
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
    d["always_build_ids"] = _parse_ids(d.get("always_build_ids"))
    # Keep the RAW column beside the parsed policy: a NULL/blank one is an account that has never
    # used the control, which is who the code default speaks for and who the build page tells that
    # the default moved. Other writers (onboarding, source memory, freshness) create the ROW, so
    # "row exists" proves nothing here — only this column does.
    d["reaction_policy_stored"] = bool(str(d.get("reaction_policy") or "").strip())
    d["reaction_policy"] = _parse_reaction_policy(d.get("reaction_policy"))
    d["onboarded"] = bool(d.get("onboarded"))
    d["build_pins"] = _parse_build_pins(d.get("build_pins"))
    d["per_order_plans"] = bool(d.get("per_order_plans"))
    # None means "no ceiling", and it has to stay None rather than becoming 0.0: `apply_account_
    # build_options` only fills a field the account has an opinion about, and a 0 would read as one.
    try:
        _mj = float(d.get("max_reaction_job_days") or 0.0)
    except (TypeError, ValueError):
        _mj = 0.0
    d["max_reaction_job_days"] = _mj if _mj > 0 else None
    # The remembered source set, with the single-key column as its fallback so an account that last
    # bound a build before sets existed still arrives with its picker answered.
    try:
        keys = [str(k) for k in json.loads(d.get("last_source_keys") or "[]") if str(k).strip()]
    except Exception:
        keys = []
    if not keys and (d.get("last_source_key") or "").strip():
        keys = [d["last_source_key"].strip()]
    d["last_source_keys"] = keys
    return d


def _parse_ids(value) -> list[int]:
    """The blacklist column holds a JSON array; '' / NULL / anything unparseable means no list."""
    try:
        return [int(v) for v in json.loads(value or "[]")]
    except Exception:
        return []


def default_reaction_policy() -> dict:
    """What an account gets before it has ever set a policy. The categories, and WHY those, are in
    `app/industry/categories.py` — this only says that reactions run at all, which is not a default
    anyone would want flipped: buying every reaction in is a whole line of business turned off."""
    from app.industry.categories import DEFAULT_BUY_CATEGORIES
    return {"build_reactions": True, "buy_categories": sorted(DEFAULT_BUY_CATEGORIES)}


def _parse_reaction_policy(value) -> dict:
    """The stored policy, normalised. A MISSING key takes the code default, a PRESENT one is obeyed
    exactly — that distinction is the whole point: an empty stored `buy_categories` is an account
    saying "build all three", and must not be re-defaulted into buying composites on the next read.
    Since `set_reaction_policy` always writes both keys, "absent" means only one thing: this account
    has never used the control. An unparseable value lands in the same place a never-set one does,
    which is still the conservative end — reactions keep running, only the default family is bought.
    Unknown category keys are dropped against the registry: a key that vanished in a rename must not
    keep half-applying."""
    from app.industry.categories import REACTION_CATEGORIES
    dflt = default_reaction_policy()
    d = {}
    try:
        raw = json.loads(value or "{}")
        if isinstance(raw, dict):
            d = raw
    except Exception:
        d = {}
    if "buy_categories" in d:
        cats = [str(k) for k in (d.get("buy_categories") or []) if str(k) in REACTION_CATEGORIES]
    else:
        cats = dflt["buy_categories"]
    return {"build_reactions": bool(d.get("build_reactions", dflt["build_reactions"])),
            "buy_categories": sorted(set(cats))}


def get_reaction_policy(context_id: int) -> dict:
    return get_settings(context_id).get("reaction_policy") or default_reaction_policy()


def set_reaction_policy(context_id: int, build_reactions: bool, buy_categories: list[str]) -> dict:
    """Replace the account's reaction build policy. Its OWN write path, like `set_blacklist` and for
    the same reason: the settings PUT is a debounced save of the whole plan form, so every knob move
    would carry a stale policy along with it and quietly undo a change made elsewhere on the page."""
    from app.industry.categories import REACTION_CATEGORIES
    ensure_industry_settings_table()
    policy = {"build_reactions": bool(build_reactions),
              "buy_categories": sorted({str(k) for k in (buy_categories or [])
                                        if str(k) in REACTION_CATEGORIES})}
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, reaction_policy, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "reaction_policy=excluded.reaction_policy, updated_at=excluded.updated_at",
            (context_id, json.dumps(policy), time.time()))
        con.commit()
    finally:
        con.close()
    return policy


def get_per_order_plans(context_id: int) -> bool:
    """Does this account plan each order separately? Default False — the aggregated queue plan is
    cheaper, and switching costs real ISK, so it is never turned on for somebody."""
    return bool(get_settings(context_id).get("per_order_plans"))


def set_per_order_plans(context_id: int, on: bool) -> bool:
    """Its own write path, for the same reason the blacklist has one: the settings PUT is a
    debounced save of the plan form, and this is not a knob on that form — it changes what a plan
    IS, and a stale value riding along with a slider move would silently re-quote every build."""
    ensure_industry_settings_table()
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, per_order_plans, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "per_order_plans=excluded.per_order_plans, updated_at=excluded.updated_at",
            (context_id, 1 if on else 0, time.time()))
        con.commit()
    finally:
        con.close()
    return bool(on)


def get_max_reaction_job_days(context_id: int) -> float | None:
    """The longest one reaction job may run, in days — or None when the account has set no ceiling.

    Default None, and deliberately so: a ceiling is a statement about how this builder wants to
    operate ("never park a reactor for a fortnight"), and guessing one for somebody would split
    their batches into jobs they never asked to install.
    """
    return get_settings(context_id).get("max_reaction_job_days")


def set_max_reaction_job_days(context_id: int, days: float | None) -> float | None:
    """Its own write path, for the same reason `set_per_order_plans` has one: the settings PUT is a
    debounced save of the plan form, and a slider moving must not carry a stale ceiling along and
    silently re-split every reaction in the queue. 0 / None clears it."""
    ensure_industry_settings_table()
    try:
        val = float(days) if days is not None else 0.0
    except (TypeError, ValueError):
        val = 0.0
    # A ceiling below one job's own length is unreachable anyway, and a negative one is nonsense; a
    # month is past the point where any plan is bound by it. Clamped rather than rejected — this is
    # a preference, not a command, and the plan already reports what it could not honour.
    val = 0.0 if val <= 0 else min(val, 30.0)
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, max_reaction_job_days, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "max_reaction_job_days=excluded.max_reaction_job_days, updated_at=excluded.updated_at",
            (context_id, val or None, time.time()))
        con.commit()
    finally:
        con.close()
    return val or None


def _parse_build_pins(value) -> dict[str, str]:
    """The stored family→structure pins. '' / NULL / anything unparseable means no pins, which is
    the automatic routing — the state every account was in before this existed. Normalising is the
    routing's own job (`routing.clean_pins`), so the registry check lives in one place."""
    from app.industry.routing import clean_pins
    try:
        raw = json.loads(value or "{}")
    except Exception:
        return {}
    return clean_pins(raw) if isinstance(raw, dict) else {}


def get_build_pins(context_id: int) -> dict[str, str]:
    return get_settings(context_id).get("build_pins") or {}


def set_build_pins(context_id: int, pins: dict) -> dict[str, str]:
    """Replace the account's family→structure pins. Its own write path, like `set_blacklist` and for
    the same reason: the settings PUT is a debounced save of the whole plan form, and a slider moving
    must not carry a stale pin map along and quietly move a build to another building.

    Keyed by FAMILY, so a family can be pinned to exactly one structure by construction — there is no
    "pinned to two places" state to detect and no reconciliation to get wrong. A blank/removed target
    simply drops the pin. The structure is NOT validated against the account's list here: a pin
    outlives a structure being edited, and whether it can be honoured is a question about one plan,
    answered per job where the candidates are known.
    """
    from app.industry.routing import clean_pins
    ensure_industry_settings_table()
    clean = clean_pins(pins)
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, build_pins, updated_at) "
            "VALUES (?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "build_pins=excluded.build_pins, updated_at=excluded.updated_at",
            (context_id, json.dumps(clean), time.time()))
        con.commit()
    finally:
        con.close()
    return clean


def get_always_build(context_id: int) -> list[int]:
    return get_settings(context_id).get("always_build_ids") or []


def set_component_rules(context_id: int, never: list[int], always: list[int]) -> dict:
    """Replace BOTH per-component lists in one write, because they are one decision.

    A type is "always buy this", "always build this", or neither — never two of them at once. Two
    independent setters could store a type in both, and the resolver would then answer with whichever
    rule it happened to check first (`force_build_ids` wins today, but that is a tie-break, not a
    thing a user should be able to create). Writing them together is what makes the exclusion a
    property of the store rather than a convention the UI has to keep.

    `always` wins a collision: it is the more specific instruction — the same precedence the
    resolver already applies.
    """
    ensure_industry_settings_table()
    a = sorted({int(t) for t in (always or [])})
    n = sorted({int(t) for t in (never or [])} - set(a))
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_industry_settings (context_id, never_build_ids, always_build_ids, "
            "updated_at) VALUES (?,?,?,?) ON CONFLICT(context_id) DO UPDATE SET "
            "never_build_ids=excluded.never_build_ids, "
            "always_build_ids=excluded.always_build_ids, updated_at=excluded.updated_at",
            (context_id, json.dumps(n), json.dumps(a), time.time()))
        con.commit()
    finally:
        con.close()
    return {"never_build_ids": n, "always_build_ids": a}


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
    # …and the mirror. Unioned with anything the caller sent rather than replaced: an order's own
    # "build this one anyway" is an addition to the standing rule, not a statement that the standing
    # rule no longer applies to every other component on it.
    if saved.get("always_build_ids"):
        update["force_build_ids"] = sorted(set(getattr(opts, "force_build_ids", None) or ())
                                           | {int(t) for t in saved["always_build_ids"]})
    # …and so is the reaction policy: it is the same kind of standing rule, so the checklist and the
    # customer's share link have to follow it too, or they quote a build the user isn't making.
    # …gated on its own feature, which it never was: only the FRONTEND asked the flag, so on an
    # account without it the default policy (buy composites & intermediates) was fully in force with
    # no control anywhere on the page to see or change it. That is the wrong half to ship — the
    # effect live, the escape hatch invisible — and it is expensive here, because buying a reaction
    # prices it at market inside every consumer's build cost, so the components ABOVE it lose
    # make-or-buy too and their sub-chains leave the plan. Flag off = no policy at all, which is the
    # plan that shipped before the feature existed.
    from app.features import feature_enabled_for
    policy = (saved.get("reaction_policy") or {}) if feature_enabled_for(
        "industry_reaction_policy", context_id) else {}
    if "buy_all_reactions" not in sent and policy.get("build_reactions") is False:
        update["buy_all_reactions"] = True
    if "buy_reaction_categories" not in sent and policy.get("buy_categories"):
        update["buy_reaction_categories"] = list(policy["buy_categories"])
    # …and so is the reaction job-length ceiling. It changes how many jobs a plan HAS, so a share
    # link or the start-now checklist that missed it would hand the builder a different list of jobs
    # from the one on their screen — exactly the class of bug this module exists to end. Whether it
    # is honoured at all is the feature flag's call, made once in `prepare_plan_inputs`.
    if "max_reaction_job_days" not in sent and saved.get("max_reaction_job_days"):
        update["max_reaction_job_days"] = float(saved["max_reaction_job_days"])
    # …and so are the build pins. They decide WHERE each job is installed, so a checklist or a share
    # link that missed them would tell the builder to install jobs in a different building from the
    # one their own screen named — the same class of bug as every other option here.
    if "build_pins" not in sent and saved.get("build_pins"):
        update["build_pins"] = dict(saved["build_pins"])
    return opts.model_copy(update=update) if update else opts


@router.get("/api/industry/settings")
def read_industry_settings(ctx: int = Depends(require_context)):
    s = get_settings(ctx)
    s.pop("context_id", None)
    return {"settings": s}


_SETTINGS_COLUMNS = ("struct_material_pct", "struct_time_pct", "prioritize_speed", "marginal_pct",
                     "force_build", "margin_pct", "facility_id")


def save_settings(context_id: int, fields: dict) -> None:
    """Upsert the plan-form settings row, writing ONLY the keys present in `fields`.

    Two callers with deliberately different habits share it: the PUT below sends the whole form on
    every knob move (so a key present with a None value legitimately clears that column), while the
    consolidated build-setup patch writes one section at a time and must not touch the rest. Keying
    off presence rather than value serves both, and it is why there is one column list here instead
    of the same SQL written twice.
    """
    use = {k: v for k, v in fields.items() if k in _SETTINGS_COLUMNS}
    if not use:
        return
    for flag in ("prioritize_speed", "force_build"):
        if flag in use:
            use[flag] = None if use[flag] is None else int(use[flag])
    if "facility_id" in use:
        use["facility_id"] = (use["facility_id"] or "")[:40]
    ensure_industry_settings_table()
    keys = list(use)
    con = get_connection()
    try:
        con.execute(
            f"INSERT INTO pp_industry_settings (context_id, {', '.join(keys)}, updated_at) "
            f"VALUES ({','.join('?' * (len(keys) + 2))}) "
            f"ON CONFLICT(context_id) DO UPDATE SET "
            + ", ".join(f"{k}=excluded.{k}" for k in keys)
            + ", updated_at=excluded.updated_at",
            (context_id, *[use[k] for k in keys], time.time()))
        con.commit()
    finally:
        con.close()


@router.put("/api/industry/settings")
def write_industry_settings(req: IndustrySettings, ctx: int = Depends(require_context)):
    """Save the plan form's options. Sent whenever a knob moves, so a plan run on the user's behalf
    later — a share link, a checklist — uses the settings they actually build with."""
    save_settings(ctx, req.model_dump())
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


# ── Reaction build policy ─────────────────────────────────────────────────────────────────────

class PerOrderPlansEdit(BaseModel):
    enabled: bool


@router.get("/api/industry/per-order-plans")
def read_per_order_plans(ctx: int = Depends(require_context)):
    """Whether this account's queue is planned order by order rather than as one shared batch."""
    from app.features import feature_enabled_for
    return {"enabled": get_per_order_plans(ctx),
            # Nothing is hidden server-side by the flag — a setting already switched on keeps
            # working — but the UI needs to know whether to offer it.
            "available": feature_enabled_for("industry_per_order_plans", ctx)}


@router.post("/api/industry/per-order-plans")
def edit_per_order_plans(req: PerOrderPlansEdit, ctx: int = Depends(require_context)):
    """Turning this on makes every build more expensive (shared components are built once per
    order, blueprint copies bought per order), so it is gated on the rollout ladder rather than
    settable by anyone who can guess the endpoint — and `/api/industry/queue-plan/compare` exists
    to put the number on it first."""
    from app.features import feature_enabled_for
    if not feature_enabled_for("industry_per_order_plans", ctx):
        raise HTTPException(status_code=403, detail="feature not enabled")
    return {"enabled": set_per_order_plans(ctx, req.enabled), "available": True}


class JobLengthPolicyEdit(BaseModel):
    """Days. `None` (or 0) clears the ceiling."""
    max_reaction_job_days: float | None = None


@router.get("/api/industry/job-length-policy")
def read_job_length_policy(ctx: int = Depends(require_context)):
    """How long one reaction job may run, in days. 5,000 runs of a reaction fit in a single slot and
    sit there for weeks; this is how a builder says "never more than two or three days" and has the
    work spread across the reactor slots they actually have."""
    from app.features import feature_enabled_for
    return {"max_reaction_job_days": get_max_reaction_job_days(ctx),
            # Nothing is hidden server-side by the flag beyond whether the plan HONOURS the ceiling
            # (`prepare_plan_inputs`) — the UI needs to know whether to offer the control.
            "available": feature_enabled_for("industry_job_length_policy", ctx)}


@router.post("/api/industry/job-length-policy")
def edit_job_length_policy(req: JobLengthPolicyEdit, ctx: int = Depends(require_context)):
    """Gated on the rollout ladder rather than settable by anyone who can guess the endpoint: this
    changes how many jobs a plan has, and a stored value nobody could see the effect of would be
    worse than no setting at all."""
    from app.features import feature_enabled_for
    if not feature_enabled_for("industry_job_length_policy", ctx):
        raise HTTPException(status_code=403, detail="feature not enabled")
    return {"max_reaction_job_days": set_max_reaction_job_days(ctx, req.max_reaction_job_days),
            "available": True}


class BuildPinsEdit(BaseModel):
    """The whole map, replaced. Per FAMILY, so removing a pin is sending the map without it — there
    is no half-state where a family is pinned to two structures to reconcile."""
    pins: dict[str, str] = {}


def _pins_payload(context_id: int) -> dict:
    """The pins plus the FAMILY REGISTRY they are expressed in — the frontend never hardcodes that
    list (same rule as the rig-family picker and ALERT_KINDS), so a family added in code appears in
    the pin control without a second edit. Deliberately NOT narrowed by `fittable_families`: a
    builder may pin capital ships to a Raitaru, which can build them and simply earns no rig bonus
    for it. What a hull can FIT decides the bonus, never where a job may be installed."""
    from app.industry.structures import family_registry
    return {"pins": get_build_pins(context_id), "families": family_registry(),
            "available": _pins_available(context_id)}


def _pins_available(context_id: int) -> bool:
    """Pins ride on the ROUTING flag: with routing off nothing is routed at all, so a pin has
    nothing to attach to and a flag of its own would only add a state that cannot do anything."""
    from app.features import feature_enabled_for
    from app.industry.routing import FEATURE_KEY
    return feature_enabled_for(FEATURE_KEY, context_id)


@router.get("/api/industry/build-pins")
def read_build_pins(ctx: int = Depends(require_context)):
    """Which rig families this account builds in a fixed structure, whatever the routing scores."""
    return _pins_payload(ctx)


@router.post("/api/industry/build-pins")
def edit_build_pins(req: BuildPinsEdit, ctx: int = Depends(require_context)):
    """Gated on the rollout ladder rather than settable by anyone who can guess the endpoint: a pin
    moves where jobs are installed and therefore what the build costs."""
    if not _pins_available(ctx):
        raise HTTPException(status_code=403, detail="feature not enabled")
    set_build_pins(ctx, req.pins)
    return _pins_payload(ctx)


class ReactionPolicyEdit(BaseModel):
    """Either half can be sent on its own: flipping the master switch must not clear a category
    selection the user spent time on, and vice versa."""
    build_reactions: bool | None = None
    buy_categories: list[str] | None = None


def _policy_payload(context_id: int) -> dict:
    """The policy plus the CATEGORY REGISTRY it is expressed in. The frontend never hardcodes these
    labels — same rule as ALERT_KINDS — so they travel with every read and every write."""
    from app.industry.categories import category_registry
    s = get_settings(context_id)
    return {"policy": s.get("reaction_policy") or default_reaction_policy(),
            # Is this the code default rather than a choice? The build page uses it to tell exactly
            # the affected accounts that the default moved — and to stay silent for everyone who
            # already picked, whose plans did not change.
            "defaulted": not s.get("reaction_policy_stored"),
            "categories": category_registry()}


@router.get("/api/industry/reaction-policy")
def read_reaction_policy(ctx: int = Depends(require_context)):
    """Whether this account's builds run reactions, and which families of them."""
    return _policy_payload(ctx)


@router.post("/api/industry/reaction-policy")
def edit_reaction_policy(req: ReactionPolicyEdit, ctx: int = Depends(require_context)):
    """Its own write path, deliberately not a field on the settings PUT — see `set_reaction_policy`.
    Either half may be sent alone; whatever is omitted keeps its stored value."""
    cur = get_reaction_policy(ctx)
    set_reaction_policy(
        ctx,
        cur["build_reactions"] if req.build_reactions is None else req.build_reactions,
        cur["buy_categories"] if req.buy_categories is None else req.buy_categories)
    return _policy_payload(ctx)
