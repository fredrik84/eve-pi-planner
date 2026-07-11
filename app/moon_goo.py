"""Admin-managed price/stock list for the B0SS alliance's moon-goo-for-sale deal — the input
side of the reactions profitability tool (see app/reactions.py). Not a public market price:
this is alliance-internal, manually updated by whoever posts the current price sheet, so it
needs a paste-import (mirrors the Planet DB import's paste-then-parse shape in app/planetary.py,
though the actual column shape here is unrelated — a flat price list, not planet data).
"""
import csv
import io
import re

from fastapi import APIRouter, Depends
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, load_pi_data
from app.esi import require_admin, require_b0ss

router = APIRouter()


@ensure_once
def ensure_moon_goo_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_moon_goo_prices (
                type_id    INTEGER PRIMARY KEY,
                name       TEXT NOT NULL,
                sell_price REAL NOT NULL DEFAULT 0,
                stock      INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        """)
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
    """Parse pasted price-sheet text (the B0SS sheet's own CSV/TSV export shape:
    type_id, group_id, name, Jita Buy Price, B0SS Sale Price, Stock[, Stock Total]) into
    {type_id, name, sell_price, stock} rows. Column order is header-detected, not positional,
    so a reordered or narrower paste (e.g. missing group_id) still works.

    Uses csv.reader (not a naive line-split) because the sheet's own export has a note cell
    with an embedded newline before the real header row — splitlines() would treat that as two
    rows and corrupt everything after it. Also scans forward for the header instead of assuming
    row 0 is it, since the same export has 2 blank/note rows above the real header. Returns
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


class GooImportRequest(BaseModel):
    text: str


@router.get("/api/moon-goo")
def list_moon_goo(ctx: int = Depends(require_b0ss)):
    ensure_moon_goo_table()
    con = get_connection()
    try:
        rows = [dict(r) for r in con.execute(
            "SELECT type_id, name, sell_price, stock, updated_at FROM pp_moon_goo_prices ORDER BY name"
        )]
    finally:
        con.close()
    return {"prices": rows}


@router.post("/api/moon-goo/import")
def import_moon_goo(req: GooImportRequest, ctx: int = Depends(require_admin)):
    ensure_moon_goo_table()
    pi_types = load_pi_data()["types"]
    rows, errors = _parse_goo_paste(req.text, pi_types)
    con = get_connection()
    try:
        for r in rows:
            con.execute(
                "INSERT INTO pp_moon_goo_prices (type_id, name, sell_price, stock, updated_at) "
                "VALUES (?,?,?,?,CURRENT_TIMESTAMP) "
                "ON CONFLICT (type_id) DO UPDATE SET name=excluded.name, sell_price=excluded.sell_price, "
                "stock=excluded.stock, updated_at=excluded.updated_at",
                (r["type_id"], r["name"], r["sell_price"], r["stock"]),
            )
        con.commit()
    finally:
        con.close()
    return {"ok": True, "imported": len(rows), "errors": errors}
