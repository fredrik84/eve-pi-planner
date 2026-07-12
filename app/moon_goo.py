"""Group-managed price list for a moon-goo-for-sale deal (see app.groups) — the input side of
the reactions profitability tool (app/reactions.py). Not a public market price: each group's
sheet is internal to that alliance, manually updated by whoever posts the current price sheet
(their group manager, or a site admin on their behalf), so it needs a paste-import (mirrors the
Planet DB import's paste-then-parse shape in app/planetary.py, though the actual column shape
here is unrelated — a flat price list, not planet data).
"""
import csv
import io
import re

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, load_pi_data
from app.esi import require_context
from app.groups import is_group_manager, bootstrap_group_id

router = APIRouter()


@ensure_once
def ensure_moon_goo_table():
    con = get_connection()
    try:
        table_exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pp_moon_goo_prices'"
        ).fetchone()
        migrate_rows = None
        if table_exists:
            cols = {r["name"] for r in con.execute("PRAGMA table_info(pp_moon_goo_prices)")}
            if "group_id" not in cols:
                # Old shape: type_id INTEGER PRIMARY KEY, one global sheet. Two groups must be
                # able to price the same material differently now, so the key becomes
                # (group_id, type_id) — SQLite can't ALTER a PRIMARY KEY in place, so this is a
                # guarded rebuild-and-copy, run once, tagging every pre-existing row with the
                # bootstrap (B0SS) group's id so its pricing keeps working unchanged.
                migrate_rows = con.execute(
                    "SELECT type_id, name, sell_price, stock, updated_at FROM pp_moon_goo_prices"
                ).fetchall()
                con.execute("DROP TABLE pp_moon_goo_prices")
                con.commit()
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_moon_goo_prices (
                group_id   INTEGER NOT NULL,
                type_id    INTEGER NOT NULL,
                name       TEXT NOT NULL,
                sell_price REAL NOT NULL DEFAULT 0,
                stock      INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (group_id, type_id)
            )
        """)
        con.commit()
        if migrate_rows:
            gid = bootstrap_group_id()
            for r in migrate_rows:
                con.execute(
                    "INSERT INTO pp_moon_goo_prices (group_id, type_id, name, sell_price, stock, updated_at) "
                    "VALUES (?,?,?,?,?,?) ON CONFLICT (group_id, type_id) DO NOTHING",
                    (gid, r["type_id"], r["name"], r["sell_price"], r["stock"], r["updated_at"]),
                )
            con.commit()
    finally:
        con.close()


_NUM_RE = re.compile(r"[^0-9.\-]")  # strips "ISK", spaces, thousands commas — keeps digits/./-


def _clean_num(raw: str) -> float | None:
    s = _NUM_RE.sub("", raw or "")
    if not s or s in ("-", "."):
        return None
    try:
        return float(s)
    except ValueError:
        return None


HEADER_ALIASES = {
    "type_id": "type_id", "typeid": "type_id",
    "moon goo": "name", "name": "name",
    "b0ss sale price": "sell_price", "sale price": "sell_price", "price": "sell_price",
    "stock": "stock",
}


def _parse_goo_paste(text: str, pi_types: dict) -> tuple[list[dict], list[str]]:
    """Parse pasted price-sheet text (a typical alliance sheet's own CSV/TSV export shape:
    type_id, group_id, name, Jita Buy Price, Sale Price, Stock[, Stock Total]) into
    {type_id, name, sell_price, stock} rows. Column order is header-detected, not positional,
    so a reordered or narrower paste (e.g. missing group_id) still works.

    Uses csv.reader (not a naive line-split) because a sheet export can have a note cell with
    an embedded newline before the real header row — splitlines() would treat that as two rows
    and corrupt everything after it. Also scans forward for the header instead of assuming row
    0 is it, since the same export can have blank/note rows above the real header. Returns
    (rows, errors) — does not write anything."""
    stripped = (text or "").strip()
    if not stripped:
        return [], []
    sep = "\t" if "\t" in stripped.splitlines()[0] else ","
    all_rows = list(csv.reader(io.StringIO(stripped), delimiter=sep))

    col: dict[str, int] = {}
    header_idx = None
    for ri, row in enumerate(all_rows):
        candidate: dict[str, int] = {}
        for i, h in enumerate(row):
            key = HEADER_ALIASES.get(h.strip().lower())
            if key and key not in candidate:
                candidate[key] = i
        if "type_id" in candidate:
            col, header_idx = candidate, ri
            break
    if header_idx is None:
        return [], ["No 'type_id' column found — paste the sheet's own header + data rows"]

    rows: list[dict] = []
    errors: list[str] = []
    for parts in all_rows[header_idx + 1:]:
        if not any(p.strip() for p in parts):
            continue

        def get(key: str) -> str:
            idx = col.get(key)
            return parts[idx].strip() if idx is not None and idx < len(parts) else ""

        tid_raw = get("type_id")
        try:
            type_id = int(tid_raw)
        except ValueError:
            if tid_raw:
                errors.append(f"Skipped row — bad type_id {tid_raw!r}")
            continue
        sde_name = pi_types.get(type_id, {}).get("name")
        name = sde_name or get("name") or str(type_id)
        if not sde_name:
            errors.append(f"{type_id} ({name}): not found in the SDE types table — kept anyway, double-check the id")
        sell_price = _clean_num(get("sell_price")) or 0.0
        stock = int(_clean_num(get("stock")) or 0)
        rows.append({"type_id": type_id, "name": name, "sell_price": sell_price, "stock": stock})

    return rows, errors


def _require_group_access(ctx: int, group_id: int):
    if not is_group_manager(ctx, group_id):
        raise HTTPException(status_code=403, detail="Only a manager of this group can edit its price sheet")


class GooImportRequest(BaseModel):
    text: str


@router.get("/api/moon-goo")
def list_moon_goo(ctx: int = Depends(require_context)):
    """The material-availability reference list the wizard's "uncheck what you can't reliably
    get" filter uses — deliberately global (the deduped union of type_ids across EVERY group's
    sheet, not just the caller's own), since that filter is a player self-selection concern
    ("I structurally can't get this material") unrelated to whose price sheet applies."""
    ensure_moon_goo_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT type_id, MAX(name) AS name, MAX(sell_price) AS sell_price, "
            "MAX(stock) AS stock, MAX(updated_at) AS updated_at "
            "FROM pp_moon_goo_prices GROUP BY type_id ORDER BY name"
        )]
    finally:
        con.close()
    return {"prices": rows}


@router.get("/api/moon-goo/{group_id}")
def list_group_moon_goo(group_id: int, ctx: int = Depends(require_context)):
    """This specific group's own price sheet — what the group-scoped admin editor reads."""
    _require_group_access(ctx, group_id)
    ensure_moon_goo_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT type_id, name, sell_price, stock, updated_at FROM pp_moon_goo_prices "
            "WHERE group_id=? ORDER BY name", (group_id,)
        )]
    finally:
        con.close()
    return {"prices": rows}


@router.post("/api/moon-goo/{group_id}/import")
def import_moon_goo(group_id: int, req: GooImportRequest, ctx: int = Depends(require_context)):
    _require_group_access(ctx, group_id)
    ensure_moon_goo_table()
    pi_types = load_pi_data()["types"]
    rows, errors = _parse_goo_paste(req.text, pi_types)
    con = get_connection()
    try:
        for r in rows:
            con.execute(
                "INSERT INTO pp_moon_goo_prices (group_id, type_id, name, sell_price, stock, updated_at) "
                "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT (group_id, type_id) DO UPDATE SET name=excluded.name, sell_price=excluded.sell_price, "
                "stock=excluded.stock, updated_at=excluded.updated_at",
                (group_id, r["type_id"], r["name"], r["sell_price"], r["stock"]),
            )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "imported": len(rows), "errors": errors}


class GooRowUpsert(BaseModel):
    type_id: int
    sell_price: float
    stock: int


@router.post("/api/moon-goo/{group_id}/row")
def upsert_moon_goo_row(group_id: int, req: GooRowUpsert, ctx: int = Depends(require_context)):
    """Add or edit a single material row — the paste-import above is for bulk sheet updates;
    this is for a quick one-off correction (a stock refresh, a price typo) without re-pasting
    the whole sheet."""
    _require_group_access(ctx, group_id)
    ensure_moon_goo_table()
    pi_types = load_pi_data()["types"]
    name = pi_types.get(req.type_id, {}).get("name") or str(req.type_id)
    con = get_connection()
    try:
        con.execute(
            "INSERT INTO pp_moon_goo_prices (group_id, type_id, name, sell_price, stock, updated_at) "
            "VALUES (?,?,?,?,?,CURRENT_TIMESTAMP) "
            "ON CONFLICT (group_id, type_id) DO UPDATE SET name=excluded.name, sell_price=excluded.sell_price, "
            "stock=excluded.stock, updated_at=excluded.updated_at",
            (group_id, req.type_id, name, req.sell_price, req.stock),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "name": name}


@router.delete("/api/moon-goo/{group_id}/{type_id}")
def delete_moon_goo_row(group_id: int, type_id: int, ctx: int = Depends(require_context)):
    _require_group_access(ctx, group_id)
    ensure_moon_goo_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_moon_goo_prices WHERE group_id=? AND type_id=?", (group_id, type_id))
        con.commit()
    finally:
        con.close()
    return {"ok": True}
