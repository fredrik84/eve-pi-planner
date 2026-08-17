"""Blueprints and formulas DECLARED BY HAND, and the owned-blueprint map the cost resolver
consumes."""
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

from app.industry.blueprints.esi import (
    _STACK_CAP,
    _batch_key,
    _blueprint_product_index,
    _copy_rank,
    classify_blueprint,
    ensure_char_blueprints_table,
)
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

# The suffix every reaction-formula ITEM carries. Used only by the paste fallback below.
_FORMULA_SUFFIX = " reaction formula"


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
        # WHERE each pasted print physically is — read out of the paste when the window carried it
        # (see `_split_location`), asked for when it did not. **Recorded and displayed ONLY.**
        # Nothing may key, group, replace, count or plan off these: prints move between containers
        # all the time, and a model that treats a location as an identity turns a move into a
        # duplicate (see `_batch_key`). Kept as two fields rather than folded into `batch_name` so a
        # later consumer gets the structure back without having to unpick a display string.
        add_columns(con, "pp_industry_blueprints",
                    "structure TEXT DEFAULT ''", "container TEXT DEFAULT ''")
        con.commit()
        _migrate_location_batches(con)
    finally:
        con.close()


@ensure_once
def ensure_paste_unresolved_table():
    """Names a paste could not resolve to a type, KEPT — one row per (batch, name).

    They used to live for exactly as long as the import's own status line, which was fine while an
    unmatched name only meant "we imported 237 of your 238 formulas". It stops being fine the
    moment ABSENCE becomes knowledge (`app/reactions/library.py`): a formula whose name we failed
    to resolve is then indistinguishable from one the user does not own, and the plan starts
    telling them to go buy a formula sitting in their hangar. It has happened once already — a
    client copy carried `Fullerides Reaction Formula` where the SDE has the singular, fixed in
    `ee633be` by the product-name fallback — so the next rename is a matter of time.

    Replaced per batch, exactly like the batch's own rows: a re-paste is a fresh statement about
    that window, and a name it no longer carries is no longer unresolved.
    """
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_blueprint_paste_unresolved (
                context_id INTEGER NOT NULL,
                batch      TEXT NOT NULL,
                name       TEXT NOT NULL,
                batch_name TEXT NOT NULL DEFAULT '',
                updated_at REAL,
                PRIMARY KEY (context_id, batch, name)
            )
        """)
        con.commit()
    finally:
        con.close()


def paste_unresolved_names(context_id: int) -> list[dict]:
    """[{name, batch_name}] — every pasted line this account holds that resolved to no type."""
    ensure_paste_unresolved_table()
    con = get_connection()
    try:
        return [{"name": r["name"], "batch_name": r["batch_name"] or ""}
                for r in con.execute(
                    "SELECT name, batch_name FROM pp_blueprint_paste_unresolved "
                    "WHERE context_id=? ORDER BY name", (context_id,))]
    except Exception:
        return []
    finally:
        con.close()


def _record_unresolved(con, context_id: int, batch: str, batch_name: str, names: list[str]) -> None:
    """Replace this batch's unresolved names — inside the import's own transaction, so the rows and
    the warning about what is missing from them can never disagree."""
    try:
        con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=? AND batch=?",
                    (context_id, batch))
        now = _time.time()
        for n in dict.fromkeys(names):
            con.execute(
                "INSERT INTO pp_blueprint_paste_unresolved (context_id, batch, name, batch_name, "
                "updated_at) VALUES (?,?,?,?,?)", (context_id, batch, str(n)[:200], batch_name, now))
    except Exception:
        pass                        # never fail an import over its own footnote


def _migrate_location_batches(con) -> None:
    """Re-key the `paste:loc:` batches written by the short-lived per-container model.

    Those rows were keyed on structure+container, which no batch is any more. Left alone they would
    be ORPHANS: nothing the user can paste produces that key again, so the batch could never be
    replaced — it could only sit there inflating the holding until noticed and ✕'d. Re-keying each
    one to `_batch_key(batch_name)` — its own displayed name, e.g. `Santo BPO — MTO2-2 - Ctrl C` —
    makes it an ordinary named batch: re-pastable under that name, replaceable, deletable.

    Two batches can only collide here if they displayed identically, which the label already
    prevented; if they somehow did, merging them is the right answer under the new model anyway.
    Runs inside `ensure_manual_blueprints_table`, so once per process, and after the first pass the
    SELECT matches nothing.
    """
    try:
        rows = con.execute(
            "SELECT DISTINCT COALESCE(batch,'') AS batch, COALESCE(batch_name,'') AS batch_name "
            # The pattern is a PARAMETER, not a literal: `_pg_translate` escapes a literal `%` to
            # `%%` for psycopg2's interpolation, which is then skipped entirely when a statement
            # carries no params — so an inline `LIKE 'paste:loc:%'` reaches Postgres as `%%`.
            "FROM pp_industry_blueprints WHERE COALESCE(batch,'') LIKE ?",
            ("paste:loc:%",)).fetchall()
        for r in rows:
            con.execute("UPDATE pp_industry_blueprints SET batch=? WHERE batch=?",
                        (_batch_key(r["batch_name"]), r["batch"]))
        if rows:
            con.commit()
    except Exception:
        pass                              # a fresh DB has no such rows; never block startup on this


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

    Empty unless `industry_manual_blueprints` is on, which is the right default for every Industry
    caller: with the flag off, a plan must be byte-for-byte what shipped before declarations existed.

    **`force=True` reads the rows whatever the flag says, and it has a real caller now** — not only
    the admin/test path it was written for. `app.reactions.library.held_formula_products` uses it
    because Reactions has its own paste route (`POST /api/reactions/formulas/paste`, 2026-08-14) and
    therefore its own declarations, made by users who may have no Industry flag at all. Telling such
    a user to go and buy a formula they had just finished declaring is the one direction that report
    must never point. Safe because that call is a pure read used *only* to say what is MISSING, so a
    wider answer can only ever withdraw a "buy this", never plan work. **Anything else reaching for
    `force=True` should say here why it is not the flag's business.**
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
    return dict(request_memo(("owned_blueprints", context_id),
                             lambda: _owned_blueprints(context_id)))


def _owned_blueprints(context_id: int) -> dict[int, dict]:
    """The real read — memoised per request by `owned_blueprints`. On an account with fourteen
    characters this parses fourteen blueprint JSON blobs and indexes the whole `blueprints` table,
    and a single reactions order report asked for it twice over (the formula cap and the
    missing-formula check each rebuilt it), on top of every Industry plan path that wants it."""
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


def declared_products(owned: dict[int, dict] | None) -> set[int]:
    """Products whose holding the user stated BY HAND (`owned_blueprints`' merge rule).

    The evidence layers below must not add anything for these. A hand-declared formula, a formula
    in a pasted hangar and a formula seen in an observed job are three descriptions of one physical
    item at least as often as they are three items, and the declaration is the only one of the three
    that is a statement of totality — so it answers alone.

    **It is also the answer to "do we KNOW this holding", per product.** `blueprint_coverage` asks
    that question of the ACCOUNT, and rightly so for an ESI reading: a scope that 12 of 14
    characters never granted makes every scanned count a floor, and capping on a floor serialises
    work the builder can really do. A DECLARATION is not a scan — it is the user stating what they
    own, and `owned_blueprints` already treats it as authoritative enough to REPLACE the reading for
    its product. Suppressing a declared product's cap because some *other* character never granted
    a scope answers a per-product question with an account-wide one: a real account declared 238
    formulas, held 10 of one of them, and was assigned 20 concurrent jobs. So both cap sites
    (`BuildParams.prints_known`, `formula_concurrency_caps`) ask this set first and the coverage
    gate second. Products with no declaration are untouched — they stay uncapped on a floor.
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
    declared = declared_products(owned)
    for prod, b in _formula_stock_buckets(context_id).items():
        if prod in declared:
            continue                        # the user stated this holding — see declared_products
        extra = _stock_extra(b, _seen_personally(owned, prod))
        if extra > 0:
            out[prod] = extra
    return out


def _stock_extra(b: dict[str, int], seen_personally: int) -> int:
    """The EXTRA prints one product's stock buckets prove over the personal blueprint list."""
    return max(0, b["personal"] - seen_personally) + max(b["corp"], b["paste"])
