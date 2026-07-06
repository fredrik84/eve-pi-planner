"""Pooled measured-extraction-yield signal.

`pp_colony_yield` (app/esi.py) already records each colony's real per-program extraction yield
(`peak_day` — install-time P0/day, comparable across players since it's the headline rate at
program start, not a decay-averaged figure), but it's keyed by `character_id` and scoped private.
`planet_id` in that table is EVE's own stable planet identifier though — not user-identifying — so
unlike the rest of a user's data, yield samples for the SAME planet from DIFFERENT players are
directly poolable, the same way the community Planet DB already pools submitted richness values.

This module aggregates those samples, across every user, into a new shared table keyed the same way
as `pp_planets` (`system, planet_num`) so it's a trivial join for read paths. Deliberately NOT wired
into the planner's assignment math yet — see CLAUDE.md / the feature's memory note for why (sparse
early coverage, must not dominate the static richness value).
"""
import logging
from datetime import datetime, timezone

from app.sde import get_connection, ensure_once
from app.cache import cache_invalidate
from app.planetary import _NAME_TO_COL, _PLANETS_CACHE_KEY

log = logging.getLogger(__name__)

# Below this many pooled samples, don't publish an average — a 1-2 sample "average" is really just
# one player's exact yield re-published as if it were a pooled community figure.
_MIN_YIELD_SAMPLES = 3


@ensure_once
def ensure_yield_avg_table():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_planet_yield_avg (
            system       TEXT    NOT NULL,
            planet_num   INTEGER NOT NULL,
            p0_name      TEXT    NOT NULL,
            sample_count INTEGER NOT NULL,
            avg_peak_day REAL    NOT NULL,
            measured_pct REAL    NOT NULL,
            updated_at   TEXT    NOT NULL,
            PRIMARY KEY (system, planet_num, p0_name)
        )
    """)
    con.commit()
    con.close()


# Baseline extraction rate the planner's "value" (0-100+) richness scale is defined against —
# same constant as planner.py's `_kk` (48_000 * 24), kept independent here to avoid importing the
# whole planner module just for one number.
_BASELINE_P0_PER_DAY = 48_000 * 24

# Next in the sequence after build_sde.py (918_273_645) / populate_geo.py (918_273_646) /
# notifications.py (918_273_647).
_YIELD_ADVISORY_LOCK_KEY = 918_273_648


def aggregate_colony_yields() -> dict:
    """Pool pp_colony_yield across ALL users into pp_planet_yield_avg, grouped by
    (system, planet_num, p0_name). Safe to call repeatedly (full recompute + upsert each time —
    the source table is capped at _YIELD_KEEP rows/colony, so this stays cheap).

    Guarded by a Postgres advisory lock (same pattern as notifications.py's
    check_and_send_notifications) — every pod/worker runs its own APScheduler on its own 3am
    timer, so without a lock several could run this concurrently.

    Resource identity comes from `pp_char_planets.p0_name` (the ESI pin's product_type_id,
    reliably populated per colony), NOT `pp_colony_yield.p0_type_id` — the latter is only ever
    set by `_record_yield_sample`'s tier==0 check in pi_sim.py, which never fires for a colony
    that has any on-planet factories (the standard build), so it's ~always NULL in practice.

    Note: a split-extraction colony's `peak_day` is the SUM across all its P0 resources, and
    `pp_char_planets.p0_name` only ever holds ONE of them (whichever extractor pin was seen last
    during the scan) — such samples will inflate whichever resource they land under. Pre-existing
    limitation of the underlying data, not introduced here; acceptable for a supplementary signal.

    Returns {"planets": N, "samples": M} — distinct (system, planet_num, p0) rows written, and
    total pooled samples that fed them.
    """
    from app.db import _IS_POSTGRES
    ensure_yield_avg_table()
    con = get_connection()
    try:
        if _IS_POSTGRES:
            con.execute("SELECT pg_advisory_lock(?)", (_YIELD_ADVISORY_LOCK_KEY,))
        try:
            rows = con.execute("""
                SELECT ss.name AS system, cp.planet_num AS planet_num, cp.p0_name AS p0_display_name,
                       AVG(cy.peak_day) AS avg_peak_day, COUNT(*) AS sample_count
                FROM pp_colony_yield cy
                JOIN pp_char_planets cp
                  ON cp.character_id = cy.character_id AND cp.planet_id = cy.planet_id
                JOIN solar_systems ss ON ss.system_id = cp.solar_system_id
                WHERE cp.p0_name IS NOT NULL AND cp.planet_num IS NOT NULL
                GROUP BY ss.name, cp.planet_num, cp.p0_name
            """).fetchall()

            now = datetime.now(timezone.utc).isoformat()
            written = 0
            pooled_samples = 0
            for r in rows:
                sample_count = r["sample_count"]
                if sample_count < _MIN_YIELD_SAMPLES:
                    continue
                p0_name = _NAME_TO_COL.get(r["p0_display_name"].lower())
                if not p0_name:
                    continue
                avg_peak_day = r["avg_peak_day"]
                measured_pct = (avg_peak_day / _BASELINE_P0_PER_DAY) * 100.0
                con.execute(
                    "INSERT INTO pp_planet_yield_avg "
                    "(system, planet_num, p0_name, sample_count, avg_peak_day, measured_pct, updated_at) "
                    "VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT (system, planet_num, p0_name) DO UPDATE SET"
                    " sample_count=EXCLUDED.sample_count, avg_peak_day=EXCLUDED.avg_peak_day,"
                    " measured_pct=EXCLUDED.measured_pct, updated_at=EXCLUDED.updated_at",
                    (r["system"], r["planet_num"], p0_name, sample_count, avg_peak_day, measured_pct, now),
                )
                written += 1
                pooled_samples += sample_count
            con.commit()
            cache_invalidate(_PLANETS_CACHE_KEY)

            log.info("aggregate_colony_yields: %d planets, %d pooled samples", written, pooled_samples)
            return {"planets": written, "samples": pooled_samples}
        finally:
            if _IS_POSTGRES:
                con.execute("SELECT pg_advisory_unlock(?)", (_YIELD_ADVISORY_LOCK_KEY,))
                con.commit()
    finally:
        con.close()
