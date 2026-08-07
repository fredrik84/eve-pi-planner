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
# ...in the SHORT layout. The window copies in TWO layouts, depending on whether a container is
# selected in its tree — the long one names WHERE each print is:
#
#   SHORT (a container IS selected, so the window is already scoped and says nothing about where):
#     4 x Nanotransistors Reaction Formula<TAB>0<TAB>0<TAB>-1<TAB>Composite
#
#   LONG (nothing selected — every row carries its structure and its container):
#     [N x ]<name> TAB <ME> TAB <TE> TAB <runs> TAB <?> TAB <structure> TAB <container> TAB <category>
#     Warp Core Stabilizer I Blueprint<TAB>10<TAB>20<TAB>-1<TAB>0<TAB>MTO2-2 - Ctrl C<TAB>Santo BPO<TAB>Warp Core Stabilizer
#
# Both must parse, and mixed in one paste. See `_split_location` for how the location columns are
# found and why they are counted from the END of the line.
#
# Nothing about the format needs translating — `runs = -1` is already this module's encoding for an
# original, and the ME/TE columns are the numbers a corp-hangar print could otherwise not state.
#
# **Each paste is a NAMED BATCH, exactly like a pasted stock source** (`add_pasted_source` in
# assets.py), and for the same reason: the user pastes ONCE PER CHARACTER. A paste that replaced the
# whole library would wipe the previous character's prints; one that appended would double the
# holding the moment the same window is pasted again after buying a print. Replacing only its own
# batch is the rule that survives both.
#
# **ONE PASTE IS ONE BATCH, and its identity is its NAME — never where its prints are.** The long
# layout is read for what it says (structure and container land on every row, and suggest the batch's
# default name), but a batch is not a place. Keying a batch on its container was tried and reverted:
# prints MOVE between containers, so a window re-pasted after a move replaced the new container and
# left the old container's batch standing — the same five formulas counted twice. That fails in the
# dangerous direction, because an over-counted print cap lets the planner schedule parallel jobs off
# prints the user does not have. A re-paste under the same name replaces EVERYTHING that batch last
# declared, whatever containers it named this time, which is what makes a move track correctly.

_STACK_RE = _re.compile(r"^(\d[\d,]*)\s*[x×]\s+(.+)$", _re.IGNORECASE)

_PASTE_BATCH_DEFAULT = "Industry window"


def _batch_key(label: str) -> str:
    """A stable id for a batch NAME — **the only identity a batch has**, so re-pasting the same
    window replaces it wherever its prints have moved to since.

    Deliberately a digest and not `hash()`: Python randomises string hashing per process, so a
    key built that way silently stops matching after a pod restart — which for this feature would
    mean a second copy of every print rather than a replacement.

    Deliberately the NAME and nothing else. A key derived from a location was tried (`paste:loc:`,
    reverted, migrated away in `_migrate_location_batches`) and it double-counted the moment a
    builder moved prints between two of their own containers: the new place got a fresh batch and
    the old place's batch was never named again, so nothing replaced it.
    """
    norm = (label or "").strip().lower() or _PASTE_BATCH_DEFAULT.lower()
    return "paste:" + _hashlib.sha1(norm.encode("utf-8")).hexdigest()[:16]


def _batch_label(structure: str, container: str) -> str:
    """How a LOCATION reads for display, and the default name offered for a batch found in one.

    Qualified by the structure, because a container name alone ("Santo BPO") is not unique across
    an account's structures and two batches reading identically in the list is indistinguishable
    from a bug. A label, never a key — see `_batch_key`.
    """
    structure, container = (structure or "").strip(), (container or "").strip()
    if container and structure:
        return f"{container} — {structure}"
    return container or structure


def _default_batch_name(locations: list[dict]) -> str:
    """The batch name to offer when the user typed none, derived from where the paste says its
    prints are. **A default, not a dependency** — it saves typing, and the moment it is written to a
    row it is just a name like any other.

    Stable for the same window, which is what makes an un-named re-paste land back on the same
    batch: one container gives its qualified label, several containers in one structure give the
    structure (so re-shuffling prints between cans inside a structure keeps the name), and several
    structures give the first structure alphabetically plus a count. A paste with no location at all
    gets the generic default, exactly as before.

    The honest caveat: a builder who never names their batches and then moves prints to a DIFFERENT
    STRUCTURE gets a different default, and so a second batch. Naming the batch once removes the
    ambiguity for good, which is why the UI offers the default in the name box rather than hiding
    it — a name the user can see is a name they can keep.
    """
    if not locations:
        return _PASTE_BATCH_DEFAULT
    if len(locations) == 1:
        return _batch_label(locations[0]["structure"], locations[0]["container"]) \
            or _PASTE_BATCH_DEFAULT
    structs = sorted({(l["structure"] or "").strip() for l in locations if (l["structure"] or "")})
    if len(structs) == 1:
        return structs[0]
    if not structs:
        return _PASTE_BATCH_DEFAULT
    return f"{structs[0]} +{len(structs) - 1} more"


def _is_number(s: str) -> bool:
    try:
        float((s or "").replace(",", "").replace(" ", ""))
        return True
    except ValueError:
        return False


def _split_location(parts: list[str]) -> tuple[str, str]:
    """(structure, container) for one industry-window row — `('', '')` when the row carries none.

    **Counted from the END of the line, because this column layout is INFERRED, not documented.**
    All we have is two real copies out of the client (see the header above): a short one ending
    `… runs TAB category`, and a long one with `? TAB structure TAB container` wedged in before the
    same trailing category. The one thing both samples agree on is that the CATEGORY IS LAST, so
    that is the only thing worth anchoring to: `[-3]` is the structure and `[-2]` the container.
    Read that way, a column being added, a column being dropped, or the unknown `0` at index 4
    moving all leave the location intact — absolute indices would silently start reading a
    different field. Nothing here depends on that `0`; we do not know what it means.

    Fewer than 7 fields is the short layout, which has no location in it at all. (The long sample
    has 8; 7 is the floor at which `[-3]`/`[-2]` can still be past the four leading columns this
    module does understand.)

    **Guard:** both fields must be non-empty and neither may be a bare number. If the layout ever
    changes such that this rule lands on the wrong columns, what it lands on is overwhelmingly
    likely to be one of the numeric columns — and refusing then degrades to "ask the user where
    these prints are", which is the honest answer. Inventing a structure called `0` is not.
    """
    while len(parts) > 1 and parts[-1] == "":
        parts = parts[:-1]                  # a trailing tab must not shift what "last" means
    if len(parts) < 7:
        return "", ""
    structure, container = parts[-3].strip(), parts[-2].strip()
    if not structure or not container:
        return "", ""
    if _is_number(structure) or _is_number(container):
        return "", ""
    return structure, container


def _parse_paste_line(line: str) -> dict | None:
    """One industry-window row → `{name, me, te, runs, quantity, structure, container}`, or None if
    it is not one. `structure`/`container` are `''` for a short-layout row (see `_split_location`).

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
    structure, container = _split_location(parts)
    return {"name": name, "me": me, "te": te, "runs": runs, "quantity": qty,
            "structure": structure, "container": container}


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

    empty = {"entries": [], "unknown": [], "no_product": [], "ignored": ignored, "locations": [],
             "suggested_name": _PASTE_BATCH_DEFAULT,
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

        # ── Fallback: a formula named after its PRODUCT ──────────────────────────────────────
        # Real case, 2026-08-07: a client copy carried "Fullerides Reaction Formula" while this SDE
        # calls the item "Fulleride Reaction Formula" — singular. CCP renames things and our SDE
        # snapshot lags it, so an exact-name miss is NOT proof the print does not exist, and telling
        # a user who pasted 238 real formulas that one of them isn't a thing is the wrong answer.
        #
        # When a name ends in "Reaction Formula" the stem is the PRODUCT's name, and a product
        # identifies its reaction uniquely (`reactions.output_type_id`). So: strip, look the stem up
        # as a product, take the reaction that makes it.
        #
        # Deliberately narrow. It runs ONLY after an exact match has failed, and only resolves when
        # the stem is a real type that some reaction actually outputs — so it can neither override a
        # good match nor invent a print. 78 of 111 formulas in this SDE are exactly
        # "<product> Reaction Formula" and never reach here; the 33 that differ (the "Pure …"
        # boosters) match on their real name and never reach here either.
        missing_names = [n for n in names if n.lower() not in lookup]
        stems: dict[str, str] = {}          # product-name (lower) -> the name as pasted
        for n in missing_names:
            low = n.lower()
            if low.endswith(_FORMULA_SUFFIX):
                stem = low[:-len(_FORMULA_SUFFIX)].strip()
                if stem:
                    stems.setdefault(stem, n)
        if stems:
            out_to_reaction: dict[int, int] = {}
            try:
                for r in con.execute("SELECT reaction_id, output_type_id FROM reactions"):
                    out_to_reaction.setdefault(int(r["output_type_id"]), int(r["reaction_id"]))
            except Exception:
                out_to_reaction = {}        # manufacturing-only SDE: nothing to fall back to
            keys = sorted(stems)
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for r in con.execute(
                    f"SELECT type_id, name FROM types WHERE LOWER(name) IN ({marks})",
                    tuple(chunk),
                ).fetchall():
                    reaction_id = out_to_reaction.get(int(r["type_id"]))
                    pasted = stems.get(r["name"].lower())
                    if reaction_id and pasted:
                        lookup[pasted.lower()] = reaction_id

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
        # The LOCATION is part of the grouping key so the same print in two containers stays two
        # rows and each one keeps the place it was found in — the columns exist to be RECORDED.
        # It changes no total: a holding sums every row of a product (`manual_blueprints`), so two
        # rows of 2 and 3 and one row of 5 are the same five prints. Short-layout rows all carry
        # ('', '') and so group exactly as they always did.
        struct, cont = r.get("structure") or "", r.get("container") or ""
        key = (int(prod), me, te, runs, struct, cont)
        g = groups.setdefault(key, {
            "product_type_id": int(prod), "name": r["name"], "me": me, "te": te, "runs": runs,
            "quantity": 0, "kind": "bpo" if runs < 0 else "bpc",
            "formula": tid in formula_ids, "structure": struct, "container": cont,
        })
        g["quantity"] += int(r["quantity"])

    entries = list(groups.values())
    for e in entries:
        e["quantity"] = min(e["quantity"], _STACK_CAP)
    # Every distinct place this paste named — reported so the preview can say where the prints are
    # and so a default batch name can be offered. It is a DESCRIPTION of the paste; the import files
    # all of it under one batch whatever this says.
    locs: dict[tuple, dict] = {}
    for e in entries:
        if not (e["structure"] or e["container"]):
            continue
        lk = (e["structure"], e["container"])
        loc = locs.setdefault(lk, {"structure": e["structure"], "container": e["container"],
                                   "name": _batch_label(e["structure"], e["container"]),
                                   "prints": 0, "products": set()})
        loc["prints"] += e["quantity"]
        loc["products"].add(e["product_type_id"])
    locations = [{**v, "products": len(v["products"])}
                 for v in sorted(locs.values(), key=lambda x: x["name"].lower())]
    return {"entries": entries, "unknown": unknown, "no_product": no_product, "ignored": ignored,
            "locations": locations, "suggested_name": _default_batch_name(locations),
            "prints": sum(e["quantity"] for e in entries),
            "formulas": sum(e["quantity"] for e in entries if e["formula"]),
            "blueprints": sum(e["quantity"] for e in entries if not e["formula"]),
            "products": len({e["product_type_id"] for e in entries}),
            "lines": len(parsed)}


def replace_blueprint_batch(context_id: int, name: str, text: str,
                            structure: str = "", container: str = "") -> dict:
    """Import one pasted industry window as ONE batch, REPLACING that whole batch and nothing else.

    **A batch is its NAME.** Every row the paste yields is filed under `_batch_key(name)`, and the
    import deletes everything that key held first — regardless of which containers the previous
    paste named or this one does. That is precisely what makes a MOVE track: paste five formulas in
    "Santo BPO", move them into "New Can" in game, re-paste the same window under the same name, and
    the holding is still five. Keying per container (tried, reverted) left the old container's batch
    standing and made it ten, which is the dangerous direction — an over-counted print cap lets the
    planner run parallel jobs off prints that do not exist.

    Where the prints are is still read and STORED per row: the long layout's own structure/container
    when the row carried them, otherwise the `structure`/`container` the UI asked for. Display and
    future use only. The one thing location does decide is the DEFAULT NAME when the user typed none
    (`_default_batch_name`), which saves typing without becoming an identity.

    Re-pasting the same window after buying a print updates it; pasting a second character's window
    under its own name adds to the library beside the first. Rows typed in on the form carry an empty
    batch and are never touched by any paste.
    """
    ensure_manual_blueprints_table()
    ensure_paste_unresolved_table()
    res = parse_blueprint_paste(text)
    ask_struct, ask_cont = (structure or "").strip(), (container or "").strip()
    label = (name or "").strip() or res.get("suggested_name") or _PASTE_BATCH_DEFAULT
    if not (name or "").strip() and not res.get("locations") and (ask_struct or ask_cont):
        # Nothing typed and the paste named no place of its own — the place the user picked in the
        # "Where are these?" box is the most useful name we can offer them.
        label = _batch_label(ask_struct, ask_cont) or _PASTE_BATCH_DEFAULT
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
        # The batch replaces itself WHOLE — one DELETE by key, before a single row is written. Not
        # per container: a container this window no longer mentions is a container the user emptied,
        # and leaving its rows behind is exactly the double-count this replaced.
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, key))
        nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_industry_blueprints "
                              "WHERE context_id=?", (context_id,)).fetchone()["n"])
        now = _time.time()
        for e in res["entries"]:
            # The row's own place if the window stated one, else the place the user was asked for.
            e_struct = e.get("structure") or ""
            e_cont = e.get("container") or ""
            if not (e_struct or e_cont):
                e_struct, e_cont = ask_struct, ask_cont
            con.execute(
                "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                "quantity, prefer, updated_at, batch, batch_name, structure, container) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (context_id, nxt, e["product_type_id"], e["me"], e["te"], e["runs"],
                 e["quantity"], prefer.get(e["product_type_id"], ""), now,
                 key, label, e_struct, e_cont))
            nxt += 1
        # What this window named and we could not resolve. Kept, not just reported once — see
        # `ensure_paste_unresolved_table`.
        _record_unresolved(con, context_id, key, label,
                           list(res["unknown"]) + list(res["no_product"]))
        con.commit()
    finally:
        con.close()
    res.update({"added": res["prints"], "batch": key, "name": label})
    return res


def list_blueprint_batches(context_id: int) -> list[dict]:
    """The pasted batches this account holds — one per pasted window, in practice.

    Grouped on the batch KEY only. The location columns are summarised, never grouped on: one batch
    routinely spans several containers (a window copied with nothing selected in the tree names all
    of them), and grouping by place here would report one paste as several batches sharing a key —
    which is how the ✕ and the print counts would start disagreeing with what a re-paste replaces.
    `places` is that span; `structure`/`container` are filled in only when there is exactly one, so
    a caller can display a location without having to guess whether it is representative. The span
    is folded in PYTHON from a second small query rather than as a `COUNT(DISTINCT a || sep || b)`,
    which would need a separator literal that behaves identically on SQLite and Postgres and could
    still be a real character in a container name.
    """
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT COALESCE(batch,'') AS batch, COALESCE(batch_name,'') AS batch_name, "
            "COUNT(*) AS rows_n, SUM(quantity) AS prints, COUNT(DISTINCT type_id) AS products "
            "FROM pp_industry_blueprints WHERE context_id=? AND COALESCE(batch,'')<>'' "
            "GROUP BY COALESCE(batch,''), COALESCE(batch_name,'') ORDER BY batch_name",
            (context_id,)).fetchall()
        places: dict[str, set] = {}
        for r in con.execute(
            "SELECT DISTINCT COALESCE(batch,'') AS batch, COALESCE(structure,'') AS structure, "
            "COALESCE(container,'') AS container FROM pp_industry_blueprints "
            "WHERE context_id=? AND COALESCE(batch,'')<>''", (context_id,)).fetchall():
            if r["structure"] or r["container"]:
                # ('', '') is "this row claims no place", which is not a place — counting it would
                # report a mixed batch as one place more than it actually names.
                places.setdefault(r["batch"], set()).add((r["structure"], r["container"]))
    except Exception:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        seen = places.get(r["batch"], set())
        only = next(iter(seen)) if len(seen) == 1 else ("", "")
        out.append({"batch": r["batch"], "name": r["batch_name"] or _PASTE_BATCH_DEFAULT,
                    "structure": only[0], "container": only[1], "places": len(seen),
                    "rows": int(r["rows_n"] or 0), "prints": int(r["prints"] or 0),
                    "products": int(r["products"] or 0)})
    return out


def delete_blueprint_batch(context_id: int, batch: str) -> None:
    """Drop one pasted batch. Every other batch, and everything typed in by hand, stays."""
    ensure_manual_blueprints_table()
    if not (batch or "").strip():
        return                            # '' is the hand-typed rows — never deletable in bulk
    ensure_paste_unresolved_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, batch))
        # Its unresolved names go with it: they described THAT paste, and a warning about a batch
        # the user has deleted is a warning they cannot act on.
        con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=? AND batch=?",
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
    """A copied industry window, the name of the batch it becomes, and where its prints are.

    `name` is the batch — the only thing that decides what a re-paste replaces. One window per
    character, in practice; the name is what keeps a second character's paste from replacing the
    first one's.

    `structure`/`container` are the ANSWER TO THE QUESTION the UI asks when the paste itself carries
    no location (the short layout, i.e. a container was selected in the client, which is exactly the
    case where the window knows where it is and doesn't say). They are RECORDED on the rows that
    named no place of their own, and they may supply a default name when none was typed — they never
    key, group or replace anything.
    """
    name: str = ""
    text: str
    structure: str = ""
    container: str = ""


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
    """Import a pasted industry window as one named batch, replacing that whole batch — every row it
    previously declared, whatever containers this paste or the last one named."""
    if not _manual_enabled(context_id):
        raise HTTPException(status_code=403, detail="feature not enabled")
    res = replace_blueprint_batch(context_id, req.name, req.text, req.structure, req.container)
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
