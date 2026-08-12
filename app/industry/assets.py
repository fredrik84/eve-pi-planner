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

Corp hangars come in two ways. `/corporations/{id}/assets/` is gated behind the **Director** role
with nothing weaker on offer, so for most corp members it can never answer — those users PASTE a
hangar instead, which is a source like any other: selectable, counted identically, and it works for
everyone. For a director it does answer, and directors run their builds out of corp hangars and
containers, so `refresh_corp_assets` reads them into exactly the same source list. A 403 there is
not an error, it is the normal answer for a non-director, and it's reported as "no Director role"
rather than a failure. Corp assets are scanned ONLY on request (never as part of the personal
refresh), and once per corporation no matter how many of the account's characters are in it.

Assets are read on demand and cached, like the blueprint and job caches — never polled, since a full
asset list is a heavy call.
"""

from __future__ import annotations

import json
import time

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.sde import add_columns
from app import esi_http
from app.esi import require_context, _get_valid_token, ASSETS_SCOPE, CORP_ASSETS_SCOPE
from app.industry._router import router

# Personal hangar flags — assets in a ship fit, delivery hangar or contract are NOT usable stock.
_USABLE_FLAGS = {"Hangar", "HangarAll"}
# The seven corp hangar divisions. Same idea as the personal flags: anything else a corp owns
# (deliveries, impounded, a ship's hold) is not stock a job can be fed from.
_CORP_FLAGS = {f"CorpSAG{n}": n for n in range(1, 8)}
# Flags an item carries when it sits INSIDE a plain container (station/secure/audit can). Anything
# else nested — Cargo, DroneBay, a slot — means it's in a ship and can't be fed to a job without
# unloading it first, so containment only counts through these.
_CONTENT_FLAGS = {"AutoFit", "Unlocked", "Locked"}
# Player-owned structure ids are allocated well above this; NPC station ids are 6-8 digits. Which
# side of it an id falls on decides whether the lookup is the public station endpoint or the
# ACL-gated structure one.
_STRUCTURE_ID_FLOOR = 100_000_000


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
        # Which scan owns a source ("char:<id>", "corp:<id>", "paste"). Without it a re-scan could
        # only replace the sources it found THIS time, so a container that had been emptied kept its
        # last known contents forever — stock you can no longer draw from, which is precisely the
        # error this module treats as the dangerous one.
        add_columns(con, "pp_asset_sources", "scope TEXT DEFAULT ''",
                    # WHERE the source is. A container was identified by its own name and its parent
                    # hangar and nothing else, which is ambiguous exactly when it matters — picking
                    # which box a build sources from with cans in several stations. BIGINT because a
                    # structure id is well past 2^31 (a Postgres INTEGER would overflow).
                    "location_id BIGINT",
                    "location_name TEXT DEFAULT ''",
                    "system_name TEXT DEFAULT ''")
        # Station and structure ids are static; assets are re-scanned often. Resolving them on every
        # scan would be a per-container ESI call for an answer that cannot change. Global, not
        # per-account: the id→name mapping is a property of New Eden, and a row is only ever read
        # back for an account that already holds assets there.
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_locations (
                location_id BIGINT PRIMARY KEY,
                name        TEXT NOT NULL DEFAULT '',
                system_name TEXT NOT NULL DEFAULT '',
                updated_at  REAL
            )
        """)
        # A named, reusable set of sources — "reaction stock" = three cans across two stations —
        # so binding a build to a familiar group of boxes is one pick rather than three. It is a
        # convenience OVER the per-plan set, not a second system: choosing one simply expands to
        # its keys on the order, which is the only thing any plan ever reads.
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_source_sets (
                context_id INTEGER NOT NULL,
                id         INTEGER NOT NULL,
                name       TEXT    NOT NULL,
                keys       TEXT    NOT NULL DEFAULT '[]',
                updated_at REAL,
                PRIMARY KEY (context_id, id)
            )
        """)
        con.commit()
    finally:
        con.close()


def list_source_sets(context_id: int) -> list[dict]:
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT id, name, keys FROM pp_source_sets WHERE context_id = ? ORDER BY name",
            (context_id,)).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        try:
            keys = [str(k) for k in json.loads(r["keys"] or "[]") if str(k).strip()]
        except Exception:
            keys = []
        out.append({"id": r["id"], "name": r["name"], "keys": keys})
    return out


def save_source_set(context_id: int, name: str, keys: list[str], set_id: int | None = None) -> dict:
    """Create or rename/replace one named set. Keyed by name when no id is given, so saving the same
    name twice updates it rather than growing a second set the user has to tell apart."""
    ensure_asset_tables()
    label = (name or "").strip()[:60]
    uniq = list(dict.fromkeys(k for k in (keys or []) if k))
    if not label:
        return {}
    con = get_connection()
    try:
        row = None
        if set_id is not None:
            row = con.execute("SELECT id FROM pp_source_sets WHERE context_id=? AND id=?",
                              (context_id, int(set_id))).fetchone()
        if row is None:
            row = con.execute("SELECT id FROM pp_source_sets WHERE context_id=? AND LOWER(name)=?",
                              (context_id, label.lower())).fetchone()
        if row is not None:
            con.execute("UPDATE pp_source_sets SET name=?, keys=?, updated_at=? "
                        "WHERE context_id=? AND id=?",
                        (label, json.dumps(uniq), time.time(), context_id, row["id"]))
            new_id = row["id"]
        else:
            nxt = con.execute("SELECT COALESCE(MAX(id),0)+1 AS n FROM pp_source_sets WHERE context_id=?",
                              (context_id,)).fetchone()["n"]
            con.execute("INSERT INTO pp_source_sets (context_id, id, name, keys, updated_at) "
                        "VALUES (?,?,?,?,?)", (context_id, nxt, label, json.dumps(uniq), time.time()))
            new_id = nxt
        con.commit()
    finally:
        con.close()
    return {"id": new_id, "name": label, "keys": uniq}


def delete_source_set(context_id: int, set_id: int) -> None:
    ensure_asset_tables()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_source_sets WHERE context_id=? AND id=?", (context_id, int(set_id)))
        con.commit()
    finally:
        con.close()


def _system_name(system_id: int, client=None) -> str:
    """System id → name, from the local `solar_systems` table, else ESI (and cached there).

    `solar_systems` is populated as a side effect of PI colony scans, so an account with no colonies
    in the system a container sits in simply has no row — hence the fallback.
    """
    if not system_id:
        return ""
    con = get_connection()
    try:
        row = con.execute("SELECT name FROM solar_systems WHERE system_id = ?",
                          (int(system_id),)).fetchone()
        if row and row["name"]:
            return row["name"]
    except Exception:
        return ""
    finally:
        con.close()
    try:
        r = esi_http.get(f"universe/systems/{int(system_id)}/?datasource=tranquility", client=client)
        if r.status_code != 200:
            return ""
        name = (r.json() or {}).get("name") or ""
    except Exception:
        return ""
    if name:
        con = get_connection()
        try:
            con.execute("INSERT OR IGNORE INTO solar_systems VALUES (?,?)", (int(system_id), name))
            con.commit()
        except Exception:
            pass
        finally:
            con.close()
    return name


def _resolve_location(location_id: int, token: str | None, client=None) -> dict:
    """{name, system_name} for a station or structure id — cached, and never fatal.

    A station is public. A **structure is ACL-gated**: a character with no access gets a 403, which
    is the normal answer for somebody else's citadel and must degrade to "no system name" rather
    than failing the asset scan — exactly what container naming already does. The unresolvable
    answer is cached too, or every scan would retry the same 403.
    """
    lid = int(location_id)
    con = get_connection()
    try:
        row = con.execute("SELECT name, system_name FROM pp_locations WHERE location_id = ?",
                          (lid,)).fetchone()
        if row:
            return {"name": row["name"] or "", "system_name": row["system_name"] or ""}
    except Exception:
        return {"name": "", "system_name": ""}
    finally:
        con.close()

    name, sys_id = "", 0
    try:
        if lid >= _STRUCTURE_ID_FLOOR:
            from app.markets import structure_info
            sj = structure_info(lid, token or "", client=client) or {}
            name = sj.get("name") or ""
            sys_id = int(sj.get("solar_system_id") or 0)
        else:
            r = esi_http.get(f"universe/stations/{lid}/?datasource=tranquility", client=client)
            if r.status_code == 200:
                sj = r.json() or {}
                name = sj.get("name") or ""
                sys_id = int(sj.get("system_id") or 0)
    except Exception:
        name, sys_id = "", 0

    out = {"name": name, "system_name": _system_name(sys_id, client=client) if sys_id else ""}
    con = get_connection()
    try:
        con.execute("INSERT OR IGNORE INTO pp_locations (location_id, name, system_name, updated_at) "
                    "VALUES (?,?,?,?)", (lid, out["name"], out["system_name"], time.time()))
        con.commit()
    except Exception:
        pass
    finally:
        con.close()
    return out


def _apply_locations(sources: dict, token: str | None, client=None) -> None:
    """Fill each source's station/structure + system name. Best-effort, like container naming — a
    source with no resolvable location is still perfectly usable, it just isn't grouped."""
    for meta in sources.values():
        lid = meta.get("location_id")
        if not lid:
            continue
        try:
            loc = _resolve_location(int(lid), token, client=client)
        except Exception:
            continue
        meta["location_name"] = loc.get("name") or ""
        meta["system_name"] = loc.get("system_name") or ""


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


def _paginate(client, url: str, token: str, cap: int = 30):
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


def _names_for(client, url: str, token: str, item_ids: list[int]) -> dict[int, str]:
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


def _split_by_source(rows, hangar_flags, hangar_key, hangar_name, cont_key=None):
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

    def root_of(a, depth=0):
        """The outermost asset of this item's chain — the one whose `location_id` is the station or
        structure itself, and whose `location_flag` is the hangar."""
        if depth > 12:
            return a
        p = _parent(a)
        return a if p is None else root_of(p, depth + 1)

    def hangar_of(a, depth=0):
        """The hangar flag at the top of this item's chain (for naming a container's parent)."""
        if depth > 12:
            return None
        return root_of(a, depth).get("location_flag")

    sources: dict[str, dict] = {}
    stock: dict[str, dict[int, float]] = {}
    containers: set[int] = set()
    cont_key = cont_key or (lambda loc: f"cont:{loc}")

    for a in rows:
        if not usable(a):
            continue
        tid = a.get("type_id")
        if tid is None:
            continue
        parent = _parent(a)
        if parent is not None:                       # sits inside a container
            loc = int(a["location_id"])
            key = cont_key(loc)
            containers.add(loc)
            root = root_of(parent)
            pflag = root.get("location_flag")
            # `item_id` is carried so container names can be applied without parsing it back out of
            # the key — the key format differs between the personal and corp scans. `location_id` is
            # the station or structure the whole chain roots in, which is what says WHERE this box
            # is; only containers carry one, because a hangar source key (`char:<id>`) spans every
            # station that character has a hangar in and so has no single location.
            sources.setdefault(key, {"kind": "container", "name": f"Container {loc}", "item_id": loc,
                                     "parent": hangar_name(pflag) if pflag in hangar_flags else "",
                                     "location_id": int(root.get("location_id") or 0) or None})
        else:                                        # loose in the hangar itself
            flag = a.get("location_flag")
            key = hangar_key(flag)
            sources.setdefault(key, {"kind": "hangar", "name": hangar_name(flag), "parent": ""})
        s = stock.setdefault(key, {})
        s[int(tid)] = s.get(int(tid), 0.0) + float(a.get("quantity") or 0)

    return sources, stock, sorted(containers)


def _apply_container_names(sources: dict, names: dict[int, str]) -> None:
    for meta in sources.values():
        nm = names.get(meta.get("item_id"))
        if nm:
            meta["name"] = nm


def _store(context_id: int, sources: dict, stock: dict, scope_keys: set[str],
           scope: str = "") -> None:
    """Replace the stored sources/stock for `scope_keys`, preserving each source's enabled flag so a
    refresh never silently switches a hangar off (or on).

    When `scope` is given, every source that scan owns is replaced — not just the ones it found this
    time. That's what retires a container which has since been emptied or unanchored; leaving its
    last contents behind would have the planner counting stock that isn't there.
    """
    ensure_asset_tables()
    if not scope_keys and not scope:
        return
    # Stock feeds `formula_print_floor`, which is part of the account snapshot every plan is built
    # from — see graph._account_snapshot.
    from app.industry.graph import clear_account_snapshot
    clear_account_snapshot(context_id)
    con = get_connection()
    try:
        keys = list(scope_keys) or ["\x00none"]     # a non-key, so the IN clause stays valid
        marks = ",".join("?" * len(keys))
        where = f"context_id = ? AND (key IN ({marks})" + (" OR scope = ?)" if scope else ")")
        args = (context_id, *keys, *((scope,) if scope else ()))
        prior = {r["key"]: r["enabled"] for r in con.execute(
            f"SELECT key, enabled FROM pp_asset_sources WHERE {where}", args).fetchall()}
        con.execute(f"DELETE FROM pp_asset_sources WHERE {where}", args)
        if prior:
            con.execute(
                f"DELETE FROM pp_asset_stock WHERE context_id = ? AND key IN ({','.join('?' * len(prior))})",
                (context_id, *prior))
        now = time.time()
        for key, meta in sources.items():
            con.execute(
                "INSERT INTO pp_asset_sources (context_id, key, kind, name, parent, enabled, "
                "item_count, updated_at, scope, location_id, location_name, system_name) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (context_id, key, meta["kind"], meta["name"], meta["parent"],
                 int(prior.get(key, 0)), len(stock.get(key, {})), now, scope,
                 meta.get("location_id"), meta.get("location_name") or "",
                 meta.get("system_name") or ""),
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
                        _apply_container_names(
                            srcs, _names_for(client, f"characters/{cid}/assets/names/", token, conts))
                    _apply_locations(srcs, token, client=client)
                    _store(context_id, srcs, stock, set(srcs), scope=f"char:{cid}")
                    ok += 1

        except Exception:
            failed += 1

    return {"characters": len(chars), "refreshed": ok, "failed": failed,
            "needs_reauth": [c["character_name"] for c in needs]}


def _corp_scanners(context_id: int) -> list[dict]:
    """Characters whose token carries the corp-assets scope. Only these are worth trying; anyone
    else would 403 on the scope before the role even came into it."""
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_id, character_name, scopes FROM pp_characters WHERE context_id = ?",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return [{"character_id": r["character_id"], "character_name": r["character_name"]}
            for r in rows if CORP_ASSETS_SCOPE in (r["scopes"] or "").split()]


def _division_names(client, corp_id: int, token: str) -> dict[int, str]:
    """{division number: name} for the corp's seven hangars. Best-effort — an unnamed division is
    still a perfectly usable source, so this never fails a scan."""
    try:
        r = esi_http.get(f"corporations/{corp_id}/divisions/", client=client, token=token)
        if r.status_code != 200:
            return {}
        return {int(h["division"]): h["name"] for h in (r.json() or {}).get("hangar", [])
                if h.get("name")}
    except Exception:
        return {}


def refresh_corp_assets(context_id: int) -> dict:
    """Read corp hangars and containers for every corporation the account can actually read one for.

    Scanned per CORPORATION, not per character: several toons in the same corp see the same hangars,
    and asking ESI once per toon would be the same heavy call repeated for an identical answer.

    A 403 means the character isn't a Director. That is the expected outcome for most players and
    is reported as such — not as a failure, and not as something to retry.
    """
    ensure_asset_tables()
    scanners = _corp_scanners(context_id)
    if not scanners:
        return {"scanned": 0, "corporations": [], "no_role": [], "needs_scope": True}

    seen: set[int] = set()
    done: list[str] = []
    no_role: list[str] = []
    failed = 0
    for c in scanners:
        cid, cname = c["character_id"], c["character_name"]
        token = _get_valid_token(cid)
        if not token:
            failed += 1
            continue
        try:
            with esi_http.client(timeout=25) as client:
                pub = esi_http.get(f"characters/{cid}/", client=client).json()
                corp_id = int(pub.get("corporation_id") or 0)
                if not corp_id or corp_id in seen:
                    continue
                rows = _paginate(client, f"corporations/{corp_id}/assets/", token)
                if isinstance(rows, str):          # "role" — not a Director, nothing to retry
                    no_role.append(cname)
                    continue
                seen.add(corp_id)
                corp_name = (esi_http.get(f"corporations/{corp_id}/", client=client).json()
                             or {}).get("name") or f"Corp {corp_id}"
                divs = _division_names(client, corp_id, token)
                srcs, stock, conts = _split_by_source(
                    rows, set(_CORP_FLAGS),
                    lambda f, _c=corp_id: f"corp:{_c}:h{_CORP_FLAGS[f]}",
                    lambda f, _n=corp_name, _d=divs: (
                        f"{_n} — {_d.get(_CORP_FLAGS[f]) or f'hangar {_CORP_FLAGS[f]}'}"),
                    cont_key=lambda loc, _c=corp_id: f"corp:{_c}:c{loc}",
                )
                if conts:
                    _apply_container_names(
                        srcs, _names_for(client, f"corporations/{corp_id}/assets/names/", token, conts))
                _apply_locations(srcs, token, client=client)
                _store(context_id, srcs, stock, set(srcs), scope=f"corp:{corp_id}")
                done.append(corp_name)
        except Exception:
            failed += 1

    return {"scanned": len(done), "corporations": done, "no_role": no_role, "failed": failed,
            "needs_scope": False}


def parse_stock_paste(text: str) -> tuple[dict[int, float], list[str]]:
    """An EVE inventory paste → ({type_id: qty}, names we couldn't match).

    Shared by every "paste what you've got" path (a stock source here, an order's sourced materials
    in sourcing.py): same parser the PI tools use, same SDE name resolution, so the two can't
    disagree about what a pasted hangar contains.
    """
    from app.pi import parse_inventory

    parsed = parse_inventory(text or "")
    if not parsed:
        return {}, []

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
    return stock, unknown


def add_pasted_source(context_id: int, name: str, text: str) -> dict:
    """Turn an EVE inventory paste into a stock source.

    The corp assets endpoint needs Director, which most corp members will never have — pasting the
    hangar is the equivalent that works for everyone. Pasted sources are enabled on creation: you
    just went to the trouble of pasting it, so meaning to use it is a safe assumption.
    """
    ensure_asset_tables()
    stock, unknown = parse_stock_paste(text)
    if not stock and not unknown:
        return {"added": 0, "unknown": [], "error": "empty"}
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


def _place_label(location_name: str, system_name: str) -> str:
    """"Where is this box" as one line: the station or structure, with its system named.

    Every NPC station name already begins with its system (`Jita IV - Moon 4 - …`), and players name
    structures the same way often enough to matter, so the system is appended only when the name
    doesn't already lead with it — otherwise the label reads "Jita IV - Moon 4 - CNAP · Jita".
    """
    loc = (location_name or "").strip()
    sysn = (system_name or "").strip()
    if loc and sysn:
        return loc if loc.lower().startswith(sysn.lower()) else f"{loc} · {sysn}"
    return loc or sysn


def list_sources(context_id: int) -> list[dict]:
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT key, kind, name, parent, enabled, item_count, location_id, "
            "COALESCE(location_name,'') AS location_name, COALESCE(system_name,'') AS system_name "
            "FROM pp_asset_sources WHERE context_id = ? ORDER BY kind, name",
            (context_id,),
        ).fetchall()
    finally:
        con.close()
    return [{"key": r["key"], "kind": r["kind"], "name": r["name"], "parent": r["parent"],
             "enabled": bool(r["enabled"]), "item_count": r["item_count"],
             # Where the box is. `place` is the one string every list groups by, built here so the
             # four places that show containers can't each invent their own wording.
             "location_id": r["location_id"], "location": r["location_name"],
             "system": r["system_name"], "place": _place_label(r["location_name"], r["system_name"]),
             # Corp sources are worth distinguishing in the UI: they're shared with the whole corp,
             # so "is this mine to spend" is a different question than for a personal hangar.
             "corp": str(r["key"]).startswith("corp:")} for r in rows]


def set_sources(context_id: int, keys: list[str], enabled: bool) -> None:
    # Stock feeds `formula_print_floor`, which is in the account snapshot every plan is built from.
    from app.industry.graph import clear_account_snapshot
    clear_account_snapshot(context_id)
    ensure_asset_tables()
    con = get_connection()
    try:
        for k in keys:
            con.execute("UPDATE pp_asset_sources SET enabled = ? WHERE context_id = ? AND key = ?",
                        (1 if enabled else 0, context_id, k))
        con.commit()
    finally:
        con.close()


def enabled_source_keys(context_id: int) -> list[str]:
    """The account-wide "the planner may spend these" tick list.

    Still the pool for any plan that has not been given a set of its own — which is every plan that
    predates per-plan sources, and is why turning that model on narrows nothing.
    """
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT key FROM pp_asset_sources WHERE context_id = ? AND enabled = 1",
            (context_id,)).fetchall()
    except Exception:
        return []
    finally:
        con.close()
    return [r["key"] for r in rows]


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


def source_quantities(context_id: int, key: str) -> dict[int, float]:
    """{type_id: qty} in ONE source, enabled or not.

    Deliberately ignores the enabled flag: this answers "what is in that container", which is a
    question about a specific box the user pointed at, not about the pool the planner may spend.
    """
    ensure_asset_tables()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT type_id, qty FROM pp_asset_stock WHERE context_id = ? AND key = ?",
            (context_id, key),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(r["type_id"]): float(r["qty"] or 0) for r in rows}


def source_quantities_multi(context_id: int, keys: list[str]) -> dict[int, float]:
    """{type_id: qty} pooled across SEVERAL named sources — a build's reaction stock and its
    manufacturing stock legitimately sit in different stations.

    No double counting is possible on the sum itself (an item belongs to exactly one source by
    construction, and the key set is de-duplicated first), so the boxes simply add up.
    Like `source_quantities`, this deliberately ignores the enabled flag: it asks what is in the
    boxes the user pointed at, not what the planner may spend.
    """
    ensure_asset_tables()
    uniq = list(dict.fromkeys(k for k in (keys or []) if k))
    if not uniq:
        return {}
    con = get_connection()
    try:
        marks = ",".join("?" * len(uniq))
        rows = con.execute(
            f"SELECT type_id, SUM(qty) AS q FROM pp_asset_stock WHERE context_id = ? "
            f"AND key IN ({marks}) GROUP BY type_id",
            (context_id, *uniq),
        ).fetchall()
    except Exception:
        return {}
    finally:
        con.close()
    return {int(r["type_id"]): float(r["q"] or 0) for r in rows}


def source_name(context_id: int, key: str) -> str | None:
    ensure_asset_tables()
    con = get_connection()
    try:
        row = con.execute("SELECT name FROM pp_asset_sources WHERE context_id = ? AND key = ?",
                          (context_id, key)).fetchone()
    except Exception:
        return None
    finally:
        con.close()
    return row["name"] if row else None


def source_labels(context_id: int, keys: list[str]) -> list[dict]:
    """`[{key, name, place}]` for the bound set, in the order given — so a panel can say which boxes
    a build pulls from AND where each one is, without a lookup per key."""
    uniq = list(dict.fromkeys(k for k in (keys or []) if k))
    if not uniq:
        return []
    by_key = {s["key"]: s for s in list_sources(context_id)}
    out = []
    for k in uniq:
        s = by_key.get(k)
        # A source that has since vanished from the scan still gets a row: the order is bound to it,
        # and silently dropping it would leave the panel claiming a binding the user never made.
        out.append({"key": k, "name": (s or {}).get("name") or k,
                    "place": (s or {}).get("place") or "", "missing": s is None})
    return out


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
        # Whether anything on this account could even try a corp scan. Nobody with the scope means
        # the button would only ever produce an error, so the UI offers a re-auth instead.
        "corp_scannable": len(_corp_scanners(context_id)),
        "corp_sources": sum(1 for s in srcs if s.get("corp")),
        "sets": list_source_sets(context_id),
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


@router.post("/api/industry/assets/refresh-corp")
def industry_assets_refresh_corp(ctx: int = Depends(require_context)):
    """Read the corp hangars and containers of any corporation a character on this account has the
    Director role in. Separate from the personal refresh on purpose: it's a heavy call that most
    accounts can't make at all, so it runs only when asked for."""
    res = refresh_corp_assets(ctx)
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


@router.get("/api/industry/assets/sources/{key}/items")
def industry_assets_source_items(key: str, ctx: int = Depends(require_context)):
    """What one source actually HOLDS: `[{type_id, name, qty, formula}]`, biggest stacks first.

    Reported from use 2026-08-08: *"I've pasted a bunch of formulas and I can see where it's
    located, but I cannot see what it contains."* The list showed a name, "pasted", and an item-type
    count — enough to know a paste landed, and not enough to know whether the formula you pasted it
    for is in there. A source you cannot read the contents of is a source you have to take on faith,
    and this one caps how many reaction jobs the planner will schedule.

    `formula` marks a reaction formula, since that is the reason most of these pastes exist.
    """
    ensure_asset_tables()
    con = get_connection()
    try:
        owns = con.execute("SELECT 1 FROM pp_asset_sources WHERE context_id=? AND key=?",
                           (ctx, key)).fetchone()
        if not owns:
            raise HTTPException(status_code=404, detail="No such stock source")
        rows = [dict(r) for r in con.execute(
            "SELECT s.type_id, s.qty, COALESCE(t.name, '') AS name FROM pp_asset_stock s "
            "LEFT JOIN types t ON t.type_id = s.type_id "
            "WHERE s.context_id=? AND s.key=? ORDER BY s.qty DESC", (ctx, key))]
        # A formula is the reaction's own "blueprint" type, so the reactions table is the authority
        # on which of these are formulas rather than a name match on "Reaction Formula".
        formula_ids = {int(r["reaction_id"]) for r in con.execute(
            "SELECT reaction_id FROM reactions")}
    finally:
        con.close()
    return {"items": [{"type_id": r["type_id"], "name": r["name"] or f"#{r['type_id']}",
                       "qty": r["qty"], "formula": int(r["type_id"]) in formula_ids}
                      for r in rows]}


@router.delete("/api/industry/assets/sources/{key}")
def industry_assets_source_delete(key: str, ctx: int = Depends(require_context)):
    delete_source(ctx, key)
    return assets_status(ctx)


class SourceSetSave(BaseModel):
    name: str
    keys: list[str] = []
    id: int | None = None


@router.get("/api/industry/source-sets")
def industry_source_sets(ctx: int = Depends(require_context)):
    """Named groups of stock sources, so "reaction stock" can be bound to a build in one pick."""
    return {"sets": list_source_sets(ctx)}


@router.post("/api/industry/source-sets")
def industry_source_set_save(req: SourceSetSave, ctx: int = Depends(require_context)):
    save_source_set(ctx, req.name, req.keys, req.id)
    return {"sets": list_source_sets(ctx)}


@router.delete("/api/industry/source-sets/{set_id}")
def industry_source_set_delete(set_id: int, ctx: int = Depends(require_context)):
    delete_source_set(ctx, set_id)
    return {"sets": list_source_sets(ctx)}
