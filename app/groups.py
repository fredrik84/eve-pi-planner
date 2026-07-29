"""
Generic alliance-scoped group management: a group maps to one EVE alliance, can have delegated
managers (characters who can edit that group's own data without being a site admin), and can be
restricted to a specific set of app pages.

Started life as a one-off B0SS-alliance hardcode (app.esi's old B0SS_ALLIANCE_ID/is_b0ss_member)
built for the Reactions moon-goo pricing tool. Generalized 2026-07-12 once a second real need for
alliance-scoped data (JF import/collateral settings varying per alliance, "I doubt everyone has
the same values") plus the explicit ask to support other alliances made the one-off no longer
sufficient — this repo is open-source, so a single hardcoded alliance ID baked into the code
doesn't make sense either. See [[project_eve_pi_planner_group_management_todo]].

Global (site) admins create groups and assign managers (`pp_group_managers`) — no self-service
group registration yet, by explicit user decision ("let's just let admins manage it" for now).
A group manager can edit ONLY their own group's owned data (moon-goo price sheet, reaction
settings — see app.moon_goo / app.reactions for that actual data), never another group's.

`pp_group_pages` is a real per-group PAGE-level access control (replaced an earlier, narrower
"grant a specific internal feature flag" design 2026-07-12, per explicit user redirect — "I
don't want people to enable or disable such specific features. Maybe just allow or disallow
access to pages"): if a group has ANY rows here, its members are restricted to exactly those
pages, even ones that would otherwise be open to everyone (Dashboard, Planetary Planning, etc.)
— an empty/unconfigured group is the safe default (unrestricted, same as today, so nothing
regresses for an existing group on deploy).

Enforcement is **both** ends. The frontend hides the nav tab; the backend rejects the calls via
`require_page(key)`, applied as a router-level dependency on the self-contained feature routers
(app.reactions, app.industry). It used to be frontend-only, which meant a restricted member could
still call the endpoints directly and get everything the tab would have shown — fine as a
visibility gate, wrong for something the Admin UI presents as access control. Real authorization
is still "this is your own data" via require_context; this is a second, coarser gate on top."""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once
from app.esi import (require_admin, require_context, session_context_id,
                     admin_and_tester_status_for_context, _context_character_names)

router = APIRouter()

# The alliance this whole feature was originally built for — used only to seed a "B0SS" group on
# first migration so its existing pp_moon_goo_prices/pp_reaction_settings data keeps working
# unchanged after the generalization (see those modules' own migrations).
_BOOTSTRAP_ALLIANCE_ID = 99007887
_BOOTSTRAP_GROUP_NAME = "B0SS"

# The app's main navigable pages/tabs that make sense to restrict per group — deliberately a
# short, curated, user-recognizable list, NOT the full app.features.FEATURE_REGISTRY (that has
# ~18 granular internal flags like "pad_fill"/"esi_cache_skip" that mean nothing to a group
# admin picking which pages an alliance's members can reach). Keys match the DOM's data-tab
# attributes (static/index.html) so the frontend can hide/show by direct selector.
PAGE_REGISTRY = [
    {"key": "dashboard", "label": "Dashboard"},
    {"key": "analyze", "label": "Setup Analysis"},
    {"key": "planetary", "label": "Planetary Planning"},
    {"key": "planner", "label": "Find Buildables / Refill a Plan"},
    {"key": "layout", "label": "Factory Layout"},
    {"key": "planetdb", "label": "Planet DB"},
    {"key": "reactions", "label": "Reactions"},
    {"key": "industry", "label": "Industry"},
]
_PAGE_KEYS = {p["key"] for p in PAGE_REGISTRY}


@ensure_once
def ensure_group_tables():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_groups (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT NOT NULL,
                alliance_id INTEGER UNIQUE,
                created_at  TEXT
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_group_managers (
                group_id       INTEGER NOT NULL,
                character_name TEXT NOT NULL COLLATE NOCASE,
                PRIMARY KEY (group_id, character_name)
            )
        """)
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_group_pages (
                group_id INTEGER NOT NULL,
                page_key TEXT NOT NULL,
                PRIMARY KEY (group_id, page_key)
            )
        """)
        con.commit()
        row = con.execute("SELECT id FROM pp_groups WHERE alliance_id=?", (_BOOTSTRAP_ALLIANCE_ID,)).fetchone()
        if not row:
            con.execute(
                "INSERT INTO pp_groups (name, alliance_id, created_at) VALUES (?,?,?)",
                (_BOOTSTRAP_GROUP_NAME, _BOOTSTRAP_ALLIANCE_ID, datetime.now(timezone.utc).isoformat()),
            )
            con.commit()
    finally:
        con.close()


def bootstrap_group_id() -> int:
    """The B0SS group's id — used only by app.moon_goo/app.reactions' one-time data migrations
    to tag pre-existing rows so they keep pricing correctly after the generalization; never
    referenced in normal request handling."""
    ensure_group_tables()
    con = get_connection()
    try:
        row = con.execute("SELECT id FROM pp_groups WHERE alliance_id=?", (_BOOTSTRAP_ALLIANCE_ID,)).fetchone()
    finally:
        con.close()
    return row["id"]


def member_group(context_id: int) -> dict | None:
    """The group whose alliance a real (non-dummy) character of this context belongs to, or
    None. Deliberately NO admin-preview override (unlike is_admin/is_tester elsewhere in this
    app) — once there can be more than one group, an admin previewing the site can't
    automatically inherit any ONE alliance's below-market pricing; verify a specific group's
    pricing with a test character whose alliance_id is set to that group's, same as this
    feature has been tested all session."""
    ensure_group_tables()
    con = get_connection()
    try:
        row = con.execute(
            "SELECT g.id, g.name, g.alliance_id FROM pp_characters c "
            "JOIN pp_groups g ON g.alliance_id = c.alliance_id "
            "WHERE c.context_id=? AND COALESCE(c.is_dummy,0)=0 LIMIT 1",
            (context_id,),
        ).fetchone()
    finally:
        con.close()
    return dict(row) if row else None


def is_group_manager(context_id: int, group_id: int) -> bool:
    """Site admins can manage every group; otherwise the context must have a character listed
    as a manager of this specific group."""
    ensure_group_tables()
    is_admin, _ = admin_and_tester_status_for_context(context_id)
    if is_admin:
        return True
    names = set(_context_character_names(context_id))
    if not names:
        return False
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_name FROM pp_group_managers WHERE group_id=?", (group_id,)
        ).fetchall()
    finally:
        con.close()
    return any((r["character_name"] or "").lower() in names for r in rows)


def managed_group_ids(context_id: int) -> list[int]:
    """Every group this context can manage — every group at all for a site admin, else the
    specific ones it's a designated manager of. Drives the Admin UI's group picker and the
    "does this account see a group-management UI at all" signal on GET /api/characters."""
    ensure_group_tables()
    is_admin, _ = admin_and_tester_status_for_context(context_id)
    con = get_connection()
    try:
        if is_admin:
            rows = con.execute("SELECT id FROM pp_groups").fetchall()
            return [r["id"] for r in rows]
        names = set(_context_character_names(context_id))
        if not names:
            return []
        rows = con.execute("SELECT group_id, character_name FROM pp_group_managers").fetchall()
        return sorted({r["group_id"] for r in rows if (r["character_name"] or "").lower() in names})
    finally:
        con.close()


def caller_allowed_pages(context_id: int) -> list[str] | None:
    """None = unrestricted (the safe default — either not a member of any group, or the group
    has zero page restrictions configured, so nothing regresses for an existing group the first
    time this ships). A list = the caller's group has restricted its members to exactly these
    pages — checked via member_group (real alliance membership), not managed_group_ids, since
    this is about what a group's actual MEMBERS can reach, not who manages the group's data."""
    group = member_group(context_id)
    if not group:
        return None
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT page_key FROM pp_group_pages WHERE group_id=?", (group["id"],)
        ).fetchall()
    finally:
        con.close()
    if not rows:
        return None
    return [r["page_key"] for r in rows]


def require_page(page_key: str):
    """FastAPI dependency factory: 403 unless the caller's group may reach this page.

    Page access used to be advertised to the SPA (`restricted_pages` on /api/characters) and
    enforced ONLY by hiding tabs in the browser — so a restricted member could still call the
    endpoints directly and get everything. The Admin UI presents this as access control, so it
    has to actually control access.

    `caller_allowed_pages` returns None for "unrestricted", which is what almost every account
    gets (not in a group, or the group set no page restrictions) — so this is a no-op unless a
    group manager has deliberately narrowed the list. Anonymous callers pass through untouched:
    they have no group, and each endpoint's own require_context/require_admin still applies.
    """
    def _dep(pp_session: str = Cookie(default=None)) -> None:
        context_id = session_context_id(pp_session)
        if context_id is None:
            return
        allowed = caller_allowed_pages(context_id)
        if allowed is not None and page_key not in allowed:
            raise HTTPException(
                status_code=403,
                detail="Your group doesn't have access to this page.",
            )
    return _dep


# ── Admin CRUD (site admin only — group creation/manager assignment is manual for now) ─────

class GroupSave(BaseModel):
    name: str
    alliance_id: int


@router.get("/api/admin/groups")
def list_groups(_: int = Depends(require_admin)):
    ensure_group_tables()
    con = get_connection()
    try:
        groups = [dict(r) for r in con.execute(
            "SELECT id, name, alliance_id, created_at FROM pp_groups ORDER BY name COLLATE NOCASE"
        )]
        managers = con.execute("SELECT group_id, character_name FROM pp_group_managers").fetchall()
        pages = con.execute("SELECT group_id, page_key FROM pp_group_pages").fetchall()
    finally:
        con.close()
    for g in groups:
        g["managers"] = sorted(r["character_name"] for r in managers if r["group_id"] == g["id"])
        g["allowed_pages"] = sorted(r["page_key"] for r in pages if r["group_id"] == g["id"])
    return {"groups": groups, "page_registry": PAGE_REGISTRY}


@router.post("/api/admin/groups")
def create_group(req: GroupSave, _: int = Depends(require_admin)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    ensure_group_tables()
    con = get_connection()
    try:
        existing = con.execute("SELECT 1 FROM pp_groups WHERE alliance_id=?", (req.alliance_id,)).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="A group for that alliance already exists")
        con.execute(
            "INSERT INTO pp_groups (name, alliance_id, created_at) VALUES (?,?,?)",
            (name, req.alliance_id, datetime.now(timezone.utc).isoformat()),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.put("/api/admin/groups/{group_id}")
def update_group(group_id: int, req: GroupSave, _: int = Depends(require_admin)):
    name = (req.name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Group name is required")
    ensure_group_tables()
    con = get_connection()
    try:
        existing = con.execute(
            "SELECT id FROM pp_groups WHERE alliance_id=? AND id!=?", (req.alliance_id, group_id)
        ).fetchone()
        if existing:
            raise HTTPException(status_code=400, detail="A group for that alliance already exists")
        cur = con.execute(
            "UPDATE pp_groups SET name=?, alliance_id=? WHERE id=?", (name, req.alliance_id, group_id)
        )
        con.commit()
        changed = cur.rowcount
    finally:
        con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


@router.delete("/api/admin/groups/{group_id}")
def delete_group(group_id: int, _: int = Depends(require_admin)):
    ensure_group_tables()
    con = get_connection()
    try:
        cur = con.execute("DELETE FROM pp_groups WHERE id=?", (group_id,))
        con.execute("DELETE FROM pp_group_managers WHERE group_id=?", (group_id,))
        con.execute("DELETE FROM pp_group_pages WHERE group_id=?", (group_id,))
        con.commit()
        changed = cur.rowcount
    finally:
        con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Group not found")
    return {"ok": True}


class ManagerAdd(BaseModel):
    character_name: str


@router.post("/api/admin/groups/{group_id}/managers")
def add_group_manager(group_id: int, req: ManagerAdd, _: int = Depends(require_admin)):
    name = (req.character_name or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="Character name is required")
    ensure_group_tables()
    con = get_connection()
    try:
        if not con.execute("SELECT 1 FROM pp_groups WHERE id=?", (group_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Group not found")
        con.execute(
            "INSERT OR IGNORE INTO pp_group_managers (group_id, character_name) VALUES (?,?)",
            (group_id, name),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/admin/groups/{group_id}/managers/{character_name}")
def remove_group_manager(group_id: int, character_name: str, _: int = Depends(require_admin)):
    ensure_group_tables()
    con = get_connection()
    try:
        cur = con.execute(
            "DELETE FROM pp_group_managers WHERE group_id=? AND character_name=?",
            (group_id, character_name),
        )
        con.commit()
        changed = cur.rowcount
    finally:
        con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Manager not found")
    return {"ok": True}


class PageAllow(BaseModel):
    page_key: str


@router.post("/api/admin/groups/{group_id}/pages")
def allow_group_page(group_id: int, req: PageAllow, _: int = Depends(require_admin)):
    """Adding the FIRST allowed page for a group flips it from unrestricted to restricted-to-
    exactly-the-checked-pages (see caller_allowed_pages) — a group with zero rows here has full
    access, matching how every group behaves today until an admin deliberately restricts one."""
    if req.page_key not in _PAGE_KEYS:
        raise HTTPException(status_code=404, detail="Unknown page")
    ensure_group_tables()
    con = get_connection()
    try:
        if not con.execute("SELECT 1 FROM pp_groups WHERE id=?", (group_id,)).fetchone():
            raise HTTPException(status_code=404, detail="Group not found")
        con.execute(
            "INSERT OR IGNORE INTO pp_group_pages (group_id, page_key) VALUES (?,?)",
            (group_id, req.page_key),
        )
        con.commit()
    finally:
        con.close()
    return {"ok": True}


@router.delete("/api/admin/groups/{group_id}/pages/{page_key}")
def disallow_group_page(group_id: int, page_key: str, _: int = Depends(require_admin)):
    """Removing the LAST allowed page returns the group to fully unrestricted (zero rows), not
    to zero-pages-reachable — a group is only ever restricted while it has at least one
    explicitly allowed page; there's no way to configure "allow nothing" through this UI."""
    ensure_group_tables()
    con = get_connection()
    try:
        cur = con.execute(
            "DELETE FROM pp_group_pages WHERE group_id=? AND page_key=?",
            (group_id, page_key),
        )
        con.commit()
        changed = cur.rowcount
    finally:
        con.close()
    if not changed:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


# ── Caller-facing (any logged-in manager, not just site admins) ────────────────────────────

@router.get("/api/groups/mine")
def my_groups(context_id: int = Depends(require_context)):
    """Groups the caller can manage — every group for a site admin, else just the ones they're
    a designated manager of. Drives the group-scoped Admin UI (Moon-goo prices, Reaction
    settings) without needing the site-admin-only CRUD endpoints above."""
    ids = managed_group_ids(context_id)
    if not ids:
        return {"groups": []}
    con = get_connection()
    try:
        placeholders = ",".join("?" * len(ids))
        rows = con.execute(
            f"SELECT id, name, alliance_id FROM pp_groups WHERE id IN ({placeholders}) "
            f"ORDER BY name COLLATE NOCASE", ids,
        ).fetchall()
    finally:
        con.close()
    return {"groups": [dict(r) for r in rows]}
