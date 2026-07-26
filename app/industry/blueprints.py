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
import time as _time

import httpx
from fastapi import Depends

from app.sde import get_connection, ensure_once
from app import esi_http
from app.esi import require_context, ESI_BASE, _get_valid_token, BLUEPRINTS_SCOPE

from app.industry._router import router

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


def _better(a: dict, b: dict) -> bool:
    """Which owned blueprint to prefer for one product: a BPO beats a BPC (unlimited runs, no
    re-buy); among the same kind, higher ME wins, then higher TE."""
    rank = {"bpo": 1, "bpc": 0}
    if rank[a["kind"]] != rank[b["kind"]]:
        return rank[a["kind"]] > rank[b["kind"]]
    if a["me"] != b["me"]:
        return a["me"] > b["me"]
    return a["te"] > b["te"]


def owned_blueprints(context_id: int) -> dict[int, dict]:
    """product_type_id -> {me, te, kind ('bpo'|'bpc'), runs} for the best blueprint the account owns
    for that product, across all its characters. Maps each blueprint's type_id to its product via
    the SDE `blueprints` table. Empty if nothing's connected/cached."""
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
    finally:
        con.close()

    owned: dict[int, dict] = {}
    for row in rows:
        try:
            items = _json.loads(row["blueprints_json"])
        except Exception:
            continue
        for b in items:
            prod = bp2prod.get(b.get("type_id"))
            if not prod:
                continue
            cand = {"me": b.get("me", 0) or 0, "te": b.get("te", 0) or 0,
                    "kind": "bpo" if b.get("quantity") == -1 else "bpc",
                    "runs": b.get("runs", -1)}
            cur = owned.get(prod)
            if cur is None or _better(cand, cur):
                owned[prod] = cand
    return owned


@router.post("/api/industry/blueprints/refresh")
def refresh_blueprints(context_id: int = Depends(require_context)):
    """Re-read owned blueprints from ESI for the caller's characters that granted the blueprint
    scope. Best-effort per character — one failure never blocks the others."""
    ensure_char_blueprints_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, scopes FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchall()
        refreshed, skipped = 0, 0
        for c in chars:
            if BLUEPRINTS_SCOPE not in (c["scopes"] or ""):
                skipped += 1
                continue
            tok = _get_valid_token(c["character_id"])
            if not tok:
                skipped += 1
                continue
            bps = fetch_character_blueprints(c["character_id"], tok)
            if bps is None:
                skipped += 1
                continue
            con.execute(
                "INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?) ON CONFLICT(character_id) DO UPDATE SET "
                "blueprints_json=excluded.blueprints_json, fetched_at=excluded.fetched_at",
                (c["character_id"], _json.dumps(bps), _time.time()),
            )
            refreshed += 1
        con.commit()
    finally:
        con.close()
    return {"refreshed": refreshed, "skipped": skipped}


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
