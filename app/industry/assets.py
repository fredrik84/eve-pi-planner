"""Assets — what you already own, and which of it the planner is allowed to use.

Feeds two things the planner previously had to guess at:

* **`on_hand`.** The planner has always accepted an `on_hand` map (`aggregate_demand` subtracts it
  from gross demand) but nothing ever populated it, so every plan assumed you owned nothing and
  cheerfully told you to build components already sitting in your hangar. This is that missing
  source.
* **Epoch-free progress.** Job history can only answer "did I build this *recently*"; owning the
  output answers "is this done" outright, with no start-date guessing and surviving a re-queue.

**Stock is per SOURCE, and every source is opt-in.** A source is one personal hangar, one container,
or one pasted hangar. Nothing counts until you tick it. Being wrong here is asymmetric: counting
stock you cannot actually draw from makes the planner build too little and the shopping list miss
materials, which is worse than the over-build you get from ignoring stock entirely.

Sources are FLAT: an item belongs to exactly one source, the container it sits in if any, otherwise
the hangar itself. There is deliberately no "enabling a division also enables its containers" rule,
which keeps every toggle unambiguous.

Corp hangars come in by PASTING them, not over ESI. `/corporations/{id}/assets/` is gated behind the
**Director** role with nothing weaker on offer, so for almost every corp member it can never answer;
building on it would mean requesting a permission most users can't use, spending an ESI call per
character to get a 403, and showing a role error nobody can act on. A pasted hangar is a source like
any other — selectable, counted identically, and it works for everyone.

Assets are read on demand and cached, like the blueprint and job caches — never polled, since a full
asset list is a heavy call.
"""

from __future__ import annotations

import time

from fastapi import Depends
from pydantic import BaseModel

from app.db import get_connection
from app import esi_http
from app.esi import require_context, _get_valid_token, ASSETS_SCOPE
from app.industry._router import router

# Personal hangar flags — assets in a ship fit, delivery hangar or contract are NOT usable stock.
_USABLE_FLAGS = {"Hangar", "HangarAll"}
# Flags an item carries when it sits INSIDE a plain container (station/secure/audit can). Anything
# else nested — Cargo, DroneBay, a slot — means it's in a ship and can't be fed to a job without
# unloading it first, so containment only counts through these.
_CONTENT_FLAGS = {"AutoFit", "Unlocked", "Locked"}


def ensure_asset_tables():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_asset_sources (
                context_id  INTEGER NOT NULL,
                key         TEXT    NOT NULL,
                kind        TEXT    NOT NULL,
                name        TEXT    NOT NULL DEFAULT '',
                parent      TEXT    NOT NULL DEFAULT '',
                enabled     INTEGER NOT NULL DEFAULT 0,
                item_count  INTEGER NOT NULL DEFAULT 0,
                updated_at  REAL,
                PRIMARY KEY (context_id, key)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_asset_stock (
                context_id INTEGER NOT NULL,
                key        TEXT    NOT NULL,
                type_id    INTEGER NOT NULL,
                qty        REAL    NOT NULL DEFAULT 0,
                PRIMARY KEY (context_id, key, type_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_asset_stock_ctx ON pp_asset_stock (context_id)")
        con.commit()
    finally:
        con.close()


def chars_by_scope(context_id: int) -> tuple[list[dict], list[dict]]:
    """(can_scan, needs_reauth) for this account's characters.

    An EVE token only carries the scopes granted at its last authorisation, so a character connected
    before the assets scope existed holds a perfectly valid token that simply cannot read assets —
    it would 403. Checking the stored `scp` claim up front lets us name exactly who needs
    reconnecting instead of reporting a vague failure.
    """
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, character_name, scopes FROM pp_characters WHERE context_id = ?",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    ok, need = [], []
    for r in rows:
        entry = {"character_id": r["character_id"], "character_name": r["character_name"]}
        (ok if ASSETS_SCOPE in (r["scopes"] or "").split() else need).append(entry)
    return ok, need


def _paginate(client: httpx.Client, url: str, token: str, cap: int = 30):
    """All pages of an ESI asset list, or an error string ('role')."""
    rows: list[dict] = []
    page = 1
    while page <= cap:
        r = esi_http.get(url, client=client, token=token, params={"page": page})
        if r.status_code in (401, 403):
            return "role"
        if r.status_code == 404:
            break
        r.raise_for_status()
        batch = r.json()
        if not batch:
            break
        rows.extend(batch)
        if page >= int(r.headers.get("x-pages") or 1):
            break
        page += 1
    return rows


def _names_for(client: httpx.Client, url: str, token: str, item_ids: list[int]) -> dict[int, str]:
    """Resolve container names (ESI takes up to 1000 ids per call). Cosmetic — never fails a
    refresh, since an unnamed container is still a perfectly usable source."""
    out: dict[int, str] = {}
    for i in range(0, len(item_ids), 1000):
        try:
            r = esi_http.post(url, client=client, token=token, json=item_ids[i:i + 1000])
            r.raise_for_status()
            for row in r.json():
                if row.get("name"):
                    out[int(row["item_id"])] = row["name"]
        except Exception:
            continue
    return out


def _split_by_source(rows, hangar_flags, hangar_key, hangar_name):
    """Bucket every asset into exactly one source: the container it sits in, or else its hangar.

    Containment is only honoured when the chain actually roots in a usable hangar, so a can inside a
    ship fit never leaks in as usable stock.
    """
    by_id = {int(a["item_id"]): a for a in rows if a.get("item_id") is not None}

    def _parent(a):
        loc = a.get("location_id")
        return by_id.get(int(loc)) if loc is not None else None

    def usable(a, depth=0):
        """Reachable from a hangar through plain containers only. Checking every link matters: the
        outermost asset of a can inside a ship parked in the hangar is still the hangar, so a
        root-only check let a ship's cargo through as usable stock."""
        if depth > 12:
            return False                             # cycle guard
        p = _parent(a)
        if p is None:
            return a.get("location_flag") in hangar_flags
        return a.get("location_flag") in _CONTENT_FLAGS and usable(p, depth + 1)

    def hangar_of(a, depth=0):
        """The hangar flag at the top of this item's chain (for naming a container's parent)."""
        if depth > 12:
            return None
        p = _parent(a)
        return a.get("location_flag") if p is None else hangar_of(p, depth + 1)

    sources: dict[str, dict] = {}
    stock: dict[str, dict[int, float]] = {}
    containers: set[int] = set()

    for a in rows:
        if not usable(a):
            continue
        tid = a.get("type_id")
        if tid is None:
            continue
        parent = _parent(a)
        if parent is not None:                       # sits inside a container
            loc = int(a["location_id"])
            key = f"cont:{loc}"
            containers.add(loc)
            pflag = hangar_of(parent)
            sources.setdefault(key, {"kind": "container", "name": f"Container {loc}",
                                     "parent": hangar_name(pflag) if pflag in hangar_flags else ""})
        else:                                        # loose in the hangar itself
            flag = a.get("location_flag")
            key = hangar_key(flag)
            sources.setdefault(key, {"kind": "hangar", "name": hangar_name(flag), "parent": ""})
        s = stock.setdefault(key, {})
        s[int(tid)] = s.get(int(tid), 0.0) + float(a.get("quantity") or 0)

    return sources, stock, sorted(containers)


def _store(context_id: int, sources: dict, stock: dict, scope_keys: set[str]) -> None:
    """Replace the stored sources/stock for exactly `scope_keys`, preserving each source's enabled
    flag so a refresh never silently switches a hangar off (or on)."""
    ensure_asset_tables()
    if not scope_keys:
        return
    con = get_connection()
    try:
        marks = ",".join("?" * len(scope_keys))
        keys = list(scope_keys)
        prior = {r["key"]: r["enabled"] for r in con.execute(
            f"SELECT key, enabled FROM pp_asset_sources WHERE context_id = ? AND key IN ({marks})",
            (context_id, *keys),
        ).fetchall()}
        con.execute(f"DELETE FROM pp_asset_sources WHERE context_id = ? AND key IN ({marks})",
                    (context_id, *keys))
        con.execute(f"DELETE FROM pp_asset_stock WHERE context_id = ? AND key IN ({marks})",
                    (context_id, *keys))
        now = time.time()
        for key, meta in sources.items():
            con.execute(
                "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, "
                "item_count, updated_at) VALUES (?,?,?,?,?,?,?,?)",
                (context_id, key, meta["kind"], meta["name"], meta["parent"],
                 int(prior.get(key, 0)), len(stock.get(key, {})), now),
            )
            for tid, qty in stock.get(key, {}).items():
                con.execute(
                    "INSERT INTO pp_asset_stock (context_id, key, type_id, qty) VALUES (?,?,?,?)",
                    (context_id, key, int(tid), float(qty)),
                )
        con.commit()
    finally:
        con.close()


def refresh_assets(context_id: int) -> dict:
    """Re-read personal assets and rediscover the selectable sources. Existing on/off choices are
    preserved. Corp hangars are not read here — see the module docstring."""
    ensure_asset_tables()
    chars, needs = chars_by_scope(context_id)
    ok, failed = 0, 0
    for c in chars:
        cid, cname = c["character_id"], c["character_name"]
        token = _get_valid_token(cid)
        if not token:
            failed += 1
            continue
        try:
            with esi_http.client(timeout=25) as client:
                rows = _paginate(client, f"characters/{cid}/assets/", token)
                if isinstance(rows, str):
                    failed += 1
                else:
                    srcs, stock, conts = _split_by_source(
                        rows, _USABLE_FLAGS,
                        lambda _f, _c=cid: f"char:{_c}",
                        lambda _f, _n=cname: f"{_n} — personal hangar",
                    )
                    if conts:
                        nm = _names_for(client, f"characters/{cid}/assets/names/", token, conts)
                        for k, meta in srcs.items():
                            if k.startswith("cont:") and int(k.split(":")[1]) in nm:
                                meta["name"] = nm[int(k.split(":")[1])]
                    _store(context_id, srcs, stock, set(srcs))
                    ok += 1

        except Exception:
            failed += 1

    return {"characters": len(chars), "refreshed": ok, "failed": failed,
            "needs_reauth": [c["character_name"] for c in needs]}


def add_pasted_source(context_id: int, name: str, text: str) -> dict:
    """Turn an EVE inventory paste into a stock source.

    The corp assets endpoint needs Director, which most corp members will never have — pasting the
    hangar is the equivalent that works for everyone. Reuses the same paste parser the PI tools use,
    then resolves names to type_ids against the SDE. Pasted sources are enabled on creation: you
    just went to the trouble of pasting it, so meaning to use it is a safe assumption.
    """
    from app.pi import parse_inventory

    ensure_asset_tables()
    parsed = parse_inventory(text or "")
    if not parsed:
        return {"added": 0, "unknown": [], "error": "empty"}

    con = get_connection()
    try:
        lookup = {}
        names = list(parsed)
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                f"SELECT type_id, name FROM types WHERE LOWER(name) IN ({marks})",
                tuple(n.lower() for n in chunk),
            ).fetchall():
                lookup[r["name"].lower()] = r["type_id"]
    finally:
        con.close()

    stock: dict[int, float] = {}
    unknown: list[str] = []
    for nm, qty in parsed.items():
        tid = lookup.get(nm.lower())
        if tid is None:
            unknown.append(nm)
            continue
        stock[int(tid)] = stock.get(int(tid), 0.0) + float(qty)
    if not stock:
        return {"added": 0, "unknown": unknown, "error": "unrecognized"}

    key = f"paste:{abs(hash((name or 'Pasted stock').strip().lower())) % 10**12}"
    label = (name or "").strip() or "Pasted stock"
    _store(context_id, {key: {"kind": "paste", "name": label, "parent": "pasted"}},
           {key: stock}, {key})
    set_sources(context_id, [key], True)
    return {"added": len(stock), "unknown": unknown, "key": key, "name": label}


def delete_source(context_id: int, key: str) -> None:
    """Remove a source entirely. Only meaningful for pasted ones — ESI-derived sources come back on
    the next scan."""
    ensure_asset_tables()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_asset_sources WHERE context_id = ? AND key = ?", (context_id, key))
        con.execute("DELETE FROM pp_asset_stock WHERE context_id = ? AND key = ?", (context_id, key))
        con.commit()
    finally:
        con.close()


def list_sources(context_id: int) -> list[dict]:
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT key, kind, name, parent, enabled, item_count FROM pp_asset_sources "
            "WHERE context_id = ? ORDER BY kind, name",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return [{"key": r["key"], "kind": r["kind"], "name": r["name"], "parent": r["parent"],
             "enabled": bool(r["enabled"]), "item_count": r["item_count"]} for r in rows]


def set_sources(context_id: int, keys: list[str], enabled: bool) -> None:
    ensure_asset_tables()
    con = get_connection()
    try:
        for k in keys:
            con.execute("UPDATE pp_asset_sources SET enabled = ? WHERE context_id = ? AND key = ?",
                        (1 if enabled else 0, context_id, k))
        con.commit()
    finally:
        con.close()


def owned_quantities(context_id: int) -> dict[int, float]:
    """{type_id: qty} pooled across ENABLED sources only. Empty when nothing is enabled, which makes
    the planner behave exactly as it did before assets existed."""
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT s.type_id, SUM(s.qty) AS q FROM pp_asset_stock s "
            "JOIN pp_asset_sources src ON src.context_id = s.context_id AND src.key = s.key "
            "WHERE s.context_id = ? AND src.enabled = 1 GROUP BY s.type_id",
            (context_id,),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(r["type_id"]): float(r["q"] or 0) for r in rows}


def assets_status(context_id: int) -> dict:
    _ok, needs = chars_by_scope(context_id)
    srcs = list_sources(context_id)
    owned = owned_quantities(context_id)
    con = get_connection()
    try:
        row = con.execute("SELECT MAX(updated_at) AS t FROM pp_asset_sources WHERE context_id = ?",
                          (context_id,)).fetchone()
    except Exception:
        row = None
    finally:
        con.close()
    return {
        "connected": bool(srcs),
        "fetched_at": (row and row["t"]) or None,
        "sources": srcs,
        "enabled_sources": sum(1 for s in srcs if s["enabled"]),
        "distinct_types": len(owned),
        # Characters whose stored token predates the assets scope — they need one re-auth each.
        "needs_reauth": [c["character_name"] for c in needs],
        "scannable": len(_ok),
    }


@router.get("/api/industry/assets")
def industry_assets(ctx: int = Depends(require_context)):
    """Discovered stock sources (hangars, containers, pasted stock) and which of them are on."""
    return assets_status(ctx)


@router.post("/api/industry/assets/refresh")
def industry_assets_refresh(ctx: int = Depends(require_context)):
    res = refresh_assets(ctx)
    res.update(assets_status(ctx))
    return res


class SourceToggle(BaseModel):
    keys: list[str]
    enabled: bool


@router.post("/api/industry/assets/sources")
def industry_assets_sources(req: SourceToggle, ctx: int = Depends(require_context)):
    """Choose which hangars/containers the planner may draw materials from."""
    set_sources(ctx, req.keys, req.enabled)
    return assets_status(ctx)


class PasteStock(BaseModel):
    name: str = ""
    text: str


@router.post("/api/industry/assets/paste")
def industry_assets_paste(req: PasteStock, ctx: int = Depends(require_context)):
    """Add a stock source by pasting hangar contents from the EVE client — the path for anyone
    without the Director role the corp assets endpoint demands."""
    res = add_pasted_source(ctx, req.name, req.text)
    res.update(assets_status(ctx))
    return res


@router.delete("/api/industry/assets/sources/{key}")
def industry_assets_source_delete(key: str, ctx: int = Depends(require_context)):
    delete_source(ctx, key)
    return assets_status(ctx)
