"""Blueprints read from ESI, how a copy ranks against another, and batch identity."""
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


# Batch identity. Lives up here rather than beside the paste parser because the
# manual-blueprint migration below calls it, and that has to be importable BEFORE the
# parser is — otherwise the two form an import cycle when this file becomes a package.
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
