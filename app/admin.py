"""Admin features: custom production baskets (global, admin-managed) and admin-user
management. Mirrors the auth/table pattern in `bugs.py`.

A basket is a named set of PI commodities + per-run quantities. It is planned by the same
multi-product engine as the built-in Fuel Blocks basket (see `fuelblock_planner`), so any
logged-in user can pick a basket as a wizard target. Only admins create/edit/delete them.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data
from app.esi import require_admin, ADMIN_CHARACTERS, ensure_admin_table, _sessions, _load_sessions

router = APIRouter()

# Per-character config (pp_plan_config) for a basket is keyed by a synthetic type id well
# above real PI type_ids (~<100k) and the fuel-block sentinel (4312), so it never collides.
BASKET_CONFIG_BASE = 2_000_000_000


def ensure_basket_tables():
    con = get_connection()
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_baskets (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            run_size   INTEGER DEFAULT 1,
            unit_label TEXT DEFAULT 'sets',
            created_by TEXT,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
    """)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_basket_items (
            basket_id INTEGER NOT NULL,
            type_id   INTEGER NOT NULL,
            qty       REAL    NOT NULL,
            PRIMARY KEY (basket_id, type_id)
        )
    """)
    con.commit()
    con.close()


def _session_char_name(pp_session: str | None) -> str | None:
    _load_sessions()
    info = _sessions.get(pp_session) if pp_session else None
    if not info:
        return None
    con = get_connection()
    row = con.execute(
        "SELECT character_name FROM pp_characters WHERE character_id=?", (info[0],)
    ).fetchone()
    con.close()
    return row["character_name"] if row else None


# ── Baskets ─────────────────────────────────────────────────────────────────────

class BasketItem(BaseModel):
    type_id: int
    qty: float


class BasketSave(BaseModel):
    name: str
    run_size: int = 1
    unit_label: str = "sets"
    items: list[BasketItem]


def _basket_dict(con, row, types) -> dict:
    items = con.execute(
        "SELECT type_id, qty FROM pp_basket_items WHERE basket_id=? ORDER BY type_id",
        (row["id"],),
    ).fetchall()
    return {
        "id":             row["id"],
        "name":           row["name"],
        "run_size":       row["run_size"] or 1,
        "unit_label":     row["unit_label"] or "sets",
        "config_type_id": BASKET_CONFIG_BASE + row["id"],
        "items": [
            {
                "type_id": it["type_id"],
                "name":    types.get(it["type_id"], {}).get("name", "?"),
                "tier":    types.get(it["type_id"], {}).get("pi_tier"),
                "qty":     it["qty"],
            }
            for it in items
        ],
    }


@router.get("/api/baskets")
def list_baskets():
    """Public — the planner wizard lists baskets as selectable targets."""
    ensure_basket_tables()
    types = load_pi_data()["types"]
    con = get_connection()
    rows = con.execute("SELECT * FROM pp_baskets ORDER BY name").fetchall()
    out = [_basket_dict(con, r, types) for r in rows]
    con.close()
    return {"baskets": out}


def _validate_basket(req: BasketSave) -> tuple[str, list[tuple[int, float]]]:
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Basket name is required")
    if req.run_size < 1:
        raise HTTPException(status_code=400, detail="Run size must be at least 1")
    types = load_pi_data()["types"]
    seen: dict[int, float] = {}
    for it in req.items:
        t = types.get(it.type_id)
        tier = t.get("pi_tier") if t else None
        if not t or not tier:
            raise HTTPException(status_code=400, detail=f"Type {it.type_id} is not a producible PI commodity (P1–P4)")
        if it.qty <= 0:
            raise HTTPException(status_code=400, detail=f"Quantity for {t.get('name','?')} must be positive")
        seen[it.type_id] = seen.get(it.type_id, 0.0) + it.qty
    if not seen:
        raise HTTPException(status_code=400, detail="A basket needs at least one component")
    return name, list(seen.items())


def _write_items(con, basket_id: int, items: list[tuple[int, float]]):
    con.execute("DELETE FROM pp_basket_items WHERE basket_id=?", (basket_id,))
    con.executemany(
        "INSERT INTO pp_basket_items (basket_id, type_id, qty) VALUES (?,?,?)",
        [(basket_id, tid, qty) for tid, qty in items],
    )


@router.post("/api/baskets")
def create_basket(req: BasketSave, _: int = Depends(require_admin),
                  pp_session: str = Cookie(default=None)):
    name, items = _validate_basket(req)
    ensure_basket_tables()
    con = get_connection()
    if con.execute("SELECT 1 FROM pp_baskets WHERE name=?", (name,)).fetchone():
        con.close()
        raise HTTPException(status_code=400, detail="A basket with that name already exists")
    cur = con.execute(
        "INSERT INTO pp_baskets (name, run_size, unit_label, created_by, created_at) VALUES (?,?,?,?,?)",
        (name, req.run_size, (req.unit_label or "sets").strip() or "sets",
         _session_char_name(pp_session), datetime.now(timezone.utc).isoformat()),
    )
    bid = cur.lastrowid
    _write_items(con, bid, items)
    con.commit()
    con.close()
    return {"ok": True, "id": bid}


@router.put("/api/baskets/{basket_id}")
def update_basket(basket_id: int, req: BasketSave, _: int = Depends(require_admin)):
    name, items = _validate_basket(req)
    ensure_basket_tables()
    con = get_connection()
    if not con.execute("SELECT 1 FROM pp_baskets WHERE id=?", (basket_id,)).fetchone():
        con.close()
        raise HTTPException(status_code=404, detail="Basket not found")
    if con.execute("SELECT 1 FROM pp_baskets WHERE name=? AND id<>?", (name, basket_id)).fetchone():
        con.close()
        raise HTTPException(status_code=400, detail="A basket with that name already exists")
    con.execute(
        "UPDATE pp_baskets SET name=?, run_size=?, unit_label=? WHERE id=?",
        (name, req.run_size, (req.unit_label or "sets").strip() or "sets", basket_id),
    )
    _write_items(con, basket_id, items)
    con.commit()
    con.close()
    return {"ok": True}


@router.delete("/api/baskets/{basket_id}")
def delete_basket(basket_id: int, _: int = Depends(require_admin)):
    ensure_basket_tables()
    con = get_connection()
    cur = con.execute("DELETE FROM pp_baskets WHERE id=?", (basket_id,))
    con.execute("DELETE FROM pp_basket_items WHERE basket_id=?", (basket_id,))
    con.commit()
    changed = cur.rowcount
    con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Basket not found")
    return {"ok": True}


# ── Admin users ───────────────────────────────────────────────────────────────

class AdminAdd(BaseModel):
    character_name: str


@router.get("/api/admins")
def list_admins(_: int = Depends(require_admin)):
    ensure_admin_table()
    con = get_connection()
    rows = con.execute(
        "SELECT character_name, added_by, added_at FROM pp_admins ORDER BY character_name COLLATE NOCASE"
    ).fetchall()
    con.close()
    return {
        "admins":    [dict(r) for r in rows],
        "bootstrap": sorted(ADMIN_CHARACTERS),
    }


@router.post("/api/admins")
def add_admin(req: AdminAdd, _: int = Depends(require_admin),
              pp_session: str = Cookie(default=None)):
    name = (req.character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Character name is required")
    if name.lower() in ADMIN_CHARACTERS:
        raise HTTPException(status_code=400, detail="That character is already a permanent admin")
    ensure_admin_table()
    con = get_connection()
    con.execute(
        "INSERT OR IGNORE INTO pp_admins (character_name, added_by, added_at) VALUES (?,?,?)",
        (name, _session_char_name(pp_session), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.delete("/api/admins/{character_name}")
def remove_admin(character_name: str, _: int = Depends(require_admin)):
    if character_name.lower() in ADMIN_CHARACTERS:
        raise HTTPException(status_code=400, detail="Cannot remove a permanent admin")
    ensure_admin_table()
    con = get_connection()
    cur = con.execute("DELETE FROM pp_admins WHERE character_name=?", (character_name,))
    con.commit()
    changed = cur.rowcount
    con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Admin not found")
    return {"ok": True}
