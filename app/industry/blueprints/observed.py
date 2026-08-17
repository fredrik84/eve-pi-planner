"""Formulas observed in real industry jobs — the evidence floor Reactions reads
(`formula_print_floor`) — plus coverage and the ESI refresh endpoint."""
import hashlib as _hashlib
import json as _json
import logging
import re as _re
import time as _time
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, add_columns
from app import esi_http
from app.esi import require_context, BLUEPRINTS_SCOPE, CORP_INDUSTRY_JOBS_SCOPE

from app.cache import request_memo
from app.industry._router import router
from app.industry.char_cache import refresh_character_cache

from app.industry.blueprints.esi import ensure_char_blueprints_table, fetch_character_blueprints
from app.industry.blueprints.manual import (
    _formula_stock_buckets,
    _seen_personally,
    _stock_extra,
    declared_products,
)
# ── Formulas observed in real industry jobs ────────────────────────────────────────────────────
# The third evidence source, and the only one that works for the case both others miss: a builder
# who keeps their formulas in a CORP HANGAR and is not a Director can never be answered by
# `/corporations/{id}/assets/` or `/corporations/{id}/blueprints/`. But every industry job names the
# print it runs on — `blueprint_id` is the id of that SPECIFIC PHYSICAL item — and the two job
# endpoints they already grant are readable without Director:
#
#   GET /characters/{id}/industry/jobs/    esi-industry.read_character_jobs.v1, no corp role
#   GET /corporations/{id}/industry/jobs/  esi-industry.read_corporation_jobs.v1, Factory_Manager
#
# So N distinct blueprint_ids sharing one blueprint_type_id is MEASURED evidence of N physical
# formulas — wherever they live.
#
# **A FLOOR, never a cap.** A formula that has simply not been used is invisible here, so an
# observation may only ever RAISE the concurrency number. Reading it as a ceiling would serialise
# work the builder can really do, which is the exact failure "unknown never serialises" guards
# against. Concurrency only: a job says nothing about the print's ME, TE or remaining runs.

_REACTION_ACTIVITY_ID = 9      # same value as app.reactions.jobs.REACTION_ACTIVITY_ID; app/industry
                               # deliberately does not import app/reactions (see jobs.py's header)


@ensure_once
def ensure_formula_job_prints_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_formula_jobs (
                character_id INTEGER PRIMARY KEY,
                prints_json  TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


def fetch_formula_job_prints(character_id: int, access_token: str,
                             scopes: str = "") -> list[dict] | None:
    """Every reaction job this character has installed, INCLUDING FINISHED ONES, reduced to the
    print that ran it: `{blueprint_id, blueprint_type_id, blueprint_location_id}`.

    **Why this is a separate fetch and a separate table, and why `app/reactions/jobs.py` was left
    alone.** `fetch_industry_jobs` there feeds the SLOT CAPACITY math — `_character_capacities`,
    `running_counts`, every free-slot count on the Reactions tab and the Industry checklist — all of
    which COUNT ROWS in that cache. Adding `include_completed=true` to it would silently fold a
    year of finished jobs into "running" and destroy free-slot math everywhere at once. History is
    therefore fetched by its own path into its own table, and the rows here carry no `status`,
    `runs` or dates at all, so nothing that counts occupancy could consume them even by accident.

    Corp jobs are included when the character granted the corp-jobs scope: a job installed FOR
    CORPORATION never appears on the personal endpoint, and that is precisely the shape the
    corp-hangar builder's work has. Best-effort — a missing role, no corp or a network failure
    contributes nothing rather than failing the whole fetch. Returns None only if the PERSONAL call
    failed, so a bad fetch never wipes a good cache.
    """
    out: dict[int, dict] = {}

    def _absorb(jobs):
        for j in jobs:
            if j.get("activity_id") != _REACTION_ACTIVITY_ID:
                continue
            bid = j.get("blueprint_id")
            if not bid:
                continue                    # no physical print named — no evidence to record
            out[int(bid)] = {"blueprint_id": int(bid),
                             "blueprint_type_id": j.get("blueprint_type_id"),
                             "blueprint_location_id": j.get("blueprint_location_id")}

    try:
        with esi_http.client(timeout=15) as client:
            r = esi_http.get(f"characters/{character_id}/industry/jobs/", client=client,
                             token=access_token, params={"include_completed": "true"})
            r.raise_for_status()
            _absorb(r.json())
            if CORP_INDUSTRY_JOBS_SCOPE in (scopes or ""):
                try:
                    pub = esi_http.get(f"characters/{character_id}/", client=client).json()
                    corp_id = pub.get("corporation_id")
                    if corp_id:
                        cr = esi_http.get(f"corporations/{corp_id}/industry/jobs/", client=client,
                                          token=access_token,
                                          params={"include_completed": "true"})
                        cr.raise_for_status()
                        # Only THIS character's own installs: the response is the whole corp's queue,
                        # and a corpmate's formula is not one this account can run a job on.
                        _absorb([j for j in cr.json() if j.get("installer_id") == character_id])
                except Exception:
                    pass
    except Exception:
        return None
    return list(out.values())


def observed_formula_prints(context_id: int) -> dict[int, int]:
    """product_type_id -> how many DISTINCT physical formulas this account has been observed
    running jobs on. A total, not an extra — the same print can be seen by several sources, so the
    ids are unioned before they are counted.

    Two caches feed it: the job-history table above, and the Reactions tab's live job cache
    (`pp_char_industry_jobs`), which stores raw ESI objects and so has carried `blueprint_id` all
    along. Reading both means the floor works the moment Reactions has been refreshed, without
    waiting for a history fetch — and the union makes double counting impossible by construction.
    """
    ensure_formula_job_prints_table()
    con = get_connection()
    try:
        try:
            rx = {r["reaction_id"]: r["output_type_id"]
                  for r in con.execute("SELECT reaction_id, output_type_id FROM reactions")}
        except Exception:
            return {}
        if not rx:
            return {}
        chars = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=?", (context_id,))]
        if not chars:
            return {}
        holes = ",".join("?" * len(chars))
        blobs: list[str] = [r["prints_json"] for r in con.execute(
            f"SELECT prints_json FROM pp_char_formula_jobs WHERE character_id IN ({holes})", chars)]
        try:
            blobs += [r["jobs_json"] for r in con.execute(
                f"SELECT jobs_json FROM pp_char_industry_jobs WHERE character_id IN ({holes})",
                chars)]
        except Exception:
            pass            # the reactions cache table may not exist if Reactions was never used
    except Exception:
        return {}
    finally:
        con.close()

    ids_by_product: dict[int, set[int]] = {}
    for blob in blobs:
        try:
            items = _json.loads(blob or "[]")
        except Exception:
            continue
        for j in items:
            prod = rx.get(j.get("blueprint_type_id"))
            bid = j.get("blueprint_id")
            if not prod or not bid:
                continue
            ids_by_product.setdefault(prod, set()).add(int(bid))
    return {p: len(ids) for p, ids in ids_by_product.items() if ids}


def formula_print_floor(context_id: int, owned: dict[int, dict] | None = None) -> dict[int, int]:
    """product_type_id -> EXTRA concurrent reactions, over what `owned_blueprints()` counted, that
    the account's stock AND its observed jobs together prove. Drop-in for `stock_formula_prints`
    (identical contract: an extra, concurrency only, never ME/TE/runs) with observation folded in.

    Precedence per type, highest first:

      a0. **a HAND-DECLARED holding answers alone.** `owned_blueprints` has already put the declared
         prints in `owned` (its merge rule: a declaration replaces the ESI reading for its product),
         so anything added here would be a second count of formulas the user has just finished
         telling us about — the paste that names them, and the jobs they were installed on, describe
         the same physical items. Same reasoning as (a) one rung up: a declaration is the more
         explicit statement of the two, since a paste describes one container and a declaration
         describes the product.
      a. **a PASTE naming that formula wins outright.** A pasted inventory is the user stating what
         they have right now, so the observed floor is NOT added on top of it. This is a product
         decision, and it has a known edge: a paste covering only ONE container suppresses job
         evidence about formulas held elsewhere. It is the user's statement either way.
         **Confirmed and kept, 2026-08-05**, with that edge understood and accepted — a paste is
         treated as truth. Two softer rules were considered and declined: "paste wins but never
         below observed" (which would stop a paste ever saying "I sold three of these"), and a
         "this is everything I hold" checkbox on the paste form (a knob, and rule 3 says add one
         only where the math genuinely cannot decide). Do not re-litigate without new evidence —
         the failing case is pasting a DIFFERENT box than the one the formulas are in, so if that
         starts biting in practice, the checkbox is the first thing to reach for.
      b. otherwise the MAXIMUM of the asset-stock figure and the distinct observed blueprint_ids —
         a max, because both describe the same physical items from different angles, so adding them
         would count one formula twice.
      c. never below what the blueprint endpoint already reported (the return is an extra, so a
         negative is clamped to 0 and the caller's own count stands).
      d. no evidence at all → nothing here, and `_print_limits` leaves the type uncapped.
    """
    buckets = request_memo(("formula_buckets", context_id),
                           lambda: _formula_stock_buckets(context_id))
    observed = request_memo(("observed_formulas", context_id),
                            lambda: observed_formula_prints(context_id))
    declared = declared_products(owned)
    out: dict[int, int] = {}
    for prod in set(buckets) | set(observed):
        if prod in declared:
            continue                       # precedence a0 — the declaration is the whole answer
        b = buckets.get(prod) or {"personal": 0, "corp": 0, "paste": 0}
        seen = _seen_personally(owned, prod)
        extra = _stock_extra(b, seen)
        if not b["paste"]:
            extra = max(extra, observed.get(prod, 0) - seen)
        if extra > 0:
            out[prod] = extra
    return out


def blueprint_coverage(context_id: int) -> dict:
    """{characters, cached, missing, complete} — how much of this account's blueprint holding we
    can see.

    `owned_blueprints` unions the characters that HAVE a cached list, and that is routinely a
    subset: blueprint scope is opt-in per character, and a character without it can never have a
    cache at all. So the union is a floor on what the account holds, and only `complete` licenses
    anything to read it as a total (see BuildParams.prints_known). Reported to the user as well as
    consumed by the planner — silently not capping is its own kind of lie once you know the feature
    is there.
    """
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        chars = con.execute("SELECT COUNT(*) AS n FROM pp_characters WHERE context_id=?",
                            (context_id,)).fetchone()["n"] or 0
        cached = con.execute(
            "SELECT COUNT(*) AS n FROM pp_char_blueprints b "
            "JOIN pp_characters c ON b.character_id = c.character_id WHERE c.context_id=?",
            (context_id,)).fetchone()["n"] or 0
    finally:
        con.close()
    return {"characters": chars, "cached": cached, "missing": max(0, chars - cached),
            "complete": chars > 0 and cached >= chars}


@router.post("/api/industry/blueprints/refresh")
def refresh_blueprints(context_id: int = Depends(require_context)):
    """Re-read owned blueprints from ESI for the caller's characters that granted the blueprint
    scope. Best-effort per character — one failure never blocks the others."""
    ensure_char_blueprints_table()
    out = refresh_character_cache(
        context_id, scope=BLUEPRINTS_SCOPE, table="pp_char_blueprints",
        column="blueprints_json", fetch=fetch_character_blueprints)
    # The account snapshot every plan is built from holds this reading — see graph._account_snapshot.
    # Dropping it here is what makes a refresh visible on the next plan instead of up to a TTL later.
    from app.industry.graph import clear_account_snapshot
    clear_account_snapshot(context_id)
    return out
