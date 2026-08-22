"""Personal reaction-job tracking, plan slots and orphan handling — the 'what is running / what am
I committed to' layer. Reads live ESI industry jobs (opt-in scope), matches them against the
persistent plan (pp_reaction_assignments), levels that plan's run counts, and values everything via
the graph layer. Depends on settings + graph; advisor (the suggestion engine) and orders build on
top of this, so this must not import either."""
import json as _json
import logging
import math
import time as _time
from datetime import datetime, timezone

log = logging.getLogger(__name__)


from app import esi_http
from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data, ensure_once
from app.db import add_columns
from app.markets import resolve_market_data
from app.cache import cache_invalidate, charlist_key, cache_get_json, cache_set_json
from app.esi import require_context, _get_valid_token
from app import completions
from app.production import capacity_contract, skill_slot_count

from app.reactions._router import router
from app.reactions._flags import flag_on
from app.reactions.settings import effective_reaction_settings
from app.reactions.graph import (
    _load_goo_and_reached, _value_reaction_batch, _ordered_chain_tiers, reaction_stock_pool,
    _shopping_roots, tier_ranks, tidy_runs, request_memo, _TIDY_STEPS,
)


# ── Personal reaction-job tracking (opt-in scope, see app.esi.INDUSTRY_JOBS_SCOPES) ────────────
# Cache-at-fetch, not live-fetch-on-every-page-load (same shape as app.pi_sim's colony state):
# ESI already reports start_date/end_date directly for a job, so there's no decay/rate math to
# simulate forward — just cache the raw filtered job list with a fetched_at timestamp, refreshed
# on demand (a "Refresh" button, same UX as the existing planet rescan) rather than polling.

@ensure_once
def ensure_industry_jobs_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_industry_jobs (
                character_id INTEGER PRIMARY KEY,
                jobs_json    TEXT NOT NULL DEFAULT '[]',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


_structure_name_cache: dict[int, str] = {}  # structure names don't change — cache for process lifetime


def _resolve_structure_name(structure_id: int, access_token: str) -> str:
    if structure_id in _structure_name_cache:
        return _structure_name_cache[structure_id]
    name = f"Structure #{structure_id}"
    try:
        resp = esi_http.get(f"universe/structures/{structure_id}/", token=access_token, timeout=10)
        resp.raise_for_status()
        name = resp.json().get("name") or name
    except Exception:
        pass  # best-effort — an unresolvable structure just shows its raw ID, never blocks the fetch
    _structure_name_cache[structure_id] = name
    return name


# EVE industry activity id for Reactions is 9 (Manufacturing=1, ME research=4, …). Verified
# against live corp/character industry-jobs responses — a reaction-heavy corp's jobs are all
# activity 9, and there is no activity 11 at all. An earlier value of 11 here was wrong but never
# caught, because the jobs table was never populated (the refresh was unwired) so the filter never
# actually ran against real data.
REACTION_ACTIVITY_ID = 9

LEDGER = "pp_reaction_completions"


def _name_facilities(jobs: list[dict], access_token: str) -> list[dict]:
    """Stamp a readable `facility_name` on each job. Shared by the personal and corp fetches — the
    two differ in which endpoint they read and how they filter, never in this."""
    for j in jobs:
        fac_id = j.get("facility_id")
        j["facility_name"] = _resolve_structure_name(fac_id, access_token) if fac_id else "Unknown"
    return jobs


def read_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """This character's reaction jobs (activity_id 9) from ESI, facilities named. RAISES on any
    ESI failure.

    Split out from the best-effort wrapper below because `[]` means two different things to two
    different callers: to a tab refresh it means "nothing running, show an empty list", and to the
    alert rescan it would mean "the jobs you were alerted about are gone" — which is what a
    completed-reaction alert being *resolved* looks like. Storing an empty list from a failed read
    would therefore delete the snapshot the alert was computed from and report the problem as
    fixed. Exactly the trap that wiped colony rows in §37; a caller that acts on the ANSWER has to
    be able to tell it from the failure."""
    resp = esi_http.get(f"characters/{character_id}/industry/jobs/",
                        token=access_token, timeout=10)
    resp.raise_for_status()
    reaction_jobs = [j for j in resp.json() if j.get("activity_id") == REACTION_ACTIVITY_ID]
    return _name_facilities(reaction_jobs, access_token)


def fetch_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Best-effort `read_industry_jobs`: returns [] on any failure rather than raising — a refresh
    failing for one character must not block the others."""
    try:
        return read_industry_jobs(character_id, access_token)
    except Exception:
        return []


# A character's corporation, cached until ESI's own `Expires` says the answer may have changed.
# `characters/{id}/` is cached for a day at ESI's end, and the alert rescan asks for it every 15
# minutes — re-asking inside that window is the "querying before Expires" that CLAUDE.md says can
# get the whole app banned, so the window is honoured rather than approximated by a TTL of our own.
_corp_id_cache: dict[int, tuple[int | None, float]] = {}


def _corporation_of(character_id: int, client=None) -> int | None:
    hit = _corp_id_cache.get(character_id)
    if hit and hit[1] > _time.time():
        return hit[0]
    resp = esi_http.get(f"characters/{character_id}/", client=client)
    # ESI answers an error with a JSON body, so reading `corporation_id` off an unchecked response
    # yields None — which `read_corp_industry_jobs` would read as "no corp", return [] for, and a
    # caller would then store as a verified personal-only snapshot. The same trap the strict read
    # exists to close, one call earlier. Raise, and cache only a real answer.
    resp.raise_for_status()
    corp_id = resp.json().get("corporation_id")
    expires = 0.0
    try:
        expires = datetime.strptime(resp.headers["expires"],
                                    "%a, %d %b %Y %H:%M:%S %Z").replace(
            tzinfo=timezone.utc).timestamp()
    except Exception:
        pass                      # no parseable Expires — ask again next time rather than guess
    _corp_id_cache[character_id] = (corp_id, expires)
    return corp_id


class CorpJobsForbidden(Exception):
    """ESI refused the corp job queue — the character granted the scope but does not hold
    Factory_Manager/Director. A stable, expected answer, and NOT a failed read: for this character
    the corp queue is simply not visible, today or ever, so `[]` is the true answer rather than a
    gap in one."""


def read_corp_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """This character's reaction jobs installed FOR CORPORATION, RAISING on failure.

    Raises `CorpJobsForbidden` for the missing-role case specifically, so a caller that needs to
    know whether it saw the whole picture can tell "you may not look at this" (permanent, and the
    empty list is correct) from "I could not look right now" (transient, and the empty list is a
    lie). Filtered to `installer_id == character_id` — this reads the WHOLE corp's job queue over
    ESI, but only this character's own installs are what a "my jobs" view should show."""
    with esi_http.client(timeout=10) as client:
        corp_id = _corporation_of(character_id, client=client)
        if not corp_id:
            return []
        resp = esi_http.get(f"corporations/{corp_id}/industry/jobs/",
                            client=client, token=access_token)
        if resp.status_code in (401, 403):
            raise CorpJobsForbidden(f"character {character_id} may not read corp industry jobs")
        resp.raise_for_status()
        jobs = resp.json()

    reaction_jobs = [j for j in jobs if j.get("activity_id") == REACTION_ACTIVITY_ID and j.get("installer_id") == character_id]
    return _name_facilities(reaction_jobs, access_token)


def fetch_corp_industry_jobs(character_id: int, access_token: str) -> list[dict]:
    """Best-effort `read_corp_industry_jobs`: any failure (missing role, no corp, network) returns
    [] rather than raising — one character's corp-jobs lookup failing must never block their own
    personal jobs or any other character's refresh. Right for a tab refresh, which only displays
    what it got; a caller that VERIFIES an alert against the result wants the strict one."""
    try:
        return read_corp_industry_jobs(character_id, access_token)
    except Exception:
        return []


# ESI's industry-jobs endpoint caches ~5 min; a plain tab-open refresh must not re-hit ESI more
# often than that (both to respect CCP's cache and to keep the Reactions tab snappy). The manual
# "Refresh jobs" button passes force=1 to bypass this and pull immediately.
_JOBS_CACHE_TTL = 300


@router.post("/api/reactions/jobs/refresh")
def refresh_industry_jobs(force: int = 0, context_id: int = Depends(require_context)):
    """Refresh the caller's own characters' cached reaction-job list from ESI — only characters
    that have actually granted the industry-jobs scope (opted in via ?reactions=1 login) are
    fetched; others are silently skipped, not an error, since most PI-planner accounts never
    opt into this. Called on Reactions tab-open (respecting ESI's ~5min cache via _JOBS_CACHE_TTL)
    and by the manual "Refresh jobs" button (force=1, bypasses the staleness guard)."""
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        # Last-fetch time per character, read up front (one connection, closed before the ESI loop
        # below) so the staleness guard doesn't re-hit ESI for a character refreshed seconds ago.
        fetched_at_by_char = {r["character_id"]: r["fetched_at"] for r in con.execute(
            "SELECT character_id, fetched_at FROM pp_char_industry_jobs"
        )}
    finally:
        con.close()

    now = _time.time()
    skipped = 0
    fetched: list[tuple] = []          # (character_id, jobs_json, fetched_at) — written in one batch below
    for c in chars:
        scopes = c["scopes"] or ""
        if "read_character_jobs" not in scopes:
            continue
        prev = fetched_at_by_char.get(c["character_id"])
        if not force and prev is not None and (now - prev) < _JOBS_CACHE_TTL:
            skipped += 1  # still within ESI's own cache window — a re-fetch would return the same data
            continue
        token = _get_valid_token(c["character_id"])
        if not token:
            continue
        jobs = fetch_industry_jobs(c["character_id"], token)
        # Only characters that re-authorised after the corp-jobs scope was added carry it —
        # already-connected characters keep working (personal jobs only) until they reconnect,
        # no forced re-auth. job_id is unique across BOTH endpoints (it's ESI's own job
        # identifier), so a plain concat can't double-count even in the (shouldn't-happen) case
        # of a job appearing in both responses.
        if "read_corporation_jobs" in scopes:
            jobs = jobs + fetch_corp_industry_jobs(c["character_id"], token)
        fetched.append((c["character_id"], _json.dumps(jobs), _time.time()))

    # One connection for all the writes, after the (slow) ESI fetches are done — not one per char.
    if fetched:
        con = get_connection()
        try:
            con.executemany(
                "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, fetched_at=excluded.fetched_at",
                fetched,
            )
            con.commit()
        finally:
            con.close()
    refreshed = len(fetched)
    # The Characters tab shows each toon's running reaction jobs (see list_characters), served
    # from the same Redis-cached charlist payload — so a refresh that actually pulled new jobs must
    # bust that cache or the tab keeps showing stale/absent jobs until the next colony rescan.
    if refreshed:
        cache_invalidate(charlist_key(context_id))
        _invalidate_dashboard_cache(context_id)
    # Capacity may just have been freed by a completed ESI job. Reactions owns assignment, so the
    # refresh endpoint is also the natural recovery point for application-owned queued work.
    # Import lazily: orders depends on this module's allocator and must remain the upper layer.
    recovery = {"attempted": 0, "assigned": [], "blocked": []}
    try:
        from app.reactions.orders import retry_automatic_orders
        recovery = retry_automatic_orders(context_id)
    except Exception:
        log.exception("automatic reaction-order recovery failed for context %s", context_id)
    return {"ok": True, "characters_refreshed": refreshed, "characters_skipped": skipped,
            "automatic_recovery": recovery}


def refresh_character_jobs(character_id: int) -> bool:
    """Re-read ONE character's reaction jobs from ESI and store them. True only if ESI answered.

    The alert rescan's entry point (`app.notifications._default_scan`), and the reason it is a
    function rather than a call to the endpoint above: that endpoint sweeps a whole account and
    batches its writes, which is right for a tab-open and wrong for "verify the one character this
    alert is about before nagging them". Both go through the same fetch helpers, so they cannot
    disagree about what a reaction job is.

    Deliberately ignores `_JOBS_CACHE_TTL` — the caller has already checked `fetched_at` against
    it, and unlike a tab-open there is no second chance: a skip here would send an alert off data
    the feature exists to stop trusting.
    """
    ensure_industry_jobs_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT context_id, scopes FROM pp_characters WHERE character_id=?",
            (character_id,),
        ).fetchone()
    finally:
        con.close()
    scopes = (row["scopes"] if row else "") or ""
    if not row or "read_character_jobs" not in scopes:
        return False        # never granted — there is no ESI path to verify this character with
    token = _get_valid_token(character_id)
    if not token:
        return False
    try:
        jobs = read_industry_jobs(character_id, token)
    except Exception:
        log.warning("Reaction-job rescan failed for character %s", character_id, exc_info=True)
        return False
    # The corp half has to be strict too, with ONE exception. A corp-installed reaction is a real
    # source of alerts and never appears in the personal read, so a corp read that failed and
    # returned [] would store a truncated snapshot — the finished job vanishes, the alert is
    # reported as one the user had already fixed, and the next tick computes from the truncation.
    # The same trap as the personal read, one endpoint over. The exception is the missing-role
    # 403: that is permanent and means the corp queue is not visible to this character at all, so
    # [] is the true answer and failing on it would silence them forever.
    if "read_corporation_jobs" in scopes:
        try:
            jobs = jobs + read_corp_industry_jobs(character_id, token)
        except CorpJobsForbidden:
            pass
        except Exception:
            log.warning("Corp reaction-job rescan failed for character %s",
                        character_id, exc_info=True)
            return False
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?) "
            "ON CONFLICT (character_id) DO UPDATE SET jobs_json=excluded.jobs_json, "
            "fetched_at=excluded.fetched_at",
            (character_id, _json.dumps(jobs), _time.time()),
        )
        con.commit()
    finally:
        con.close()
    cache_invalidate(charlist_key(row["context_id"]))
    return True


def reaction_slots(character_row: dict) -> int:
    """1 base slot + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at
    the game's real max of 11 (5+5+1)."""
    return skill_slot_count(character_row.get("mass_reactions"),
                            character_row.get("advanced_mass_reactions"))


def reaction_capable(character_row: dict) -> tuple[bool, str]:
    """(counts toward reaction capacity, why not).

    Two conditions, both required:

    * **Job-tracking scope** — without it we can't see what's already running, so we'd have no idea
      how loaded the character is. (Also excludes a wallet-only character outright.)
    * **A reaction slot skill actually trained** — every character has one free base slot, but a
      character that has never trained Mass Reactions isn't running reactions. Counting those base
      slots inflated capacity by one per idle alt, and the optimizer sizes its suggestions against
      that ceiling, so it proposed work for characters that were never going to run it.

    Matches app/industry/slots.py's `_eligibility` so the two tools report the same capacity.
    """
    if "read_character_jobs" not in (character_row["scopes"] or ""):
        return False, "not connected for job tracking"
    trained = (character_row["mass_reactions"] or 0) > 0 or (character_row["advanced_mass_reactions"] or 0) > 0
    if not trained:
        return False, "no reaction slot skills trained"
    return True, ""


@ensure_once
def ensure_reaction_assignments_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_assignments (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                character_id INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                name         TEXT NOT NULL,
                runs         INTEGER NOT NULL,
                input_cost   REAL NOT NULL,
                reward       REAL NOT NULL,
                created_at   REAL NOT NULL
            )
        """)
        con.commit()
        # tier_order: 0 = the deepest intermediate reaction (react first — a real chain, e.g.
        # goo -> Ferrofluid -> Nonlinear Metamaterials, needs the intermediate done before the
        # top-level reaction can even start), ascending up to the top-level product itself
        # (highest number in the group = react last). Existing single-tier assignments default
        # to 0, unaffected — additive migration, matches this codebase's convention.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN tier_order INTEGER NOT NULL DEFAULT 0")
            con.commit()
        except Exception:
            pass
        # order_id: tags every row (top-level AND its chain-tier rows) created on behalf of a
        # fixed-unit customer order (see ensure_reaction_orders_table below) — NULL for every
        # assignment created the normal way (manual-assign, suggest-and-assign), no behavior
        # change there. Lets the dashboard label which slots are committed to a client job.
        try:
            con.execute("ALTER TABLE pp_reaction_assignments ADD COLUMN order_id INTEGER")
            con.commit()
        except Exception:
            pass
        # What the LEVELLER decided, written down so the dashboard can report it. All four are
        # per (stage, product) and stamped identically on every row of that group — the leveller
        # pools a product across characters, so the group is the unit the number belongs to, and
        # a reader must take each value ONCE per (type_id, tier_order) rather than summing rows.
        #
        # Why persisted rather than recomputed: `level_product_runs` runs in Step 1 of
        # `get_industry_jobs`, before the read that builds the rows, and it is the only place that
        # knows the counterfactual (what a per-product layout would have cost). Recomputing in
        # Step 4 would mean running the whole stage solve a second time, on a second connection,
        # for numbers the first run already had in hand.
        #
        #   cadence_over_h — REAL hours this product's job runs PAST the ceiling, 0 when it fits.
        #                    The cadence is a target, not an absolute (2026-08-14): a stage that
        #                    genuinely cannot fit its window overruns and says so here.
        #   surplus_runs   — runs of this product the layout makes beyond what the plan needs.
        #   jobs_saved     — jobs the shared count saved against the cheapest per-product one.
        #   recover_runs   — surplus that per-product run counts would give back.
        #
        # Through `add_columns` rather than a bare try/except chain: Postgres aborts the WHOLE
        # transaction on a failed statement, so a later already-exists ALTER rolls back the earlier
        # ones that had genuinely succeeded. That cost this project real columns once — see
        # `app/db.py`.
        add_columns(con, "pp_reaction_assignments",
                    "cadence_over_h REAL NOT NULL DEFAULT 0",
                    "surplus_runs INTEGER NOT NULL DEFAULT 0",
                    "jobs_saved INTEGER NOT NULL DEFAULT 0",
                    "recover_runs INTEGER NOT NULL DEFAULT 0")
    finally:
        con.close()


# ── Fixed-unit customer orders ──────────────────────────────────────────────────────────────
# A different framing from the day-cadence/profit-maximizing wizard above: sometimes another
# player asks for a FIXED number of finished units (a one-off job), not an ongoing weekly
# routine. An order is persistent (tracked across sessions, not a one-shot calculator) and
# committing to it occupies real reaction slots the same way the suggestion/manual-assign flow
# does — see _allocate_and_insert below, which reuses the exact slot-spreading logic
# _suggest_reactions already has.

@ensure_once
def ensure_reaction_orders_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_orders (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                context_id     INTEGER NOT NULL,
                type_id        INTEGER NOT NULL,
                name           TEXT NOT NULL,
                target_qty     REAL NOT NULL,
                top_level_runs INTEGER NOT NULL,
                assigned_runs  INTEGER NOT NULL DEFAULT 0,
                client_name    TEXT,
                notes          TEXT,
                status         TEXT NOT NULL DEFAULT 'open',
                created_at     REAL NOT NULL
            )
        """)
        con.commit()
        # What the CLIENT pays for the whole order. The one number this tool cannot derive: an
        # order's revenue is whatever was negotiated, not a market price. Without it an order's
        # profit is unknowable, and the dashboard was reporting that as 0 — asserting no profit
        # rather than admitting it did not know. NULL stays "not told", which reads as unknown
        # everywhere and never as zero.
        add_columns(con, "pp_reaction_orders", "client_price DOUBLE PRECISION")
        # A recurring order keeps the same commercial terms but releases one fresh production
        # batch on a fixed rhythm. `recurring_next_at` is the next release boundary; completed
        # cycles stay on the open order and advance this anchor instead of becoming history.
        add_columns(con, "pp_reaction_orders",
                    "recurring_interval_days DOUBLE PRECISION",
                    "recurring_next_at DOUBLE PRECISION",
                    "recurring_error TEXT",
                    "priority INTEGER NOT NULL DEFAULT 0",
                    "source_kind TEXT",
                    "source_order_id INTEGER",
                    "source_ref TEXT",
                    "source_state TEXT",
                    "source_message TEXT")
        con.execute("CREATE INDEX IF NOT EXISTS idx_rx_orders_source "
                    "ON pp_reaction_orders (context_id, source_kind, source_ref, type_id)")
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_order_sources (
                reaction_order_id      INTEGER NOT NULL,
                context_id             INTEGER NOT NULL,
                manufacturing_order_id INTEGER NOT NULL,
                runs                    INTEGER NOT NULL,
                PRIMARY KEY (reaction_order_id, manufacturing_order_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_rx_order_sources_mfg "
                    "ON pp_reaction_order_sources (context_id, manufacturing_order_id)")
        con.commit()
    finally:
        con.close()


@ensure_once
def ensure_reaction_completions_table():
    """Forward-only ledger of FINISHED reaction jobs — one row per ESI job_id, so a completion is
    recorded exactly once however many times the sweep sees it. Shape and machinery are shared with
    manufacturing (app.completions); only the valuation below is reaction-specific."""
    completions.ensure_ledger(LEDGER, "idx_rxcomp_ctx")


def log_reaction_completions(context_id: int) -> int:
    """Record reaction jobs for this context that FINISHED since the last sweep. Scan, dedupe and
    insert are shared (app.completions); this supplies the reaction VALUATION.

    A reachable product is valued through the full recipe roll-up via `_value_reaction_batch`. An
    unreachable one — goo the price sheet doesn't cover — still contributes turnover from raw
    output x price, with net left at 0 rather than overstated from an input cost we can't actually
    compute.

    **Priced at BUY orders (instant sell), not at sell orders.** This ledger is the account's
    *lifetime net profit* — the one figure a player reads as fact rather than forecast, and it
    compounds over every job for the life of the account. CLAUDE.md's pricing invariant applies
    with the most force here: a reaction good's sell-order price is not achievable profit. It was
    quoting `sell_price` until 2026-08-14, which overstated every completed job by the buy/sell
    spread on top of a cost base that charged materials and job fees but not freight or collateral.
    Both are fixed here; `fixed_costs` is the full base (materials + job fees + freight +
    collateral), so net is exactly `_value_reaction_batch`'s `net_profit_instant`.

    Rows already written stay as they were — a completion is recorded once, forward-only, and
    there is no past market price to re-derive. So the ledger heals as new jobs land rather than
    all at once."""
    ensure_industry_jobs_table()
    ensure_reaction_completions_table()
    pending = completions.pending_completions(
        context_id, "pp_char_industry_jobs", LEDGER, ("active", "paused", "ready"))
    if not pending:
        return 0

    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    pi = load_pi_data()
    types = loaded[4] if loaded else pi["types"]
    settings = effective_reaction_settings(context_id)
    con = get_connection()
    try:
        out_qty_by_type = {r["output_type_id"]: r["output_qty"]
                           for r in con.execute("SELECT output_type_id, output_qty FROM reactions")}
    finally:
        con.close()
    prod_ids = list({tid for _, _, tid, _, _ in pending if tid})
    market = resolve_market_data(context_id, prod_ids) if prod_ids else {}

    valued = []
    for jid, cid, tid, runs, end_ts in pending:
        node = reached.get(tid)
        m = market.get(tid) or {}
        sell = m.get("sell_price", 0.0) or 0.0
        # The achievable price: what a buy order pays today. `sell` is still passed because
        # _value_reaction_batch charges courier collateral against the sell value regardless of how
        # the goods are actually sold (the standard freight-collateral reference).
        buy = m.get("buy_price", 0.0) or 0.0
        vol = (types.get(tid, {}) or {}).get("volume", 0.0) or 0.0
        if node and node.get("via") and runs > 0:
            total_out = runs * node["via"]["output_qty"]
            v = _value_reaction_batch(node, total_out, sell_price=sell, volume=vol, settings=settings,
                                      buy_price=buy)
            out_val, inp_cost = v["instant_value"], v["fixed_costs"]
        else:
            out_val = (runs * out_qty_by_type.get(tid, 0.0) * buy) if runs > 0 else 0.0
            inp_cost = 0.0
        valued.append((jid, cid, tid, runs, end_ts, out_val, inp_cost))
    return completions.record_completions(LEDGER, context_id, valued)


def log_all_reaction_completions() -> int:
    """Sweep every context that tracks reaction jobs. Scheduled every 15 min alongside the
    notification check; per-context failures are isolated."""
    ensure_industry_jobs_table()
    return completions.sweep_all("pp_char_industry_jobs", log_reaction_completions, "reaction")


@router.get("/api/reactions/lifetime")
def reactions_lifetime(context_id: int = Depends(require_context)):
    """This account's lifetime reaction ledger: turnover (Σ produced output value), net profit
    (Σ output − materials − job fees), job count, and the earliest logged completion."""
    ensure_reaction_completions_table()
    return completions.lifetime(LEDGER, context_id)


def _tidy_runs_on(context_id: int) -> bool:
    """Gated: rounding buys a little surplus intermediate with real ISK, and it changes the numbers
    on a plan people are already used to reading."""
    return flag_on("reactions_tidy_runs", context_id)


def _level_runs_on(context_id: int) -> bool:
    """Gated: levelling a product across characters is the only pass that CREATES and DELETES plan
    rows, so it changes slot counts as well as numbers. Off, `level_stage_runs` still levels within
    a character with the row count fixed."""
    return flag_on("reactions_level_runs", context_id)


def _insert_assignment_rows(con, character_id: int, type_id: int, name: str, runs: float,
                             job_count: int, input_cost: float, reward: float, tier_order: int,
                             now: float, order_id: int | None = None, tidy: bool = False,
                             cycle_hours: float = 0.0, cadence_h: float = 0.0) -> None:
    """One product's worth of a job commitment, split into `job_count` separate assignment rows
    (one per actual in-game job install — see assign_reaction's own docstring for why). Shared by
    assign_reaction (order_id always None there — no behavior change) and the customer-order
    assign flow (_allocate_and_insert, order_id set) so the row-insertion shape can't drift
    between the two callers.

    `cycle_hours` and `cadence_h` are both REAL wall-clock hours, and passing them is what lets a
    row say it runs past the player's job-length window. An order's rows never reach the leveller
    (its top row is excluded from `inner` by design — the run count is what the order was quoted
    on), so nothing else will ever stamp them: if the breach is not recorded at the moment the row
    is written, it is not recorded at all, and the badge that decision 1 promises never appears on
    the one kind of plan a customer is waiting for. Both default to 0, which records no breach —
    the manual-assign path has no cadence to measure against.

    Measured against `cadence_h + _CADENCE_GRACE`, exactly as the leveller measures its own
    (`level_product_runs` builds `cap_hours_by_tid` with the grace already in it). The grace is the
    slack a player who is not punctual to the minute already absorbs, and every pass that SIZES a
    job spends it deliberately rather than buying another reactor for four minutes. A badge that
    then reported those four minutes as a breach would flag the plan for doing the thing it was
    told to do — and, worse, would mean two surfaces disagreeing about what "over the window" means,
    which is the whole class of defect this repair exists to remove."""
    job_count = max(1, job_count)
    runs_per_job = math.ceil(runs / job_count)
    # `tidy` is passed for INTERMEDIATE steps only. Their output is consumed by the stage above, so
    # a little surplus is stock rather than waste — whereas the top-level product's run count is
    # what the batch's cost, output and profit were all computed from, and moving it would make
    # every one of those figures a lie. See `tidy_runs`.
    if tidy:
        runs_per_job = tidy_runs(runs_per_job)
    # Measured AFTER `tidy_runs`, because rounding a run count up makes the job longer and it is
    # the row's real duration that either fits the window or does not.
    over_h = 0.0
    if cadence_h > 0 and cycle_hours > 0:
        over_h = max(0.0, runs_per_job * cycle_hours - (cadence_h + _CADENCE_GRACE))
    for _ in range(job_count):
        con.execute(
            "INSERT INTO pp_reaction_assignments "
            "(character_id, type_id, name, runs, input_cost, reward, created_at, tier_order, "
            "order_id, cadence_over_h) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (character_id, type_id, name, runs_per_job, input_cost / job_count, reward / job_count,
             now, tier_order, order_id, round(over_h, 2)),
        )


def live_reaction_runs(context_id: int) -> dict[tuple[int, int], list[int]]:
    """Every job this account has RUNNING in game, as (character_id, product_type_id) -> the run
    count each of those jobs carries, in the order ESI reported them.

    Opens its own connection, so it must be called BEFORE the request connection in
    `get_industry_jobs` is opened, never inside it — see the two-connections rule there.

    Count-aware on purpose: a product can hold several plan rows on one character (one per slot),
    and only as many of them are covered as there are jobs actually running. `len(v)` is that count;
    the list itself is what lets a row adopt the run count really installed rather than the one the
    plan proposed.
    """
    con = get_connection()
    try:
        cached = {r["character_id"]: _json.loads(r["jobs_json"] or "[]")
                  for r in con.execute(
                      "SELECT j.character_id, j.jobs_json FROM pp_char_industry_jobs j "
                      "JOIN pp_characters c ON c.character_id = j.character_id "
                      "WHERE c.context_id = ?", (context_id,))}
    finally:
        con.close()
    out: dict[tuple[int, int], list[int]] = {}
    for cid, jobs in cached.items():
        for job in jobs or []:
            if (job.get("status") or "").lower() in ("active", "paused", "ready"):
                out.setdefault((int(cid), int(job.get("product_type_id") or 0)),
                               []).append(int(job.get("runs") or 0))
    return out


def _plan_rows(context_id: int, cols: str) -> list[dict]:
    """Every plan row this account owns, with whichever columns the caller needs.

    The three levellers/repairs below all open with the same read — the JOIN through pp_characters
    is what scopes the plan to the account (CLAUDE.md: scope every per-user query by context_id).
    An unreadable table returns [], and every caller treats that the same way it treats an empty
    plan: nothing to do.
    """
    con = get_connection()
    try:
        return [dict(r) for r in con.execute(
            f"SELECT {cols} "
            "FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id=?", (context_id,))]
    except Exception:
        return []
    finally:
        con.close()


class ChainTier(BaseModel):
    type_id: int
    name: str
    runs: int
    job_count: int = 1
    # Which STAGE this step belongs to — steps sharing one have no dependency on each other and run
    # side by side. Optional so an older client (or a cached suggestion) still assigns; absent, the
    # server derives it from the graph, and only failing that falls back to list position, which is
    # what used to be the only rule and is what serialised siblings.
    tier: int | None = None


def restage_plan_rows(context_id: int) -> int:
    """Re-derive `tier_order` for plan rows written under the old "position in the list is the
    stage" rule. Returns how many rows moved.

    Every insert path used `enumerate(...)`, so three steps that run at the same time were stamped
    stages 0/1/2 — the dashboard greyed two of them out as "wait for the one above", and
    `_concurrent_load` counted three simultaneous jobs as one reactor. The rows already in the
    table say that, and nobody should have to clear their plan to get the truth back.

    Idempotent and cheap after the first pass: a row is only rewritten when the graph disagrees
    with what is stored, so a repaired account does no writes and the graph is only loaded when
    there is a row that could be wrong. No graph (unpriced/unreachable) means no repair — the
    stored value stands rather than being replaced by a guess.
    """
    ensure_reaction_assignments_table()
    rows = _plan_rows(context_id, "a.id, a.character_id, a.type_id, a.created_at, COALESCE(a.tier_order,0) AS tier_order")
    if not rows:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["character_id"], round(float(r["created_at"] or 0.0), 3)), []).append(r)
    # Only a group holding several DISTINCT stages can be mis-staged; a flat group is already right.
    suspect = [g for g in groups.values() if len({r["tier_order"] for r in g}) > 1]
    if not suspect:
        return 0
    try:
        loaded = _load_goo_and_reached(context_id)
        reached = loaded[1] if loaded else {}
    except Exception:
        reached = {}
    if not reached:
        return 0

    fixed = 0
    con = get_connection()
    try:
        for g in suspect:
            depths = {r["id"]: int((reached.get(int(r["type_id"])) or {}).get("depth") or 0)
                      for r in g}
            if not all(depths.values()):
                continue                      # a step we cannot place — leave the whole group alone
            order = {d: i for i, d in enumerate(sorted(set(depths.values())))}
            for r in g:
                want = order[depths[r["id"]]]
                if want != r["tier_order"]:
                    con.execute("UPDATE pp_reaction_assignments SET tier_order=? WHERE id=?",
                                (want, r["id"]))
                    fixed += 1
        if fixed:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return fixed


# ── Marking a reaction running or done by hand (`reactions_manual_done`) ───────────────────────
# ESI is the signal for what is running and what has landed, and it is right nearly always. The
# exceptions are the ones that strand a player: the job cache is up to five minutes stale, a job
# installed under a different product than planned matches nothing, and a chain reacted before this
# tool ever saw it has no jobs to read. In every one of those the page says "after stage 1
# finishes" about a stage that finished an hour ago, and there is no way to say otherwise.
#
# So the same three states Industry has, on the same terms (`app/industry/progress.py`): a mark is
# a FLOOR under what was observed, never a replacement for it — `chain_stage_state` takes the
# higher of the two — so a tick can bring a stage forward and can never hide a job ESI can see.
_RX_ALL = -1                       # "all the jobs of this group", whatever the plan says today
_RX_DONE, _RX_RUNNING = "done", "running"
_RX_STATES = (_RX_RUNNING, _RX_DONE)


def _manual_done_on(context_id: int) -> bool:
    return flag_on("reactions_manual_done", context_id)


@ensure_once
def ensure_reaction_manual_done_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_reaction_manual_done (
                context_id   INTEGER NOT NULL,
                character_id INTEGER NOT NULL,
                type_id      INTEGER NOT NULL,
                tier_order   INTEGER NOT NULL DEFAULT 0,
                jobs         INTEGER NOT NULL DEFAULT -1,
                state        TEXT    NOT NULL DEFAULT 'done',
                marked_at    REAL    NOT NULL,
                PRIMARY KEY (context_id, character_id, type_id, tier_order)
            )
        """)
        con.commit()
    finally:
        con.close()


def _assert_owns_character(context_id: int, character_id: int) -> None:
    """403 unless the character belongs to this account — CLAUDE.md rule 8. A mark is per
    character, so the id arrives from the client and has to be checked like any other."""
    con = get_connection()
    try:
        owner = con.execute("SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
                            (int(character_id), context_id)).fetchone()
    finally:
        con.close()
    if not owner:
        raise HTTPException(status_code=403, detail="Not your character")


def _manual_key(character_id, type_id, tier_order) -> tuple[int, int, int]:
    """(character, product, stage) — the plan's own grouping, and deliberately NOT the row id.

    A row id does not survive the levelling passes: `level_product_runs` re-splits a product's work
    and DELETES and re-inserts rows to do it, so a mark keyed on one would silently detach from the
    job it was about. This triple is what the dashboard groups by, what the pipeline draws a card
    for, and what the player is actually pointing at when they tick something."""
    return (int(character_id), int(type_id), int(tier_order or 0))


def reaction_manual_marks(context_id: int) -> dict[tuple[int, int, int], tuple[int, str]]:
    """{(character, product, stage): (jobs, state)} the player has marked by hand."""
    ensure_reaction_manual_done_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, type_id, tier_order, jobs, state FROM pp_reaction_manual_done "
            "WHERE context_id=?", (context_id,)).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {_manual_key(r["character_id"], r["type_id"], r["tier_order"]):
            (int(r["jobs"]), str(r["state"] or _RX_DONE)) for r in rows}


def manual_jobs(marks: dict, character_id: int, type_id: int, tier_order: int,
                steps: int, state: str = _RX_DONE) -> int:
    """How many of a group's `steps` jobs are hand-marked into `state`, resolved against the plan.

    A group carries at most ONE mark, so asking for `done` on a group marked `running` is 0 — the
    states are alternatives, not a ladder. `_RX_ALL` means "however many the plan holds today",
    which is what keeps a mark meaningful after the leveller re-splits the work into more jobs."""
    m = marks.get(_manual_key(character_id, type_id, tier_order))
    if m is None or m[1] != state:
        return 0
    return steps if m[0] == _RX_ALL else max(0, min(m[0], steps))


def set_reaction_manual(context_id: int, character_id: int, type_id: int, tier_order: int,
                        jobs: int | None, state: str = _RX_DONE) -> None:
    """Mark a group (`jobs=None` → all of it, or a count) or clear it (`jobs=0`).

    One row per group, so setting either state replaces the other — which is what makes the
    not started → running → done → not started click cycle one write each time instead of a pair
    of half-states that can disagree."""
    ensure_reaction_manual_done_table()
    if state not in _RX_STATES:
        state = _RX_DONE
    cid, tid, tier = _manual_key(character_id, type_id, tier_order)
    con = get_connection()
    try:
        if jobs is not None and jobs <= 0:
            con.execute("DELETE FROM pp_reaction_manual_done WHERE context_id=? AND character_id=? "
                        "AND type_id=? AND tier_order=?", (context_id, cid, tid, tier))
        else:
            con.execute(
                "INSERT INTO pp_reaction_manual_done (context_id, character_id, type_id, "
                "tier_order, jobs, state, marked_at) VALUES (?,?,?,?,?,?,?) "
                "ON CONFLICT(context_id, character_id, type_id, tier_order) DO UPDATE SET "
                "jobs=excluded.jobs, state=excluded.state, marked_at=excluded.marked_at",
                (context_id, cid, tid, tier, _RX_ALL if jobs is None else int(jobs), state,
                 _time.time()))
        con.commit()
    finally:
        con.close()


class ReactionMarkRequest(BaseModel):
    character_id: int
    type_id: int
    tier_order: int = 0
    state: str = _RX_DONE
    jobs: int | None = None            # None = the whole group; 0 clears the mark


@router.post("/api/reactions/mark")
def mark_reaction(req: ReactionMarkRequest, context_id: int = Depends(require_context)):
    """Mark a planned reaction running or done by hand, or clear the mark.

    Like the Industry equivalent it writes nothing to the completion ledgers: those feed lifetime
    turnover and profit, and a tick is a statement about this plan's progress, not evidence of an
    ISK-bearing job that really ran."""
    if not _manual_done_on(context_id):
        raise HTTPException(status_code=404, detail="Not enabled")
    _assert_owns_character(context_id, req.character_id)
    set_reaction_manual(context_id, req.character_id, req.type_id, req.tier_order,
                        req.jobs, req.state)
    _invalidate_dashboard_cache(context_id)
    return {"ok": True}


def chain_stage_state(rows: list[dict], jobs: list[dict], now: float,
                      marks: dict | None = None) -> list[dict]:
    """Per-chain stage progress for one character: `[{stage, steps, done, running, todo, ready,
    names}]`, one entry per stage of each chain the character is holding.

    **What makes a stage DONE is a finished job, read from ESI.** An industry job the game reports
    as `ready` (finished, output not collected) or `delivered` (collected), or whose `end_date` has
    passed, is work that is over — that is the signal the player asked for, and it needs no button.
    A job still `active`/`paused` is in progress; a planned row with no job at all is not started.

    **A stage is READY when every stage below it in its own chain is done.** Stage 1 is always
    ready. This is the answer to "can I start the next lot yet" — before this, the dashboard could
    only say "after stage 1 finishes" and leave the player to work out whether it had.

    Chains are grouped the way every other read groups them: the assign that wrote them
    (`created_at`), so two separate plans on one character don't gate each other.
    """
    done_types: dict[int, int] = {}
    live_types: dict[int, int] = {}
    for j in jobs:
        tid = j.get("product_type_id")
        if not tid:
            continue
        status = (j.get("status") or "").lower()
        finished = status in ("ready", "delivered")
        if not finished and status in ("active", "paused"):
            # ESI's own `end_date` is an ISO string (same parse the countdown uses below). A job
            # past its end date is finished whatever the cached status says — the cache is up to
            # five minutes stale, and "is stage 1 done" should not wait on a refresh.
            end = j.get("end_date")
            try:
                finished = bool(end) and datetime.fromisoformat(
                    str(end).replace("Z", "+00:00")).timestamp() <= now
            except Exception:
                finished = False
        (done_types if finished else live_types)[tid] = \
            (done_types if finished else live_types).get(tid, 0) + 1

    out: list[dict] = []
    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r.get("character_id"), round(float(r.get("created_at") or 0.0), 3)),
                          []).append(r)
    for g in groups.values():
        by_stage: dict[int, list[dict]] = {}
        for r in g:
            by_stage.setdefault(int(r.get("tier_order") or 0), []).append(r)
        lower_all_done = True
        for stage in sorted(by_stage):
            steps = by_stage[stage]
            done = running = 0
            for r in steps:
                tid = int(r["type_id"])
                if done_types.get(tid, 0) > 0:
                    done_types[tid] -= 1
                    done += 1
                elif live_types.get(tid, 0) > 0:
                    live_types[tid] -= 1
                    running += 1
            # A hand mark is a FLOOR under what ESI showed, per (character, product, stage), never a
            # replacement for it: the higher of the two wins, exactly as `resolve_done` does on the
            # Industry side. So a tick can bring a stage forward when the job cache is stale or the
            # job was installed outside this tool, and can never hide work ESI can actually see.
            if marks:
                by_product: dict[int, list[dict]] = {}
                for r in steps:
                    by_product.setdefault(int(r["type_id"]), []).append(r)
                m_done = m_run = 0
                for tid, rs in by_product.items():
                    cid = rs[0].get("character_id")
                    m_done += manual_jobs(marks, cid, tid, stage, len(rs), _RX_DONE)
                    m_run += manual_jobs(marks, cid, tid, stage, len(rs), _RX_RUNNING)
                done = min(len(steps), max(done, m_done))
                running = min(len(steps) - done, max(running, m_run))
            entry = {
                "chain": round(float(steps[0].get("created_at") or 0.0), 3),
                "stage": stage, "steps": len(steps), "done": done, "running": running,
                "todo": len(steps) - done - running,
                # Startable now: nothing below it is unfinished. The first stage always qualifies.
                # A pooled plan needs a second condition on top of this one — see
                # `_gate_stages_account_wide`, applied by the caller.
                "ready": lower_all_done,
                "names": sorted({str(r.get("name") or r["type_id"]) for r in steps}),
            }
            out.append(entry)
            lower_all_done = lower_all_done and done == len(steps)
    return out


def level_stage_runs(context_id: int) -> int:
    """Give every job of ONE product, in ONE stage, on ONE character the SAME run count. Returns
    how many rows changed.

    The complaint this exists for: "for Carbon Fibers I see 125 runs, 90 runs, 75 runs — it's all
    over the place". Those are three separate assigns, each of which sized its own chain's Carbon
    Fiber requirement exactly and correctly. Nothing was wrong with any one of them; what was wrong
    was reading three numbers off the screen and typing three different values into three
    consecutive jobs on the same character.

    **Why levelling them is sound and not a fudge:** the product is fungible. Carbon Fiber made for
    one chain is Carbon Fiber, it lands in the same hangar, and the stage above draws from the pool
    rather than from a particular job. So only the TOTAL for a (character, stage, product) has to
    hold, and how it is split across that product's jobs is ours to choose. The total is preserved,
    or rounded UP when the split doesn't divide evenly — never down, which would leave the stage
    above short.

    Deliberately conservative about everything else:

      * **row count is untouched**, so no slot is claimed or released and the capacity every other
        part of this package computed still holds;
      * **rows keep their own chain and order**, so `_shopping_roots`, `chain_stage_state` and the
        per-order give-back all still see exactly the plans they saw before;
      * it only writes when the numbers actually differ, so it is idempotent and free on a plan
        that is already level.
    """
    ensure_reaction_assignments_table()
    tidy = _tidy_runs_on(context_id)
    rows = _plan_rows(context_id, "a.id, a.character_id, a.type_id, a.runs, COALESCE(a.tier_order,0) AS tier_order")
    if not rows:
        return 0

    groups: dict[tuple, list[dict]] = {}
    for r in rows:
        groups.setdefault((r["character_id"], r["tier_order"], r["type_id"]), []).append(r)

    changed = 0
    con = get_connection()
    try:
        for g in groups.values():
            if len(g) < 2:
                continue
            total = sum(int(x["runs"] or 0) for x in g)
            per = -(-total // len(g))               # ceil: never leave the stage above short
            if tidy:
                per = tidy_runs(per)
            if all(int(x["runs"] or 0) == per for x in g):
                continue                            # already one number — nothing to write
            for x in g:
                con.execute("UPDATE pp_reaction_assignments SET runs=? WHERE id=?", (per, x["id"]))
                changed += 1
        if changed:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return changed


# ── One run count per product, across every character (`reactions_level_runs`) ─────────────────
#
# `level_stage_runs` above levels a product WITHIN one character, which is as far as it can go
# without touching the row count. The complaint that outlives it is the cross-character one:
# Carbon Fiber at 125 runs on one character, 90 on the next two and 75 on the fourth, and the same
# shape on every other product. Four numbers to read, four to type, four different finish times.
#
# The priority order is the user's own (TODO 28), and it governs strictly:
#   1. ONE run count per product — every job of it, wherever it sits.
#   2. Aligned end times — a stage lands in one go, one login collects it.
#   3. Fewer slots — the same work in fewer, fuller jobs.
# How much surplus a levelled product may produce. Deliberately WIDER than the 15% `tidy_runs`
# works to, and the reason is the user's, stated plainly: *"it's fine to build a bit too much if it
# doesn't line up."* A budget tight enough to make rounding free is too tight to make a stage land
# together — 35 runs of Oxy-Organic Solvents beside 18 has no common count inside 15% at all, so
# the product kept two numbers, which quietly ranked "cheap" above "one number" and inverted the
# priority list. Surplus is stock, and the next plan spends it (`reactions_use_stock`).
#
# What stops it running away is not the budget but WHEN it is allowed to be spent: see
# `_choose_stage_layout`, which pays surplus only to land a stage together and otherwise takes the
# cheapest count there is.
_LEVEL_BUDGET = 0.50
# A stage counts as landing together inside this much of its own longest job. Exact equality is
# not reachable — run counts are integers and cycle times differ — and a stage whose jobs finish
# within a few percent of each other is one login.
_ALIGN_TOL = 0.10
# A job that runs 7 days and 7 hours is worse than one that runs 6 days and 23: the player comes
# back on a whole-day rhythm, finds it unfinished, and every cycle after that slips a few more
# hours. So a duration that lands just UNDER a day boundary is worth preferring over one that
# steps just past it. Bucketed rather than minimised outright — chasing the last hour would spend
# real goo for nothing a player can feel.
_CADENCE_H = 24.0
_CADENCE_GRACE = 3.0
# What the stage solve lets `_level_options` OFFER, before `_stage_affordable` judges the layout as
# a whole. Deliberately far wider than the real budget: a small product's own requirement is the
# wrong yardstick for a run count the whole stage shares, and a candidate that never gets offered
# can never be weighed against what it buys.
_STAGE_SCAN_BUDGET = 20.0


def _typeable(runs: int) -> bool:
    """Is this a run count you copy, or one you read twice? Small numbers are fine as they are, and
    above that it is a round multiple of one of `tidy_runs`' own steps — 70, 125, 250.

    The step has to be worth something at that size — 68 is a multiple of 2 and is not a round
    number, so a step only counts while it is at least a twentieth of the number itself. 70, 125
    and 375 pass; 68 and 67 do not.

    Deliberately NOT `tidy_runs(r) == r`, which asks a different question: whether r would be
    rounded up FURTHER (70 becomes 75 inside a 15% budget). That says nothing about whether 70 is
    easy to type, and it is."""
    runs = int(runs)
    return runs < 10 or any(runs % s == 0 and s * 20 >= runs for s in _TIDY_STEPS)


def _level_options(total: int, cap: int, max_runs: int, budget: float = _LEVEL_BUDGET,
                    min_runs: int = 0, extra: list[int] | None = None) -> list[dict]:
    """Every run count ONE product could carry on EVERY one of its jobs, and what each costs.

    `total` is the ACCOUNT's requirement for this product in this stage and `cap` the most jobs the
    stage may spend on it. Both are pooled: a product's output lands in a shared hangar, so which
    character makes a given run is a placement question (`level_product_runs` answers it) and not
    something the run count has to respect. What is free to choose here is the SPLIT: jobs of one
    number, `ceil(total/runs)` of them.

    Returned per candidate: the run count, how many jobs it costs, the surplus it produces (rounding
    up is the only direction that is safe — the stage above consumes this one), and whether the
    number is one a human can type without checking.

    `extra` seeds counts from OUTSIDE this product — the durations the rest of its stage is
    considering. Without it a product can only ever propose counts derived from its own
    requirement, so Oxy-Organic Solvents (35 runs needed, candidates topping out at 100) could not
    reach the 120 its stage-mates were settling on however clearly that was the right answer. A
    product cannot join a stage it is not allowed to name.

    Two things get a candidate dropped: more jobs than `cap`, and more than `budget` overshoot. Both
    are a loose sanity range when this is called from the stage solve, which passes a far wider
    budget and judges the real cost across the whole stage instead (`_stage_affordable`) — a small
    product's own requirement is the wrong yardstick for a run count the whole stage shares.

    **`max_runs` is a target, not an absolute** (decision of 2026-08-14). Options that fit under it
    are returned alone whenever any exist. Only when none are affordable does the last-resort branch
    run, and it weighs the over-ceiling counts and the always-available floor TOGETHER, returning
    whichever breaches least — which is often nothing at all. Every option carries `over_runs`, how
    far past the ceiling it reaches, so nothing here can exceed the ceiling silently and nothing
    claims a breach it did not have to make.

    That distinction used to be a truth-table bug rather than a policy: `r > max_runs and
    r > min_runs` let exactly ONE candidate (`r == min_runs`) through when `min_runs > max_runs` and
    dropped every larger one, so the option set collapsed to a single answer whatever duration it
    implied, and the caller could not tell it had happened.

    This is the RANGE of what is affordable, not a recommendation — `_choose_stage_layout` decides
    which of these is worth paying for.
    """
    total = int(total)
    cap = max(1, int(cap))
    if total <= 0:
        return []
    # Every run count that divides the requirement into a whole number of jobs, plus the tidy
    # rounding of each — the only counts worth considering, since anything between two of them
    # produces the same job layout for strictly more surplus.
    cands: set[int] = {int(e) for e in (extra or []) if e and int(e) > 0}
    for j in range(1, cap + 1):
        r = max(1, -(-total // j))
        cands.add(r)
        # ...and the round number just above each, one per step of the tidy ladder, so a layout
        # that costs the same in slots can still be one you copy rather than read.
        for step in _TIDY_STEPS:
            cands.add(-(-r // step) * step)
    out: list[dict] = []
    over: list[dict] = []               # the same options, but past the ceiling — used only if
                                        # `out` is empty, and each says by how much it overruns
    for r in sorted(cands):
        # `min_runs` is the caller giving ground: the stage solve raises it when the stage was
        # promised more reactors than it has, since a longer run count is fewer jobs.
        if r < max(1, min_runs):
            continue
        jobs = max(1, -(-total // r))
        if jobs > cap:
            continue                        # more reactors than the stage actually has
        made = jobs * r
        if made - total > total * budget:
            continue                        # tidy numbers are not worth paying for in goo
        opt = {"runs": r, "jobs": jobs, "surplus": made - total, "tidy": _typeable(r),
               # Runs past the ceiling. In the SAME space `max_runs` was computed in — the caller
               # derived it from a cap in raw-SDE hours (`level_product_runs`, Step 5), so this is
               # a raw-SDE run count and the caller converts it to real hours to report it.
               "over_runs": max(0, r - max_runs)}
        (out if r <= max_runs else over).append(opt)
    if out:
        return out
    # Nothing inside the ceiling was AFFORDABLE. Before giving ground, add the one count that is
    # always available whatever it costs in surplus: the SMALLEST the work can be split into with
    # the reactors there are. A product showing three numbers is the thing this pass exists to
    # remove, and this floor is what makes "one number per product" a property of the pass rather
    # than something it manages when the arithmetic is kind. It ignores `budget` on purpose — that
    # is what "last resort" means — and it may or may not be over the ceiling.
    floor = max(1, min_runs, -(-total // cap))
    f_jobs = max(1, -(-total // floor))
    pool = list(over)
    if all(o["runs"] != floor for o in pool):
        pool.append({"runs": floor, "jobs": f_jobs, "surplus": f_jobs * floor - total,
                     "tidy": _typeable(floor), "over_runs": max(0, floor - max_runs)})
    # Then take the LEAST-breaching counts of everything on the table — ties included, nothing
    # worse. Two ways this ordering matters, and both were found the hard way:
    #
    #   * The floor has to be weighed HERE, not after `over`. Trying `over` first and only falling
    #     through to the floor when it was empty meant a floor that fits UNDER the ceiling never got
    #     considered once any over-ceiling option existed — a spurious breach badge on a row where a
    #     within-ceiling layout was available. Narrow (it needs the give-ground loop to have raised
    #     `min_runs` to within a run or two of `max_runs`) but real: ~32k of 413k swept parameter
    #     combinations, overrunning by 1-13 runs. Decision 1 says a stage that CAN be split finer to
    #     fit is, so a breach that had an alternative is a lie about the plan.
    #   * Least-breaching, not the whole set. The stage solve ranks fewest-jobs first, so handing it
    #     everything over the ceiling would make it take the longest job on offer: a 6,000-run
    #     requirement that missed by 37% came back as ONE job of 6,000 rather than 44 of 137.
    #     Missing a target is not permission to abandon it.
    least = min(o["over_runs"] for o in pool)
    return [o for o in pool if o["over_runs"] == least]



def _collection_slot(hours: float, bucket_h: float = _CADENCE_H) -> int:
    """Which SESSION the player actually COLLECTS this job in. `hours` is real wall-clock hours.

    A job is collected on a login, so what a run count really costs in time is not its duration but
    the session it lands in. 6d23h and 7d00h18m are both collected in the same session under a
    weekly rhythm — `_CADENCE_GRACE` is the slack a player who is not punctual to the minute
    already absorbs — while 7d07h waits for the next one and the reactor idles until then.

    **`bucket_h` is the player's own rhythm, not a calendar.** The rhythm is a DURATION of their
    choosing — *"nothing longer than N days"* — with no weekday, no time of day and no timezone
    (decision of 2026-08-14). So the bucket is the account's cadence when it has set one, and 24h
    only as the fallback when it has not. Bucketing a weekly player by the day was modelling a
    rhythm they do not have: under a 7-day cadence every layout that respects the ceiling is
    collected in the same session anyway, and where the daily bucket disagreed it was actively
    harmful — `slot` outranks both `surplus` and `untidy` in `_choose_stage_layout`'s score, so the
    solver would pay real goo to finish on a day the player does not log in.

    Scored ascending: a layout collected in an earlier session wins.

    **The point is as much what it does NOT do.** This replaced a 0/1 "does it land just under a
    boundary" flag (2026-08-14), which read 7d00h18m as no better than 7d16h and would then pay to
    stretch a job from 7d00h to 7d23h to score the point — real goo for a job collected on the same
    login. Once two counts share a session, this is equal and the surplus term below decides.
    """
    if hours <= 0:
        return 0
    bucket = bucket_h if bucket_h and bucket_h > 0 else _CADENCE_H
    return math.ceil(max(0.0, hours - _CADENCE_GRACE) / bucket)





def _override_time_mult(context_id: int) -> float:
    """The account's TYPED time-efficiency override as a time multiplier, or 0.0 when none is set.

    One number, one clock. `time_efficiency_pct` reduces the graph's `cycle_time` at load
    (`app/reactions/graph.py`), and this is the same value expressed the way the leveller's
    raw-SDE space wants it: `mult = 1 - pct`. Both sides of the app therefore quote the same
    duration for the same job, which is the entire point of the 2026-08-14 repair.

    Returns a MULTIPLIER on raw SDE time — dimensionless, so it converts real hours to raw-SDE
    hours by division exactly as the measured multiplier does. It does not itself belong to either
    space; every caller states which one it is in.

    0.0 means "nothing typed, use the measurement". Never fatal: a settings read that cannot reach
    the DB falls back to the measurement, which is the old behaviour."""
    try:
        s = effective_reaction_settings(context_id)
        if s.get("time_efficiency_source") != "override":
            return 0.0
        pct = float(s.get("time_efficiency_pct") or 0.0)
        mult = 1.0 - pct
        return mult if 0.01 <= mult < 1.0 else 0.0
    except Exception:
        return 0.0


def _reaction_time_mult(context_id: int, _derive: bool = True) -> float:
    """What a reaction job REALLY takes, as a fraction of its SDE cycle time — MEASURED.

    `_reaction_cycle_times` returns the raw SDE number and says, correctly, that the bonus is not
    applied because everything there compares durations against each other — a factor common to
    every product cancels out of a comparison. **It does not cancel out of a comparison with seven
    days.** The cadence ceiling was the first absolute consumer of those durations and it sized
    every job against a clock running 2.1x slow: one run of Carbon Fiber is 1h24m14s in game against
    the SDE's 3h, so a 7-day ceiling allowed 56 runs where the truth is 119, and the job count came
    out roughly double.

    **Read off real jobs, not derived from settings.** ESI gives every job a `duration` and a run
    count, so the ratio is a measurement rather than a reconstruction of hull, rigs, security and
    skills. The derivation was tried and is wrong: `struct_time_pct` is the INDUSTRY facility's
    number (62% on the reported account) and reactions run in a different structure entirely
    (44.9% there), which would have allowed 173-run jobs — 10 real days against a 7-day ceiling. A
    measurement cannot drift away from the structure the player actually reacts in.

    The median across jobs, so one oddity cannot move it, and only jobs whose product has a known
    SDE cycle.

    **The fallback errs SHORT, deliberately.** With nothing observed it falls back to the skills
    multiplier alone, which under-claims the bonus: a smaller claimed bonus means a bigger apparent
    job, which means FEWER runs allowed and a job that lands inside the ceiling. Over-claiming is
    the direction that breaks the promise the cadence makes.

    **An explicit `time_efficiency_pct` override outranks the measurement** — but only on the
    `_derive` path. The typed field's whole remaining purpose is to say *"my clock is not what you
    measured"*, and honouring it in the graph while the leveller kept measuring re-created the
    two-clocks defect in the one place a user reached for deliberately: a typed 53.2% gave the graph
    0.468 and the leveller 0.85, a 1.82x disagreement, so the plan was split ~1.8x finer than the
    window the account had just said it could fill. `_derive=False` means "measurement only" and is
    the path `derived_time_efficiency` reads, so the override is deliberately invisible there —
    that is what keeps the settings resolver from consulting itself.
    """
    if _derive:
        mult = _override_time_mult(context_id)
        if mult:
            return mult
    cyc = _reaction_cycle_times()
    mults: list[float] = []
    try:
        con = get_connection()
        try:
            rows = con.execute(
                "SELECT j.jobs_json FROM pp_char_industry_jobs j JOIN pp_characters c "
                "ON c.character_id = j.character_id WHERE c.context_id = ?", (context_id,)).fetchall()
        finally:
            con.close()
        for r in rows:
            for job in (_json.loads(r["jobs_json"]) or []):
                runs = int(job.get("runs") or 0)
                raw = cyc.get(int(job.get("product_type_id") or 0), 0.0)
                dur = float(job.get("duration") or 0.0) / 3600.0
                if runs > 0 and raw > 0 and dur > 0:
                    m = (dur / runs) / raw
                    if 0.01 <= m <= 1.0:        # a bonus, never a penalty
                        mults.append(m)
    except Exception:
        mults = []
    if mults:
        mults.sort()
        measured = mults[len(mults) // 2]
        _remember_time_mult(context_id, measured)
        return measured
    # Nothing running RIGHT NOW is the normal state between cycles, not an absence of evidence —
    # reactors are idle exactly when the player is about to re-plan, which is when this is asked.
    # Measuring only from live jobs made the ceiling flip between 119 and 65 runs depending on
    # whether anything happened to be cooking, so the last real measurement is kept and reused.
    remembered = _remembered_time_mult(context_id)
    if remembered:
        return remembered
    # Never measured, and nothing remembered — a brand new account, which is the case that matters
    # most: *"I want it to be good in the first suggestion for a user, not when they've started some
    # jobs... if they start jobs from the suggestion it'll be either wrong, or they have done all
    # the work manually."* That is right, and this fallback does NOT yet answer it.
    #
    # Deriving the structure bonus was tried TWICE and over-claimed both times, which is the
    # direction that breaks the cadence's promise:
    #   * `struct_time_pct` is the MANUFACTURING facility's number — 62% where reactions get 44.9%.
    #   * `build_structures()[].rx_bonus.te` is the structure's BEST case, "what it gives a job its
    #     rigs actually cover" (app/markets.py) — 67% on a Tatara whose Carbon Fiber jobs get 44.9%.
    #     It would have allowed 199-run jobs: 11.6 real days against a 7-day ceiling.
    #   * routing per produced-type group through `app.industry.structures.route_job` — the call the
    #     rest of the app prices a job with, and still 67% against that measured 44.9%.
    # So skills alone: it under-claims, which makes jobs look longer, which allows fewer runs and
    # lands INSIDE the window. Too many short jobs is a bad suggestion; a job that overruns the
    # cadence is a broken promise. See `reaction_time_mult_for` for the full account.
    if not _derive:
        return 0.0                  # caller wants measurement only, and there is none
    return _reaction_skill_mult(context_id)


def _remembered_time_mult(context_id: int) -> float:
    """The last measured reaction time multiplier for this account, or 0.0 if never measured."""
    try:
        from app.industry.settings import ensure_industry_settings_table
        ensure_industry_settings_table()
        con = get_connection()
        try:
            r = con.execute("SELECT reaction_time_mult FROM pp_industry_settings WHERE context_id=?",
                            (context_id,)).fetchone()
        finally:
            con.close()
        v = float((r and r["reaction_time_mult"]) or 0.0)
        return v if 0.01 <= v <= 1.0 else 0.0
    except Exception:
        return 0.0


def _remember_time_mult(context_id: int, mult: float) -> None:
    """Persist a measurement so an idle account keeps planning to the rate it really reacts at."""
    try:
        from app.industry.settings import ensure_industry_settings_table
        ensure_industry_settings_table()
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_industry_settings (context_id, reaction_time_mult) VALUES (?,?) "
                "ON CONFLICT(context_id) DO UPDATE SET reaction_time_mult=excluded.reaction_time_mult",
                (context_id, float(mult)))
            con.commit()
        finally:
            con.close()
    except Exception:
        pass



def reaction_time_mult_for(context_id: int, type_id: int | None = None) -> float:
    """The time multiplier for ONE product: the account's typed override if it set one, else
    measured if we have ever seen a job, else skills alone.

    **This is the chokepoint every duration in the package resolves through**, which is why the
    override is applied HERE rather than in each caller: `level_product_runs`' per-product ceiling,
    `split_order_tops_to_cadence`'s per-job cap and the graph's own `cycle_time` then all quote one
    clock. Honouring the typed field in the graph alone was the two-clocks defect surviving in the
    one path a user reached for deliberately — a typed 53.2% gave the graph 0.468 against the
    leveller's 0.85 and split every plan ~1.8x finer than the window the account had just said it
    could fill.

    `type_id` is accepted because the bonus really is per PRODUCT — the rig covers a group, not a
    hangar — but nothing here reads it yet: there is no per-product source that has survived being
    checked against a measurement.

    **Deriving the structure bonus has been tried three times and over-claimed every time**, always
    in the direction that overruns the cadence: `struct_time_pct` is the MANUFACTURING facility's
    number (62% where reactions get 44.9%); `build_structures()[].rx_bonus.te` is the structure's
    best case over any rig it carries (67% on a Tatara whose Carbon Fiber jobs get 44.9%); and
    routing through `app.industry.structures.route_job` per produced-type group — the same call the
    planner costs a job with, so the most faithful of the three — returned that same 67% against a
    measured 44.9%, which would size a 7-day job at 199 runs, i.e. 11.6 real days.

    Whether the app or the account's saved rig config is wrong is not knowable from here, and the
    asymmetry decides it: under-claiming costs a suggestion too many short jobs, over-claiming
    quietly breaks the promise the cadence exists to make. So skills alone. Anything proposed here
    has to be validated against `_reaction_time_mult` on an account that has really reacted BEFORE
    it is wired in — that comparison is what retired the routed attempt (removed 2026-08-14).
    """
    override = _override_time_mult(context_id)
    if override:
        return override
    measured = _reaction_time_mult(context_id, _derive=False)
    if measured:
        return measured
    return _reaction_skill_mult(context_id)


def _reaction_skill_mult(context_id: int) -> float:
    try:
        from app.industry.graph import account_industry_time_mults
        rx = account_industry_time_mults(context_id)[1]
        if rx and 0 < float(rx) <= 1.0:
            return float(rx)
    except Exception:
        pass
    return 1.0


def _reaction_cadence_hours(context_id: int) -> float:
    """The longest one reaction job should run, in hours — weekly when no value was saved.

    **One STORED number, two owners.** The value is `max_reaction_job_days` in
    `pp_industry_settings` — the same row Build rules → "Longest reaction job" writes and
    `app/industry/graph.py:1177` reads. That sharing is right and stays: the cadence is a fact about
    the player's week, not about a tab, and duplicating the storage is how two surfaces come to
    disagree about the same rhythm.

    What was wrong was the GATE, not the storage. This read `industry_job_length_policy` alone, so
    the headline requirement of the Reactions tool — *"schedule my jobs on a Saturday and handle the
    next stage a week later"* — was switched off by a decision taken about Manufacturing, and a
    reactions-only account could not reach it at all. `reactions_cadence` is this side's own key to
    the same setting; either flag opens it, neither one owns the number.

    The suggestion engine already falls back to weekly. The leveller must use that same fallback or
    the first dashboard read changes a weekly plan into longer jobs.
    """
    try:
        if not (flag_on("reactions_cadence", context_id)
                or flag_on("industry_job_length_policy", context_id)):
            return 0.0
        from app.industry.settings import (DEFAULT_REACTION_JOB_DAYS,
                                           get_max_reaction_job_days)
        return max(0.0, float(get_max_reaction_job_days(context_id)
                              or DEFAULT_REACTION_JOB_DAYS)) * 24.0
    except Exception:
        return 0.0


def _choose_stage_layout(products: dict, prefer_tidy: bool = False,
                          time_mult: float = 1.0, bucket_h: float = _CADENCE_H) -> dict:
    """Pick one run count per product so the whole STAGE lands together.

    `products` is `{key: {"cycle": hours_per_run, "options": [...], "total": runs_needed}}` for the
    products of a single stage — steps with no dependency on each other, installed in one sitting
    and collected in the next. A job's duration is `runs × cycle`, so once every job of a product
    carries the same run count, aligning the stage is choosing run counts whose durations match.

    Searching over target DURATIONS rather than over run counts directly is what makes alignment
    expressible: for each target every product takes the longest job it can that still lands by
    then, which is also its cheapest in slots.

    **Surplus is spent to LAND a stage, and for nothing else.** A layout whose jobs all finish
    within `_ALIGN_TOL` of each other wins outright, however much it overshoots (inside the budget
    `_level_options` already enforced) — *"it's fine to build a bit too much if it doesn't line
    up."* When no target lines the stage up, the surplus ordering takes over and the cheapest count
    wins. Without that split, a loose budget buys a little alignment for a lot of goo: a product
    that cannot reach the stage's duration gets pushed half way there, paying in full for a stage
    that still doesn't land together.

    `prefer_tidy` (the caller's `reactions_tidy_runs` state) puts a number you can type without
    checking ahead of the surplus, so a stage settles on 70 rather than 67 where that costs only
    surplus. It is not free — the surplus is real goo — which is why it follows the same flag that
    decides whether rounding is worth paying for at all.

    `time_mult` converts the raw-SDE durations in `products` into REAL hours, which is the only
    space `_collection_slot` is meaningful in; `bucket_h` is the player's own collection rhythm in
    real hours (their cadence, or 24h when they have not set one).
    """
    keys = [k for k, p in products.items() if p.get("options")]
    if not keys:
        return {}
    targets = sorted({o["runs"] * products[k]["cycle"] for k in keys for o in products[k]["options"]})
    # ONE run count for the whole stage, offered as a candidate layout in its own right rather than
    # left to fall out of the per-product picks — because it never does. Each product picks the
    # cheapest count that lands by the target, and cheapest means least surplus: Carbon Fiber's
    # 1045 divides exactly by 95, so it takes 95 and the stage reads 95/100/100 forever. The stage
    # can only settle on one number if "one number" is a thing being scored.
    uniform = sorted({o["runs"] for k in keys for o in products[k]["options"]})
    best_score, best_pick = None, {}
    for d in targets:
        pick = {}
        for k in keys:
            cyc = products[k]["cycle"]
            fits = [o for o in products[k]["options"] if o["runs"] * cyc <= d + 1e-9]
            # Of what fits by `d`, prefer the counts that actually LAND there — then the fewest
            # jobs, then the least surplus. Never simply the largest count that fits: that pays goo
            # for an identical layout (8 jobs of 36 where 8 of 35 was the same five reactors).
            near = [o for o in fits if o["runs"] * cyc >= (1 - _ALIGN_TOL) * d]
            pool = near or fits
            # Nothing this product can do lands by `d` — it takes its shortest, and the stage is
            # simply as spread as that product forces it to be.
            pool = pool or [min(products[k]["options"], key=lambda o: o["runs"])]
            pick[k] = min(pool, key=lambda o: ((o["jobs"], 0 if o["tidy"] else 1, o["surplus"])
                                               if prefer_tidy
                                               else (o["jobs"], o["surplus"], 0 if o["tidy"] else 1)))
        durs = [pick[k]["runs"] * products[k]["cycle"] for k in keys]
        if not _stage_affordable(products, pick):
            continue
        spread = round(max(durs) - min(durs), 6)
        jobs = sum(pick[k]["jobs"] for k in keys)
        surplus = sum(pick[k]["surplus"] for k in keys)
        untidy = sum(0 if pick[k]["tidy"] else 1 for k in keys)
        landed = 0 if spread <= _ALIGN_TOL * max(durs) else 1
        # How many DIFFERENT run counts the stage asks you to type. Landing a stage is about when
        # its jobs finish; this is about what you read off the screen while starting them.
        # *"I don't want to have to look for Carbon Fibers for each slot every time I start it to
        # figure out how many runs. The more similar number of job runs (preferably equal) between
        # products the better."* `_ALIGN_TOL` alone does not get there: with every product on the
        # same cycle time, 95 and 100 finish 5% apart and already count as landed, so nothing was
        # trying to close the last gap — and 95 won on surplus.
        numbers = len({pick[k]["runs"] for k in keys})
        # Slots first, then how many numbers there are to type, then the goo. Both after landing the
        # stage. Fewer, fuller jobs is still the whole point ("save slots, lower login cadence") and
        # the budget above bounds the surplus any of this can spend. Ranking the goo first is the
        # trap; docs/reactions.md has what it cost.
        # Which login the stage is collected on — after the numbers you type, before the goo. A
        # session earlier is worth surplus; a longer job inside the SAME session is not, which is
        # why this is the collected slot and not the raw duration.
        slot = max(_collection_slot(d_ * time_mult, bucket_h) for d_ in durs)
        score = ((landed, jobs, numbers, slot, untidy, surplus, spread) if prefer_tidy
                 else (landed, jobs, numbers, slot, surplus, untidy, spread))
        if best_score is None or score < best_score:
            best_score, best_pick = score, pick

    # ...and the same scoring over the layouts where every product carries the SAME count. A
    # product that has no option at `r` keeps its own best, so a stage still gets most of the
    # benefit when one product genuinely cannot reach the shared number.
    for r in uniform:
        pick = {}
        for k in keys:
            same = [o for o in products[k]["options"] if o["runs"] == r]
            if same:
                pick[k] = same[0]
            elif best_pick.get(k):
                pick[k] = best_pick[k]
            else:
                pick[k] = min(products[k]["options"], key=lambda o: o["runs"])
        if not _stage_affordable(products, pick):
            continue
        durs = [pick[k]["runs"] * products[k]["cycle"] for k in keys]
        spread = round(max(durs) - min(durs), 6)
        jobs = sum(pick[k]["jobs"] for k in keys)
        surplus = sum(pick[k]["surplus"] for k in keys)
        untidy = sum(0 if pick[k]["tidy"] else 1 for k in keys)
        landed = 0 if spread <= _ALIGN_TOL * max(durs) else 1
        numbers = len({pick[k]["runs"] for k in keys})
        # Which login the stage is collected on — after the numbers you type, before the goo. A
        # session earlier is worth surplus; a longer job inside the SAME session is not, which is
        # why this is the collected slot and not the raw duration.
        slot = max(_collection_slot(d_ * time_mult, bucket_h) for d_ in durs)
        score = ((landed, jobs, numbers, slot, untidy, surplus, spread) if prefer_tidy
                 else (landed, jobs, numbers, slot, surplus, untidy, spread))
        if best_score is None or score < best_score:
            best_score, best_pick = score, pick

    if best_pick:
        return best_pick
    # Every layout the stage could take is over the budget — so take the cheapest one there is
    # rather than leaving the products unlevelled, which is the outcome this pass exists to remove.
    cheap = {k: min(products[k]["options"], key=lambda o: (o["surplus"], o["jobs"])) for k in keys}
    return cheap


def _stage_affordable(products: dict, pick: dict) -> bool:
    """Is this whole-stage layout worth what it overbuilds?

    The budget is judged **per STAGE, not per product** — which is the difference between
    Oxy-Organic Solvents sitting at 35 runs beside its stage-mates at 120, and the whole stage
    reading 120. Reported, exactly: *"there's no reason why it would make 35 oxy when it could make
    120 instead and save slots."* Against its own 207-run requirement, 120 a job is nearly five
    times too much; against the stage's 4,400 runs it is 8% of the batch, and it buys the one thing
    the stage is for — every job the same length, collected in one trip.

    **Denominated in ISK, not in runs.** Runs of different products cost wildly different money, so
    a ratio of run counts is not a budget at all: a stage of Carbon Fiber (1,956 runs, cheap) beside
    a costly sibling (196 runs) reads `need = 2,152`, which lets 1,076 runs of surplus through — and
    every one of them may land on the expensive product, multiplying its material bill while the
    ratio still reports a comfortable 50%. `unit_value` (ISK per run, set by `level_product_runs`
    from the product's own instant-sell price) is what makes the 50% mean the same thing for every
    product in the stage, and it is also what lets the dashboard state the cost of this trade in
    ISK — a budget you cannot state in ISK cannot report a cost in ISK.

    Falls back to counting runs when no product carries a `unit_value` (the unit tests, and any
    account whose market lookup came back empty): a coarse budget is better than none.

    **The tradeoff this makes, stated plainly: the layout is now price-dependent.** Denominating in
    runs made the budget a pure function of the plan; denominating in ISK makes it a threshold on
    today's market, so a stage sitting near the 50% boundary can re-shape between two page loads
    with nothing about the plan having changed. That is in tension with this pass's own stated
    purpose — *"the numbers being typed into the industry window are the same every time"* — and it
    is a real cost, not a theoretical one. It is accepted because the alternative is worse: a budget
    denominated in incomparable units is not a budget at all (see the Carbon Fiber case above), and
    it cannot report what it spent in ISK, which is the whole of `reactions_ease_cost`. The
    exposure is narrow — only a stage whose surplus is within a hair of 50% can flip, and both
    sides of the flip are layouts this function already judged acceptable — so no hysteresis is
    applied today. If a plan is ever reported as shuffling with no edit behind it, this threshold
    is the first place to look, and the fix is a dead band (accept the previous layout unless the
    new one is better by some margin), not a return to counting runs.

    One ceiling, stage-wide: the surplus across everything the stage makes. A product with no
    `total` (the unit tests, which exercise the search itself) is not checked."""
    need = made = 0.0
    # ALL of them, not any: with one product unpriced its runs would weigh 0 against its
    # stage-mates' ISK, which is a different (and much tighter) budget rather than a coarser one.
    priced = bool(pick) and all(float(products[k].get("unit_value") or 0.0) > 0 for k in pick)
    for k, opt in pick.items():
        total = products[k].get("total")
        if total is None:
            return True
        unit = float(products[k].get("unit_value") or 0.0) if priced else 1.0
        need += int(total) * unit
        made += opt["jobs"] * opt["runs"] * unit
    return need <= 0 or made - need <= need * _LEVEL_BUDGET


def _reaction_cycle_times() -> dict[int, float]:
    """{output_type_id: hours per run} straight from the SDE. The structure/skill time bonus is
    deliberately NOT applied: it scales every reaction by the same factor, and everything here
    compares durations against each other."""
    con = get_connection()
    try:
        return {int(r["output_type_id"]): (float(r["cycle_time"] or 0) / 3600.0) or 1.0
                for r in con.execute(
                    "SELECT output_type_id, MIN(cycle_time) AS cycle_time FROM reactions "
                    "GROUP BY output_type_id")}
    except Exception:
        return {}
    finally:
        con.close()


def _reaction_output_qtys() -> dict[int, float]:
    """{output_type_id: units produced per run} straight from the SDE. Paired with
    `_reaction_cycle_times` so a run count can be turned into an ISK figure: one run of a product is
    worth `output_qty × price`, which is what lets the levelling budget and the surplus it spends be
    stated in money rather than in run counts of incomparable things."""
    con = get_connection()
    try:
        return {int(r["output_type_id"]): float(r["output_qty"] or 0.0)
                for r in con.execute(
                    "SELECT output_type_id, MAX(output_qty) AS output_qty FROM reactions "
                    "GROUP BY output_type_id")}
    except Exception:
        return {}
    finally:
        con.close()


def level_product_runs(context_id: int) -> int:
    """Give every job of ONE product the SAME run count across EVERY character, and re-split the
    work into as many jobs as that count needs. Returns how many rows were written.

    This is `level_stage_runs` taken to where the complaint actually lives. Levelling within a
    character can only ever move numbers around inside one column of the dashboard; the reported
    shape was 125 runs of Carbon Fiber on one character, 90 on the next two, 75 on the fourth —
    and the same on every other product. Choosing 125 for all four costs nothing (it is what the
    busiest character was already doing), makes every job finish at the same time, and takes the
    fifteen jobs down to twelve.

    **What it may not touch.** A chain's intermediate is consumed by the job above it ON THE SAME
    CHARACTER, so a chain's own requirement is a floor and work never moves between characters —
    only the split changes. The TOP row of every chain is left exactly as it is: its run count is
    what the batch's cost, output value and profit were computed from, and it is what a customer
    order gives back when it is cancelled. And a chain never loses its last row of a product: the
    dashboard reads readiness per chain (`chain_stage_state`), so a chain that stopped mentioning
    a product it is waiting on would announce the stage above as ready while those jobs ran.

    That last rule is also the limit of this pass: two separate chains on ONE character still get
    a job each rather than one shared job, even though the output is fungible and lands in the same
    hangar. Sharing needs chain identity reworked first (a real `chain_id`, so a row can belong to
    more than one chain) — see TODO 28.

    Rows keep their chain (`created_at`), stage (`tier_order`) and order, so `_shopping_roots`,
    `chain_stage_state` and the per-order give-back all still see what they saw before. Cost and
    reward are re-split across the new row count, never re-totalled. Idempotent: a plan that is
    already level is read and not written.
    """
    # ── Step 1: load every assignment row for this account ──────────────────────────────────────
    ensure_reaction_assignments_table()
    rows = _plan_rows(context_id, "a.id, a.character_id, a.type_id, a.name, a.runs, a.input_cost, a.reward, "
                      "a.created_at, a.order_id, COALESCE(a.tier_order,0) AS tier_order, "
                      # ...and what the LAST pass wrote down about what this layout cost, so an
                      # unchanged plan can be left genuinely untouched rather than re-stamped with
                      # the same four numbers on every dashboard load.
                      "COALESCE(a.cadence_over_h,0) AS cadence_over_h, "
                      "COALESCE(a.surplus_runs,0) AS surplus_runs, "
                      "COALESCE(a.jobs_saved,0) AS jobs_saved, "
                      "COALESCE(a.recover_runs,0) AS recover_runs")
    if not rows:
        return 0

    # ── Step 1b: freeze what the player has already INSTALLED ───────────────────────────────────
    # A row whose job is running in game is not a proposal any more — it is a job with a run count
    # the player typed in and materials they have already bought against. Re-splitting it changes
    # the numbers underneath work in progress, which is how a plan came back wanting 11,736 of an
    # intermediate the installed jobs were never going to make: *"I've been moving between
    # structures about 5 times now to get this sorted with the amounts."*
    #
    # Frozen per (character, product, stage), because that is the group this pass re-splits: as many
    # rows as there are running jobs of it stay exactly as they are, and only the ones still waiting
    # to be installed are fair game. A plan half-installed therefore settles instead of chasing.
    frozen: set[int] = set()
    orphan_running: dict[int, int] = {}
    try:
        live_runs = live_reaction_runs(context_id)
        live = {k: len(v) for k, v in live_runs.items()}
        adopt: list[tuple] = []
        # Freeze the WHOLE (character, product, stage) group once any of its jobs is running — not
        # just the matched rows. Freezing row-by-row left the untouched siblings alone in the pass,
        # which re-levelled them against a requirement the frozen ones no longer contributed to and
        # deleted one outright. Once you have started installing a product, its layout is settled.
        started = {(int(cid), int(tid)) for (cid, tid), n in live.items() if n > 0}
        for r in sorted(rows, key=lambda r: r["id"]):
            k = (int(r["character_id"]), int(r["type_id"]))
            if k in started:
                frozen.add(int(r["id"]))
            if live.get(k, 0) > 0:
                live[k] -= 1
                # ...and the row takes the run count the job REALLY carries. A plan that says 113
                # where the reactor is running 120 is lying about what will be made, which is what
                # the under-production warning and every materials figure are computed from.
                got = live_runs.get(k) or []
                real = got.pop(0) if got else None
                if real and real != int(r["runs"] or 0):
                    adopt.append((real, int(r["id"])))
                    r["runs"] = real
        if adopt:
            con = get_connection()
            try:
                for real, rid in adopt:
                    con.execute("UPDATE pp_reaction_assignments SET runs=? WHERE id=?", (real, rid))
                con.commit()
            finally:
                con.close()
        # Whatever running jobs are LEFT after matching are orphans — installed outside this plan,
        # so they hold a reactor no row accounts for. Everything matched is already represented by
        # its row, and subtracting it from `room` as well counts the same reactor twice.
        for (cid, _tid), n in live.items():
            if n > 0:
                orphan_running[cid] = orphan_running.get(cid, 0) + n
    except Exception:
        frozen, orphan_running = set(), {}

    # ── Step 2: exclude what may not be re-shaped (an ORDER chain's top row) ────────────────────
    # What may NOT be re-shaped: the TOP row of a customer ORDER's chain. Its run count is the batch
    # that order was quoted and priced on, and cancelling the order hands exactly those runs back
    # (`give_back_order_runs`), so moving it would make the order's own arithmetic wrong.
    #
    # Everything else is fair game — an order's intermediates, and the top row of a speculative
    # chain. Both were excluded once and both exclusions left a product showing several numbers
    # after the pass had run; see docs/reactions.md, "A speculative chain's TOP row is levelled too".
    # The top row is a property of the ORDER, not of a character. Keyed per (character, chain) this
    # protected any row that was merely the highest tier THAT CHARACTER happened to hold: a host
    # carrying one stage-1 job and nothing above it looked like the order's quoted batch, so the
    # leveller was forbidden from moving it and a stray host could never be consolidated away.
    # Found only on prod — a fixture built with `order_id=None` never reaches this branch at all.
    top_of_order: dict[int, int] = {}
    for r in rows:
        oid = r.get("order_id")
        if oid is None:
            continue
        top_of_order[int(oid)] = max(top_of_order.get(int(oid), 0), int(r["tier_order"] or 0))
    inner = [r for r in rows
             if int(r["id"]) not in frozen
             and not (r.get("order_id")
                      and int(r["tier_order"] or 0) == top_of_order[int(r["order_id"])])]
    if not inner:
        return 0

    # ── Step 3: capacity — what the plan may hold per character ─────────────────────────────────
    cycles = _reaction_cycle_times()
    # ISK per RUN of each product, so the levelling budget and the surplus it spends can be stated
    # in money (`_stage_affordable`). Instant-sell (buy orders) is the price a reaction good is
    # really worth — see CLAUDE.md's pricing invariant — and surplus intermediates are stock, so
    # what they are worth is what you would get for them, not what they are listed at. Empty when
    # the market lookup fails, and the budget falls back to counting runs.
    unit_values: dict[int, float] = {}
    try:
        _oq = _reaction_output_qtys()
        _tids = sorted({int(r["type_id"]) for r in rows})
        for tid, m in (resolve_market_data(context_id, _tids) or {}).items():
            v = float((m or {}).get("buy_price") or 0.0) * _oq.get(int(tid), 0.0)
            if v > 0:
                unit_values[int(tid)] = v
    except Exception:
        unit_values = {}
    caps_now = {c["character_id"]: c for c in _character_capacities(context_id)}
    # What the plan may hold on a character, counting EVERY planned job on it, not just the busiest
    # stage: reactors it has, minus the jobs really running in game. `free_slots` is the peak-tier
    # number — right for "what can I start now", and wrong here, because it let this pass grow
    # stage 1 into reactors that stage 2 was already holding a row in. Reported as "12 slots
    # assigned to characters that only have 10". A row is a line in the plan whether or not it can
    # be installed yet, and the plan must fit the reactors.
    # Reactors this character's PLAN may occupy. Only jobs running OUTSIDE the plan are netted off:
    # a running job that matches a plan row is that row being executed, and the row is still in the
    # list below, so subtracting it as well counts one reactor twice. That double count is what made
    # a plan shuffle itself the moment ESI first saw the jobs the player had just installed —
    # reported as "it just inventoried my jobs, and suddenly it pushed all the staged jobs to other
    # slots", which is the worst possible moment to move anything.
    room = {cid: max(0, int(c.get("slots") or 0) - orphan_running.get(cid, 0))
            for cid, c in caps_now.items()}
    # Whether a planned row on another stage holds a reactor at the same time — see `budget` below.
    peak_only = _parallel_stages_on(context_id)

    # ── Step 4: pool rows into (stage, product) buckets across characters ───────────────────────
    # (stage, product) -> every row of it, POOLED ACROSS CHARACTERS: the requirement is the
    # ACCOUNT's, the run count is the product's, and which character holds a job is just where there
    # was room. That is what makes this pass slot-efficient rather than merely tidy, and two things
    # elsewhere are corrected to match it — `_shopping_roots` (a moved row must not be mistaken for
    # a chain of its own and bought for twice) and `_gate_stages_account_wide` (a chain no longer
    # holds every stage it waits on). docs/reactions.md, "A product is POOLED across characters".
    stages: dict[int, dict[int, list[dict]]] = {}
    for r in inner:
        stages.setdefault(int(r["tier_order"] or 0), {}).setdefault(int(r["type_id"]), []).append(r)

    prefer_tidy = _tidy_runs_on(context_id)
    # The characters this plan already occupies. They are a login you are making anyway, so their
    # reactors cost nothing extra to use; anyone else has to be worth the trip (Step 5b).
    # The characters whose REACTORS are worth a login at all — computed from
    # capacity, not from who happens to hold a row today. A pending row is not a commitment: nothing
    # is installed, so moving it costs nothing, and a row sitting on a character the packing rule
    # would never have chosen is pure overhead. Rows that are genuinely RUNNING are a different
    # matter and `room` already nets those out.
    lean_set = {h["character_id"] for h in _lean_hosts(
        [{"character_id": cid, "free_slots": n} for cid, n in room.items() if n > 0])}
    # Rows per (character, stage) — counted over EVERY row, not just the re-shapeable ones. A
    # frozen row and a customer order's protected top row are excluded from `inner` because this
    # pass may not move them, but they are still lines in the plan holding a reactor. Counting only
    # `inner` meant an order's own top rows were invisible to `later` and to `budget`, so stage 1
    # was sized against reactors the order was already holding. (This was TODO §28b item 2, closed
    # 2026-08-14 — see TODO-archive.md. Its item 1 is the opposite mechanism, later stages
    # over-reserving slots they cannot use, fixed by counting the worst tier in `_concurrent_load`.)
    rows_by_char_stage: dict[int, dict[int, int]] = {}
    inner_ids = {int(r["id"]) for r in inner}
    excluded_by_char_stage: dict[int, dict[int, int]] = {}
    for r in rows:
        cid = r["character_id"]
        st = int(r["tier_order"] or 0)
        rows_by_char_stage.setdefault(cid, {})[st] = \
            rows_by_char_stage.get(cid, {}).get(st, 0) + 1
        if int(r["id"]) not in inner_ids:
            excluded_by_char_stage.setdefault(cid, {})[st] = \
                excluded_by_char_stage.get(cid, {}).get(st, 0) + 1

    plan: list[tuple] = []          # (product_key, rows, target_runs, [character per job])
    # Jobs this pass has already placed on a character, stage by stage. Reading the ORIGINAL row
    # counts for every stage instead let stage 0 fill a character up and stage 1 then fill it again
    # from the same starting point — 17 jobs on an 11-slot character. Stages are walked in order so
    # what an earlier one took is a fact by the time a later one asks.
    committed: dict[int, int] = {}
    # Both are per-ACCOUNT, so they are the same number for every stage. `_reaction_time_mult`
    # re-reads and re-parses every character's `jobs_json` and writes the measurement back, so
    # asking it once per stage was a full scan per stage for one value.
    _tmult = _reaction_time_mult(context_id)
    cadence_h = _reaction_cadence_hours(context_id)
    # ── Step 5: per stage — size every product to one shared run count ──────────────────────────
    for stage in sorted(stages):
        by_product = stages[stage]
        later = {cid: sum(n for st, n in per.items() if st > stage)
                 for cid, per in rows_by_char_stage.items()}
        # The window every job in this stage has to land inside. The account's cadence when it has
        # set one — *"nothing longer than N days"*, a DURATION and not a calendar — and otherwise
        # whatever the plan already runs, which is the old behaviour and keeps a plan built before
        # this existed from being resized by a number nobody chose.
        #
        # **It is a TARGET, not a hard ceiling** (decision of 2026-08-14). `_level_options` offers
        # only the counts that fit whenever any fit, so a stage that CAN be split finer is; a stage
        # that genuinely cannot — more reactor-hours of work than its free reactors can hold inside
        # the window — overruns, and every route that gets it there now records by how much
        # (`over_h` below) so the row can say so. The alternative, forcing the ceiling, is not
        # available: with fewer reactors than the work needs, no split fits, and pretending
        # otherwise is what made the option set collapse to a single unmeasured answer.
        #
        # It is also wider than the cadence: with none set, this falls back to the longest job the
        # plan already runs, and the promise attached to that — *levelling makes a plan tidier and
        # shorter, never slower* — is the one the same breach broke, silently, for every account
        # with `reactions_level_runs` on.
        # The cadence is REAL hours and every duration here is raw SDE hours, so it has to be
        # converted before it can be compared with them: a 7-day window is 7 days of wall clock,
        # which is `7d / mult` worth of the SDE's idea of time. Getting this wrong is what made a
        # 7-day ceiling plan 56-run jobs instead of 119. (`_tmult`/`cadence_h` are hoisted above
        # the loop — both are per-account.)
        stage_cap_hours = (cadence_h / _tmult if cadence_h else 0.0) or max(
            (int(r["runs"] or 0) * cycles.get(int(r["type_id"]), 1.0)
             for rs in by_product.values() for r in rs),
            default=0.0)
        # A character's reactors are ONE pool and every product in the stage draws from it, so the
        # stage is solved as a whole (the give-ground loop below). Per stage rather than across all
        # of them, which is right under the one-slot model: a character's load is its busiest stage,
        # so growth in stage 0 and stage 1 does not add up — this stage may use whatever the
        # character's reactors are not already committed to by its OTHER stages.
        # Every planned row is a line in the plan the character has to hold, so a stage may only
        # use what its OTHER stages have not already claimed. Removing this to free up placement
        # room was a mistake: it let stage 1 grow to 8 rows beside stage 2's 3 on a 10-reactor
        # character — 11 rows on 10 reactors, the very shape the subtraction was added for
        # ("12 slots assigned to characters that only have 10").
        #
        # It was never the thing blocking consolidation either. The stray host stayed because the
        # overflow gate read "already holds a row" as justification, and because an order's
        # protected top row was keyed per character rather than per order. With both fixed, 21
        # stage-1 rows land 7/7/7 inside this budget with nothing left over.
        #
        # `excluded_here` is the rows AT THIS STAGE this pass may not re-shape — an order's
        # protected top row, and anything frozen because its job is already installed. They hold a
        # reactor exactly as a re-shapeable row does, and leaving them out is what let a stage be
        # sized against reactors an order's own rows were holding.
        budget = {cid: max(0, n - committed.get(cid, 0) - later.get(cid, 0)
                           - excluded_by_char_stage.get(cid, {}).get(stage, 0))
                  for cid, n in room.items()}

        # The shortest this stage can possibly run: all of its work, divided by every reactor it may
        # use. Nothing shorter is installable however the run counts are chosen, so it seeds the
        # floor and the give-ground loop below only has to fix rounding — without it that loop
        # started from the asked-for length and stepped one run at a time, which never got from
        # "5 days" to what the reactors could do.
        stage_room = max(1, sum(budget.values()))
        # Unknown stays unknown: a product with no evidence caps nothing, the same rule every other
        # consumer of this follows. Read once per stage rather than per product — it is memoised,
        # but the intent is that one plan sees one answer.
        fcaps = {t: c for t, c in (formula_concurrency_caps(context_id) or {}).items() if c}
        stage_work = sum(sum(int(r["runs"] or 0) for r in rs) * cycles.get(tid, 1.0)
                         for tid, rs in by_product.items())
        d_floor = stage_work / stage_room

        # The ceiling in RAW-SDE hours and the run count it permits, per product — hoisted out of
        # `_build` so the seed and the give-ground loop below can both compare against it instead of
        # each quietly stepping past a number only `_build` could see. Two of the three routes that
        # abandoned the ceiling did exactly that.
        #
        # UNITS: `cadence_h` and `_CADENCE_GRACE` are REAL hours; `cycles` and everything derived
        # from them here are RAW SDE hours. Dividing by the time multiplier converts real → raw.
        # `_pm` is this product's own multiplier (a structure's rig bonus covers some families and
        # not others), so a per-product ceiling is re-derived from the cadence rather than rescaled
        # off the account-wide `stage_cap_hours`.
        #
        # The grace is the same slack `_collection_slot` already absorbs: truncating at exactly
        # 168.0h rejected a layout landing at 168h04m and charged an extra job and an extra reactor
        # for four minutes nobody can feel.
        cap_hours_by_tid: dict[int, float] = {}
        max_runs_by_tid: dict[int, int] = {}
        mult_by_tid: dict[int, float] = {}
        for tid, rs_all in by_product.items():
            cyc = cycles.get(tid, 1.0)
            _pm = reaction_time_mult_for(context_id, tid) if cadence_h else 0.0
            _cap_h = (((cadence_h + _CADENCE_GRACE) / _pm) if (_pm and cadence_h)
                      else stage_cap_hours)
            cap_hours_by_tid[tid] = _cap_h
            mult_by_tid[tid] = _pm or _tmult or 1.0
            max_runs_by_tid[tid] = (int(_cap_h / cyc) if cyc > 0
                                    else sum(int(r["runs"] or 0) for r in rs_all))

        # How far past the ceiling this stage ended up, per product, in REAL hours. 0 while the
        # ceiling holds. Every route that can breach it writes here: the seed below, the give-ground
        # loop, and `_level_options`' own fallback — the last of which reports itself through
        # `over_runs` on the option it returns, so it is measured off the final layout rather than
        # guessed at.
        over_h: dict[int, float] = {}
        floor_runs: dict[int, int] = {}     # per product, raised when a character is over-promised
        for tid in by_product:
            cyc = cycles.get(tid, 1.0)
            if cyc > 0 and d_floor > 0:
                # The seed: the shortest this stage could possibly run given its reactors. It has
                # never referenced `max_runs`, so a reactor-tight stage breached on attempt 0,
                # before the give-ground loop ran at all. It still may — forcing it here would only
                # hand `_level_options` an empty option set — but it no longer does so silently.
                floor_runs[tid] = max(1, int(d_floor / cyc))
                if floor_runs[tid] > max_runs_by_tid.get(tid, floor_runs[tid]):
                    over_h[tid] = max(over_h.get(tid, 0.0),
                                      (floor_runs[tid] * cyc - cap_hours_by_tid[tid])
                                      * mult_by_tid.get(tid, 1.0))
        layout: dict[int, dict] = {}
        products: dict[int, dict] = {}
        # ── Step 5a: give-ground loop — shrink until the stage fits its reactors ────────────────
        for _attempt in range(24):
            def _build(extra_hours: set[float]) -> dict:
                built: dict[int, dict] = {}
                for tid, rs_all in by_product.items():
                    cyc = cycles.get(tid, 1.0)
                    # ONE pooled requirement per product: the account needs this many runs of it,
                    # and any reactor with room can make them.
                    total = sum(int(r["runs"] or 0) for r in rs_all)
                    # Per PRODUCT, computed above: the structure's rig bonus covers some reaction
                    # families and not others, so one account-wide ceiling would be wrong for every
                    # product outside them. Raw-SDE hours and a raw-SDE run count.
                    max_runs = max_runs_by_tid.get(tid, total)
                    # The counts that would land this product on a duration the rest of the stage
                    # is considering — see `_level_options`' `extra`.
                    extra = [max(1, int(h / cyc)) for h in extra_hours if cyc > 0]
                    built[tid] = {
                        "cycle": cyc, "total": total,
                        # ISK per run, so the stage budget is judged in money (`_stage_affordable`).
                        "unit_value": unit_values.get(int(tid), 0.0),
                        # Wide here on purpose: what a run count really costs is judged across the
                        # whole stage (`_stage_affordable`), not against this one product's needs.
                        # A formula is a physical item locked in the reactor while a job runs on
                        # it, so a product can never hold more parallel jobs than there are
                        # formulas of it. The assign paths have always applied this
                        # (`formula_concurrency_caps`); THIS pass never asked, and it re-splits the
                        # work on every dashboard load — which is how a plan came back asking for 21
                        # jobs of Carbon Fiber against 20 formulas. A tighter cadence makes it bite
                        # harder, because a shorter job means more of them.
                        "options": _level_options(total, min(stage_room, fcaps.get(tid, stage_room)),
                                                  max_runs,
                                                  budget=_STAGE_SCAN_BUDGET,
                                                  min_runs=floor_runs.get(tid, 0), extra=extra),
                    }
                return built

            # Two passes: size every product on its own terms, then re-offer each of them the
            # durations the others turned out to be considering, so a stage can settle on ONE
            # length rather than each product landing wherever its own arithmetic happened to.
            products = _build(set())
            durations = {o["runs"] * p["cycle"] for p in products.values() for o in p["options"]}
            products = _build(durations)
            # The rhythm the stage is collected on: the account's cadence when it has set one,
            # 24h when it has not. Real hours, matching `_collection_slot`'s argument.
            layout = _choose_stage_layout(products, prefer_tidy, time_mult=_tmult,
                                          bucket_h=cadence_h or _CADENCE_H)
            asked = sum(opt["jobs"] for opt in layout.values())
            if asked <= stage_room:
                break
            # The stage wants more reactors than the account has free for it, so the greediest
            # product gives ground — and it gives it in one step, the NEXT run count that actually
            # changes the layout, not one more run, which would take a hundred passes to matter.
            worst_tid = max(layout, key=lambda t: layout[t]["jobs"])
            cur = layout[worst_tid]["runs"]
            higher = [o["runs"] for o in products[worst_tid]["options"] if o["runs"] > cur]
            nxt = min(higher) if higher else cur + max(1, cur // 4)
            if nxt <= floor_runs.get(worst_tid, 0):
                break                       # no room left to give — take what we have
            # Giving ground means a LONGER job, so this is the route that walks the ceiling. It was
            # raised with no reference to `max_runs` at all; it still may cross it — the stage has
            # more work than reactors and something has to give — but it now says so.
            _cyc_w = cycles.get(worst_tid, 1.0)
            if nxt > max_runs_by_tid.get(worst_tid, nxt):
                over_h[worst_tid] = max(
                    over_h.get(worst_tid, 0.0),
                    (nxt * _cyc_w - cap_hours_by_tid.get(worst_tid, nxt * _cyc_w))
                    * mult_by_tid.get(worst_tid, 1.0))
            floor_runs[worst_tid] = nxt
        # ── Step 5a-bis: measure what the answer actually cost ──────────────────────────────────
        # The BREACH, measured off the layout that was chosen rather than trusted to the route that
        # produced it. `_level_options`' own fallback is the third breach route and it needs no loop
        # to get there, so this is the only place all three are certainly caught. Real hours, so the
        # row can print it: raw-SDE duration minus the raw-SDE ceiling, × this product's multiplier.
        #
        # The WHY is structural and the same in every case, which is why it is not stored as text:
        # a stage whose work exceeds what its free reactors can turn over inside the window has no
        # split that fits, so the plan overruns rather than pretending otherwise (decision of
        # 2026-08-14 — the cadence is a stated target).
        for tid, opt in layout.items():
            _cyc = cycles.get(tid, 1.0)
            _capr = cap_hours_by_tid.get(tid, 0.0)
            if _capr > 0 and opt["runs"] * _cyc > _capr:
                over_h[tid] = max(over_h.get(tid, 0.0),
                                  (opt["runs"] * _cyc - _capr) * mult_by_tid.get(tid, 1.0))
        # ...and what EASE cost. The counterfactual is per-product run counts — every product on the
        # cheapest count its own requirement allows — against the shared layout actually chosen.
        # Only `_choose_stage_layout`'s inputs know it, which is why it is computed here and written
        # down rather than re-derived later from rows that no longer carry the alternatives.
        # `recover_runs` is what dropping to per-product counts would give back; `jobs_saved` is
        # what the shared count bought. Both per (stage, product), in RUNS — the dashboard prices
        # them, because it is the surface that holds today's market.
        ease: dict[int, dict] = {}
        for tid, opt in layout.items():
            opts = (products.get(tid) or {}).get("options") or []
            cheapest = min(opts, key=lambda o: (o["surplus"], o["jobs"])) if opts else opt
            ease[tid] = {
                "surplus_runs": max(0, int(opt.get("surplus") or 0)),
                "recover_runs": max(0, int(opt.get("surplus") or 0) - int(cheapest.get("surplus") or 0)),
                "jobs_saved": max(0, int(cheapest.get("jobs") or 0) - int(opt.get("jobs") or 0)),
            }

        # ── Step 5b: place the jobs (stable: who already runs it keeps it) ──────────────────────
        # WHERE the jobs go. A character that already runs the product keeps it (no move for its
        # own sake), then whoever has the most room — so consolidating a product onto fewer
        # reactors moves as few rows as it can.
        room_left = dict(budget)
        for tid, opt in sorted(layout.items(), key=lambda kv: -kv[1]["jobs"]):
            rs_all = sorted(by_product[tid], key=lambda r: r["id"])
            want = max(1, opt["jobs"])
            have: dict[int, int] = {}
            for r in rs_all:
                have[r["character_id"]] = have.get(r["character_id"], 0) + 1
            # Who runs it: the characters already running it keep as much of it as their room
            # allows, and only what is left over goes anywhere new. Placing purely by "most room
            # first" was not STABLE — the next pass saw the new distribution, re-sorted, and moved
            # twelve rows again for an identical layout, on every dashboard load.
            quota: dict[int, int] = {}
            left = want
            # The characters worth a login go first, even where a lesser one already holds rows of
            # this product: with nothing installed there is no churn to avoid, and consolidating is
            # the whole point. *"No one is running any products right now, so there's no reason why
            # it should give it to that character."*
            for cid in sorted(have, key=lambda c: (0 if c in lean_set else 1,
                                                    -have[c], -room_left.get(c, 0), c)):
                if cid not in lean_set:
                    continue                # ...only if the worthwhile ones cannot absorb it
                take = min(have[cid], max(0, room_left.get(cid, 0)), left)
                if take:
                    quota[cid] = take
                    room_left[cid] -= take
                    left -= take
            # ...and only THEN somewhere new — held to the same "worth a login" test the order
            # allocator uses (`_lean_hosts`). Without it these two passes disagreed by
            # construction: the assign packed an order onto three characters, then this pass ran on
            # the next dashboard load, saw a spare reactor on a fourth, and put a single job there.
            # Reported as a plan that "suddenly swapped 3x7 slots to 3x7 + 1x1" while watching it.
            #
            # Gated on the characters worth a login, NOT on "already holds a row" — that was the bug in
            # the first cut of this: a character the packing rule would never have chosen kept its
            # place forever simply because an earlier plan had put it there, and every later pass
            # read that as evidence it belonged. Where the worthwhile characters cannot hold it all,
            # the fallback below still uses whoever can.
            for cid in sorted(room_left, key=lambda c: (-room_left[c], c)):
                if left <= 0:
                    break
                free = max(0, room_left.get(cid, 0))
                if cid not in lean_set or free <= 0:
                    continue
                take = min(free, left)
                if take:
                    quota[cid] = quota.get(cid, 0) + take
                    room_left[cid] -= take
                    left -= take
            # Only now the characters the packing rule would not have picked, and only for what
            # is genuinely left over: an account whose worthwhile characters are full does still
            # need them.
            if left > 0:
                for cid in sorted(have, key=lambda c: (-have[c], -room_left.get(c, 0), c)):
                    if left <= 0:
                        break
                    if cid in lean_set:
                        continue
                    take = min(have[cid], max(0, room_left.get(cid, 0)), left)
                    if take:
                        quota[cid] = quota.get(cid, 0) + take
                        room_left[cid] -= take
                        left -= take
            if left > 0:
                continue        # nowhere to put it all — leave this product exactly as it is
            # Rows stay put wherever the quota allows; the overflow are the ones that move, and
            # the rows past `want` are the ones that go. Both fall out of this ordering.
            q = dict(quota)
            stay, move = [], []
            for r in rs_all:
                cid = r["character_id"]
                if q.get(cid, 0) > 0:
                    q[cid] -= 1
                    stay.append(r)
                else:
                    move.append(r)
            spots = [r["character_id"] for r in stay]
            for cid in sorted(q):
                spots += [cid] * q[cid]
            for cid in spots:
                committed[cid] = committed.get(cid, 0) + 1
            # ...and what this answer cost, stamped on every row of the group so the dashboard can
            # report it without re-solving the stage. Per (stage, product): a reader takes it ONCE
            # per group, not once per row.
            note = dict(ease.get(tid) or {"surplus_runs": 0, "recover_runs": 0, "jobs_saved": 0})
            note["cadence_over_h"] = round(over_h.get(tid, 0.0), 2)
            plan.append(((stage, tid), stay + move, opt["runs"], spots, note))

    # ── Step 6: write the plan back — update, delete, insert ────────────────────────────────────
    changed = 0
    _noted = False          # a cost note was written for a group whose layout did not change
    con = get_connection()
    try:
        for _pkey, rs, runs, spots, note in plan:
            jobs = len(spots)
            keep = rs[:jobs]
            drop = rs[jobs:]
            settled = (jobs == len(rs) and all(int(r["runs"] or 0) == runs for r in rs)
                       and [r["character_id"] for r in rs] == spots)
            if settled:
                # Already one number, in the right jobs, in the right hands — but what that answer
                # COST is re-measured every pass (prices move, and so does the free-reactor count),
                # so the note is written even when the layout is not. It is not counted in
                # `changed`: this pass reports rows re-shaped, and an unchanged plan must still read
                # as idempotent.
                for r in rs:
                    if (abs(float(r.get("cadence_over_h") or 0.0) - note["cadence_over_h"]) < 0.01
                            and int(r.get("surplus_runs") or 0) == note["surplus_runs"]
                            and int(r.get("jobs_saved") or 0) == note["jobs_saved"]
                            and int(r.get("recover_runs") or 0) == note["recover_runs"]):
                        continue                # the note is already what it should be
                    con.execute("UPDATE pp_reaction_assignments SET cadence_over_h=?, "
                                "surplus_runs=?, jobs_saved=?, recover_runs=? WHERE id=?",
                                (note["cadence_over_h"], note["surplus_runs"],
                                 note["jobs_saved"], note["recover_runs"], r["id"]))
                    _noted = True
                continue
            # Cost and profit are LINEAR in runs, so they scale with the work rather than being
            # re-split across it. A chain's intermediate rows carry 0 either way (the whole chain's
            # cost rolls up into its top row), but a top row carries the real ISK — and after this
            # pass it may be making more than it was asked for. Dividing the old total across the
            # new jobs would report the same profit for more goo bought.
            was = sum(int(r["runs"] or 0) for r in rs) or 1
            scale = (runs * jobs) / was
            cost = sum(float(r["input_cost"] or 0.0) for r in rs) * scale / jobs
            reward = sum(float(r["reward"] or 0.0) for r in rs) * scale / jobs
            for i, r in enumerate(keep):
                # ...and `character_id`, because a pooled product's jobs go where there is room.
                # Moving a row rather than deleting and re-inserting keeps its id, its chain and
                # its order, so everything reading those still sees the plan it saw before.
                con.execute("UPDATE pp_reaction_assignments SET runs=?, input_cost=?, reward=?, "
                            "character_id=?, cadence_over_h=?, surplus_runs=?, jobs_saved=?, "
                            "recover_runs=? WHERE id=?",
                            (runs, cost, reward, spots[i], note["cadence_over_h"],
                             note["surplus_runs"], note["jobs_saved"], note["recover_runs"],
                             r["id"]))
                changed += 1
            for r in drop:
                con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (r["id"],))
                changed += 1
            proto = rs[0]
            for cid in spots[len(keep):]:
                con.execute(
                    "INSERT INTO pp_reaction_assignments "
                    "(character_id, type_id, name, runs, input_cost, reward, created_at, "
                    "tier_order, order_id, cadence_over_h, surplus_runs, jobs_saved, recover_runs) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (cid, proto["type_id"], proto["name"], runs, cost, reward,
                     proto["created_at"], proto["tier_order"], proto["order_id"],
                     note["cadence_over_h"], note["surplus_runs"], note["jobs_saved"],
                     note["recover_runs"]))
                changed += 1
        if changed or _noted:
            con.commit()
    except Exception:
        return 0
    finally:
        con.close()
    return changed



def split_order_tops_to_cadence(context_id: int) -> int:
    """Split a customer ORDER's top row into jobs that fit the cadence. Returns rows written.

    `level_product_runs` deliberately never reshapes an order's top row: its run count is the batch
    the order was quoted on, and cancelling hands exactly those runs back
    (`give_back_order_runs`). Correct — and it meant the cadence stopped at the order's own product.
    Reported on a 7-day setting: stage 1 obediently came down to 6.88-day jobs while stage 2 sat at
    **14 days**, so the whole order still took three weeks and the cadence bought nothing.

    **The total is preserved EXACTLY, which is what makes this safe.** The batch is split into the
    fewest jobs that each fit the window, and the remainder rides on one of them rather than being
    rounded up across all of them — *"I'd rather we underfill 1 slot to line up the others"*. 1001
    runs over a 119-run ceiling becomes 8 jobs of 112 and one of 105, summing to 1001. Nothing the
    order's arithmetic reads has changed: same product, same character, same chain timestamp, same
    total. Only the row COUNT differs, and every consumer of that counts rows rather than assuming
    one.

    Held to the formulas owned like everything else — AND to the reactors owned, which it was not:
    it wrote N rows where there was 1 with no free-slot check at all, while `level_product_runs` was
    hardened for exactly that ("12 slots assigned to characters that only have 10"). A split that
    does not fit the character's remaining room is taken as far as the room allows.

    Since 2026-08-14 the order is paced at QUOTE time instead (`_allocate_and_insert`), so on a
    freshly committed order this pass finds every top row already inside the window and writes
    nothing. It stays because orders committed before that, and orders whose cadence was set or
    tightened afterwards, still need it — and because it is the second half of the guarantee that
    the commit-time quote and the dashboard agree.
    """
    cadence_h = _reaction_cadence_hours(context_id)
    if cadence_h <= 0:
        return 0
    mult = _reaction_time_mult(context_id)
    cyc = _reaction_cycle_times()
    fcaps = {t: c for t, c in (formula_concurrency_caps(context_id) or {}).items() if c}
    # Reactors per character, and what the plan already holds on each — this pass may only spend
    # what is genuinely left. Read before the connection below opens: `_character_capacities` and
    # `_plan_rows` open their own, and two at once is the 2026-07-13 pool-exhaustion incident.
    room: dict[int, int] = {}
    try:
        room = {c["character_id"]: max(0, int(c.get("slots") or 0))
                for c in _character_capacities(context_id)}
        for r in _plan_rows(context_id, "a.id, a.character_id"):
            cid = r["character_id"]
            if cid in room:
                room[cid] -= 1
    except Exception:
        room = {}
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT a.id, a.character_id, a.type_id, a.name, a.runs, a.input_cost, a.reward, "
            "a.tier_order, a.created_at, a.order_id, "
            # Carried across the re-split below. They are the leveller's cost note for the group,
            # and a row that is deleted and re-inserted must not silently lose it — see the INSERT.
            "COALESCE(a.surplus_runs,0) AS surplus_runs, "
            "COALESCE(a.jobs_saved,0) AS jobs_saved, "
            "COALESCE(a.recover_runs,0) AS recover_runs "
            "FROM pp_reaction_assignments a "
            "JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id = ? AND a.order_id IS NOT NULL", (context_id,))]
        top: dict[tuple, int] = {}
        for r in rows:
            k = (r["character_id"], round(float(r["created_at"] or 0.0), 3))
            top[k] = max(top.get(k, 0), int(r["tier_order"] or 0))

        written = 0
        for r in rows:
            k = (r["character_id"], round(float(r["created_at"] or 0.0), 3))
            if int(r["tier_order"] or 0) != top[k]:
                continue                       # an intermediate — the leveller already has it
            raw = cyc.get(int(r["type_id"]), 0.0)
            runs = int(r["runs"] or 0)
            if raw <= 0 or runs <= 0:
                continue
            # UNITS: `raw` is RAW SDE hours per run; `cadence_h` and `_CADENCE_GRACE` are REAL
            # hours. Dividing by the measured multiplier converts the window into raw-SDE space,
            # which is the space `raw` lives in. The grace is the same slack `_collection_slot`
            # absorbs — truncating at exactly 168.0h charged an extra job for four minutes.
            per_job_cap = int(((cadence_h + _CADENCE_GRACE) / mult) / raw)
            if per_job_cap <= 0 or runs <= per_job_cap:
                continue                       # already inside the window
            jobs = -(-runs // per_job_cap)
            cap = fcaps.get(int(r["type_id"]))
            if cap:
                jobs = min(jobs, max(1, int(cap)))
            # ...and the character's own reactors. One row becomes `jobs` rows, so the split costs
            # `jobs - 1` more lines in the plan; a character with two reactors free cannot hold nine
            # of them however well that would fit the cadence. Splitting as far as the room allows
            # is still better than not splitting at all — and when either cap binds the job stays
            # over the window, which the INSERT below measures and records on every row it writes.
            free = room.get(r["character_id"])
            if free is not None:
                jobs = min(jobs, max(1, free + 1))
            if jobs <= 1:
                continue
            if free is not None:
                room[r["character_id"]] = max(0, free - (jobs - 1))
            base, rem = divmod(runs, jobs)
            if base <= 0:
                continue
            # `rem` jobs carry one extra run; the rest carry `base`. Sums to `runs` exactly.
            sizes = [base + 1] * rem + [base] * (jobs - rem)
            unit_cost = float(r["input_cost"] or 0.0) / runs
            unit_reward = float(r["reward"] or 0.0) / runs
            con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (r["id"],))
            for n in sizes:
                # **The breach, re-measured for the row that is actually being written.** A DELETE
                # plus INSERT that omits these four columns does not leave them alone — it resets
                # them to the schema default, so a row that arrived here carrying a real overrun
                # left claiming none. Reported: `cadence_over_h: 673.2` became six rows of `0.0`,
                # each 450h against a 24h window, silently.
                #
                # `cadence_over_h` is RECOMPUTED rather than carried, because the split is exactly
                # the thing that changes it: the point of this pass is to make the job shorter, so
                # the old row's overrun is not this row's. It is 0 whenever the split succeeded,
                # and the residual whenever a formula cap or the free-reactor check bound first.
                #
                # UNITS: `n * raw` is RAW SDE hours; x `mult` makes it real wall-clock hours, the
                # space `cadence_h` is in and the space the badge prints. `_CADENCE_GRACE` is
                # absorbed, matching `per_job_cap` above and the leveller's own measurement — the
                # split deliberately spends the grace, so reporting it back as a breach would flag
                # the plan for doing what it was told.
                over_h = max(0.0, n * raw * mult - (cadence_h + _CADENCE_GRACE))
                # The other three are the leveller's cost note for this (stage, product) group and
                # have nothing to do with the split, so they are carried across unchanged.
                con.execute(
                    "INSERT INTO pp_reaction_assignments (character_id, type_id, name, runs, "
                    "input_cost, reward, created_at, tier_order, order_id, cadence_over_h, "
                    "surplus_runs, jobs_saved, recover_runs) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (r["character_id"], r["type_id"], r["name"], n, round(unit_cost * n, 2),
                     round(unit_reward * n, 2), r["created_at"], r["tier_order"], r["order_id"],
                     round(over_h, 2), int(r.get("surplus_runs") or 0),
                     int(r.get("jobs_saved") or 0), int(r.get("recover_runs") or 0)))
                written += 1
        if written:
            con.commit()
        return written
    finally:
        con.close()


def _stage_of_tiers(context_id: int, product_id: int, tiers: list) -> list[int]:
    """A STAGE index per client-supplied chain tier — steps that can run at the same time share one.

    Three sources, in order of how much they know:

      1. the tier's own `tier`, when the client sent one (the wizard and the opportunity list now
         both carry it, computed from the graph's dependency depth);
      2. the GRAPH, re-derived here — the authority, and what repairs a client that didn't send it;
      3. list position, the old rule, kept only as a last resort for a chain we cannot resolve at
         all. It is wrong for siblings, which is the whole reason this function exists.

    Never trusts (1) blindly over (2) when the two disagree in shape: a stamped stage that skips a
    number (0, 2) would leave an empty stage on the dashboard, so the values are re-densified.
    """
    if not tiers:
        return []
    stages: list[int] | None = None
    stamped = [t.tier for t in tiers]
    if all(s is not None for s in stamped):
        stages = [int(s) for s in stamped]
    else:
        try:
            loaded = _load_goo_and_reached(context_id)
            reached = loaded[1] if loaded else {}
            node = reached.get(int(product_id)) or {}
            if node.get("via"):
                depth_of = {int(tid): int((reached.get(int(tid)) or {}).get("depth") or 1)
                            for tid, _ in [(t.type_id, None) for t in tiers]}
                stages = [depth_of.get(int(t.type_id), 1) for t in tiers]
        except Exception:
            stages = None
    if stages is None:
        stages = list(range(len(tiers)))            # last resort — see the docstring
    # Densify: whatever the source, stages must be 0..n with no gaps, in dependency order.
    order = {d: i for i, d in enumerate(sorted(set(stages)))}
    return [order[d] for d in stages]


def _trim_tiers_by_stock(context_id: int, tiers: list) -> list[dict]:
    """Spend held intermediates against a caller-supplied chain, IN PLACE, and report what that
    covered as `[{type_id, name, units, runs_saved}]`.

    Each tier's requirement is its `runs x output_qty`; stock is taken off the front of that and the
    runs recomputed, with a fully-covered tier dropped from the list entirely. Deliberately does NOT
    recurse: by the time a chain reaches this function it is already flat (`_ordered_chain_tiers`
    exploded it), so a tier this drops may leave its own feeder tiers behind — those are still real
    work whose output is still consumed by the tiers above them, just not by this one. The planning
    paths that build the chain themselves (`_ordered_chain_tiers` with a pool) do the recursive
    version, and they are what the wizard and orders use.

    No pool (flag off) ⇒ the list is untouched and this returns [].
    """
    pool = reaction_stock_pool(context_id)
    if not pool or not tiers:
        return []
    # How many units a run of this product yields must come from the formula the PLAN is using —
    # `reached[tid]["via"]` — not from an arbitrary row of `reactions`. Several formulas can output
    # the same product with wildly different batch sizes (20 units vs 10,000 in this SDE), and
    # reading the wrong one misjudges coverage by that same factor, in whichever direction the row
    # order happens to fall. The graph load is cached and this only runs on a user-initiated assign
    # with the feature on and stock present.
    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    if not reached:
        return []                       # no graph, no evidence — leave the caller's chain alone

    covered: list[dict] = []
    keep = []
    for t in tiers:
        via = (reached.get(int(t.type_id)) or {}).get("via") or {}
        qty = float(via.get("output_qty") or 0)
        have = pool.get(int(t.type_id), 0.0)
        if qty <= 0 or have <= 0:
            keep.append(t)
            continue
        needed = t.runs * qty
        used = min(have, needed)
        pool[int(t.type_id)] = have - used
        left_runs = math.ceil((needed - used) / qty)
        covered.append({"type_id": int(t.type_id), "name": t.name, "units": round(used, 1),
                        "runs_saved": t.runs - max(0, left_runs)})
        if left_runs > 0:
            t.runs = left_runs
            t.job_count = max(1, min(t.job_count, left_runs))
            keep.append(t)
    tiers[:] = keep
    return [c for c in covered if c["runs_saved"] > 0]


class AssignRequest(BaseModel):
    character_id: int
    type_id: int
    name: str
    runs: int  # total runs across all jobs for this suggestion
    job_count: int = 1  # how many separate in-game job installs this splits into (one per slot)
    input_cost: float
    reward: float
    # Intermediate reactions this product's own formula needs (see _explode_chain_tiers in
    # _suggest_reactions), deepest-first — each becomes its own set of assignment rows the
    # player must install and let finish BEFORE the top-level reaction above can even start.
    chain_tiers: list[ChainTier] = []


def _assign_guards_on(context_id: int) -> bool:
    """Gated: both halves change what a repeated press DOES. Replacing rather than appending is
    almost always what was meant, but "almost" is why it rolls out rather than lands."""
    return flag_on("reactions_assign_guard", context_id)


def _clear_assignment_group(con, character_id: int, type_id: int, tier_order: int) -> int:
    """Drop this character's existing plan rows for one (product, tier) — the rows a re-assign is
    about to rewrite. Returns how many went.

    **`order_id IS NULL` only.** Rows raised by the customer-order flow belong to an order that was
    committed against real capacity; a suggestion re-assign must not silently eat them.
    """
    cur = con.execute(
        "DELETE FROM pp_reaction_assignments WHERE character_id=? AND type_id=? AND tier_order=? "
        "AND order_id IS NULL", (character_id, type_id, tier_order))
    return cur.rowcount or 0


def _assigned_slot_capacity(con, character_id: int) -> int:
    """This character's total reaction slots, or 0 when we cannot tell (never scanned).

    0 means "unknown", and unknown never refuses: the same rule the print caps follow — a cap built
    on absent evidence blocks work the player can really do.
    """
    row = con.execute(
        "SELECT character_id, mass_reactions, advanced_mass_reactions, scopes FROM pp_characters "
        "WHERE character_id=?", (character_id,)).fetchone()
    if not row:
        return 0
    try:
        return int(reaction_slots(row)) if reaction_capable(row)[0] else 0
    except Exception:
        return 0


def _parallel_stages_on(context_id: int) -> bool:
    """Gate for ONE slot model (`reactions_parallel_stages`). Off ⇒ `_character_capacities` and the
    dashboard count every planned row against the pool exactly as they always did.

    Why a flag for what is arguably a bug fix: it makes free-slot counts BIGGER, so the wizard and
    customer orders will schedule more work per character. That is the point, and it is also a
    planning-behaviour change on live accounts — CLAUDE.md rule 2.
    """
    return flag_on("reactions_parallel_stages", context_id)


def _concurrent_load(rows: list[dict], adding: dict[int, int] | None = None) -> int:
    """Peak slots a character's plan actually occupies at once.

    **Chain tiers are SEQUENTIAL** — tier 0 must finish before tier 1 can start — so counting every
    row against the slot pool would reject legitimate deep chains that never run simultaneously.
    What competes for slots is everything sharing a tier_order, so the load is the WORST tier, not
    the sum. `adding` is {tier_order: rows} for the assignment being considered.

    **This is THE slot model** — the assign guard, `_character_capacities` and the dashboard's own
    free-slot count all go through it. They used not to: the guard counted the worst tier while the
    other two counted every row, so a 3-stage chain of one job each was authorised as needing one
    slot and then reported as occupying three, and the allocators that read those numbers quietly
    planned less work than the account had reactors for.
    """
    per_tier: dict[int, int] = {}
    for r in rows:
        t = int(r.get("tier_order") or 0)
        per_tier[t] = per_tier.get(t, 0) + 1
    for t, n in (adding or {}).items():
        per_tier[t] = per_tier.get(t, 0) + n
    return max(per_tier.values(), default=0)


@router.post("/api/reactions/assign")
def assign_reaction(req: AssignRequest, context_id: int = Depends(require_context)):
    """Commit a suggested (character, product) pairing as standing "go do this" instructions —
    surfaced on the dashboard until ESI confirms a matching job is actually running, at which
    point it's auto-cleared (see get_industry_jobs). A suggestion sized to use multiple reaction
    slots at once (job_count > 1, e.g. a big batch that needs several parallel jobs to finish
    within the chosen cadence) becomes that many SEPARATE assignment rows — one per actual
    in-game job install — so the dashboard shows the real number of slots this occupies, not one
    square standing in for several.

    Any chain_tiers (intermediate reactions this product's own formula needs, e.g. goo ->
    Ferrofluid -> this product — see _explode_chain_tiers) get their own assignment rows too,
    tagged with a LOWER tier_order so the dashboard can show them as "react this first." Their
    input_cost/reward are recorded as 0 — the full chain's cost/profit is already rolled up into
    the top-level row (unit_cost is computed recursively down to raw goo), so giving the
    intermediate rows their own nonzero values would double-count it if anything ever sums
    pp_reaction_assignments financially. (Expected output value is priced LIVE off current
    market data in get_industry_jobs, not stored here — see that function's own notes on why.)"""
    ensure_reaction_assignments_table()
    # Chain tiers arrive from the CLIENT (the wizard's suggestion, or the manual modal scaling an
    # opportunity's recipe), and the opportunity list is deliberately stock-blind — it is cached and
    # its tiers get scaled linearly, which stock coverage is not. So the trim happens here, at the
    # one point every path passes through to create rows: whatever the caller asked for, a stage the
    # hangar already covers is not committed to a reactor.
    stock_covered = _trim_tiers_by_stock(context_id, req.chain_tiers)
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")

        now = _time.time()
        tier_of = _stage_of_tiers(context_id, req.type_id, req.chain_tiers)
        top_tier_order = (max(tier_of) + 1) if tier_of else 0
        guarded = _assign_guards_on(context_id)
        replaced = 0
        if guarded:
            # **Idempotent.** This endpoint was a bare INSERT, so re-posting the same suggestion
            # appended a second full set of rows — and a frontend bug that reported every SUCCESSFUL
            # assign as failed turned two suggestions into 27 rows on a 10-slot character (reported
            # 2026-08-01). The bug is fixed; any transient failure plus a retry could still do it.
            # Replacing the (character, product, tier) group makes a retry a no-op. Deliberate
            # parallelism is unaffected: how many jobs a product runs side by side is `job_count`,
            # which sets the row count WITHIN the group, so it is expressed here and not by pressing
            # the button twice.
            for tier_order, tier in zip(tier_of, req.chain_tiers):
                replaced += _clear_assignment_group(con, req.character_id, tier.type_id, tier_order)
            replaced += _clear_assignment_group(con, req.character_id, req.type_id, top_tier_order)

            # **Capacity.** Nothing stopped the total exceeding the character's real reaction slots,
            # which is how a 10-slot character ended up holding 27 rows. What competes for a slot is
            # everything at the same TIER — tiers are sequential — so a deep chain is not penalised.
            existing = [dict(r) for r in con.execute(
                "SELECT tier_order FROM pp_reaction_assignments WHERE character_id=?",
                (req.character_id,))]
            # Per STAGE, summed: two siblings in one stage really do hold two reactors at once.
            adding: dict[int, int] = {}
            for stage, t in zip(tier_of, req.chain_tiers):
                adding[stage] = adding.get(stage, 0) + max(1, t.job_count)
            adding[top_tier_order] = max(1, req.job_count)
            peak = _concurrent_load(existing, adding)
            slots = _assigned_slot_capacity(con, req.character_id)
            if slots and peak > slots:
                con.rollback()
                raise HTTPException(
                    status_code=409,
                    detail=f"That needs {peak} reaction slots at once and this character has "
                           f"{slots}. Assign fewer jobs, or spread them across characters.")

        tidy = _tidy_runs_on(context_id)
        for tier_order, tier in zip(tier_of, req.chain_tiers):
            _insert_assignment_rows(con, req.character_id, tier.type_id, tier.name, tier.runs,
                                     tier.job_count, 0.0, 0.0, tier_order, now, tidy=tidy)

        _insert_assignment_rows(con, req.character_id, req.type_id, req.name, req.runs,
                                 req.job_count, req.input_cost, req.reward, top_tier_order, now)
        con.commit()
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    # `stock_covered` is why the plan may hold fewer stages than the caller asked for — returned so
    # the UI can say so rather than leaving a stage to vanish silently.
    return {"ok": True, "replaced": replaced, "stock_covered": stock_covered}


class AdoptOrphanRequest(BaseModel):
    character_id: int
    type_id: int
    runs: int


@router.post("/api/reactions/adopt-orphan")
def adopt_orphan_job(req: AdoptOrphanRequest, context_id: int = Depends(require_context)):
    """Adopt an ORPHAN running job (one installed in-game with no plan slot — see get_industry_jobs'
    `orphan` flag) INTO the recurring plan. Creates the same pp_reaction_assignments rows a normal
    assign would (top-level + any chain tiers), costed entirely server-side from our own SDE recipe
    — ESI only told us the product + run count, the recipe gives everything else. Once adopted it
    matches (covers) the running job, so it stays hidden from "to install" while the job runs and
    reappears as "to install" when the job finishes: it's now part of the recurring loadout and its
    materials join the next-cycle shopping list. An orphan left un-adopted stays a one-off and never
    becomes recurring."""
    ensure_reaction_assignments_table()
    # All own-connection helpers below run BEFORE the write connection is opened — never hold two at
    # once (the 2026-07-13 pool-exhaustion incident).
    loaded = _load_goo_and_reached(context_id)
    if loaded is None:
        raise HTTPException(status_code=400, detail="No priced materials to cost this reaction")
    _goo, reached, _rbo, _ibr, types = loaded
    node = reached.get(req.type_id)
    if not node or not node.get("via"):
        raise HTTPException(status_code=400, detail="Not a reachable reaction product")

    settings = effective_reaction_settings(context_id)
    m = resolve_market_data(context_id, [req.type_id]).get(req.type_id)
    out_qty = node["via"]["output_qty"]
    total_out = req.runs * out_qty
    vol = (types.get(req.type_id, {}).get("volume") or 0.0)
    # Priced at BUY orders (instant sell), like every other profit figure in the package: the
    # adopted row's `reward` is a profit, and a reaction good's sell-order price is not achievable
    # profit (CLAUDE.md). `sell_price` is still passed so _value_reaction_batch can charge courier
    # collateral against the sell value, which is the freight reference's rule regardless of how
    # the goods are actually sold. Netted against `fixed_costs` — the full base, not materials and
    # job fees alone — so an adopted row agrees with the plan totals it joins.
    sell = (m or {}).get("sell_price") or 0.0
    buy = (m or {}).get("buy_price") or 0.0
    v = _value_reaction_batch(node, total_out, sell, vol, settings, buy_price=buy)
    input_cost = v["input_cost"]   # materials only — matches how plan rows store input_cost
    reward = v["net_profit_instant"]

    # Chain tiers (intermediate reactions this formula needs), same as assign_reaction — recorded at
    # 0 cost since the whole chain's cost already rolls up into the top-level row's unit_cost.
    ordered = _ordered_chain_tiers(node["via"]["inputs"], req.runs, reached,
                                    reaction_stock_pool(context_id))

    con = get_connection()
    try:
        owner = con.execute(
            "SELECT 1 FROM pp_characters WHERE character_id=? AND context_id=?",
            (req.character_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=403, detail="Not your character")
        now = _time.time()
        ranks = tier_ranks(ordered)
        for (tier_tid, info), tier_order in zip(ordered, ranks):
            _insert_assignment_rows(con, req.character_id, tier_tid,
                                     types.get(tier_tid, {}).get("name", str(tier_tid)),
                                     info["runs"], 1, 0.0, 0.0, tier_order, now,
                                     tidy=_tidy_runs_on(context_id))
        _insert_assignment_rows(con, req.character_id, req.type_id,
                                 types.get(req.type_id, {}).get("name", str(req.type_id)),
                                 req.runs, 1, input_cost, reward,
                                 (max(ranks) + 1) if ranks else 0, now)
        con.commit()
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    return {"ok": True}


@router.delete("/api/reactions/assign/{assignment_id}")
def unassign_reaction(assignment_id: int, context_id: int = Depends(require_context)):
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        owner = con.execute(
            "SELECT a.id FROM pp_reaction_assignments a JOIN pp_characters c ON c.character_id=a.character_id "
            "WHERE a.id=? AND c.context_id=?",
            (assignment_id, context_id),
        ).fetchone()
        if not owner:
            raise HTTPException(status_code=404, detail="Assignment not found")
        con.execute("DELETE FROM pp_reaction_assignments WHERE id=?", (assignment_id,))
        con.commit()
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    return {"ok": True}


@router.delete("/api/reactions/assign")
def unassign_all_reactions(context_id: int = Depends(require_context)):
    """Clear every pending assignment across all of the caller's characters in one go —
    "Clear all" on the dashboard, for starting a fresh suggestion set without hand-cancelling
    each pending slot one at a time.

    **Order-linked rows go too, and their order's counter is put back.** This used to delete them
    and leave `pp_reaction_orders.assigned_runs` untouched, which is the one combination that
    strands an order: it claims its full run count, holds no rows, schedules nothing, and cannot be
    re-assigned because `remaining` is already zero. Its sibling `_clear_assignment_group` skips
    order rows entirely (a suggestion re-assign must not eat a committed order), and that asymmetry
    is deliberate — a per-product re-assign is a narrow action, "Clear all" is the player saying
    clear all. What was neither honest nor useful was clearing them and keeping the number.

    How much to give back: the TOP row of each chain, which is what `assigned_runs` was incremented
    by in the first place (`_allocate_and_insert` returns the sum of the per-host shares, and each
    host's share IS its top-level row's runs). `_shopping_roots` already identifies exactly those
    rows, for exactly the same reason — a chain's top is the row that stands for the batch.

    A row ESI has confirmed as RUNNING is deleted too, as it always was; the job keeps running
    in-game and shows up as an orphan the player can adopt back into the plan.
    """
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    cleared = 0
    orders_reset: list[int] = []
    con = get_connection()
    try:
        char_ids = [r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        )]
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            rows = [dict(r) for r in con.execute(
                f"SELECT character_id, type_id, runs, order_id, created_at, "
                f"COALESCE(tier_order,0) AS tier_order FROM pp_reaction_assignments "
                f"WHERE character_id IN ({placeholders})", char_ids)]
            cleared = len(rows)
            con.execute(f"DELETE FROM pp_reaction_assignments WHERE character_id IN ({placeholders})",
                        char_ids)
            orders_reset = give_back_order_runs(con, rows)
            if orders_reset:
                marks = ",".join("?" * len(orders_reset))
                con.execute(f"UPDATE pp_reaction_orders SET recurring_error=NULL WHERE id IN ({marks})",
                            orders_reset)
            con.commit()
    finally:
        con.close()
    _invalidate_dashboard_cache(context_id)
    return {"ok": True, "cleared": cleared, "orders_reset": orders_reset}


def _gate_stages_account_wide(characters: list[dict]) -> None:
    """A stage is startable only once every stage BELOW it is finished across the whole account.

    `chain_stage_state` answers this per chain, which was the whole truth while a chain's every
    stage lived on the character that would consume it. Pooling ended that (`level_product_runs`
    lays a product out across whichever reactors had room), so a chain can now hold no row at all
    for a stage it is waiting on — and "every step of MY chain below this is done" is then
    vacuously true the moment the plan is made, which would light up "stage 2, start now" while
    the Carbon Fiber for it sat unstarted on somebody else.

    Conservative on purpose: a stage nobody has finished holds back every stage above it, even
    across chains that do not share materials. That matches how the plan is actually worked —
    install a stage, come back, install the next — and being early with "start now" is the failure
    that matters here. Mutates the entries in place; no return.
    """
    pending: dict[int, bool] = {}
    for c in characters:
        for e in c.get("stages") or []:
            if e.get("todo") or e.get("running"):
                pending[int(e.get("stage") or 0)] = True
    if not pending:
        return
    for c in characters:
        for e in c.get("stages") or []:
            stage = int(e.get("stage") or 0)
            if e.get("ready") and any(pending.get(st) for st in range(stage)):
                e["ready"] = False


def _plan_missing_formulas(context_id: int, characters: list[dict]) -> dict:
    """The acquire-list for what is ALREADY planned — every not-yet-running slot on the dashboard.

    Imported at call time: `library.py` is wired into the package after this module, and a
    module-level import here would depend on that ordering. A failure degrades to "nothing to
    report", which is this report's own empty state anyway.
    """
    try:
        from app.reactions.library import (missing_formulas, wanted_from_sequence,
                                            jobs_from_sequence)
        pending = [p for c in characters for p in (c.get("pending") or [])]
        # Every pending row is one in-game job, so the row count per product is how many formulas
        # have to be held at once (`missing_formulas`' `jobs`).
        return missing_formulas(context_id, wanted_from_sequence(pending),
                                jobs=jobs_from_sequence(pending))
    except Exception:
        return {"complete": False, "formulas": [], "unresolved": []}


def give_back_order_runs(con, rows: list[dict]) -> list[int]:
    """Hand a customer order back the runs its deleted plan rows were holding. Returns the ids of
    the orders that moved. Does NOT commit — the caller owns the transaction that deleted the rows,
    and the counter must move with them or not at all.

    **How much comes back is the TOP row of each chain**, which is exactly what `assigned_runs` was
    incremented by (`_allocate_and_insert` returns the sum of the per-host shares, and a host's
    share IS its top-level row's runs). `_shopping_roots` already identifies those rows, for the
    same reason: a chain's top stands for the batch, its tiers are the work underneath.

    Shared by "Clear all" and the per-order clear so the two can never drift into disagreeing about
    what a cleared order is owed — the disagreement that stranded orders #36-#39 in the first place.
    """
    by_order: dict[int, list[dict]] = {}
    for r in rows:
        if r.get("order_id"):
            by_order.setdefault(int(r["order_id"]), []).append(r)
    moved: list[int] = []
    for order_id, order_rows in by_order.items():
        give_back = sum(int(r["runs"] or 0) for r in _shopping_roots(order_rows))
        if give_back <= 0:
            continue
        # Never below zero: the counter is a commitment total, and an order whose rows were already
        # partly cleared elsewhere must not go negative and start claiming capacity it never had.
        con.execute(
            "UPDATE pp_reaction_orders SET assigned_runs = "
            "CASE WHEN assigned_runs > ? THEN assigned_runs - ? ELSE 0 END WHERE id=?",
            (give_back, give_back, order_id))
        moved.append(order_id)
    return sorted(moved)


def _plan_intermediates(context_id: int, rows: list[dict]) -> set[int]:
    """Products in this plan that ANOTHER row of it consumes — the plan's intermediates.

    Structural, from the recipes: a product is an intermediate here if something else the plan makes
    eats it. That replaced reading it off the stored costs (`input_cost == 0 and reward == 0`),
    which mislabelled a customer order's top row — stored at zero reward deliberately — as an
    intermediate, and with it every figure derived from the distinction. Empty on no graph, which
    degrades to "everything is an end product": the same reading the package had before chains.
    """
    if not rows:
        return set()
    try:
        loaded = _load_goo_and_reached(context_id)
        reached = loaded[1] if loaded else {}
    except Exception:
        return set()
    if not reached:
        return set()
    made = {int(r["type_id"]) for r in rows}
    return {int(i["type_id"])
            for tid in made
            for i in (((reached.get(tid) or {}).get("via") or {}).get("inputs") or [])} & made


def _plan_totals(context_id: int, rows: list[dict], order_meta: dict[int, dict],
                 cycle_hours_by_type: dict[int, float], output_qty_by_type: dict[int, float],
                 market_by_type: dict) -> dict:
    """What the whole plan costs, is worth, and earns per day — valued from the ROWS and today's
    prices, never from the per-row `input_cost`/`reward` written at assign time.

    Those stored figures were the old source and they were wrong twice over. **Chain-tier rows are
    stored at zero cost**, so a plan whose goo all sits in intermediates reported almost nothing
    committed (4.93m against a real ~590m of materials). And **a customer order's top row is stored
    at zero reward** on purpose — an order's revenue is what the client agreed to pay, which nothing
    here can derive — so a plan made of orders reported zero profit per day, asserting that it earns
    nothing rather than admitting nobody had said.

    So:

    * **isk_committed is MATERIALS ONLY** — the plan's materials at their current unit cost,
      literally the shopping list, priced (`_plan_materials`). It is a useful number in its own
      right ("what I must go and buy") and the dashboard labels it *Materials committed*. It is
      NOT the cost base for profit;
    * **total_cost** is that plus the costs a shopping list does not carry: job install fees,
      export freight and courier collateral, from `_value_reaction_batch` on the END products (the
      recipe roll-up already folds every intermediate's fees into the product above it, so
      charging end products only is the whole chain, counted once). **Profit is derived from
      total_cost**, never from the materials-only figure — netting revenue against an incomplete
      cost is how a plan reported a margin it does not have;
    * **output value** counts only END products, the rows nothing else in the plan consumes. An
      intermediate's value is already inside the product above it. Valued at the **buy** order
      price (instant sell) per CLAUDE.md's pricing invariant: a reaction good's sell-order price is
      not achievable profit, and this tile feeds a profit figure. It was quoting `sell_price` until
      2026-08-14;
    * an ORDER's revenue is its `client_price`, apportioned across however much of it is assigned
      so far. An order with no price contributes to neither revenue nor profit, and is counted in
      `unpriced_orders` so the caller can say so instead of showing a confident zero;
    * **profit per day** divides by the plan's MAKESPAN — stages run in sequence, so it is the sum
      over stages of the longest job in each. Dividing each row by its own duration (the old rule)
      over-counts badly on a plan that is mostly short parallel jobs. `makespan_hours` is returned
      so the caller can combine this half with the orphan-job half under ONE definition of "per
      day" instead of adding two incompatible rates together.

    Every duration here is in TIME-EFFICIENCY-REDUCED hours: `cycle_hours_by_type` is built by the
    caller from raw SDE cycle_time x (1 - time_efficiency_pct), and that efficiency is now the
    account's measured reaction multiplier (see effective_reaction_settings). It is NOT the
    raw-SDE space the leveller works in.
    """
    out = {"isk_committed": 0.0, "materials_committed": 0.0, "job_fees": 0.0, "freight": 0.0,
           "collateral": 0.0, "total_cost": 0.0, "output_value": 0.0, "sell_order_value": 0.0,
           "net_profit": 0.0, "net_profit_per_day": 0.0, "makespan_hours": 0.0,
           "unpriced_orders": 0}
    if not rows:
        return out
    loaded = _load_goo_and_reached(context_id)
    reached = loaded[1] if loaded else {}
    types = loaded[4] if loaded else {}
    if not reached:
        return out
    settings = effective_reaction_settings(context_id)

    # What the plan must BUY: its own materials, row by row, at the price the graph settled on.
    from app.reactions.graph import _plan_materials
    in_house = {int(r["type_id"]) for r in rows}

    def _cost(subset):
        pool = reaction_stock_pool(context_id)
        return sum(units * float((reached.get(tid) or {}).get("unit_cost") or 0.0)
                   for tid, units in _plan_materials(subset, reached, pool, in_house).items())

    out["isk_committed"] = out["materials_committed"] = _cost(rows)

    # END products only — anything another row eats is not sellable output. Same set the per-row
    # display uses, so a row shown as an intermediate can never be counted as revenue.
    consumed = _plan_intermediates(context_id, rows)

    order_revenue: dict[int, float] = {}
    # An order's invoice is apportioned across its TOP-tier rows only. A tier counts as consumed
    # only if a surviving row's recipe still lists it, so a tier whose only consumer was trimmed
    # away (`_trim_tiers_by_stock`) becomes a phantom end product and used to book a SECOND slice
    # of the same client_price — narrow, but it inflated revenue silently rather than failing
    # loudly. The order's real product is whatever sits at its highest stage; anything below that
    # is intermediate stock and is valued at the market like any other unsold output.
    order_top_tier: dict[int, int] = {}
    for r in rows:
        oid = r.get("order_id")
        if oid and int(r["type_id"]) not in consumed:
            order_top_tier[int(oid)] = max(order_top_tier.get(int(oid), -1),
                                           int(r.get("tier_order") or 0))
    for r in rows:
        tid = int(r["type_id"])
        if tid in consumed:
            continue
        oid = r.get("order_id")
        meta = order_meta.get(int(oid)) if oid else None
        price = (meta or {}).get("client_price")
        total_runs = (meta or {}).get("top_level_runs") or 0
        # What the chain really costs beyond its materials: job install fees, export freight and
        # courier collateral. Charged on end products only — the recipe roll-up on `node` already
        # carries every intermediate's fees — so this is the whole chain, counted once.
        node = reached.get(tid)
        oq = output_qty_by_type.get(tid, 0.0)
        m = market_by_type.get(tid)
        total_out = int(r["runs"] or 0) * oq
        v = None
        if node and node.get("via") and total_out > 0:
            vol = ((types.get(tid, {}) or {}).get("volume") or 0.0)
            sell = (m or {}).get("sell_price", 0.0) or 0.0
            buy = (m or {}).get("buy_price", 0.0) or 0.0
            v = _value_reaction_batch(node, total_out, sell, vol, settings, buy_price=buy)
            out["job_fees"] += v["job_cost"]
            out["freight"] += v["shipping"]
            out["collateral"] += v["collateral"]
        is_order_top = bool(oid) and int(r.get("tier_order") or 0) == order_top_tier.get(int(oid or 0), -1)
        if oid and price and total_runs > 0 and is_order_top:
            # An order is sold at its AGREED price, not at the market — apportioned by how much of
            # it this row represents, so a half-assigned order contributes half its invoice.
            out["output_value"] += float(price) * (int(r["runs"] or 0) / total_runs)
            out["sell_order_value"] += float(price) * (int(r["runs"] or 0) / total_runs)
        else:
            # No agreed price (or not an order at all): value the goods at what they are worth on
            # the market. For an order that is a STAND-IN, not the invoice — but it is the honest
            # floor ("if the client fell through you could sell these"), and it beats reporting a
            # plan full of real work as producing nothing, which is what a hard 0 did.
            if oid and not price and (meta or {}).get("source_kind") != "manufacturing":
                order_revenue.setdefault(int(oid), 0.0)
            if v is not None:
                # Buy orders, not sell: this feeds a profit figure (CLAUDE.md's pricing invariant).
                out["output_value"] += v["instant_value"]
                out["sell_order_value"] += v["output_value"]
            elif m and oq:
                out["output_value"] += total_out * (m.get("buy_price") or 0.0)
                out["sell_order_value"] += total_out * (m.get("sell_price") or 0.0)
    out["unpriced_orders"] = len([o for o, v in order_revenue.items() if v == 0.0])

    # Every row is now valued one way or the other — at the client's price where there is one, at
    # the market otherwise — so profit is simply value minus cost. `unpriced_orders` no longer means
    # "left out of this number"; it means "part of it is a market estimate, not an invoice", which
    # is what the dashboard says. The cost it is netted against is the FULL one, not the shopping
    # list: job fees, freight and collateral are ISK the plan really spends.
    out["total_cost"] = (out["materials_committed"] + out["job_fees"]
                         + out["freight"] + out["collateral"])
    out["net_profit"] = out["output_value"] - out["total_cost"]
    # Makespan: stages are sequential, so the plan is done when the last stage's longest job is.
    # Reduced hours (see the docstring), and returned so the orphan half can share this definition.
    by_stage: dict[int, float] = {}
    for r in rows:
        hours = int(r["runs"] or 0) * cycle_hours_by_type.get(int(r["type_id"]), 0.0)
        st = int(r.get("tier_order") or 0)
        by_stage[st] = max(by_stage.get(st, 0.0), hours)
    out["makespan_hours"] = sum(by_stage.values())
    days = out["makespan_hours"] / 24.0
    out["net_profit_per_day"] = out["net_profit"] / days if days > 0 else 0.0
    return out


def _unplanned_running_totals(context_id: int, unplanned_running: list[tuple[int, float]],
                              output_qty_by_type: dict[int, float],
                              cycle_hours_by_type: dict[int, float]) -> dict[str, float]:
    """Committed-total deltas (ISK / output value / net profit / makespan) for ORPHAN running
    jobs — ones running in-game with no plan slot (see get_industry_jobs). ESI gives only product +
    run count, so they're valued from our own SDE recipe via _value_reaction_batch, priced with the
    caller's own settings (group sheet, reaction system, time efficiency) exactly like every other
    path. Lazy: the (heavier) reaction cost graph loads only when there ARE such jobs, so a fully-
    planned dashboard pays nothing. A product with no reachable recipe or no live market price is
    skipped rather than guessed — same rule the opportunity list follows.

    **Two things this used to get wrong, and one tile carried both.** It valued output at
    `sell_price` — the achievable-profit signal must be the buy order (CLAUDE.md's pricing
    invariant) — and it netted against materials + job fees only. Both fixed: `fixed_costs` is the
    full base and `instant_value` is what a buy order pays.

    **And it returns no rate.** It used to compute `net_profit / (runs x cycle / 24)` per job and
    SUM those, which is exactly the over-counting rule `_plan_totals`' own docstring abandoned: a
    handful of short parallel jobs each report a huge daily rate and the sum is nonsense. That sum
    was then ADDED to the plan's makespan-derived rate, so one tile held two incompatible
    definitions of "per day". It now returns `makespan_hours` — orphan jobs run in parallel, so
    theirs is the longest single job — and the caller derives ONE rate for both halves.

    Hours here are TIME-EFFICIENCY-REDUCED (`cycle_hours_by_type`), not raw SDE."""
    totals = {"isk_committed": 0.0, "output_value": 0.0, "sell_order_value": 0.0,
              "job_fees": 0.0, "freight": 0.0, "collateral": 0.0, "total_cost": 0.0,
              "net_profit": 0.0, "makespan_hours": 0.0}
    if not unplanned_running:
        return totals
    graph = _load_goo_and_reached(context_id)
    reached = graph[1] if graph else {}
    types = graph[4] if graph else {}
    settings = effective_reaction_settings(context_id)
    up_ids = list({tid for tid, _ in unplanned_running})
    up_market = resolve_market_data(context_id, up_ids) if up_ids else {}
    for tid, runs in unplanned_running:
        node = reached.get(tid)
        m = up_market.get(tid)
        if not node or not node.get("via") or not m:
            continue
        total_out = runs * output_qty_by_type.get(tid, 0.0)
        vol = (types.get(tid, {}).get("volume") or 0.0)
        v = _value_reaction_batch(node, total_out, m["sell_price"], vol, settings,
                                  buy_price=(m.get("buy_price") or 0.0))
        totals["isk_committed"] += v["input_cost"]
        totals["job_fees"] += v["job_cost"]
        totals["freight"] += v["shipping"]
        totals["collateral"] += v["collateral"]
        totals["total_cost"] += v["fixed_costs"]
        totals["output_value"] += v["instant_value"]
        totals["sell_order_value"] += v["output_value"]
        totals["net_profit"] += v["net_profit_instant"]
        cyc = cycle_hours_by_type.get(tid, 0)
        if cyc > 0 and runs > 0:
            # Parallel, not sequential: these are separate jobs already in reactors, so the orphan
            # half is done when its LONGEST one is.
            totals["makespan_hours"] = max(totals["makespan_hours"], runs * cyc)
    return totals


_DASHBOARD_CACHE_TTL = 20  # short-lived Redis cache for the whole /api/reactions/jobs payload.
# This endpoint was the slowest thing on tab-open: it repairs the plan (restage + level + split,
# each its own pass over every assignment row), then re-reads it and prices it live off today's
# market — all synchronously, on every plain tab-open/tab-switch, even when nothing had changed
# since the last read. The repair passes are idempotent (re-running them against an already-tidy
# plan is a no-op), so skipping them for a few seconds costs nothing but the redundant work.
# Every endpoint that can change the plan's shape or its inputs (assign/unassign/mark/adopt-orphan,
# order actions, cadence and settings saves, a jobs refresh that actually pulled something new)
# invalidates this key explicitly via _invalidate_dashboard_cache — the TTL is only a safety net
# for the rare write path that doesn't, same "TTL is enough" convention as _OPPS_CACHE_TTL.


def _dashboard_cache_key(context_id: int) -> str:
    return f"rx:dash:{context_id}"


def _invalidate_dashboard_cache(context_id: int) -> None:
    cache_invalidate(_dashboard_cache_key(context_id))


@router.get("/api/reactions/jobs")
def get_industry_jobs(context_id: int = Depends(require_context)):
    """Personal reaction-job status for the Reactions wizard's dashboard page — see
    `_get_industry_jobs_uncached` for the payload shape. Cached for `_DASHBOARD_CACHE_TTL`
    seconds, invalidated on every write that can change it (see `_invalidate_dashboard_cache`)."""
    cache_key = _dashboard_cache_key(context_id)
    cached = cache_get_json(cache_key)
    if cached is not None:
        return cached
    result = _get_industry_jobs_uncached(context_id)
    cache_set_json(cache_key, result, ttl=_DASHBOARD_CACHE_TTL)
    return result


def _get_industry_jobs_uncached(context_id: int) -> dict:
    """Personal reaction-job status for the Reactions wizard's dashboard page: currently
    running jobs (from the last refresh), a capacity summary (free slots right now, across
    every character that's opted into tracking), the per-character opt-in breakdown so the UI
    can offer to connect any character that hasn't opted in yet, and any standing "assigned but
    not yet actually running" instructions (see assign_reaction) — a context can hold several
    characters (an account's own alts, or characters from separate EVE accounts logged into the
    same session), and each authorises the tracking scope independently."""
    # ── Step 1: repair the plan BEFORE reading it (own connections — never two at once) ─────────
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    ensure_reaction_orders_table()
    # Fetched BEFORE opening the main connection below, not inside that try block — this opens
    # its OWN connection internally (member_group/get_reaction_settings/account override), and
    # holding two connections open at once per request is exactly what exhausted the pool under
    # concurrency (see app.db._pg_pool's queue.Queue: bounded at pool size, get() waits up to 15s
    # then raises) — a real production incident on 2026-07-13 traced to this exact pattern taking
    # down unrelated endpoints (Dashboard, Setup Analysis) once the pool was saturated. Never hold
    # a second get_connection() open while a first one from the same request is still live.
    time_eff = effective_reaction_settings(context_id).get("time_efficiency_pct", 0.0)
    # Repair the plan BEFORE it is read, not after. Both passes rewrite `pp_reaction_assignments`
    # (`restage_plan_rows` fixes stages stamped under the old position rule; the levelling pass
    # gives each product one run count and re-splits the jobs), and they used to run at the far end
    # of this function — after the SELECT below had already copied the rows into `assignments`. The
    # writes landed, so a SECOND load showed the levelled plan and the one that triggered it showed
    # the old numbers: a plan assigned and immediately re-read came back with the numbers it was
    # asked for rather than the ones it now holds, which reads as the pass not working at all.
    # Both open their own connections, so they must run before the read connection below exists —
    # never two at once (the 2026-07-13 pool-exhaustion incident, noted above).
    try:
        restage_plan_rows(context_id)
        # ...and give each product one run count instead of one per assign, so the numbers being
        # typed into the industry window are the same every time. Across every character behind
        # `reactions_level_runs` (which also re-splits the work into as many jobs as that number
        # needs); within a character otherwise, which is as far as the row count can stay fixed.
        if _level_runs_on(context_id):
            level_product_runs(context_id)
        else:
            level_stage_runs(context_id)
        # ...and the one thing the leveller may not touch: a customer order's own top row. It is
        # split here instead, preserving the batch total exactly, so the cadence reaches the whole
        # chain rather than stopping one stage short of the product being sold.
        split_order_tops_to_cadence(context_id)
    except Exception:
        pass
    # What the player has marked running or done by hand. Own connection, so it belongs here with
    # the other step-1 reads and not inside the one below — never two at once.
    marks = reaction_manual_marks(context_id) if _manual_done_on(context_id) else {}
    # ── Step 2: one connection — characters, cached ESI jobs, assignments, orders, SDE lookups ───
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json, fetched_at FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        assignments: dict[int, list] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                # ...and what the leveller decided this layout cost (Step 1 wrote them, per
                # (stage, product) — see `ensure_reaction_assignments_table`). They are read here
                # rather than recomputed because the pass that knows the counterfactual has already
                # run and closed its connection by the time these rows are read.
                f"SELECT id, character_id, type_id, name, runs, input_cost, reward, tier_order, "
                f"created_at, order_id, COALESCE(cadence_over_h,0) AS cadence_over_h, "
                f"COALESCE(surplus_runs,0) AS surplus_runs, COALESCE(jobs_saved,0) AS jobs_saved, "
                f"COALESCE(recover_runs,0) AS recover_runs "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) ORDER BY tier_order", char_ids,
            ):
                assignments.setdefault(r["character_id"], []).append(dict(r))
        # Client-order labels for any pending row committed via a customer order (see
        # _allocate_and_insert) — so the dashboard can show "Order: <client>" on those slots
        # instead of just the product name, distinguishing client-committed jobs from
        # speculative-profit ones at a glance.
        order_ids = list({a["order_id"] for rows in assignments.values() for a in rows if a.get("order_id")})
        order_labels: dict[int, str] = {}
        order_meta: dict[int, dict] = {}
        if order_ids:
            placeholders_o = ",".join("?" * len(order_ids))
            for r in con.execute(
                f"SELECT id, client_name, client_price, top_level_runs, source_kind FROM pp_reaction_orders "
                f"WHERE id IN ({placeholders_o})", order_ids,
            ):
                order_labels[r["id"]] = r["client_name"] or f"Order #{r['id']}"
                # ...and what the client pays, so an order's rows can be valued at the agreed price
                # rather than at a market rate nobody is selling them at. See `_plan_totals`.
                order_meta[r["id"]] = {"client_price": r["client_price"],
                                       "top_level_runs": r["top_level_runs"],
                                       "source_kind": r["source_kind"]}
        # output_type_id -> cycle hours, so a stored assignment (which only keeps `runs`, not its
        # own formula) can be turned into a real duration for the profit/day normalization below —
        # PI's headline number is already a rate (value_per_day), so Reactions' should be too.
        # Reduced by time_eff (fetched above, before this connection was opened) — this query
        # bypasses _load_reaction_graph (which applies the same correction for the opportunity/
        # suggestion/order paths), so the reduction has to be applied here too or a pending
        # assignment's reported profit/day would understate itself using the slower raw SDE time.
        # Same idea, output units per run — needed to turn a pending row's `runs` into an actual
        # output quantity for the live output-value estimate below. Both dicts come from one scan.
        cycle_hours_by_type = {}
        output_qty_by_type = {}
        for r in con.execute("SELECT output_type_id, cycle_time, output_qty FROM reactions"):
            cycle_hours_by_type[r["output_type_id"]] = (r["cycle_time"] or 0) * (1 - time_eff) / 3600.0
            output_qty_by_type[r["output_type_id"]] = r["output_qty"]
        # output_type_id -> set of its recipe's input type_ids, so a running job whose product is
        # itself an input to ANOTHER running reaction can be flagged as an intermediate (its output
        # is consumed on-site by the next tier, not a separately sellable end product). Used to keep
        # the dashboard's "produced units" export to real end products only — see the `consumed`
        # flag on each running job below.
        reaction_inputs_by_output: dict[int, set[int]] = {}
        for r in con.execute(
            "SELECT r.output_type_id AS out, ri.type_id AS inp "
            "FROM reactions r JOIN reaction_inputs ri ON ri.reaction_id = r.reaction_id"
        ):
            reaction_inputs_by_output.setdefault(r["out"], set()).add(r["inp"])
        # Product names for the running-job display — a running job carries only its product
        # type_id from ESI, and the frontend's opportunity-list name lookup misses anything not
        # currently in that list (it showed "#16665" for Hexite). Bounded to reaction outputs
        # (~68 rows), so this is cheap and always complete.
        name_by_type = {r["type_id"]: r["name"] for r in con.execute(
            "SELECT type_id, name FROM types WHERE type_id IN (SELECT output_type_id FROM reactions)")}
    finally:
        con.close()

    # ── Step 3: price the plan live off today's market ──────────────────────────────────────────
    # Expected output value is priced LIVE off today's market, not stored at assign-time — a
    # stored snapshot would need retroactive backfilling for every row created before this
    # existed (impossible — no way to know a past market price) and would go stale for older
    # rows anyway as prices move. One bulk fetch across every distinct assigned type_id, same
    # pattern _build_opportunities already uses.
    all_assigned_type_ids = list({r["type_id"] for rows in assignments.values() for r in rows})
    market_by_type = resolve_market_data(context_id, all_assigned_type_ids) if all_assigned_type_ids else {}

    # ── Step 4: per character — match running jobs to plan rows, build pending + stages ─────────
    now = _time.time()
    running: list[dict] = []
    # Per product, account-wide: what the plan asks for, and what the jobs really installed will
    # make. Compared once at the end — under-production is a failure the player must hear about (the
    # stage above cannot start, and they find out in a structure with the materials already bought);
    # over-production is just stock, and is deliberately silent.
    _want: dict[int, int] = {}
    _cover: dict[int, int] = {}
    _pname: dict[int, str] = {}
    characters: list[dict] = []
    # Time-weighted overall completion of all running jobs: Σ elapsed / Σ total duration.
    running_elapsed_sec = 0.0
    running_total_sec = 0.0
    total_slots = 0
    used_slots = 0
    peak_only = _parallel_stages_on(context_id)
    tracked_any = False
    # Which planned products another planned row consumes — the structural read of "this is an
    # intermediate", replacing the old `input_cost == 0 and reward == 0` proxy.
    all_rows = [a for rows_ in assignments.values() for a in rows_]
    consumed_by_plan = _plan_intermediates(context_id, all_rows)
    plan_totals = _plan_totals(context_id, all_rows, order_meta,
                                cycle_hours_by_type, output_qty_by_type, market_by_type)
    pending_isk_committed = plan_totals["isk_committed"]
    pending_net_profit = plan_totals["net_profit"]
    pending_net_profit_per_day = plan_totals["net_profit_per_day"]
    pending_output_value = plan_totals["output_value"]
    unplanned_running: list[tuple[int, float]] = []  # (product_type_id, runs) of running jobs with
    # no covering plan row — installed straight in-game (e.g. a corp job) rather than via the tool's
    # assign flow. Valued after the loop from our own SDE recipe so they still count in the totals.
    for c in chars:
        opted_in, why_not = reaction_capable(c)
        slots = reaction_slots(c)
        if not opted_in:
            characters.append({"character_name": c["character_name"], "tracked": False,
                               "slots": slots, "reason": why_not,
                               # Held a jobs snapshot, then lost the SCOPE — re-authorising through
                               # the normal login drops the opt-in one. Different from "never
                               # connected": there is stale data on the page to explain, and the
                               # alert rescan is holding this character's reaction alerts back.
                               # The scope has to be tested explicitly: `opted_in` is also false for
                               # a scope-holding character with no reaction skill trained, and that
                               # one is tracked fine — flagging it would be a permanent warning
                               # about nothing, which re-authorising could never clear.
                               "scope_lost": ("read_character_jobs" not in (c["scopes"] or "")
                                              and c["character_id"] in cached)})
            continue
        tracked_any = True
        total_slots += slots
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        active = [j for j in jobs if j.get("status") in ("active", "paused", "ready")]
        used_slots += len(active)
        # Count-aware, not just a set of type_ids present — a big batch can be split into
        # several separate pending assignment rows for the SAME product (one per job slot), so
        # only as many of them may be cleared as there are actually-running jobs of that type;
        # naive set-membership would wrongly clear every pending row for a product the moment
        # just ONE of its several intended jobs gets installed.
        running_type_counts: dict[int, int] = {}
        # ...and the RUN COUNT each of those jobs really carries, oldest first, so a row can be
        # credited with what was actually installed rather than what the plan proposed. The two
        # differ whenever the industry window's default is accepted instead of the planned number.
        running_runs: dict[int, list[int]] = {}
        for j in active:
            tid = j.get("product_type_id")
            running_type_counts[tid] = running_type_counts.get(tid, 0) + 1
            running_runs.setdefault(tid, []).append(int(j.get("runs") or 0))

        # How many rows of each (product, stage) group the player has marked by hand, spent row by
        # row below so marking 2 of a group's 4 jobs leaves the other 2 alone. A marked row STAYS in
        # `pending` and carries the mark instead of vanishing: the page has to be able to draw it
        # and let the player take the mark back, and a row that quietly disappeared when ticked
        # would be a mark you could set and never clear.
        manual_left: dict[tuple[int, int, str], int] = {}
        if marks:
            grouped: dict[tuple[int, int], int] = {}
            for a in assignments.get(c["character_id"], []):
                k = (a["type_id"], int(a.get("tier_order") or 0))
                grouped[k] = grouped.get(k, 0) + 1
            for (tid, tier), n in grouped.items():
                for st in _RX_STATES:
                    got = manual_jobs(marks, c["character_id"], tid, tier, n, st)
                    if got:
                        manual_left[(tid, tier, st)] = got

        pending = []
        for a in assignments.get(c["character_id"], []):
            # A live ESI job of this product covers a planned slot: it's hidden from the "to
            # install" list (the running-job square already occupies that slot in the loadout) and
            # NOT deleted — the plan is a persistent loadout, so when the job finishes and drops
            # out of ESI this row reappears as "to install". Count-aware, so N running jobs cover
            # exactly N of this product's planned rows.
            is_running = running_type_counts.get(a["type_id"], 0) > 0
            if is_running:
                running_type_counts[a["type_id"]] -= 1
                # What this row is really going to make. A job installed at 120 runs where the plan
                # said 113 covers MORE than the row promised; one installed at 100 covers less, and
                # that is the case worth telling the player about.
                got = running_runs.get(a["type_id"]) or []
                made = got.pop(0) if got else int(a["runs"] or 0)
                _cover[a["type_id"]] = _cover.get(a["type_id"], 0) + made
            else:
                _cover[a["type_id"]] = _cover.get(a["type_id"], 0) + int(a["runs"] or 0)
            _want[a["type_id"]] = _want.get(a["type_id"], 0) + int(a["runs"] or 0)
            _pname[a["type_id"]] = a["name"]
            # A hand mark on a row ESI has nothing to say about. Done is spent first: with a group
            # part-done and part-running, the finished ones are the ones that are certainly over.
            marked = None
            if not is_running:
                tier_a = int(a.get("tier_order") or 0)
                for st in (_RX_DONE, _RX_RUNNING):
                    if manual_left.get((a["type_id"], tier_a, st), 0) > 0:
                        manual_left[(a["type_id"], tier_a, st)] -= 1
                        marked = st
                        break
            # An INTERMEDIATE is a product another row of this plan consumes — its output is not
            # sellable, it feeds the stage above. Read structurally (`in_house`, below) rather than
            # from the stored `input_cost == 0 and reward == 0`, which was only ever a proxy: it
            # also caught a customer order's top row (stored at zero reward on purpose) and any row
            # whose costs had not been written, and it is the reason the headline totals were wrong.
            m = market_by_type.get(a["type_id"])
            out_qty_per_run = output_qty_by_type.get(a["type_id"], 0.0)
            # There is deliberately NO per-row `output_value` here any more. It was quoted at the
            # sell price while `pending_output_value` — the tile a user actually reads — is quoted
            # at buy orders, so the payload contradicted itself by the whole spread (290.7m against
            # 277.5m on a live plan). Nothing in `reactions.js` ever read it, so deleting it is
            # strictly better than repricing a field with no reader: one fewer number that can drift,
            # and one of the two remaining entries off the pricing guard's known-open list.
            # How long this job will actually run, so a pending row can say what it costs in time.
            # UNITS: `cycle_hours_by_type` is the time-efficiency-REDUCED clock (see its build
            # above), i.e. real wall-clock hours — the same space every other duration on this page
            # is in, and the space `cadence_over_h` was converted into by the leveller.
            row_hours = round(int(a["runs"] or 0) * cycle_hours_by_type.get(a["type_id"], 0.0), 2)
            # What the levelling pass decided, and what it cost — priced HERE because this is the
            # surface holding today's market, while the pass that chose the layout is not.
            #
            # Instant-sell (buy orders), per CLAUDE.md's pricing invariant: surplus intermediates
            # are stock, and what stock is worth is what you would actually get for it. Both figures
            # are per (type_id, tier_order) and repeated on every row of that group — a reader takes
            # each ONCE per group rather than summing rows, since the leveller pools a product
            # across characters and the number belongs to the pool.
            _unit_isk = (float(m["buy_price"]) * out_qty_per_run) if (m and out_qty_per_run) else 0.0
            # Only NOT-yet-running rows show up as "to install" squares.
            if not is_running:
                pending.append({
                    "assignment_id": a["id"], "type_id": a["type_id"], "name": a["name"], "runs": a["runs"],
                    "tier_order": a["tier_order"],
                    "hours": row_hours,
                    # Real hours this job runs PAST the player's window, 0 when it fits. The cadence
                    # is a stated target, not an absolute: a stage with more work than its free
                    # reactors can turn over inside the window overruns, and this is it saying so
                    # rather than the plan quietly quoting eleven days on a seven-day rhythm.
                    "cadence_over_h": round(float(a.get("cadence_over_h") or 0.0), 2),
                    "surplus_isk": round(int(a.get("surplus_runs") or 0) * _unit_isk, 2),
                    "recoverable_isk": round(int(a.get("recover_runs") or 0) * _unit_isk, 2),
                    "jobs_saved": int(a.get("jobs_saved") or 0),
                    # Whether dropping to a per-product run count would give anything back at all —
                    # DERIVED here rather than left to the reader to infer from `recoverable_isk`,
                    # which is 0 both when there is nothing to recover and when the product simply
                    # has no live price. The "N more numbers to type" half of the remedy counts
                    # products, not ISK, so it must not silently shrink when a price goes missing.
                    "can_recover": int(a.get("recover_runs") or 0) > 0,
                    # `running` or `done` when the player said so by hand and ESI has not caught up
                    # (or never will). Absent otherwise. Keeps the row on the page so the mark can
                    # be taken back, while the checklist and the slot count both read it.
                    "marked": marked,
                    # Which assign wrote this row — the chain it belongs to, so the UI can tell
                    # whether ITS stage below has finished rather than some other plan's.
                    "chain": round(float(a.get("created_at") or 0.0), 3), "input_cost": a["input_cost"], "reward": a["reward"],
                    "order_id": a.get("order_id"), "order_label": order_labels.get(a.get("order_id")),
                })
        # What the plan occupies AT ONCE, which for pending rows is the worst tier and not the
        # count: stage 2 is queued behind stage 1, so it is not holding a reactor while stage 1
        # runs. Same model as the assign guard and `_character_capacities` — the three used to
        # disagree, and this was the one the player read off the page.
        # A job marked DONE has given its reactor back — it is over, and counting it would idle a
        # slot the character really has. One marked RUNNING is the opposite: it is cooking right
        # now, ESI simply cannot see it yet, so it holds its slot exactly as a job ESI reports
        # would. Getting this backwards is what would make a hand mark cost the player capacity.
        holding = [p for p in pending if p.get("marked") != _RX_DONE]
        pending_load = _concurrent_load(holding) if peak_only else len(holding)
        used_slots += pending_load

        characters.append({
            "character_id": c["character_id"], "character_name": c["character_name"], "tracked": True,
            "slots": slots, "free_slots": max(0, slots - len(active) - pending_load),
            "pending": pending,
            # Opted into tracking but the token lacks the structure-read scope — facility names
            # can't resolve (show "Structure #<id>"); the UI nudges a re-authorise. See
            # esi.STRUCTURES_SCOPE's note on why older tokens don't carry it.
            "needs_structures": "universe.read_structures" not in (c["scopes"] or ""),
            # Per-chain, per-stage progress read off ESI's own job states — what makes "you can
            # start stage 2 now" a fact the page can state instead of a wait the player has to
            # track themselves. See chain_stage_state.
            "stages": chain_stage_state(assignments.get(c["character_id"], []), jobs, now, marks),
            # What this character has been marked by hand, so the page can draw the tick it is
            # showing rather than infer it back out of a covered row that now looks like any other.
            "marks": [{"type_id": tid, "tier_order": tier, "state": st,
                       "jobs": None if n == _RX_ALL else n}
                      for (cid, tid, tier), (n, st) in (marks or {}).items()
                      if cid == c["character_id"]],
        })
        # ── Step 4a: orphan jobs (running in-game with no plan slot) and running-job rows ───────
        # Whatever remains in running_type_counts after plan-row matching is ORPHAN jobs: running
        # in-game with no plan slot (installed outside the tool's assign flow — e.g. a corp job).
        # Flag each running job as orphan/planned here, collect orphans for SDE-recipe valuation
        # after the loop, and carry character_id so the UI can offer "add to plan" on an orphan.
        orphan_remaining = dict(running_type_counts)
        for j in active:
            tid = j.get("product_type_id")
            is_orphan = orphan_remaining.get(tid, 0) > 0
            if is_orphan:
                orphan_remaining[tid] -= 1
                unplanned_running.append((tid, j.get("runs") or 0))
            end = j.get("end_date")
            start = j.get("start_date")
            hours_left = None
            progress_pct = None
            if end:
                try:
                    end_ts = datetime.fromisoformat(end.replace("Z", "+00:00")).timestamp()
                    hours_left = round((end_ts - now) / 3600.0, 1)
                    if start:
                        start_ts = datetime.fromisoformat(start.replace("Z", "+00:00")).timestamp()
                        total = end_ts - start_ts
                        if total > 0:
                            progress_pct = max(0.0, min(1.0, (now - start_ts) / total))
                            running_elapsed_sec += max(0.0, min(total, now - start_ts))
                            running_total_sec += total
                except Exception:
                    pass
            running.append({
                "character_id": c["character_id"],
                "character_name": c["character_name"],
                "product_type_id": tid,
                "name": name_by_type.get(tid),
                "runs": j.get("runs"),
                # Units produced per run (from our SDE recipe) so the dashboard can total the
                # end product of every running job (runs × output_qty) into a copyable list.
                "output_qty": output_qty_by_type.get(tid, 0.0),
                "facility_name": j.get("facility_name"),
                "status": j.get("status"),
                "hours_left": hours_left,
                "progress_pct": round(progress_pct, 4) if progress_pct is not None else None,
                "orphan": is_orphan,
            })

    # ── Step 5: flag intermediates consumed by another running reaction ─────────────────────────
    # Flag each running job that is an INTERMEDIATE consumed by another running reaction: its
    # product is an input to some other running job's recipe, so its output feeds the next tier
    # on-site rather than being a separately sellable end product. Lets the "produced units" export
    # count only real end products — summing every running job (intermediates included) and pricing
    # it double-counts value already embedded in the final product (a chain reaction's units are
    # not additional sellable output). Purely running-set based, so it also covers orphan jobs.
    running_output_types = {j["product_type_id"] for j in running}
    consumed_types = {
        inp for out in running_output_types
        for inp in reaction_inputs_by_output.get(out, ())
        if inp in running_output_types
    }
    for j in running:
        j["consumed"] = j["product_type_id"] in consumed_types

    # ── Step 6: fold in unplanned running jobs, then gate stages account-wide ───────────────────
    # Fold in running jobs that had no plan slot (see _unplanned_running_totals) — valued from our
    # own SDE recipe so in-game/corp jobs still count toward the committed totals.
    up = _unplanned_running_totals(context_id, unplanned_running, output_qty_by_type, cycle_hours_by_type)
    pending_isk_committed += up["isk_committed"]          # MATERIALS only, both halves
    pending_output_value += up["output_value"]
    pending_net_profit += up["net_profit"]
    # The full cost base behind the profit figure: materials plus job install fees, export freight
    # and courier collateral. `pending_isk_committed` deliberately stays materials-only — it is the
    # priced shopping list and the dashboard labels it as one — but profit is never netted against
    # it. See `_plan_totals`.
    pending_total_cost = plan_totals["total_cost"] + up["total_cost"]
    pending_job_fees = plan_totals["job_fees"] + up["job_fees"]
    pending_freight = plan_totals["freight"] + up["freight"]
    pending_collateral = plan_totals["collateral"] + up["collateral"]
    pending_sell_order_value = plan_totals["sell_order_value"] + up["sell_order_value"]
    # ONE definition of "per day" for both halves. The plan's own half is makespan-derived (stages
    # are sequential; see `_plan_totals`); the orphan half is the longest job already in a reactor.
    # The two run CONCURRENTLY — orphans are cooking while the plan waits to be installed — so the
    # combined window is the longer of the two, not their sum. Adding the two halves' RATES, which
    # is what this did until 2026-08-14, mixed a makespan rate with a sum of per-job rates and
    # over-counted whichever half was made of short parallel jobs.
    _span_h = max(plan_totals["makespan_hours"], up["makespan_hours"])
    pending_net_profit_per_day = pending_net_profit / (_span_h / 24.0) if _span_h > 0 else 0.0

    # A stage is startable only once every stage below it is done ACROSS the account — see
    # `_gate_stages_account_wide`. Applied here, with every character's stages in hand, rather than
    # inside `chain_stage_state`, which sees one character at a time.
    _gate_stages_account_wide(characters)

    # ── Step 7: assemble the dashboard payload ──────────────────────────────────────────────────
    # One entry per product that will fall SHORT, with the runs to add. A job installed at fewer
    # runs than planned is the only way this happens, and it is invisible until the stage above
    # refuses to start — reported after a batch where three jobs went in at 120 runs instead of the
    # planned 113. Over-production produces nothing here on purpose: 21 runs of spare goo is stock,
    # not a problem, and a warning nobody needs to act on is one they learn to ignore.
    under_production = [
        {"type_id": tid, "name": _pname.get(tid, str(tid)),
         "planned": _want[tid], "covered": _cover.get(tid, 0),
         "short_runs": _want[tid] - _cover.get(tid, 0)}
        for tid in sorted(_want, key=lambda t: -(_want[t] - _cover.get(t, 0)))
        if _want[tid] - _cover.get(tid, 0) > 0
    ]

    free_slots = max(0, total_slots - used_slots)
    return {
        "tracked": tracked_any,
        "characters": characters,
        "under_production": under_production,
        "running": sorted(running, key=lambda r: r["hours_left"] if r["hours_left"] is not None else 1e9),
        "running_progress_pct": round(running_elapsed_sec / running_total_sec, 4) if running_total_sec > 0 else None,
        "total_slots": total_slots,
        "free_slots": free_slots,
        # Unlike Manufacturing, a saved reaction assignment claims capacity before installation.
        # Keep the legacy flat fields during migration; new shared UI reads this explicit contract.
        "capacity": capacity_contract(reservation_model="reserved",
                                      reaction=(total_slots, free_slots)),
        # MATERIALS only — the priced shopping list. Rendered as "Materials committed"; it is not
        # what profit is netted against (that is pending_total_cost). See `_plan_totals`.
        "pending_isk_committed": round(pending_isk_committed, 2),
        # The full cost base, and its parts, so the UI can show what the shopping list leaves out.
        "pending_total_cost": round(pending_total_cost, 2),
        "pending_job_fees": round(pending_job_fees, 2),
        "pending_freight": round(pending_freight, 2),
        "pending_collateral": round(pending_collateral, 2),
        "pending_net_profit": round(pending_net_profit, 2),
        "pending_net_profit_per_day": round(pending_net_profit_per_day, 2),
        # Output at BUY orders (instant sell) — the achievable figure profit is derived from.
        "pending_output_value": round(pending_output_value, 2),
        # ...and the same output at sell orders, as market context only. Never netted into profit.
        "pending_sell_order_value": round(pending_sell_order_value, 2),
        "pending_makespan_hours": round(_span_h, 2),
        # Formulas the CURRENT plan needs and the account does not hold. The three planning
        # surfaces already refuse to hand you a stage you can't install, but only from the moment
        # they were switched on — a plan assigned before that (or before a formula was sold) sits
        # in these slots with nothing saying why it can't be installed. This is the same report,
        # asked of what is actually planned. Empty unless a paste made the library complete.
        "missing_formulas": _plan_missing_formulas(context_id, characters),
        # Orders in the plan with no agreed price. Their production cost IS counted (it is real ISK
        # you spend) but they contribute no revenue, so the profit figure understates by however
        # much they are worth. Reported so the UI can say that rather than show a confident number.
        "unpriced_orders": plan_totals["unpriced_orders"],
    }


# ── Character slot capacity (used by the advisor and by order allocation) ──────────────────

def _character_capacities(context_id: int) -> list[dict]:
    """Per-character free reaction slots right now (capacity minus currently-running jobs AND
    minus already-pending assignments from a previous suggestion the player hasn't installed
    yet) — only characters that have opted into job tracking count, since we can't know a
    non-tracked character's current load. A fresh "Suggest reactions" run must not double-book
    slots a prior suggestion already claimed but hasn't been confirmed as running by ESI yet;
    mirrors get_industry_jobs' slot math, which does the same running+pending subtraction.

    Pending rows count by their WORST TIER, not their total (`_concurrent_load`) — a chain's stages
    never run at the same moment, so reserving a slot for each of them at once idles reactors the
    account really has. Behind `reactions_parallel_stages`; off, this is the old per-row sum."""
    peak_only = _parallel_stages_on(context_id)
    ensure_industry_jobs_table()
    ensure_reaction_assignments_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, mass_reactions, advanced_mass_reactions, scopes "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
            (context_id,),
        ).fetchall()
        cached = {r["character_id"]: r for r in con.execute(
            "SELECT character_id, jobs_json FROM pp_char_industry_jobs"
        )}
        char_ids = [c["character_id"] for c in chars]
        # Grouped by (character, TIER) rather than counted per character: what competes for a slot
        # is everything at the same tier_order, so a character's planned load is the worst tier and
        # not the sum (`_concurrent_load`). Counting the sum reserved a slot for every stage of a
        # chain at once — stages that by definition never run at the same time — and every
        # allocator downstream of this then had fewer slots to place work in than the account owns.
        pending_tiers: dict[int, dict[int, int]] = {}
        if char_ids:
            placeholders = ",".join("?" * len(char_ids))
            for r in con.execute(
                f"SELECT character_id, COALESCE(tier_order,0) AS tier_order, COUNT(*) AS n "
                f"FROM pp_reaction_assignments WHERE character_id IN ({placeholders}) "
                f"GROUP BY character_id, COALESCE(tier_order,0)", char_ids,
            ):
                pending_tiers.setdefault(r["character_id"], {})[int(r["tier_order"])] = r["n"]
    finally:
        con.close()

    result = []
    for c in chars:
        if not reaction_capable(c)[0]:
            continue
        slots = reaction_slots(c)
        row = cached.get(c["character_id"])
        jobs = _json.loads(row["jobs_json"]) if row else []
        used = len([j for j in jobs if j.get("status") in ("active", "paused", "ready")])
        tiers = pending_tiers.get(c["character_id"], {})
        used += (max(tiers.values(), default=0) if peak_only else sum(tiers.values()))
        result.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "free_slots": max(0, slots - used),
            # The reactors this character HAS, and how many are taken by jobs really running in
            # game. `free_slots` above nets off pending rows by their worst tier — right for "what
            # can I start now", wrong for "how many rows may this plan hold in total", which is
            # what the levelling pass has to answer (see level_product_runs' budget).
            "slots": slots,
            "running": len([j for j in jobs if j.get("status") in ("active", "paused", "ready")]),
        })
    return result


# ── Formula concurrency: a formula is an item, and it is LOCKED while a job runs on it ─────────
# Slots were the only thing this package ever allocated against, so one Ferrofluid formula and ten
# free reactors produced a plan telling the user to install ten parallel jobs they physically
# cannot. The Industry scheduler has modelled exactly this for a while (schedule.py::_print_limits,
# "one formula is one concurrent reaction however many reactor slots are free"); this is the same
# cap, reading the same evidence, on the Reactions side.
#
# **Dependency direction: app/reactions -> app/industry, and only from inside a function.**
# app/industry/slots.py deliberately does not import app.reactions to keep the packages acyclic;
# the same discipline in this direction means the evidence layer is REUSED rather than
# reimplemented (formula_print_floor already unions the personal blueprint cache, enabled asset
# stock incl. corp scans and pastes, and distinct observed blueprint_ids, with a settled
# precedence) without either package's module-import graph gaining an edge — the import happens at
# call time, and a failure to import degrades to "no cap", which is the safe direction.

def _formula_caps_on(context_id: int) -> bool:
    """Gated: capping changes what gets suggested and what an assign commits, so it rolls out
    rather than lands. Off ⇒ `formula_concurrency_caps` returns {} ⇒ the slot math is exactly what
    it was."""
    return flag_on("reactions_formula_cap", context_id)


def formula_concurrency_caps(context_id: int) -> dict[int, int]:
    """Memoised per request — see `_formula_concurrency_caps` for the rules it applies."""
    return dict(request_memo(("formula_caps", context_id),
                             lambda: _formula_concurrency_caps(context_id)))


def _formula_concurrency_caps(context_id: int) -> dict[int, int]:
    """reaction product_type_id -> how many jobs of that product may run AT ONCE, for the products
    we have evidence about. **A MISSING KEY MEANS UNKNOWN, and unknown never refuses** — the same
    rule `_assigned_slot_capacity` follows, and the reason this returns a sparse map rather than a
    number per product: a cap built on absent evidence blocks work the player can really do.

    Three things make a key absent:

      * the flag is off — {} outright, so nothing anywhere behaves differently;
      * the account's blueprint picture is incomplete (`blueprint_coverage().complete` is false —
        the scope is opt-in per character, so a partly-connected account's counts are a FLOOR, and
        a floor read as a total serialises real work) **and the user has not DECLARED that product
        by hand**. A declaration is not a scan: it is the user stating what they own, which
        `owned_blueprints` already treats as authoritative enough to replace the ESI reading for
        that product, so it is known whatever some other character's scope says. Gating it on
        account-wide coverage cost a real user the cap they had just typed in — 238 formulas
        declared, 10 held of the product they ordered, 20 concurrent jobs assigned, because 12 of
        their 14 characters had never granted the scope. Mirrors `BuildParams.prints_known(tid)`;
      * that particular formula was never seen by any of the three evidence sources.

    Concurrency only, per the evidence layer's own contract: an asset row and a job state no ME, no
    TE and no remaining runs, and a formula has no ME/TE anyway (rig-based) and cannot be copied.

    Nothing here is per-TIER: a chain's tiers run in sequence, so one formula legitimately serves
    tier 0 and then tier 1. Callers apply the cap within a tier, never across tiers.
    """
    if not _formula_caps_on(context_id):
        return {}
    try:
        from app.industry.blueprints import (
            blueprint_coverage, declared_products, formula_print_floor, owned_blueprints)
        owned = owned_blueprints(context_id)
        floor = formula_print_floor(context_id, owned)
        declared = declared_products(owned)
        complete = bool(blueprint_coverage(context_id).get("complete"))
        if not complete and not declared:
            return {}
    except Exception:
        return {}                       # evidence unavailable is evidence absent — never cap

    con = get_connection()
    try:
        outputs = {r["output_type_id"] for r in con.execute(
            "SELECT output_type_id FROM reactions")}
    except Exception:
        return {}
    finally:
        con.close()

    caps: dict[int, int] = {}
    for prod in (set(owned) | set(floor)) & outputs:
        if not complete and prod not in declared:
            continue                              # a floor, not a total — see the docstring
        own = owned.get(prod) or {}
        held = own.get("copies")
        n_owned = (len(held) if held else 1) if own else 0
        n_extra = int(floor.get(prod) or 0)       # already de-duplicated against `owned`
        if not n_owned and not n_extra:
            continue                              # unobserved — no key, no cap
        caps[prod] = max(1, n_owned + n_extra)
    return caps


def _cap_jobs(cap: int | None, want: int) -> int:
    """`want` jobs of one product at one moment, held to the formulas there are. No cap = no
    change; a cap never takes the count below 1, since a tier at zero jobs cannot be installed."""
    return max(1, min(want, cap)) if cap else want


# ── Customer orders: committing a fixed order to real reaction slots ───────────────────────────

def _pack_hosts_on(context_id: int) -> bool:
    """Gate for filling one character before using two (`reactions_pack_hosts`). Off ⇒ an order is
    shared across every character with room, exactly as it always was. Flagged because it changes
    where live orders get placed, not because it is in doubt — CLAUDE.md rule 2."""
    return flag_on("reactions_pack_hosts", context_id)


# How much speed an extra character has to buy to be worth the login. Adding a host with F free
# reactors to the S you already have cuts the remaining wait by F/(S+F); below this, it doesn't.
_WORTH_A_LOGIN = 0.20


def _lean_hosts(hosts: list[dict], min_gain: float = _WORTH_A_LOGIN) -> list[dict]:
    """The characters worth involving in an order, roomiest first — the rest of the work goes to
    them.

    Reported from a live order (#45, 1000 runs of Reinforced Carbon Fiber): stage 1 spread over 5
    characters with two holding ONE job each, stage 2 over SEVEN with five holding one each — while
    the characters that already had jobs sat on free reactors. *"To lessen logins we should try and
    run as lean as possible... it's fully possible to not spread the Stage 2 work over all
    characters."*

    **The rule is marginal gain, and it needs no cadence.** An order's wait is its reactor-hours
    divided by the reactors running them, so a host with `F` free slots added to the `S` already
    committed cuts that wait by `F / (S + F)`. Keep taking hosts while that is worth a login and
    stop at the first one that isn't. It is the same reasoning `_fit_chain_slots` already uses to
    hand slots to tiers, one level up: spend the next unit where it actually buys something.

    Why this rather than a job-length ceiling: a duration ceiling needs a number nobody has set
    (`max_reaction_job_days` defaults to None, deliberately), and it answers the wrong question —
    "is this job too long" instead of "is this character worth the trip". A relative gain needs no
    unit at all and scales itself: a small order lands on ONE character because the second buys
    nothing, and a huge one still spreads because every host is pulling real weight.

    On the reported account (three 10-slot characters, four 5-slot) the 4th character buys 14% and
    the run stops at three — 30 slots against the 33 the sprawl over seven was really using.

    The FIRST host is always kept whatever it buys: the order has to be placed somewhere.
    """
    if not hosts:
        return []
    ranked = sorted(hosts, key=lambda h: -h["free_slots"])
    keep, have = [ranked[0]], max(1, ranked[0]["free_slots"])
    for h in ranked[1:]:
        f = h["free_slots"]
        if f <= 0 or f / float(have + f) < min_gain:
            break                       # ...and everyone past it buys less still — they are sorted
        keep.append(h)
        have += f
    return keep


def _compact_hosts(hosts: list[dict], wanted_slots: int, per_chain: int) -> list[dict]:
    """Smallest roomiest-first host set that can hold a cadence-sized complete chain."""
    keep: list[dict] = []
    for host in hosts:
        keep.append(host)
        if sum(h["free_slots"] for h in keep) >= wanted_slots + per_chain * (len(keep) - 1):
            break
    return keep


def _fit_chain_slots(works: list[float], caps: list[int], budget: int) -> list[int]:
    """How many slots each tier of ONE chain gets, out of a character's free slots.

    A chain is installed tier by tier — the intermediate has to finish before the job eating it can
    start — so the chain takes `sum(work_i / slots_i)` and the tiers do NOT want equal shares. On
    the order this was written for, Carbon Fiber carries 1956 runs beside Oxy-Organic Solvents'
    196; a slot given to the second saves a tenth of what the same slot saves on the first.

    So each spare slot goes to whichever tier gains most from it — the saving from one more being
    `work_i/s_i - work_i/(s_i+1)`. That is exactly optimal for this objective (it is separable and
    convex in the slot counts) and reads more honestly than the closed form, which is slots
    proportional to the square root of work.

    Every tier starts at one slot because a chain with a tier at zero cannot be installed at all.
    `caps` stops a tier being given more slots than it has runs, which would just create empty jobs.
    """
    n = len(works)
    if n == 0 or budget <= 0:
        return []
    slots = [1] * n
    spare = budget - n
    while spare > 0:
        best, best_gain = -1, 0.0
        for i in range(n):
            if slots[i] >= caps[i]:
                continue
            gain = works[i] / slots[i] - works[i] / (slots[i] + 1)
            if gain > best_gain:
                best, best_gain = i, gain
        if best < 0:
            break            # every tier already has a slot per run; more would be empty jobs
        slots[best] += 1
        spare -= 1
    return slots


def _allocate_and_insert(context_id: int, type_id: int, name: str, node: dict, reached: dict,
                          types: dict, runs_needed: int, order_id: int) -> dict:
    """Commits `runs_needed` top-level runs (plus the intermediate chain tiers the formula needs)
    to as many real reaction slots as will actually make the order finish sooner.

    **A slot is a rate, not a container.** The first version of this put each tier in exactly one
    job, reasoning that because this app models no per-job run cap, a whole batch always "fits" in
    one slot once a character has one free. It does fit — and it then takes as long as running
    every run end to end. A real 400,000-unit Reinforced Carbon Fiber order became four jobs of
    ~2000 runs each while fifty-five reaction slots sat idle.

    **An order is paced to the cadence, HERE, at quote time** (2026-08-14). It used to run flat out
    on the argument that a customer is waiting, while `split_order_tops_to_cadence` paced it anyway
    on the next dashboard load — so the layout the player showed the customer at commit time and
    the layout the dashboard showed a page later were different, about the same order. That
    disagreement is the defect, not the pacing: the two surfaces have to answer the same question
    the same way, and the honest place to answer it is where the commitment is made.

    Pacing is also no longer destructive. The cadence is a stated target rather than an absolute, so
    a chain that cannot fit its window inside the reactors and formulas available overruns and says
    so, instead of being split until it fits. What the cadence buys is still what it always bought:
    every tier of the order collected on the rhythm the player actually logs in on.

    Take every free slot that still reduces the finish time, stop where another slot would only add
    an empty job — then add whatever more the cadence needs, as far as the free slots and the
    formulas allow. Assign a smaller batch (`req.runs`) when you want to keep slots back for other
    work; that is still the knob.

    **Each character gets a COMPLETE chain.** An intermediate's output has to be physically on the
    character running the job that consumes it, so the split across characters is by whole chains
    and never by tier: a character either hosts the product and all of its intermediates, or takes
    no part in the order. Runs are shared out in proportion to free slots, because slots are what a
    character can actually turn into throughput.

    **And a formula is one job at a time.** Slots are not the only limit: the print is a physical
    item locked into the reactor for the job's duration, so a tier never gets more parallel jobs
    than there are formulas of it (`formula_concurrency_caps` — flag-gated, and silent about
    anything it has no evidence for). The cap is per PRODUCT and account-wide, not per character,
    because that is what the item is, so it also bounds how many characters can host the order at
    all: every host needs at least one job of every tier.
    """
    chars = [c for c in _character_capacities(context_id) if c["free_slots"] > 0]
    if not chars or runs_needed <= 0:
        return {"runs_assigned": 0, "characters": []}

    formula = node.get("via")
    # Intermediates already held are spent against this order as it is placed. `ordered_all` only
    # sizes the chain (how many slots a host needs, which formulas are scarce), so it walks a COPY
    # — spending the real pool here would leave nothing for the per-host walks that create the rows.
    stock_pool = reaction_stock_pool(context_id)
    ordered_all = _ordered_chain_tiers(formula["inputs"], runs_needed, reached,
                                        dict(stock_pool)) if formula else []
    tier_count = len(ordered_all)
    per_chain = tier_count + 1          # one slot per intermediate, plus the product itself
    caps_by_type = formula_concurrency_caps(context_id)
    chain_caps = {t: caps_by_type[t] for t in [tid for tid, _ in ordered_all] + [type_id]
                  if caps_by_type.get(t)}
    order_cadence_h = _reaction_cadence_hours(context_id)
    top_cycle_h = (node.get("cycle_time") or 0) / 3600.0

    hosts = sorted((c for c in chars if c["free_slots"] >= per_chain),
                    key=lambda c: -c["free_slots"])
    if not hosts:
        return {"runs_assigned": 0, "characters": [], "error":
                 f"Needs {tier_count} intermediate reaction job slot(s) plus 1 for the product "
                 f"itself, all on one character — none of your tracked characters has that much free "
                 f"right now. Free up slots, or assign a smaller batch."}
    # A character can only take a share if there is at least one run in it for them, so a two-run
    # order never fragments across fourteen characters just because the slots exist.
    hosts = hosts[:max(1, min(len(hosts), runs_needed))]
    # ...and each host runs the WHOLE chain, so it needs one formula of every tier for itself. The
    # scarcest formula in the chain is therefore also the most characters this order can use.
    if chain_caps:
        hosts = hosts[:max(1, min(chain_caps.values()))]
    # Use the fewest characters that can hold a cadence-sized layout. The old path selected hosts
    # from all FREE capacity and then spent every slot on marginal speed: after two Manufacturing
    # plans were deleted, 150 Carbon Polymer runs expanded into 25 seven-run jobs across five
    # characters. Two jobs already fit the weekly cadence; the other 23 bought minutes at the cost
    # of blocking the account. Cadence is the user's effort contract, so it is also the capacity
    # target—not merely a lower bound before opportunistically consuming everything else.
    all_steps = [t for _, t in ordered_all]
    all_runs = [int(t["runs"]) for t in all_steps] + [runs_needed]
    all_cycle_h = [((t.get("cycle_time") or 0) / 3600.0) for t in all_steps] + [top_cycle_h]
    cadence_jobs = []
    for tid, run_n, cycle_h in zip([t for t, _ in ordered_all] + [type_id], all_runs, all_cycle_h):
        cap_runs = int((order_cadence_h + _CADENCE_GRACE) / cycle_h) if cycle_h > 0 else run_n
        jobs = max(1, -(-run_n // max(1, cap_runs)))
        cadence_jobs.append(_cap_jobs(chain_caps.get(tid), min(run_n, jobs)))
    wanted_slots = sum(cadence_jobs)
    # Every additional host duplicates the chain's one-job-per-tier floor.
    hosts = _compact_hosts(hosts, wanted_slots, per_chain)

    # How the order's runs are split across the characters that will run it: PROPORTIONAL to each
    # host's free slots. The roomiest character does the most work, so every host finishes at
    # roughly the same time and the order completes as early as its capacity allows. That is why one
    # product shows different run counts on different hosts; an even split was tried and reverted —
    # docs/reactions.md, "An order's runs follow capacity, not fairness".
    capacity = sum(h["free_slots"] for h in hosts)
    shares = [int(runs_needed * h["free_slots"] / capacity) for h in hosts]
    for i in range(runs_needed - sum(shares)):      # rounding remainder, roomiest first
        shares[i % len(hosts)] += 1

    now = _time.time()
    unit_cost = node.get("unit_cost", 0.0) + node.get("job_cost", 0.0)
    # The window every job of this order should land inside — the same stored setting the dashboard
    # paces against, so the quote the customer is shown and the plan the next page load draws are
    # one answer rather than two.
    #
    # UNITS: every `cycle_time` in this function comes from the reaction GRAPH, which has already
    # applied the time-efficiency reduction (`app/reactions/graph.py`'s `cycle_time` at load). So
    # these are REAL hours and `cadence_h` — also real hours — is compared against them directly.
    # No `_reaction_time_mult` conversion here; that belongs to `level_product_runs`, which works in
    # raw-SDE hours because it reads `_reaction_cycle_times` instead.
    placed: list[dict] = []
    left = dict(chain_caps)             # formulas of each type still unspoken for, account-wide
    con = get_connection()
    try:
        for idx, (host, share) in enumerate(zip(hosts, shares)):
            if share <= 0:
                continue
            # ...and the real pool here, consumed host by host: the units exist once, so the first
            # host's share spends them and the next host reacts what is genuinely left to react.
            tiers = _ordered_chain_tiers(formula["inputs"], share, reached, stock_pool) if formula else []
            # Stage per step, not position in the list: siblings share a stage and run together.
            ranks = tier_ranks(tiers)
            works = [t["runs"] * ((t["cycle_time"] or 0) / 3600.0) for _, t in tiers]
            caps = [max(1, int(t["runs"])) for _, t in tiers]
            works.append(share * top_cycle_h)
            caps.append(share)
            # Formulas left for this host, holding one of each back for every host still to come —
            # a host that cannot install a tier at all cannot run its share of the chain.
            after = len(hosts) - idx - 1
            for i, tid in enumerate([t for t, _ in tiers] + [type_id]):
                if tid in left:
                    caps[i] = _cap_jobs(max(1, left[tid] - after), caps[i])
            # Begin compact: one job per tier. The cadence pass below adds only the jobs needed to
            # keep collection inside the configured window. It deliberately does not spend every
            # otherwise-free reactor on progressively smaller runtime gains.
            slots = [1] * len(works)
            # `_fit_chain_slots` minimises the SUM of the tier durations, which was the right
            # objective while every tier was its own stage. Now that siblings share one, what gates
            # the stage above is the LAST of them to land — so re-balance within each stage the
            # same way the wizard does. Slot-neutral, so the fit above still decides how much
            # capacity the chain gets. Imported at call time: advisor sits above this module.
            if _parallel_stages_on(context_id) and len(tiers) > 1:
                from app.reactions.advisor import _align_stage_jobs
                align = [{"character_id": host["character_id"], "tier": ranks[i],
                          "runs": int(t["runs"]), "cycle_hours": (t["cycle_time"] or 0) / 3600.0,
                          "jobs": slots[i], "cap": caps[i]}
                         for i, (_tid, t) in enumerate(tiers)]
                _align_stage_jobs(align)
                for i, a in enumerate(align):
                    slots[i] = a["jobs"]
            # ...and THEN pace it. `_fit_chain_slots` spends slots on finishing sooner overall; this
            # spends what is left on making no single job outrun the player's window. Applied after
            # the alignment because it only ever ADDS jobs, so it cannot undo a slot-neutral
            # rebalance — it takes the reactors that rebalance did not need.
            #
            # It is a target, not an absolute: bounded by the host's free reactors and by the
            # formulas of each tier, so a chain that genuinely cannot fit stays over the window
            # rather than being split into jobs there is nothing to install them with. Those
            # over-window jobs carry the same breach badge the levelled plan does — stamped by
            # `_insert_assignment_rows` below, which is the ONLY chance they get: an order's rows
            # are excluded from the leveller by design, so nothing else will ever measure them.
            #
            # REAL hours throughout: every `cycle_time` here comes off the graph, which has already
            # applied the account's time efficiency.
            per_run_h = [((t["cycle_time"] or 0) / 3600.0) for _, t in tiers] + [top_cycle_h]
            if order_cadence_h > 0:
                tier_runs = [int(t["runs"]) for _, t in tiers] + [share]
                want_jobs = []
                for i, (c_h, r_n) in enumerate(zip(per_run_h, tier_runs)):
                    cap_runs = int((order_cadence_h + _CADENCE_GRACE) / c_h) if c_h > 0 else 0
                    want_jobs.append(-(-r_n // cap_runs) if cap_runs > 0 else slots[i])
                spare = max(0, host["free_slots"] - sum(slots))
                # Worst overrun first: with one reactor to give, it goes to the tier that is
                # furthest past the window, not to whichever happens to be listed first.
                for i in sorted(range(len(slots)), key=lambda j: -(want_jobs[j] - slots[j])):
                    if spare <= 0:
                        break
                    add = min(max(0, want_jobs[i] - slots[i]),
                              max(0, caps[i] - slots[i]), spare)
                    slots[i] += add
                    spare -= add
            for i, tid in enumerate([t for t, _ in tiers] + [type_id]):
                if tid in left:
                    left[tid] = max(0, left[tid] - slots[i])

            # Every row carries its own duration and the window it was paced against, so a job the
            # caps above could not bring inside the window says so on the dashboard instead of
            # looking like any other row. `_insert_assignment_rows` absorbs `_CADENCE_GRACE` the
            # same way the pace loop above spends it and the same way the leveller measures — one
            # definition of "over the window" on every surface.
            for i, (tid, info) in enumerate(tiers):
                _insert_assignment_rows(con, host["character_id"], tid,
                                         types.get(tid, {}).get("name", str(tid)),
                                         info["runs"], slots[i], 0.0, 0.0,
                                         ranks[i], now, order_id, tidy=_tidy_runs_on(context_id),
                                         cycle_hours=per_run_h[i], cadence_h=order_cadence_h)
            _insert_assignment_rows(con, host["character_id"], type_id, name, share, slots[-1],
                                     unit_cost * share, 0.0,
                                     (max(ranks) + 1) if ranks else 0, now, order_id,
                                     cycle_hours=top_cycle_h, cadence_h=order_cadence_h)
            placed.append({"character_id": host["character_id"],
                            "character_name": host["character_name"],
                            "runs": share, "jobs": sum(slots)})
        con.commit()
    finally:
        con.close()
    return {"runs_assigned": sum(p["runs"] for p in placed), "characters": placed}
