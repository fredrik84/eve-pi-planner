"""The hand-declared blueprint endpoints."""
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

from app.industry.blueprints.esi import _STACK_CAP, _blueprint_product_index, ensure_char_blueprints_table
from app.industry.blueprints.manual import _manual_enabled, ensure_manual_blueprints_table, owned_blueprints
from app.industry.blueprints.paste import (
    delete_blueprint_batch,
    list_blueprint_batches,
    parse_blueprint_paste,
    replace_blueprint_batch,
)

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
    # Every manual-blueprint write returns through here, and a declared print changes the ME/TE the
    # plan is built on — so the account snapshot must not outlive the edit. Also fires on the plain
    # read, which is a rare settings-page call and costs one extra rebuild rather than a wrong plan.
    from app.industry.graph import clear_account_snapshot
    clear_account_snapshot(context_id)
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
