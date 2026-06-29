"""Admin features: custom production baskets (global, admin-managed) and admin-user
management. Mirrors the auth/table pattern in `bugs.py`.

A basket is a named set of PI commodities + per-run quantities. It is planned by the same
multi-product engine as the built-in Fuel Blocks basket (see `fuelblock_planner`), so any
logged-in user can pick a basket as a wizard target. Only admins create/edit/delete them.
"""
import os
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

from app.sde import get_connection, load_pi_data
from app.esi import (
    require_admin, require_context, is_admin, session_context_id,
    ADMIN_CHARACTERS, ensure_admin_table, _sessions, _load_sessions,
    corp_wallet_summary, _db_tester_names,
)

router = APIRouter()

# Prometheus metrics endpoint — disabled by default.
# Enable by setting PROMETHEUS_ENABLED=1 in the environment.
# Secure with PROMETHEUS_TOKEN=<secret>; Prometheus scrape config:
#   bearer_token: <secret>
_PROMETHEUS_ENABLED = os.environ.get("PROMETHEUS_ENABLED", "0").strip() == "1"
_PROMETHEUS_TOKEN   = os.environ.get("PROMETHEUS_TOKEN", "").strip()


@router.get("/api/corp-wallet")
def corp_wallet(ctx: int = Depends(require_admin)):
    """Admin: corp wallet balance + recent player donations, read via a wallet-scoped
    character in the admin's own context (require_admin returns that context id)."""
    return corp_wallet_summary(ctx)

# Per-character config (pp_plan_config) for a basket is keyed by a synthetic type id well
# above real PI type_ids (~<100k) and the fuel-block sentinel (4312), so it never collides.
BASKET_CONFIG_BASE = 2_000_000_000


_NEW_BASKETS_DDL = """
    CREATE TABLE IF NOT EXISTS pp_baskets (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        name       TEXT NOT NULL,
        run_size   INTEGER DEFAULT 1,
        unit_label TEXT DEFAULT 'sets',
        context_id INTEGER,            -- NULL = global (admin-owned); else private to a context
        created_by TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
"""


def ensure_basket_tables():
    """Create the basket tables, migrating the original schema (global `name UNIQUE`, no
    `context_id`) to the private-basket schema. The old table-level UNIQUE on name is
    rebuilt away so two users can own a same-named private basket; uniqueness is now scoped
    per owner via the (IFNULL(context_id,-1), name) index (globals share the -1 bucket)."""
    con = get_connection()
    has_table = con.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pp_baskets'"
    ).fetchone()
    if has_table:
        cols = {r["name"] for r in con.execute("PRAGMA table_info(pp_baskets)")}
        if "context_id" not in cols:
            # Rebuild: drops the legacy global UNIQUE(name); existing baskets become global.
            con.execute("ALTER TABLE pp_baskets RENAME TO pp_baskets_old")
            con.execute(_NEW_BASKETS_DDL)
            con.execute(
                "INSERT INTO pp_baskets (id, name, run_size, unit_label, context_id, created_by, created_at) "
                "SELECT id, name, run_size, unit_label, NULL, created_by, created_at FROM pp_baskets_old"
            )
            con.execute("DROP TABLE pp_baskets_old")
    else:
        con.execute(_NEW_BASKETS_DDL)
    con.execute("""
        CREATE TABLE IF NOT EXISTS pp_basket_items (
            basket_id INTEGER NOT NULL,
            type_id   INTEGER NOT NULL,
            qty       REAL    NOT NULL,
            PRIMARY KEY (basket_id, type_id)
        )
    """)
    con.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS ix_baskets_owner_name "
        "ON pp_baskets (IFNULL(context_id, -1), name)"
    )
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
    make_global: bool = False  # admin-only; ignored for non-admins (basket stays private)


def _basket_dict(con, row, types, ctx: int | None = None) -> dict:
    items = con.execute(
        "SELECT type_id, qty FROM pp_basket_items WHERE basket_id=? ORDER BY type_id",
        (row["id"],),
    ).fetchall()
    owner = row["context_id"]
    return {
        "id":             row["id"],
        "name":           row["name"],
        "run_size":       row["run_size"] or 1,
        "unit_label":     row["unit_label"] or "sets",
        "config_type_id": BASKET_CONFIG_BASE + row["id"],
        "global":         owner is None,
        "owned":          owner is not None and owner == ctx,
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
def list_baskets(pp_session: str = Cookie(default=None)):
    """Public — lists global baskets to everyone, plus the caller's own private baskets."""
    ensure_basket_tables()
    ctx = session_context_id(pp_session)
    types = load_pi_data()["types"]
    con = get_connection()
    if ctx is not None:
        rows = con.execute(
            "SELECT * FROM pp_baskets WHERE context_id IS NULL OR context_id=? "
            "ORDER BY context_id IS NULL DESC, name",
            (ctx,),
        ).fetchall()
    else:
        rows = con.execute(
            "SELECT * FROM pp_baskets WHERE context_id IS NULL ORDER BY name"
        ).fetchall()
    out = [_basket_dict(con, r, types, ctx) for r in rows]
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


def _name_clash(con, name: str, owner: int | None, exclude_id: int | None = None) -> bool:
    """True if another basket in the same scope (same owner, or both global) has this name."""
    if owner is None:
        sql = "SELECT 1 FROM pp_baskets WHERE name=? AND context_id IS NULL"
        params: tuple = (name,)
    else:
        sql = "SELECT 1 FROM pp_baskets WHERE name=? AND context_id=?"
        params = (name, owner)
    if exclude_id is not None:
        sql += " AND id<>?"
        params = params + (exclude_id,)
    return con.execute(sql, params).fetchone() is not None


def _basket_for_edit(con, basket_id: int, ctx: int, admin: bool):
    """Return the basket row the caller may edit/delete, else raise 404/403. Owners edit
    their own private baskets; admins edit global (NULL-context) baskets."""
    row = con.execute("SELECT * FROM pp_baskets WHERE id=?", (basket_id,)).fetchone()
    if not row:
        raise HTTPException(status_code=404, detail="Basket not found")
    owner = row["context_id"]
    if owner is not None and owner == ctx:
        return row
    if owner is None and admin:
        return row
    raise HTTPException(status_code=403, detail="You can only edit your own baskets")


@router.post("/api/baskets")
def create_basket(req: BasketSave, ctx: int = Depends(require_context),
                  pp_session: str = Cookie(default=None)):
    name, items = _validate_basket(req)
    ensure_basket_tables()
    owner = None if (req.make_global and is_admin(pp_session)) else ctx
    con = get_connection()
    if _name_clash(con, name, owner):
        con.close()
        raise HTTPException(status_code=400, detail="You already have a basket with that name")
    cur = con.execute(
        "INSERT INTO pp_baskets (name, run_size, unit_label, context_id, created_by, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (name, req.run_size, (req.unit_label or "sets").strip() or "sets",
         owner, _session_char_name(pp_session), datetime.now(timezone.utc).isoformat()),
    )
    bid = cur.lastrowid
    _write_items(con, bid, items)
    con.commit()
    con.close()
    return {"ok": True, "id": bid}


@router.put("/api/baskets/{basket_id}")
def update_basket(basket_id: int, req: BasketSave, ctx: int = Depends(require_context),
                  pp_session: str = Cookie(default=None)):
    name, items = _validate_basket(req)
    ensure_basket_tables()
    con = get_connection()
    row = _basket_for_edit(con, basket_id, ctx, is_admin(pp_session))
    if _name_clash(con, name, row["context_id"], exclude_id=basket_id):
        con.close()
        raise HTTPException(status_code=400, detail="You already have a basket with that name")
    con.execute(
        "UPDATE pp_baskets SET name=?, run_size=?, unit_label=? WHERE id=?",
        (name, req.run_size, (req.unit_label or "sets").strip() or "sets", basket_id),
    )
    _write_items(con, basket_id, items)
    con.commit()
    con.close()
    return {"ok": True}


@router.delete("/api/baskets/{basket_id}")
def delete_basket(basket_id: int, ctx: int = Depends(require_context),
                  pp_session: str = Cookie(default=None)):
    ensure_basket_tables()
    con = get_connection()
    _basket_for_edit(con, basket_id, ctx, is_admin(pp_session))
    con.execute("DELETE FROM pp_baskets WHERE id=?", (basket_id,))
    con.execute("DELETE FROM pp_basket_items WHERE basket_id=?", (basket_id,))
    con.commit()
    con.close()
    return {"ok": True}


# ── Admin users ───────────────────────────────────────────────────────────────

class AdminAdd(BaseModel):
    character_name: str


@router.get("/api/character-names")
def all_character_names(_: int = Depends(require_admin)):
    """All real (non-dummy) character names known to the system, for admin autocomplete."""
    con = get_connection()
    rows = con.execute(
        "SELECT DISTINCT character_name FROM pp_characters "
        "WHERE COALESCE(is_dummy,0)=0 ORDER BY character_name COLLATE NOCASE"
    ).fetchall()
    con.close()
    return {"names": [r["character_name"] for r in rows]}


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


# ── Tester management ─────────────────────────────────────────────────────────

@router.get("/api/testers")
def list_testers(_: int = Depends(require_admin)):
    con = get_connection()
    rows = con.execute(
        "SELECT character_name, added_by, added_at FROM pp_testers ORDER BY character_name COLLATE NOCASE"
    ).fetchall()
    con.close()
    return {"testers": [dict(r) for r in rows]}


@router.post("/api/testers")
def add_tester(req: AdminAdd, _: int = Depends(require_admin),
               pp_session: str = Cookie(default=None)):
    name = (req.character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Character name is required")
    con = get_connection()
    con.execute(
        "INSERT OR IGNORE INTO pp_testers (character_name, added_by, added_at) VALUES (?,?,?)",
        (name, _session_char_name(pp_session), datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True}


@router.get("/api/admin/stats")
def admin_stats(_: int = Depends(require_admin)):
    """Aggregate system health stats — no names, counts only."""
    con = get_connection()
    s = {}

    # Users
    s["contexts"] = con.execute("SELECT COUNT(*) FROM pp_user_contexts").fetchone()[0]
    s["active_7d"] = con.execute(
        "SELECT COUNT(DISTINCT context_id) FROM pp_sessions "
        "WHERE created_at >= datetime('now', '-7 days')"
    ).fetchone()[0]
    s["active_30d"] = con.execute(
        "SELECT COUNT(DISTINCT context_id) FROM pp_sessions "
        "WHERE created_at >= datetime('now', '-30 days')"
    ).fetchone()[0]
    s["characters"] = con.execute(
        "SELECT COUNT(*) FROM pp_characters WHERE is_dummy=0"
    ).fetchone()[0]
    s["dummy_characters"] = con.execute(
        "SELECT COUNT(*) FROM pp_characters WHERE is_dummy=1"
    ).fetchone()[0]

    # Planet DB
    s["planet_rows"] = con.execute("SELECT COUNT(*) FROM pp_planets").fetchone()[0]
    s["systems_covered"] = con.execute(
        "SELECT COUNT(DISTINCT system) FROM pp_planets"
    ).fetchone()[0]
    s["constellations_covered"] = con.execute(
        "SELECT COUNT(DISTINCT constellation) FROM pp_planets WHERE constellation IS NOT NULL"
    ).fetchone()[0]
    s["pending_submissions"] = con.execute(
        "SELECT COUNT(*) FROM pp_planet_submissions WHERE status='pending'"
    ).fetchone()[0]

    # Usage
    s["profiles"] = con.execute("SELECT COUNT(*) FROM pp_profiles").fetchone()[0]
    s["shares"] = con.execute("SELECT COUNT(*) FROM pp_shares").fetchone()[0]
    s["shares_7d"] = con.execute(
        "SELECT COUNT(*) FROM pp_shares WHERE created_at >= datetime('now', '-7 days')"
    ).fetchone()[0]
    s["shares_30d"] = con.execute(
        "SELECT COUNT(*) FROM pp_shares WHERE created_at >= datetime('now', '-30 days')"
    ).fetchone()[0]
    s["shares_never_accessed"] = con.execute(
        "SELECT COUNT(*) FROM pp_shares "
        "WHERE last_accessed IS NULL AND created_at < datetime('now', '-7 days')"
    ).fetchone()[0]
    s["bugs_open"] = con.execute(
        "SELECT COUNT(*) FROM pp_bugs WHERE status='open'"
    ).fetchone()[0]
    s["bugs_total"] = con.execute("SELECT COUNT(*) FROM pp_bugs").fetchone()[0]

    # Raw data
    s["char_planet_scans"] = con.execute("SELECT COUNT(*) FROM pp_char_planets").fetchone()[0]
    s["colony_yield_rows"] = con.execute("SELECT COUNT(*) FROM pp_colony_yield").fetchone()[0]
    s["sessions"] = con.execute("SELECT COUNT(*) FROM pp_sessions").fetchone()[0]
    s["sessions_stale_90d"] = con.execute(
        "SELECT COUNT(*) FROM pp_sessions WHERE created_at < datetime('now', '-90 days')"
    ).fetchone()[0]

    con.close()
    return s


# Prometheus metric name → (help text, type)
_METRIC_META = {
    "evepi_users_total":              ("Total user accounts (contexts)", "gauge"),
    "evepi_users_active_7d":          ("Accounts with a session in the last 7 days", "gauge"),
    "evepi_users_active_30d":         ("Accounts with a session in the last 30 days", "gauge"),
    "evepi_characters_total":         ("Total non-dummy characters", "gauge"),
    "evepi_dummy_characters_total":   ("Total dummy/placeholder characters", "gauge"),
    "evepi_planet_rows_total":        ("Rows in the shared planet database", "gauge"),
    "evepi_systems_covered_total":    ("Systems with at least one planet row", "gauge"),
    "evepi_profiles_total":           ("Saved planner profiles", "gauge"),
    "evepi_shares_total":             ("Stored plan share links", "gauge"),
    "evepi_shares_7d_total":          ("Share links created in the last 7 days", "gauge"),
    "evepi_shares_never_accessed":    ("Share links created >7 days ago and never opened", "gauge"),
    "evepi_bugs_open":                ("Open bug reports", "gauge"),
    "evepi_bugs_total":               ("Total bug reports", "gauge"),
    "evepi_colony_scans_total":       ("Character colony scan records", "gauge"),
    "evepi_sessions_total":           ("Total stored sessions", "gauge"),
    "evepi_sessions_stale_90d":       ("Sessions older than 90 days", "gauge"),
}

# Maps admin_stats dict keys → Prometheus metric names (same order as above).
_STATS_TO_METRIC = {
    "contexts":             "evepi_users_total",
    "active_7d":            "evepi_users_active_7d",
    "active_30d":           "evepi_users_active_30d",
    "characters":           "evepi_characters_total",
    "dummy_characters":     "evepi_dummy_characters_total",
    "planet_rows":          "evepi_planet_rows_total",
    "systems_covered":      "evepi_systems_covered_total",
    "profiles":             "evepi_profiles_total",
    "shares":               "evepi_shares_total",
    "shares_7d":            "evepi_shares_7d_total",
    "shares_never_accessed":"evepi_shares_never_accessed",
    "bugs_open":            "evepi_bugs_open",
    "bugs_total":           "evepi_bugs_total",
    "char_planet_scans":    "evepi_colony_scans_total",
    "sessions":             "evepi_sessions_total",
    "sessions_stale_90d":   "evepi_sessions_stale_90d",
}


@router.get("/metrics", response_class=PlainTextResponse, include_in_schema=False)
def prometheus_metrics(request: Request):
    """Prometheus text-format metrics scrape endpoint.
    Disabled unless PROMETHEUS_ENABLED=1. When PROMETHEUS_TOKEN is set, the
    request must carry 'Authorization: Bearer <token>'."""
    if not _PROMETHEUS_ENABLED:
        raise HTTPException(status_code=404, detail="Not found")

    if _PROMETHEUS_TOKEN:
        auth = request.headers.get("Authorization", "")
        if auth != f"Bearer {_PROMETHEUS_TOKEN}":
            raise HTTPException(status_code=401, detail="Unauthorized")

    stats = admin_stats(0)  # pass dummy ctx — admin_stats ignores it

    lines = []
    for stat_key, metric_name in _STATS_TO_METRIC.items():
        help_text, metric_type = _METRIC_META[metric_name]
        lines.append(f"# HELP {metric_name} {help_text}")
        lines.append(f"# TYPE {metric_name} {metric_type}")
        lines.append(f"{metric_name} {stats[stat_key]}")

    return "\n".join(lines) + "\n"


@router.delete("/api/testers/{character_name}")
def remove_tester(character_name: str, _: int = Depends(require_admin)):
    con = get_connection()
    cur = con.execute("DELETE FROM pp_testers WHERE character_name=?", (character_name,))
    con.commit()
    changed = cur.rowcount
    con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Tester not found")
    return {"ok": True}
