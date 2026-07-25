"""Character assets — what you already own.

Feeds two things the planner previously had to guess at:

* **`on_hand`.** The planner has always accepted an `on_hand` map (`aggregate_demand` subtracts it
  from gross demand) but nothing ever populated it, so every plan assumed you owned nothing and
  cheerfully told you to build components already sitting in your hangar. This is that missing
  source.
* **Epoch-free progress.** Job history can only answer "did I build this *recently*"; owning the
  output answers "is this done" outright, with no start-date guessing and surviving a re-queue.

Assets are read once per refresh and cached per character, like the blueprint and job caches — this
is not polled, since a full asset list is a heavy call.
"""

from __future__ import annotations

import json
import time

import httpx
from fastapi import Depends

from app.db import get_connection
from app.esi import ESI_BASE, require_context, _get_valid_token
from app.industry._router import router

# Only these location flags are materials you can actually feed into a job. Assets in a ship fit,
# a delivery hangar or a contract are NOT usable stock, and counting them would under-build.
_USABLE_FLAGS = {
    "Hangar", "HangarAll", "Unlocked", "AutoFit",
    "CorpSAG1", "CorpSAG2", "CorpSAG3", "CorpSAG4", "CorpSAG5", "CorpSAG6", "CorpSAG7",
}


def ensure_char_assets_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_assets (
                character_id INTEGER PRIMARY KEY,
                assets_json  TEXT NOT NULL DEFAULT '{}',
                fetched_at   REAL
            )
        """)
        con.commit()
    finally:
        con.close()


def fetch_character_assets(character_id: int, access_token: str) -> dict[int, int] | None:
    """{type_id: quantity} of usable stock for one character, following ESI's page header.

    Returns None on failure so a bad fetch never wipes a good cache; {} means genuinely nothing.
    """
    totals: dict[int, int] = {}
    try:
        with httpx.Client(timeout=20) as client:
            page = 1
            while page <= 20:                      # hard stop; 20 pages = 20k assets
                r = client.get(
                    f"{ESI_BASE}/characters/{character_id}/assets/",
                    headers={"Authorization": f"Bearer {access_token}"},
                    params={"page": page},
                )
                if r.status_code == 404:
                    break
                r.raise_for_status()
                rows = r.json()
                if not rows:
                    break
                for a in rows:
                    if a.get("location_flag") not in _USABLE_FLAGS:
                        continue
                    tid = a.get("type_id")
                    if tid is None:
                        continue
                    totals[int(tid)] = totals.get(int(tid), 0) + int(a.get("quantity") or 0)
                pages = int(r.headers.get("x-pages") or 1)
                if page >= pages:
                    break
                page += 1
    except Exception:
        return None
    return totals


def refresh_assets(context_id: int) -> dict:
    """Re-read assets for every character in the account that still holds a valid token."""
    ensure_char_assets_table()
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name FROM pp_characters WHERE context_id = ?",
            (context_id,),
        ).fetchall()
    finally:
        con.close()

    ok, failed = 0, 0
    for c in chars:
        cid = c["character_id"]
        token = _get_valid_token(cid)
        if not token:
            failed += 1
            continue
        totals = fetch_character_assets(cid, token)
        if totals is None:
            failed += 1
            continue
        con = get_connection()
        try:
            con.execute(
                "INSERT INTO pp_char_assets (character_id, assets_json, fetched_at) VALUES (?,?,?) "
                "ON CONFLICT (character_id) DO UPDATE SET assets_json=excluded.assets_json, "
                "fetched_at=excluded.fetched_at",
                (cid, json.dumps({str(k): v for k, v in totals.items()}), time.time()),
            )
            con.commit()
        finally:
            con.close()
        ok += 1
    return {"characters": len(chars), "refreshed": ok, "failed": failed}


def owned_quantities(context_id: int) -> dict[int, float]:
    """{type_id: quantity} pooled across every character in the account. Empty when assets have
    never been fetched, which makes the planner behave exactly as it did before this existed."""
    ensure_char_assets_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT a.assets_json FROM pp_char_assets a "
            "JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id = ?",
            (context_id,),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()

    out: dict[int, float] = {}
    for r in rows:
        try:
            data = json.loads(r["assets_json"] or "{}")
        except Exception:
            continue
        for k, v in data.items():
            out[int(k)] = out.get(int(k), 0.0) + float(v or 0)
    return out


def assets_status(context_id: int) -> dict:
    ensure_char_assets_table()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT COUNT(*) AS n, MAX(a.fetched_at) AS t FROM pp_char_assets a "
            "JOIN pp_characters c ON c.character_id = a.character_id "
            "WHERE c.context_id = ?",
            (context_id,),
        ).fetchone()
    finally:
        con.close()
    n = (row and row["n"]) or 0
    owned = owned_quantities(context_id) if n else {}
    return {"connected": bool(n), "characters": n,
            "fetched_at": (row and row["t"]) or None, "distinct_types": len(owned)}


@router.get("/api/industry/assets")
def industry_assets(ctx: int = Depends(require_context)):
    """Whether we have an asset snapshot, and how much of it. Own-account scoped."""
    return assets_status(ctx)


@router.post("/api/industry/assets/refresh")
def industry_assets_refresh(ctx: int = Depends(require_context)):
    res = refresh_assets(ctx)
    res.update(assets_status(ctx))
    return res
