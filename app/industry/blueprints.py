"""Industry planner — owned-blueprint auto-detection from ESI.

Reads the player's real blueprints (`GET /characters/{id}/blueprints/`, opt-in
`esi-characters.read_blueprints.v1` scope) so the planner uses their actual ME/TE and knows which
BPOs/BPCs they hold — zero manual entry. This is the automatic version of the blueprint library:
owned BPO → build at its researched ME/TE with no BPC cost; not owned → the planner can flag it.

Cache-at-fetch like app/reactions' industry-jobs: store the raw filtered list per character with a
fetched_at, refreshed on demand (a "Refresh blueprints" button), not polled. `owned_blueprints()`
collapses the account's characters into one product→best-blueprint map the cost resolver consumes.
"""
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


def _blueprint_product_index(con) -> dict[int, int]:
    """blueprint_type_id -> product_type_id, manufacturing AND reaction formulas.

    Split out of `owned_blueprints` so the hand-declaration write path resolves a typed-in blueprint
    to the same product the ESI reader would have — two indexes that disagree would file a declared
    print under a product no plan ever asks about.
    """
    idx = {r["blueprint_type_id"]: r["product_type_id"]
           for r in con.execute("SELECT blueprint_type_id, product_type_id FROM blueprints")}
    # ...and REACTION FORMULAS, which this map used to drop on the floor. `blueprints` is filled
    # from the SDE's manufacturing activity only, so not one of the 112 `reaction_id`s appears in
    # it and every formula ESI returned was discarded at this join — 50 distinct formulas sitting
    # in the cache in production, unused. A `reaction_id` IS the formula item's own type_id (they
    # are the "… Reaction Formula" types), so the mapping needs no new data, fetch or scope.
    try:
        for r in con.execute("SELECT reaction_id, output_type_id FROM reactions"):
            idx.setdefault(r["reaction_id"], r["output_type_id"])
    except Exception:
        pass              # an SDE without the reactions table is a manufacturing-only answer
    return idx


# ── Blueprints and formulas DECLARED BY HAND ──────────────────────────────────────────────────
# `GET /characters/{id}/blueprints/` is PERSONAL-ONLY, and there is no endpoint that answers "what
# is in this corp hangar" without the Director role. A builder whose prints live in a corp hangar
# can therefore state their ME/TE to us in no way at all, and every such build is planned at ME 0 /
# TE 0 — the un-researched worst case — so its materials and its duration are both wrong. Pasting a
# hangar as STOCK was the answer for FORMULAS (an asset row is a whole truth for a formula, which
# has no ME/TE and cannot be copied) and deliberately credits nothing for a manufacturing blueprint,
# because an asset row states no ME, no TE and no runs. This table is where the user states them.
#
# The row encoding is the one this module already uses internally: **runs NULL/absent = a BPO**
# (`owned_blueprints` returns `runs = -1` for an original), anything else a BPC with that many runs.
# `quantity` expands the row into that many separate physical prints, exactly like an ESI stack.

MANUAL_FEATURE_KEY = "industry_manual_blueprints"


@ensure_once
def ensure_manual_blueprints_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_industry_blueprints (
                context_id INTEGER NOT NULL,
                id         INTEGER NOT NULL,
                type_id    INTEGER NOT NULL,
                me         INTEGER NOT NULL DEFAULT 0,
                te         INTEGER NOT NULL DEFAULT 0,
                runs       INTEGER NOT NULL DEFAULT -1,
                quantity   INTEGER NOT NULL DEFAULT 1,
                prefer     TEXT    NOT NULL DEFAULT '',
                updated_at REAL,
                PRIMARY KEY (context_id, id)
            )
        """)
        # Which PASTE a row came from — see `replace_blueprint_batch`. Empty = typed in one at a
        # time on the form, which is why it is also the value every pre-existing row migrates to:
        # a paste may never delete a row a person entered by hand.
        add_columns(con, "pp_industry_blueprints",
                    "batch TEXT DEFAULT ''", "batch_name TEXT DEFAULT ''")
        con.commit()
    finally:
        con.close()


def _manual_enabled(context_id: int) -> bool:
    """THE gate for hand-declared prints. One place, asked by every reader, so with the flag off
    `owned_blueprints` is byte-for-byte the function that shipped before this existed."""
    try:
        from app.features import feature_enabled_for
        return bool(feature_enabled_for(MANUAL_FEATURE_KEY, context_id))
    except Exception:
        return False


def manual_blueprints(context_id: int, force: bool = False) -> dict[int, dict]:
    """product_type_id -> `{copies, prefer}` for the prints this account has DECLARED BY HAND.

    `copies` is in the same shape `owned_blueprints` builds from ESI (`{me, te, kind, runs}`, one
    entry per physical print, a `quantity` row expanded), so the two merge without a second
    vocabulary. `prefer` is the product-level BPO-vs-BPC choice (see `_apply_kind_preference`).

    Empty unless the feature is on — `force=True` is for the admin/test path that wants the rows
    whatever the flag says.
    """
    if not force and not _manual_enabled(context_id):
        return {}
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT type_id, me, te, runs, quantity, prefer FROM pp_industry_blueprints "
            "WHERE context_id=? ORDER BY id", (context_id,)).fetchall()
    except Exception:
        return {}
    finally:
        con.close()

    out: dict[int, dict] = {}
    for r in rows:
        prod = int(r["type_id"])
        slot = out.setdefault(prod, {"copies": [], "prefer": ""})
        try:
            runs = int(r["runs"])
        except (TypeError, ValueError):
            runs = -1
        kind = "bpo" if runs < 0 else "bpc"
        entry = {"me": int(r["me"] or 0), "te": int(r["te"] or 0), "kind": kind,
                 "runs": -1 if kind == "bpo" else max(0, runs)}
        try:
            n = int(r["quantity"] or 0)
        except (TypeError, ValueError):
            n = 0
        # A row declaring ZERO prints is not a print — it is the product-level preference on its
        # own, which is how an account whose prints ESI CAN see says "use the original, not the
        # copies" without having to re-type a holding we already read correctly.
        for _ in range(max(0, min(n, _STACK_CAP))):
            slot["copies"].append(dict(entry))
        pref = str(r["prefer"] or "").strip().lower()
        if pref in ("bpo", "bpc") and not slot["prefer"]:
            slot["prefer"] = pref          # first row naming a preference decides; see the endpoint
    return out


def _apply_kind_preference(copies: list[dict], prefer: str) -> list[dict]:
    """The account's stated BPO-vs-BPC choice for one product, applied to its whole holding.

    They are not interchangeable and the difference shows up in two numbers at once: an ORIGINAL
    consumes nothing and covers any batch, so it costs no copies but is ONE print and therefore one
    job at a time; a stack of COPIES has finite runs and is spent, so it costs ISK once the runs run
    out but N of them run N jobs side by side. The plan cannot pick between "cheaper" and "sooner"
    for the builder, so the builder says.

    Only ever narrows, and only when there is a real choice to make — a product holding just one
    kind is returned untouched, so a preference can never empty a holding.
    """
    if prefer not in ("bpo", "bpc"):
        return copies
    if len({c["kind"] for c in copies}) < 2:
        return copies
    return [c for c in copies if c["kind"] == prefer]


def owned_blueprints(context_id: int) -> dict[int, dict]:
    """product_type_id -> what the account owns for that product, across all its characters:
    `{me, te, kind, runs, copies, copy_count}`.

    **Every copy counts.** This used to collapse a product's blueprints down to the single best one
    and throw the rest away, so an account holding 21 Nitrogen Fuel Block copies worth 3,975 runs
    was credited with 175 and told to buy the rest. `copies` is the whole holding, ordered the way
    the plan will consume it (`_copy_rank`); `runs` is the TOTAL coverage (-1 = an original, which
    covers any batch); `me`/`te` describe the copy the first job will run off, and the per-job
    values come off `copies` (see BuildParams.me_te_for). Empty if nothing's connected/cached.

    **Two sources, and the merge rule between them is REPLACEMENT, per product.** Beside the ESI
    cache sits `pp_industry_blueprints` — prints the user declared by hand, which is the only way an
    account can state the ME/TE of a print ESI cannot see (a corp hangar has no readable blueprint
    endpoint without the Director role). For a product the user has declared at least one print for,
    the declaration IS the holding and the ESI reading for that product is dropped. Products they
    did not declare are untouched, so this is never account-wide.

    Why replacement and not addition — the choice matters, because getting it wrong is silent:

      * **The two sources cannot be reconciled item by item.** An ESI row's identity is its
        `item_id`, which the game client never shows and the user therefore cannot type. There is no
        key to match a declaration against a scanned row, so ADDING would double-count every print
        that is declared *and* scanned, unboundedly and invisibly — hand-enter the 21 fuel-block
        copies you already hold and the plan credits you with 42.
      * **Replacement's failure is bounded and visible.** What you declared is what the plan uses
        for that product; leave a print out and you can see it on the plan and add a row. An
        over-count cannot be seen at all — it just quietly plans work that cannot be installed.
      * **It is the rule this module already applies to hand-entered evidence.** A pasted hangar
        wins outright for the formulas it names (`formula_print_floor`, precedence a), for exactly
        the same reason: a user statement is treated as the statement it is, rather than blended
        with a reading it may or may not overlap.

    A declared product carries `source: "manual"` on its entry, and that mark is load-bearing twice
    over: `prepare_plan_inputs` reports it as `me_source = "declared"` (the user's word, which is a
    different kind of evidence from a measurement and must not be reported as one), and
    `formula_print_floor` reads it to keep the stock/observed evidence layer from adding a second
    count of the same physical formula on top of the declared one.
    """
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT b.blueprints_json FROM pp_char_blueprints b "
            "JOIN pp_characters c ON b.character_id = c.character_id WHERE c.context_id=?",
            (context_id,),
        ).fetchall()
        bp2prod = _blueprint_product_index(con)
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

    # The hand-declared layer. Replacement per product, per the merge rule above — a declared
    # product's ESI copies are discarded rather than added to, and one that declares only a
    # preference (quantity 0) keeps its ESI copies and just states which kind to spend.
    manual = manual_blueprints(context_id)
    declared: set[int] = set()
    for prod, m in manual.items():
        if m["copies"]:
            by_product[prod] = [dict(c) for c in m["copies"]]
            declared.add(prod)

    owned: dict[int, dict] = {}
    for prod, copies in by_product.items():
        prefer = (manual.get(prod) or {}).get("prefer") or ""
        if prefer:
            copies = _apply_kind_preference(copies, prefer)
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
        # Additive keys, and only where they mean something: an account with no declarations gets
        # exactly the dict this function has always returned, key for key.
        if prod in declared:
            owned[prod]["source"] = "manual"
        if prefer:
            owned[prod]["prefer"] = prefer
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


def _declared_products(owned: dict[int, dict] | None) -> set[int]:
    """Products whose holding the user stated BY HAND (`owned_blueprints`' merge rule).

    The evidence layers below must not add anything for these. A hand-declared formula, a formula
    in a pasted hangar and a formula seen in an observed job are three descriptions of one physical
    item at least as often as they are three items, and the declaration is the only one of the three
    that is a statement of totality — so it answers alone.
    """
    return {p for p, o in (owned or {}).items() if (o or {}).get("source") == "manual"}


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
    declared = _declared_products(owned)
    for prod, b in _formula_stock_buckets(context_id).items():
        if prod in declared:
            continue                        # the user stated this holding — see _declared_products
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
    buckets = _formula_stock_buckets(context_id)
    observed = observed_formula_prints(context_id)
    declared = _declared_products(owned)
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
    return refresh_character_cache(
        context_id, scope=BLUEPRINTS_SCOPE, table="pp_char_blueprints",
        column="blueprints_json", fetch=fetch_character_blueprints)


# ── The industry window, pasted ───────────────────────────────────────────────────────────────
# Declaring prints one at a time is unusable at the scale a real builder has: ~100 reaction formulas
# on one character and more on the others. EVE's own industry window copies to the clipboard as
# exactly the data this table stores, so the whole library arrives in one paste:
#
#     [N x ]<name> TAB <ME> TAB <TE> TAB <runs> TAB <category>
#
# Nothing about the format needs translating — `runs = -1` is already this module's encoding for an
# original, and the ME/TE columns are the numbers a corp-hangar print could otherwise not state.
#
# **Each paste is a NAMED BATCH, exactly like a pasted stock source** (`add_pasted_source` in
# assets.py), and for the same reason: the user pastes ONCE PER CHARACTER. A paste that replaced the
# whole library would wipe the previous character's prints; one that appended would double the
# holding the moment the same window is pasted again after buying a print. Replacing only its own
# batch is the rule that survives both.

_STACK_RE = _re.compile(r"^(\d[\d,]*)\s*[x×]\s+(.+)$", _re.IGNORECASE)

_PASTE_BATCH_DEFAULT = "Industry window"


def _batch_key(label: str) -> str:
    """A stable id for a batch NAME, so re-pasting the same window replaces it.

    Deliberately a digest and not `hash()`: Python randomises string hashing per process, so a
    key built that way silently stops matching after a pod restart — which for this feature would
    mean a second copy of every print rather than a replacement.
    """
    norm = (label or "").strip().lower() or _PASTE_BATCH_DEFAULT.lower()
    return "paste:" + _hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _parse_paste_line(line: str) -> dict | None:
    """One industry-window row → `{name, me, te, runs, quantity}`, or None if it is not one.

    None covers the section headers (`Formulas:`, `Blueprints:`) and anything else pasted along with
    the window: they are counted and reported, never guessed at. A real row is tab-separated and its
    second column is the ME number, which is the cheapest test that a header can never pass.
    """
    parts = [p.strip() for p in line.split("\t")]
    if len(parts) < 2 or not parts[0]:
        return None

    def _num(idx: int, dflt: int) -> int | None:
        if idx >= len(parts) or parts[idx] == "":
            return dflt
        try:
            return int(float(parts[idx].replace(",", "").replace(" ", "")))
        except ValueError:
            return None

    me = _num(1, None)
    if me is None:
        return None                      # column 2 is not a number — not an industry-window row
    te, runs = _num(2, 0), _num(3, -1)
    if te is None or runs is None:
        return None
    name, qty = parts[0], 1
    m = _STACK_RE.match(name)
    if m:
        # "4 x Nanotransistors Reaction Formula" — a STACK of four separate physical prints. Names
        # that merely start with a digit ("1MN Afterburner I Blueprint") do not match: the x and the
        # space after it are required.
        try:
            qty = max(1, int(m.group(1).replace(",", "")))
        except ValueError:
            qty = 1
        name = m.group(2).strip()
    return {"name": name, "me": me, "te": te, "runs": runs, "quantity": qty}


def parse_blueprint_paste(text: str) -> dict:
    """A copied EVE industry window → the rows `pp_industry_blueprints` stores, plus what was not
    understood. Pure: reads the SDE, writes nothing, so the preview and the import see one answer.

    Grouping: **a repeated line is a separate physical print.** The window lists one line per item,
    so four identical `Photonic Metamaterials Reaction Formula` lines are four formulas, and the
    stack prefix multiplies each. Quantities are summed per (product, ME, TE, runs) — prints that
    differ in research are genuinely different prints and stay separate rows.

    Names resolve through the SDE `types` table and then `_blueprint_product_index()`, the same index
    the ESI reader uses, so a declared print files under the product a plan actually asks about.
    Anything unresolved is REPORTED: a paste that matched nothing is almost always the wrong window,
    and dropping it silently would leave the user staring at an unchanged list.
    """
    parsed: list[dict] = []
    ignored: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        row = _parse_paste_line(line)
        if row is None:
            ignored.append(line.strip())
            continue
        parsed.append(row)

    empty = {"entries": [], "unknown": [], "no_product": [], "ignored": ignored,
             "prints": 0, "formulas": 0, "blueprints": 0, "products": 0, "lines": len(parsed)}
    if not parsed:
        return empty

    con = get_connection()
    try:
        lookup: dict[str, int] = {}
        names = sorted({r["name"] for r in parsed})
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                f"SELECT type_id, name FROM types WHERE LOWER(name) IN ({marks})",
                tuple(n.lower() for n in chunk),
            ).fetchall():
                lookup[r["name"].lower()] = int(r["type_id"])
        bp2prod = _blueprint_product_index(con)
        try:
            formula_ids = {int(r["reaction_id"])
                           for r in con.execute("SELECT reaction_id FROM reactions")}
        except Exception:
            formula_ids = set()          # a manufacturing-only SDE knows no formulas; cosmetic only
    finally:
        con.close()

    groups: dict[tuple, dict] = {}
    unknown: list[str] = []
    no_product: list[str] = []
    for r in parsed:
        tid = lookup.get(r["name"].lower())
        if tid is None:
            if r["name"] not in unknown:
                unknown.append(r["name"])
            continue
        prod = bp2prod.get(tid)
        if prod is None:
            # A real item that this SDE cannot turn into a product — a blueprint for something we
            # do not know how to build. Filing it under itself would invent a product no plan asks
            # for, so it is reported instead.
            if r["name"] not in no_product:
                no_product.append(r["name"])
            continue
        me = max(0, min(10, int(r["me"])))
        te = max(0, min(20, int(r["te"])))
        runs = -1 if int(r["runs"]) < 0 else int(r["runs"])
        key = (int(prod), me, te, runs)
        g = groups.setdefault(key, {
            "product_type_id": int(prod), "name": r["name"], "me": me, "te": te, "runs": runs,
            "quantity": 0, "kind": "bpo" if runs < 0 else "bpc",
            "formula": tid in formula_ids,
        })
        g["quantity"] += int(r["quantity"])

    entries = list(groups.values())
    for e in entries:
        e["quantity"] = min(e["quantity"], _STACK_CAP)
    return {"entries": entries, "unknown": unknown, "no_product": no_product, "ignored": ignored,
            "prints": sum(e["quantity"] for e in entries),
            "formulas": sum(e["quantity"] for e in entries if e["formula"]),
            "blueprints": sum(e["quantity"] for e in entries if not e["formula"]),
            "products": len({e["product_type_id"] for e in entries}),
            "lines": len(parsed)}


def replace_blueprint_batch(context_id: int, name: str, text: str) -> dict:
    """Import one pasted industry window as a named batch, REPLACING that batch and nothing else.

    Re-pasting the same window after buying a print updates it; pasting a second character's window
    under its own name adds to the library beside the first. Rows typed in on the form carry an
    empty batch and are never touched by any paste.
    """
    ensure_manual_blueprints_table()
    res = parse_blueprint_paste(text)
    label = (name or "").strip() or _PASTE_BATCH_DEFAULT
    key = _batch_key(label)
    if not res["entries"]:
        res.update({"added": 0, "batch": key, "name": label,
                    "error": "unrecognized" if (res["unknown"] or res["no_product"]) else "empty"})
        return res

    con = get_connection()
    try:
        # The BPO-vs-BPC choice is a property of the PRODUCT (see `edit_manual_blueprint`), so a
        # paste must carry forward one the user already made rather than blanking it.
        prefer = {int(r["type_id"]): str(r["prefer"] or "") for r in con.execute(
            "SELECT type_id, prefer FROM pp_industry_blueprints WHERE context_id=? AND prefer<>''",
            (context_id,)).fetchall()}
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, key))
        nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_industry_blueprints "
                              "WHERE context_id=?", (context_id,)).fetchone()["n"])
        now = _time.time()
        for e in res["entries"]:
            con.execute(
                "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                "quantity, prefer, updated_at, batch, batch_name) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (context_id, nxt, e["product_type_id"], e["me"], e["te"], e["runs"],
                 e["quantity"], prefer.get(e["product_type_id"], ""), now, key, label))
            nxt += 1
        con.commit()
    finally:
        con.close()
    res.update({"added": res["prints"], "batch": key, "name": label})
    return res


def list_blueprint_batches(context_id: int) -> list[dict]:
    """The pasted batches this account holds — one per character's window, in practice."""
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT COALESCE(batch,'') AS batch, COALESCE(batch_name,'') AS batch_name, "
            "COUNT(*) AS rows_n, SUM(quantity) AS prints, COUNT(DISTINCT type_id) AS products "
            "FROM pp_industry_blueprints WHERE context_id=? AND COALESCE(batch,'')<>'' "
            "GROUP BY COALESCE(batch,''), COALESCE(batch_name,'') ORDER BY batch_name",
            (context_id,)).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    return [{"batch": r["batch"], "name": r["batch_name"] or _PASTE_BATCH_DEFAULT,
             "rows": int(r["rows_n"] or 0), "prints": int(r["prints"] or 0),
             "products": int(r["products"] or 0)} for r in rows]


def delete_blueprint_batch(context_id: int, batch: str) -> None:
    """Drop one pasted batch. Every other batch, and everything typed in by hand, stays."""
    ensure_manual_blueprints_table()
    if not (batch or "").strip():
        return                            # '' is the hand-typed rows — never deletable in bulk
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, batch))
        con.commit()
    finally:
        con.close()


class ManualBlueprintEdit(BaseModel):
    """One hand-declared print. `runs` absent/None = a BPO — the encoding `owned_blueprints` already
    uses internally (`runs = -1` for an original), so there is no second convention to learn.

    `type_id` may be the BLUEPRINT's type or the PRODUCT's; it is resolved to the product on write,
    since that is the only key any planner ever looks a holding up by.
    """
    id: int | None = None
    type_id: int
    me: float = 0
    te: float = 0
    runs: int | None = None
    quantity: int = 1
    prefer: str = ""


def _manual_payload(context_id: int) -> dict:
    """Every declared row plus the product name, so the settings list reads as items rather than
    ids. `enabled` says whether the planner is actually consuming them — a list the plan ignores
    must not look like one it obeys."""
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT id, type_id, me, te, runs, quantity, prefer, COALESCE(batch,'') AS batch, "
            "COALESCE(batch_name,'') AS batch_name FROM pp_industry_blueprints "
            "WHERE context_id=? ORDER BY id", (context_id,)).fetchall()]
        ids = {int(r["type_id"]) for r in rows}
        names = {}
        if ids:
            names = {r["type_id"]: r["name"] for r in con.execute(
                f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                tuple(ids))}
    finally:
        con.close()
    for r in rows:
        r["name"] = names.get(int(r["type_id"]), f"Type {r['type_id']}")
        r["kind"] = "bpo" if int(r["runs"] or -1) < 0 else "bpc"
    return {"enabled": _manual_enabled(context_id), "entries": rows,
            "batches": list_blueprint_batches(context_id)}


@router.get("/api/industry/manual-blueprints")
def read_manual_blueprints(context_id: int = Depends(require_context)):
    """The prints and formulas this account has declared by hand."""
    return _manual_payload(context_id)


@router.post("/api/industry/manual-blueprints")
def edit_manual_blueprint(req: ManualBlueprintEdit, context_id: int = Depends(require_context)):
    """Declare or edit one print. Flag-gated like every other write that moves what a build costs —
    a declared ME/TE changes every material and duration figure for its product."""
    if not _manual_enabled(context_id):
        raise HTTPException(status_code=403, detail="feature not enabled")
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        prod = _blueprint_product_index(con).get(int(req.type_id))
        if prod is None:
            # Already a product? Then it is a product we can build, and that is what we file it as.
            row = con.execute("SELECT 1 AS ok FROM types WHERE type_id=?",
                              (int(req.type_id),)).fetchone()
            if not row:
                raise HTTPException(status_code=400, detail="unknown type")
            prod = int(req.type_id)
        me = int(max(0.0, min(10.0, float(req.me or 0))))
        te = int(max(0.0, min(20.0, float(req.te or 0))))
        runs = -1 if req.runs is None or int(req.runs) < 0 else int(req.runs)
        qty = max(0, min(int(req.quantity or 0), _STACK_CAP))
        prefer = str(req.prefer or "").strip().lower()
        if prefer not in ("bpo", "bpc"):
            prefer = ""
        if req.id:
            con.execute(
                "UPDATE pp_industry_blueprints SET type_id=?, me=?, te=?, runs=?, quantity=?, "
                "prefer=?, updated_at=? WHERE context_id=? AND id=?",
                (prod, me, te, runs, qty, prefer, _time.time(), context_id, int(req.id)))
        else:
            nxt = con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_industry_blueprints "
                              "WHERE context_id=?", (context_id,)).fetchone()["n"]
            con.execute(
                "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                "quantity, prefer, updated_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (context_id, int(nxt), prod, me, te, runs, qty, prefer, _time.time()))
        # The BPO-vs-BPC choice is a property of the PRODUCT, not of one row — two rows for one
        # product cannot sensibly disagree about which kind the plan should spend. Setting it on any
        # row sets it for the product, so the reader's "first row wins" can never surprise anyone.
        con.execute("UPDATE pp_industry_blueprints SET prefer=? WHERE context_id=? AND type_id=?",
                    (prefer, context_id, prod))
        con.commit()
    finally:
        con.close()
    return _manual_payload(context_id)


class BlueprintPaste(BaseModel):
    """A copied industry window, and the name of the batch it becomes. One window per character —
    the name is what keeps a second character's paste from replacing the first one's."""
    name: str = ""
    text: str


@router.post("/api/industry/manual-blueprints/paste/preview")
def preview_manual_blueprint_paste(req: BlueprintPaste,
                                   context_id: int = Depends(require_context)):
    """What this paste WOULD declare — counts, and every name we could not place. Nothing is
    written. Same parse the import runs, so the preview cannot promise a different import."""
    if not _manual_enabled(context_id):
        raise HTTPException(status_code=403, detail="feature not enabled")
    return parse_blueprint_paste(req.text)


@router.post("/api/industry/manual-blueprints/paste")
def import_manual_blueprint_paste(req: BlueprintPaste,
                                  context_id: int = Depends(require_context)):
    """Import a pasted industry window as a named batch, replacing that batch only."""
    if not _manual_enabled(context_id):
        raise HTTPException(status_code=403, detail="feature not enabled")
    res = replace_blueprint_batch(context_id, req.name, req.text)
    return {**_manual_payload(context_id), "imported": res}


@router.delete("/api/industry/manual-blueprints/batches/{batch}")
def delete_manual_blueprint_batch(batch: str, context_id: int = Depends(require_context)):
    """Drop one pasted batch — the other characters' batches and the hand-typed rows survive.
    Not flag-gated, for the same reason deleting a single row isn't."""
    delete_blueprint_batch(context_id, batch)
    return _manual_payload(context_id)


@router.delete("/api/industry/manual-blueprints/{entry_id}")
def delete_manual_blueprint(entry_id: int, context_id: int = Depends(require_context)):
    """Undeclare one print. Not flag-gated: removing a statement must stay possible even if the
    feature is rolled back under an account that already made one."""
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND id=?",
                    (context_id, int(entry_id)))
        con.commit()
    finally:
        con.close()
    return _manual_payload(context_id)


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
