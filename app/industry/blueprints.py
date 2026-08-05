"""Industry planner — owned-blueprint auto-detection from ESI.

Reads the player's real blueprints (`GET /characters/{id}/blueprints/`, opt-in
`esi-characters.read_blueprints.v1` scope) so the planner uses their actual ME/TE and knows which
BPOs/BPCs they hold — zero manual entry. This is the automatic version of the blueprint library:
owned BPO → build at its researched ME/TE with no BPC cost; not owned → the planner can flag it.

Cache-at-fetch like app/reactions' industry-jobs: store the raw filtered list per character with a
fetched_at, refreshed on demand (a "Refresh blueprints" button), not polled. `owned_blueprints()`
collapses the account's characters into one product→best-blueprint map the cost resolver consumes.
"""
import json as _json
import logging
from fastapi import Depends

from app.sde import get_connection, ensure_once
from app import esi_http
from app.esi import require_context, BLUEPRINTS_SCOPE, CORP_INDUSTRY_JOBS_SCOPE

from app.industry._router import router
from app.industry.char_cache import refresh_character_cache

log = logging.getLogger(__name__)


@ensure_once
def ensure_char_blueprints_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_blueprints (
                character_id    INTEGER PRIMARY KEY,
                blueprints_json TEXT NOT NULL DEFAULT '[]',
                fetched_at      REAL
            )
        """)
        con.commit()
    finally:
        con.close()


def fetch_character_blueprints(character_id: int, access_token: str) -> list[dict] | None:
    """This character's blueprints, paginated. Each: {type_id (the BLUEPRINT type), me, te,
    quantity (-1 = BPO original, -2 = BPC copy, >0 = stacked BPOs), runs (-1 for BPO)}. Returns
    None on any failure so a bad fetch never wipes a good cache; [] means genuinely none."""
    out: list[dict] = []
    try:
        with esi_http.client(timeout=15) as client:
            page = 1
            while True:
                r = esi_http.get(f"characters/{character_id}/blueprints/", client=client,
                                 token=access_token, params={"page": page})
                r.raise_for_status()
                data = r.json()
                if not data:
                    break
                for b in data:
                    out.append({
                        "type_id": b.get("type_id"),
                        "me": b.get("material_efficiency", 0) or 0,
                        "te": b.get("time_efficiency", 0) or 0,
                        "quantity": b.get("quantity", 0),
                        "runs": b.get("runs", -1),
                    })
                pages = int(r.headers.get("X-Pages", "1") or 1)
                if page >= pages:
                    break
                page += 1
    except Exception:
        return None
    return out


_STACK_CAP = 200          # how far a stack of identical prints is expanded into separate items


def _copy_rank(c: dict) -> tuple:
    """Consumption order for one product's copies: BEST RESEARCHED FIRST, an original winning ties.

    Not "BPO before everything", which is what this used to be. A job runs off one copy and takes
    that copy's ME/TE, so a ME10 copy beside an un-researched original should run first and the
    original should carry whatever is left — which is also why the original sorts last among equals
    only: it is the one that never runs out, so it is the right thing to fall back to.
    """
    return (-(c["me"] or 0), -(c["te"] or 0), 0 if c["kind"] == "bpo" else 1)


def classify_blueprint(quantity, runs) -> str:
    """'bpo' | 'bpc' for one ESI blueprint row.

    **`runs == -1` is the unambiguous marker** — a real copy always carries a positive run count.
    Quantity alone is not: ESI uses -1 for a singleton and -2 for a copy, but a POSITIVE quantity
    is a stack of ORIGINALS fresh from the market, and reading `quantity == -1` as the only original
    filed all of those as copies carrying -1 runs, i.e. as covering nothing at all. There were 26
    such blueprints in production, each one telling its owner to go and buy a print they hold.
    """
    try:
        r = int(runs)
    except (TypeError, ValueError):
        return "bpo"
    if r < 0:
        return "bpo"
    return "bpc" if quantity == -2 else "bpo"


def owned_blueprints(context_id: int) -> dict[int, dict]:
    """product_type_id -> what the account owns for that product, across all its characters:
    `{me, te, kind, runs, copies, copy_count}`.

    **Every copy counts.** This used to collapse a product's blueprints down to the single best one
    and throw the rest away, so an account holding 21 Nitrogen Fuel Block copies worth 3,975 runs
    was credited with 175 and told to buy the rest. `copies` is the whole holding, ordered the way
    the plan will consume it (`_copy_rank`); `runs` is the TOTAL coverage (-1 = an original, which
    covers any batch); `me`/`te` describe the copy the first job will run off, and the per-job
    values come off `copies` (see BuildParams.me_te_for). Empty if nothing's connected/cached.
    """
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT b.blueprints_json FROM pp_char_blueprints b "
            "JOIN pp_characters c ON b.character_id = c.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
        bp2prod = {r["blueprint_type_id"]: r["product_type_id"]
                   for r in con.execute("SELECT blueprint_type_id, product_type_id FROM blueprints")}
        # ...and REACTION FORMULAS, which this map used to drop on the floor. `blueprints` is filled
        # from the SDE's manufacturing activity only, so not one of the 112 `reaction_id`s appears in
        # it and every formula ESI returned was discarded at this join — 50 distinct formulas sitting
        # in the cache in production, unused. A `reaction_id` IS the formula item's own type_id (they
        # are the "… Reaction Formula" types), so the mapping needs no new data, fetch or scope.
        try:
            for r in con.execute("SELECT reaction_id, output_type_id FROM reactions"):
                bp2prod.setdefault(r["reaction_id"], r["output_type_id"])
        except Exception:
            pass          # an SDE without the reactions table is a manufacturing-only answer
    finally:
        con.close()

    by_product: dict[int, list[dict]] = {}
    for row in rows:
        try:
            items = _json.loads(row["blueprints_json"])
        except Exception:
            continue
        for b in items:
            prod = bp2prod.get(b.get("type_id"))
            if not prod:
                continue
            runs = b.get("runs", -1)
            kind = classify_blueprint(b.get("quantity"), runs)
            entry = {"me": b.get("me", 0) or 0, "te": b.get("te", 0) or 0, "kind": kind,
                     "runs": -1 if kind == "bpo" else max(0, int(runs or 0))}
            # A positive quantity is a STACK, and every print in it is a separate item that can hold
            # a separate job — twenty Synth Mindflood formulas are twenty reactors' worth, not one.
            # Copies never stack (quantity -2), so this only ever expands originals. Bounded, because
            # the count only decides how many jobs may run at once and nobody runs 200 at a time.
            try:
                n = int(b.get("quantity") or 1)
            except (TypeError, ValueError):
                n = 1
            for _ in range(max(1, min(n, _STACK_CAP))):
                by_product.setdefault(prod, []).append(dict(entry))

    owned: dict[int, dict] = {}
    for prod, copies in by_product.items():
        copies.sort(key=_copy_rank)
        has_bpo = any(c["kind"] == "bpo" for c in copies)
        best = copies[0]
        owned[prod] = {
            "me": best["me"], "te": best["te"],
            # Coverage semantics, deliberately not the top copy's kind: an original anywhere in the
            # holding means every run is covered, whatever the best-researched copy happens to be.
            "kind": "bpo" if has_bpo else "bpc",
            "runs": -1 if has_bpo else sum(c["runs"] for c in copies),
            "copies": copies, "copy_count": len(copies),
        }
    return owned


def _formula_stock_buckets(context_id: int) -> dict[int, dict[str, int]]:
    """product_type_id -> {personal, corp, paste}: formula counts in ENABLED stock, bucketed by
    where the evidence came from. Split out of `stock_formula_prints` so the job-observation floor
    (`formula_print_floor`) can apply its own precedence to the same buckets rather than re-deriving
    them — the paste bucket in particular has to be recognisable, since a paste overrides.
    """
    from app.industry.assets import ensure_asset_tables

    ensure_asset_tables()
    con = get_connection()
    try:
        try:
            rx = {r["reaction_id"]: r["output_type_id"]
                  for r in con.execute("SELECT reaction_id, output_type_id FROM reactions")}
        except Exception:
            return {}                       # a manufacturing-only SDE knows no formulas at all
        if not rx:
            return {}
        rows = con.execute(
            "SELECT COALESCE(src.scope,'') AS scope, src.key AS key, s.type_id AS type_id, "
            "SUM(s.qty) AS q FROM pp_asset_stock s "
            "JOIN pp_asset_sources src ON src.context_id = s.context_id AND src.key = s.key "
            "WHERE s.context_id = ? AND src.enabled = 1 "
            "GROUP BY COALESCE(src.scope,''), src.key, s.type_id",
            (context_id,),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()

    buckets: dict[int, dict[str, int]] = {}
    for r in rows:
        prod = rx.get(int(r["type_id"]))
        if not prod:
            continue                        # not a reaction formula — see stock_formula_prints
        scope, key = str(r["scope"] or ""), str(r["key"] or "")
        if scope.startswith("char:") or key.startswith("char:") or key.startswith("cont:"):
            bucket = "personal"
        elif scope.startswith("corp:") or key.startswith("corp:"):
            bucket = "corp"
        else:
            bucket = "paste"
        b = buckets.setdefault(prod, {"personal": 0, "corp": 0, "paste": 0})
        b[bucket] += max(0, int(float(r["q"] or 0)))
    return buckets


def _seen_personally(owned: dict[int, dict] | None, prod: int) -> int:
    """How many prints of `prod` the personal blueprint endpoint already reported — the figure
    `_print_limits` starts from, so every "extra" in this module means extra *over this*."""
    own = (owned or {}).get(prod) or {}
    held = own.get("copies")
    return len(held) if held else (1 if own else 0)


def stock_formula_prints(context_id: int, owned: dict[int, dict] | None = None) -> dict[int, int]:
    """product_type_id -> how many EXTRA concurrent reactions the account's enabled stock proves,
    on top of whatever `owned_blueprints()` already counted.

    Why this exists: `pp_char_blueprints` is filled from `GET /characters/{id}/blueprints/`, which
    returns PERSONAL blueprints only. A builder who keeps their formulas in a corp hangar container
    has none of them there, so the print cap never fires and the plan schedules N parallel reactions
    off one formula they own a single copy of. The formulas ARE visible in `pp_asset_stock` — from a
    corp asset scan, or from a pasted hangar (the path every non-Director has).

    **Concurrency only, and only for FORMULAS.** An asset row carries a type_id and a quantity and
    nothing else — no ME, no TE, no remaining runs. For a reaction that is the whole truth anyway: a
    formula has no ME/TE (rig-based) and cannot be copied, so the only thing owning one more of them
    changes is how many jobs may run at once. A manufacturing BLUEPRINT in stock is deliberately NOT
    counted here: its cap is entangled with run coverage and its ME/TE decides what the build costs,
    so an asset row would have to invent both — and a plan that quietly credits an unknown-ME print
    is worse than one that admits it can't see the print at all. Personal blueprints are already read
    properly by ESI; the corp-hangar hole is a formula problem in practice.

    **No double counting.** The two sources genuinely overlap: a personal asset scan and the
    blueprint endpoint see the SAME items, so a formula in a character's own container is in both.
    So the counts are bucketed by where the evidence came from and only then summed:

      * personal (`char:*`-scoped sources) — the same population `pp_char_blueprints` describes, so
        the two are reconciled with `max`, never added.
      * corp scans and pastes — invisible to the personal scans, so they add to the personal figure.
        Between themselves they are also reconciled with `max`: a paste exists because the corp
        endpoint needs Director, so a paste and a corp scan are most likely the same hangar seen
        twice, and adding them would double a box the builder owns one of.

    Enabled sources only, like every other read of stock (see the module docstring in assets.py) —
    a formula in a container the user hasn't ticked is not one they've said they will spend.
    """
    out: dict[int, int] = {}
    for prod, b in _formula_stock_buckets(context_id).items():
        extra = _stock_extra(b, _seen_personally(owned, prod))
        if extra > 0:
            out[prod] = extra
    return out


def _stock_extra(b: dict[str, int], seen_personally: int) -> int:
    """The EXTRA prints one product's stock buckets prove over the personal blueprint list."""
    return max(0, b["personal"] - seen_personally) + max(b["corp"], b["paste"])


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

      a. **a PASTE naming that formula wins outright.** A pasted inventory is the user stating what
         they have right now, so the observed floor is NOT added on top of it. This is a product
         decision, and it has a known edge: a paste covering only ONE container suppresses job
         evidence about formulas held elsewhere. It is the user's statement either way.
      b. otherwise the MAXIMUM of the asset-stock figure and the distinct observed blueprint_ids —
         a max, because both describe the same physical items from different angles, so adding them
         would count one formula twice.
      c. never below what the blueprint endpoint already reported (the return is an extra, so a
         negative is clamped to 0 and the caller's own count stands).
      d. no evidence at all → nothing here, and `_print_limits` leaves the type uncapped.
    """
    buckets = _formula_stock_buckets(context_id)
    observed = observed_formula_prints(context_id)
    out: dict[int, int] = {}
    for prod in set(buckets) | set(observed):
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
    return refresh_character_cache(
        context_id, scope=BLUEPRINTS_SCOPE, table="pp_char_blueprints",
        column="blueprints_json", fetch=fetch_character_blueprints)


@router.get("/api/industry/blueprints")
def industry_blueprints(context_id: int = Depends(require_context)):
    """Connection state + how many distinct products the account owns a blueprint for — drives the
    'Connect blueprints' vs 'N blueprints detected' UI."""
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        connected = con.execute(
            "SELECT COUNT(*) AS n FROM pp_characters WHERE context_id=? AND scopes LIKE ?",
            (context_id, f"%{BLUEPRINTS_SCOPE}%"),
        ).fetchone()["n"] > 0
    finally:
        con.close()
    owned = owned_blueprints(context_id)
    return {"connected": connected, "owned_count": len(owned)}
