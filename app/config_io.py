"""Export and import an account's build configuration as one portable JSON document.

**A serialiser, not a storage format.** TODO §18 asked whether the configuration should be reshaped
into one keyed blob and the answer, measured, was no (`docs/config-shape-2026-08.md`): 13 rows across
10 settings-shaped tables is not a storage problem. What survived was §18b — a tester's ravworks
export showed that a *portable* config object is genuinely useful, and portability turned out to be
independent of storage shape. So every field below is read through the reader that already owns it
(`account_setup`, `_list_markets`, `_account_reaction_settings_override`) and written back through
the setter that already owns it (`apply_patch`, `_upsert_settings`, the markets insert). This module
owns no table and no truth.

**What it discloses, stated plainly.** The export carries structure names, their location and system
ids, and placeholder character names — the account chose full fidelity over a scrubbed file, so the
UI warns on download rather than the file quietly being less than it looks. It is served by
`require_context` like every other per-account read (CLAUDE.md rule 8); the privacy question here is
about what the *user* then sends to someone, which is theirs to decide with the warning in front of
them. `EXPORT_DISCLOSES` is the list that warning is built from, so the two cannot drift.

**Import validates the whole document before it writes anything** — see `import_config` for what
that does and does not promise. A half-applied config is worse than a refused one: the sections
interlock (a pin names a structure; a source set names containers), and a user who has just been
told "imported" has no way to tell which half took. The writes span four stores and cannot be one
transaction, so every check a later writer would make is hoisted into `_validate` instead, and every
section is idempotent so a re-import finishes rather than doubles.

Two ids are not portable and are remapped or dropped rather than written through:

  * **pins** are stored as `s:<pp_markets row id>` — an id that means nothing in another account, or
    even in the same account after a structure is removed and re-added. They travel as the
    structure's `location_id`, which names the real building, and are re-resolved on import.
  * **source keys** name asset containers this account has scanned. They are exported so a
    self-backup restores the tick list, and any key the importing account does not have is reported
    as skipped rather than written as a pointer to nothing.
"""
from __future__ import annotations

import json
import time

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.db import get_connection
from app.esi import require_context

router = APIRouter()

FEATURE_KEY = "config_export_import"

# **Bump this whenever a SECTION is added, not only on a breaking change.** The importer refuses an
# unknown industry section outright rather than skipping it, which is the right answer for a file
# that is hand-edited and passed between people — a typo silently doing nothing is how somebody
# comes to believe a setting was imported. The cost of that strictness is exactly this rule: a new
# section makes new files unreadable to an older deploy, so the version has to say so and the older
# deploy has to refuse the file for a reason it can state, rather than choking on a field name.
CONFIG_VERSION = 1
CONFIG_KIND = "eve-pi-planner.config"

# What a shared export tells its recipient. The download warning is rendered from this list so the
# two cannot drift — a new identifying field added to the export belongs here in the same edit.
EXPORT_DISCLOSES = [
    "the names, systems and location ids of your build structures",
    "your placeholder characters' names and declared job slots",
    "your freight rates, facility tax and reaction system",
    "the names of your saved stock-source sets",
]


def _available(context_id: int) -> bool:
    from app.features import feature_enabled_for
    return feature_enabled_for(FEATURE_KEY, context_id)


def _require_available(context_id: int) -> None:
    if not _available(context_id):
        raise HTTPException(status_code=403, detail="feature not enabled")


# ── Export ────────────────────────────────────────────────────────────────────────────────────

# The structure columns worth carrying, borrowed wholesale from the group-share path: describing a
# building is describing a building, and a second list here would be the one that goes stale when a
# rig column is added. `location_id` doubles as the identity an import matches on.
def _structures(context_id: int) -> list[dict]:
    from app.markets import _list_markets, _SHAREABLE_COLS
    out = []
    for m in _list_markets("account", context_id):
        if m["kind"] != "structure":
            continue
        row = {c: m.get(c) for c in _SHAREABLE_COLS if c in m}
        row["kind"] = "structure"
        out.append(row)
    return out


def _pins_by_location(context_id: int) -> dict[str, int]:
    """The pin map with each target rewritten from `s:<row id>` to the structure's location_id.

    The row id is the account's own primary key — meaningless anywhere else, and not even stable
    here (removing a structure and adding it back mints a new one). The location is the building."""
    from app.industry.settings import get_build_pins
    from app.markets import _list_markets
    by_id = {str(m["id"]): m.get("location_id") for m in _list_markets("account", context_id)}
    out = {}
    for family, target in (get_build_pins(context_id) or {}).items():
        loc = by_id.get(str(target).removeprefix("s:"))
        if loc is not None:
            out[family] = int(loc)
    return out


def _placeholders(context_id: int) -> list[dict]:
    """Declared capacity for characters that have no ESI to read — the ravworks export's "declared
    slots", reached from our end. Real characters are deliberately absent: their slots come from
    measured skills, so writing them from a file would be inventing capacity (see slots.py)."""
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT character_name, COALESCE(interplanetary_consolidation,0) AS ipc, "
            "COALESCE(command_center_upgrades,0) AS ccu, COALESCE(dummy_mfg_slots,0) AS mfg, "
            "COALESCE(dummy_rx_slots,0) AS rx FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=1 ORDER BY character_name",
            (context_id,)).fetchall()
    finally:
        con.close()
    return [{"name": r["character_name"], "max_planets": (r["ipc"] or 0) + 1,
             "ccu": r["ccu"], "mfg_slots": r["mfg"], "rx_slots": r["rx"]} for r in rows]


def _sources(context_id: int) -> dict:
    """The account-wide "the planner may spend these" ticks and the named sets over them. Tolerant
    for the same reason `_sources_section` is: an account with no asset scan has none of this, and
    that is a normal empty state."""
    try:
        from app.industry.assets import enabled_source_keys, list_source_sets
        return {"enabled_keys": enabled_source_keys(context_id),
                "sets": [{"name": s["name"], "keys": s["keys"]} for s in list_source_sets(context_id)]}
    except Exception:
        return {"enabled_keys": [], "sets": []}


def export_config(context_id: int) -> dict:
    """The whole configuration, as the sections an import writes back — nothing derived.

    `account_setup` answers with the pick-lists as well as the choices (the category registry, the
    rig families, the container list), because the modal it was written for renders controls. An
    export carries only what was CHOSEN: a registry is a fact about the running app, and a file that
    restated it would be a second, stale copy of something the importing deploy already knows."""
    from app.industry.build_setup import account_setup
    from app.reactions.settings import _account_reaction_settings_override
    a = account_setup(context_id)
    return {
        "kind": CONFIG_KIND,
        "version": CONFIG_VERSION,
        "exported_at": time.time(),
        "industry": {
            "facility": a["facility"],
            "threshold": a["threshold"],
            "margin": a["margin"],
            # The policy only — `categories` and `defaulted` are the registry and a fact about
            # storage, neither of which a file can carry into another deploy meaningfully.
            "reactions": a["reactions"]["policy"],
            # Names ride along because a list of type ids is not something anyone can read; the
            # importer matches on `type_id` and never trusts the name.
            "components": a["components"]["items"],
            "job_length": a["job_length"],
            "per_order_plans": a["per_order_plans"],
            "pins": _pins_by_location(context_id),
        },
        "structures": _structures(context_id),
        # The account's PERSONAL override only. A group default is the group's configuration, not
        # this account's, and a member cannot write it back — exporting it would put a value in the
        # file that import is obliged to refuse.
        "reaction_settings": _account_reaction_settings_override(context_id),
        "sources": _sources(context_id),
        "placeholders": _placeholders(context_id),
    }


# ── Import ────────────────────────────────────────────────────────────────────────────────────

_INDUSTRY_SECTIONS = ("facility", "threshold", "margin", "reactions", "components",
                      "job_length", "pins", "per_order_plans")


class ConfigImport(BaseModel):
    """The document, plus the two answers a user gives about how to apply it.

    `dry_run` is not a debug flag — it is how the UI shows what an import will do before it does it,
    which is the only honest way to offer "load someone else's config" to somebody who has spent
    weeks on their own. `replace_structures` is the one genuinely destructive choice in here, so it
    is opt-in and never inferred: the default merges, and a location the account already has keeps
    the answers it already had."""
    config: dict
    dry_run: bool = False
    replace_structures: bool = False


# A settings value is a number, a string, a bool or nothing. Pinned because these fields are copied
# STRAIGHT through to `save_settings` — a nested object in `facility_id` used to reach the writer and
# come back as a 500 with the structures already merged, which is the half-applied import this
# module's whole shape exists to prevent.
_SCALAR_FIELDS = {
    "facility": ("facility_id", "struct_material_pct", "struct_time_pct"),
    "threshold": ("marginal_pct", "force_build", "prioritize_speed"),
    "margin": ("margin_pct",),
    "job_length": ("max_reaction_job_days",),
}
# What one file may carry. Not a performance limit — it is the same reasoning as
# `add_dummy_characters`' clamp of 100: a file is passed between people, and a hand-edited one
# should be refused rather than allowed to write ten thousand rows into somebody's account.
_MAX_STRUCTURES = 200
_MAX_PLACEHOLDERS = 100


def _validate(doc: dict) -> list[str]:
    """Everything wrong with this document, as a list — not the first thing wrong.

    A validator that stops at the first error makes fixing a hand-edited file a guessing game one
    round-trip at a time, and this file is meant to be passed between people.

    **Everything the writers cannot survive is checked HERE**, because the writes themselves span
    four stores and cannot be one transaction: structures commit before the industry patch runs, so
    anything that would raise in a later writer has to be caught before the first one starts. Each
    check below exists because the value it rejects reached a writer and produced a 500 with half
    the file applied."""
    problems: list[str] = []
    if not isinstance(doc, dict):
        return ["not a configuration file (expected a JSON object)"]
    if doc.get("kind") != CONFIG_KIND:
        problems.append(f"not an eve-pi-planner config file (kind={doc.get('kind')!r})")
    try:
        version = int(doc.get("version"))
    except (TypeError, ValueError):
        version = -1
    if version < 1:
        problems.append("missing or unreadable version")
    elif version > CONFIG_VERSION:
        problems.append(f"written by a newer version of the app (file v{version}, this deploy reads "
                        f"up to v{CONFIG_VERSION}) — update, or export again from the older deploy")
    for key, typ, label in (("industry", dict, "object"), ("structures", list, "list"),
                            ("sources", dict, "object"), ("placeholders", list, "list")):
        if key in doc and doc[key] is not None and not isinstance(doc[key], typ):
            problems.append(f"`{key}` must be a JSON {label}")
    ind = doc.get("industry")
    if isinstance(ind, dict):
        for section in ind:
            if section not in _INDUSTRY_SECTIONS:
                problems.append(f"unknown industry section `{section}`")
        for section, fields in _SCALAR_FIELDS.items():
            body = ind.get(section)
            if body is None:
                continue
            if not isinstance(body, dict):
                problems.append(f"`industry.{section}` must be a JSON object")
                continue
            for f in fields:
                if f in body and not isinstance(body[f], (int, float, str, bool, type(None))):
                    problems.append(f"`industry.{section}.{f}` must be a number, text or true/false")
        pins = ind.get("pins")
        if pins is not None and not isinstance(pins, dict):
            problems.append("`industry.pins` must be a JSON object")
        per_order = ind.get("per_order_plans")
        if per_order is not None and not isinstance(per_order, dict):
            problems.append("`industry.per_order_plans` must be a JSON object")
        rx_policy = ind.get("reactions")
        if rx_policy is not None and not isinstance(rx_policy, dict):
            problems.append("`industry.reactions` must be a JSON object")
        items = ind.get("components")
        if items is not None and not isinstance(items, list):
            problems.append("`industry.components` must be a list")
        elif isinstance(items, list):
            for it in items:
                if not isinstance(it, dict) or not str(it.get("type_id", "")).strip().lstrip("-").isdigit():
                    problems.append("every component needs a numeric `type_id`")
                    break
    problems += _validate_structures(doc)
    problems += _validate_reaction_settings(doc)
    holders = doc.get("placeholders")
    if isinstance(holders, list) and len(holders) > _MAX_PLACEHOLDERS:
        problems.append(f"{len(holders)} placeholder characters — no more than {_MAX_PLACEHOLDERS} "
                        f"can be imported from one file")
    return problems


def _validate_structures(doc: dict) -> list[str]:
    """`name` and `kind` are `NOT NULL` in `pp_markets`, and a duplicate location is the exact state
    the merge exists to prevent — all three used to reach the writer, one as an IntegrityError and
    one as two rows for one building with rig answers that can disagree."""
    from app.markets import _ALLOWED_KINDS
    problems: list[str] = []
    rows = doc.get("structures")
    if not isinstance(rows, list):
        return problems
    if len(rows) > _MAX_STRUCTURES:
        problems.append(f"{len(rows)} structures — no more than {_MAX_STRUCTURES} can be imported "
                        f"from one file")
    seen: set[int] = set()
    for i, s in enumerate(rows):
        label = f"structure #{i + 1}"
        if not isinstance(s, dict):
            problems.append(f"{label} is not a JSON object")
            continue
        loc = s.get("location_id")
        if loc in (None, "") or not str(loc).lstrip("-").isdigit():
            problems.append(f"{label} has no usable `location_id`")
        elif int(loc) in seen:
            problems.append(f"{label} names a location already in this file — one building, one entry")
        else:
            seen.add(int(loc))
        if not isinstance(s.get("name"), str) or not s["name"].strip():
            problems.append(f"{label} has no `name`")
        if s.get("kind", "structure") not in _ALLOWED_KINDS:
            problems.append(f"{label} has an unknown `kind` ({s.get('kind')!r})")
    return problems


def _validate_reaction_settings(doc: dict) -> list[str]:
    """Built and system-checked here rather than at write time. `_upsert_settings` runs AFTER the
    structures are committed, so an unrecognised solar system used to 400 with half the file in."""
    rx = doc.get("reaction_settings")
    if rx is None:
        return []
    if not isinstance(rx, dict):
        return ["`reaction_settings` must be a JSON object or null"]
    try:
        _reaction_settings_update(rx)
    except HTTPException as e:
        return [f"reaction settings: {e.detail}"]
    except Exception as e:
        return [f"reaction settings: {e}"]
    return []


def _reaction_settings_update(rx: dict):
    """The file's reaction settings as the module's own update model, validated its own way. One
    function so the check the validator runs and the value the writer writes cannot differ."""
    from app.reactions.settings import ReactionSettingsUpdate, _validate_reaction_system
    try:
        req = ReactionSettingsUpdate(**{k: rx[k] for k in (
            "import_isk_per_m3", "export_isk_per_m3", "export_collateral_pct",
            "reaction_system", "facility_tax_pct", "time_efficiency_pct") if k in rx})
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e))
    _validate_reaction_system(req.reaction_system)
    return req


def _industry_patch(doc: dict, available: dict, loc_to_row: dict[int, int]) -> tuple[dict, list[str]]:
    """The document's industry half as a sparse `BuildSetupPatch` body, plus what was left out.

    Sections the account may not see are dropped HERE with a note rather than sent and refused:
    `apply_patch` 403s the whole write on a denied section, which for an import would mean one flag
    being off costs the user every other section in the file."""
    ind = doc.get("industry") or {}
    patch: dict = {}
    skipped: list[str] = []
    for section in ("facility", "threshold", "margin"):
        if isinstance(ind.get(section), dict):
            patch[section] = ind[section]
    if isinstance(ind.get("reactions"), dict):
        if available.get("reactions"):
            p = ind["reactions"]
            patch["reactions"] = {"build_reactions": bool(p.get("build_reactions")),
                                  "buy_categories": [str(c) for c in (p.get("buy_categories") or [])]}
        else:
            skipped.append("reactions — not enabled for this account")
    if isinstance(ind.get("components"), list):
        if available.get("components"):
            patch["components"] = {"items": [{"type_id": int(i["type_id"]),
                                              "rule": "build" if i.get("rule") == "build" else "buy"}
                                             for i in ind["components"]]}
        else:
            skipped.append("components — not enabled for this account")
    if isinstance(ind.get("job_length"), dict):
        if available.get("job_length"):
            patch["job_length"] = ind["job_length"]
        else:
            skipped.append("job length — not enabled for this account")
    if isinstance(ind.get("pins"), dict):
        if available.get("pins"):
            pins, lost = {}, 0
            for family, loc in ind["pins"].items():
                row = loc_to_row.get(int(loc)) if str(loc).lstrip("-").isdigit() else None
                if row is None:
                    lost += 1
                else:
                    pins[str(family)] = f"s:{row}"
            patch["pins"] = {"pins": pins}
            if lost:
                skipped.append(f"{lost} build pin(s) — the structure they name is not in this account")
        else:
            skipped.append("build pins — not enabled for this account")
    return patch, skipped


def _import_structures(context_id: int, doc: dict, replace: bool,
                       dry_run: bool) -> tuple[dict, dict[int, int], list[str]]:
    """Merge the file's structures into the account's list and report what happened.

    Matched on `location_id`, the same identity `adopt_market` uses: the same building described
    twice is two sets of rig answers that can disagree, which is a wrong efficiency the plan quotes
    and cannot see is wrong. Returns the location→row map the pins are then resolved against.

    **Only rows of kind `structure` are in scope, on BOTH sides.** `pp_markets` is one table holding
    two different things (`_ALLOWED_KINDS`): the structures this exports, and the REGION markets that
    are the account's pricing chain. A region row is not in the file, so a merge that considered it
    would find it unmentioned — and `replace_structures` would delete the pricing chain of anyone
    who backed their own config up and restored it. The checkbox says structures; it now means it.
    """
    from app.markets import ensure_markets_table, _SHAREABLE_COLS
    incoming, notes = _incoming_structures(context_id, doc)
    ensure_markets_table()
    con = get_connection()
    try:
        # Matched regardless of `active`, but only against structures. An inactive row at a location
        # in the file is that building, deactivated — updating it and leaving it inactive reported
        # "updated" for a structure that stayed invisible to every reader, so the update turns it
        # back on. Inserting a second row beside it would be the duplicate this all exists to avoid.
        have = {r["location_id"]: r["id"] for r in con.execute(
            "SELECT id, location_id FROM pp_markets "
            "WHERE owner_kind='account' AND owner_id=? AND kind='structure'",
            (context_id,)).fetchall()}
        report = {"added": 0, "updated": 0, "removed": 0, "unchanged": 0}
        loc_to_row = dict(have)
        if dry_run:
            for s in incoming:
                loc = int(s["location_id"])
                report["updated" if loc in have else "added"] += 1
                # A structure the merge WOULD create still has to resolve a pin, or the preview
                # reports pins as lost that the real import would land — the one number a user
                # would act on by not importing.
                loc_to_row.setdefault(loc, -1)
            if replace:
                report["removed"] = len({*have} - {int(s["location_id"]) for s in incoming})
            return report, loc_to_row, notes

        nxt = (con.execute("SELECT COALESCE(MAX(priority), -1) AS m FROM pp_markets "
                           "WHERE owner_kind='account' AND owner_id=?", (context_id,))
               .fetchone()["m"] + 1)
        for s in incoming:
            loc = int(s["location_id"])
            vals = [_column_value(c, s.get(c)) for c in _SHAREABLE_COLS]
            if loc in have:
                con.execute(f"UPDATE pp_markets SET active=1, "
                            f"{', '.join(c + '=?' for c in _SHAREABLE_COLS)} WHERE id=?",
                            (*vals, have[loc]))
                report["updated"] += 1
            else:
                con.execute(
                    f"INSERT INTO pp_markets (owner_kind, owner_id, priority, active, "
                    f"{', '.join(_SHAREABLE_COLS)}) "
                    f"VALUES (?,?,?,1,{','.join('?' * len(_SHAREABLE_COLS))})",
                    ("account", context_id, nxt, *vals))
                nxt += 1
                report["added"] += 1
                row_id = _inserted_id(con, context_id, loc)
                # Recorded in BOTH maps: a file naming one location twice would otherwise miss the
                # match on the second pass and insert a duplicate — the very state `_validate` now
                # refuses, kept correct here too so the two do not have to agree by luck.
                have[loc] = loc_to_row[loc] = row_id
        if replace:
            keep = {int(s["location_id"]) for s in incoming}
            for loc, row_id in list(have.items()):
                if loc not in keep:
                    con.execute("DELETE FROM pp_markets WHERE id=? AND owner_kind='account' "
                                "AND owner_id=? AND kind='structure'", (row_id, context_id))
                    loc_to_row.pop(loc, None)
                    report["removed"] += 1
        con.commit()
    finally:
        con.close()
    return report, loc_to_row, notes


def _incoming_structures(context_id: int, doc: dict) -> tuple[list[dict], list[str]]:
    """The file's structures, minus the ones this account may not create, with a note for each.

    A negative `location_id` is a HAND-DESCRIBED structure (`is_manual_location`) — a building the
    app has never seen over ESI. `add_manual_structure` gates those on `industry_manual_structures`
    and forces `price_from=0`, because a made-up building must never enter the pricing chain. A file
    is an easier way to reach this table than that endpoint, so it applies the same two rules rather
    than being the way around them."""
    from app.markets import is_manual_location, _manual_structures_on
    rows = [s for s in (doc.get("structures") or []) if isinstance(s, dict)
            and str(s.get("kind", "structure")) == "structure"]
    try:
        manual_ok = _manual_structures_on(context_id)
    except Exception:
        manual_ok = False
    out: list[dict] = []
    notes: list[str] = []
    dropped = 0
    forced = 0
    for s in rows:
        if is_manual_location(s.get("location_id")):
            if not manual_ok:
                dropped += 1
                continue
            if s.get("price_from"):
                s = {**s, "price_from": 0}
                forced += 1
        out.append(s)
    if dropped:
        notes.append(f"{dropped} hand-described structure(s) — describing a structure by hand is not "
                     f"enabled for this account")
    if forced:
        notes.append(f"{forced} hand-described structure(s) were imported as build-only — a building "
                     f"the app has never seen cannot price a market")
    return out, notes


# The `pp_markets` columns that are NOT NULL with a default, and what that default is. A file need
# not spell every column out — a hand-written one describing a structure in four fields is a
# perfectly reasonable thing to be handed — and omitting one of these used to reach the INSERT as a
# NULL and come back an IntegrityError. The column defaults only apply to a bare INSERT; this writer
# names every column, so it has to supply them itself.
_MARKET_DEFAULTS = {"kind": "structure", "price_from": 1, "build_mfg": 0, "build_rx": 0,
                    "me_rig": 0, "te_rig": 0, "rx_me_rig": 0, "rx_te_rig": 0}


def _column_value(column: str, value):
    """A `pp_markets` value as the column stores it. The four rig-family columns are JSON TEXT that
    `_list_markets` hands back parsed, so an export round-trip has to re-encode them — writing the
    Python list straight in is how a row comes back as the string "['capital']"."""
    if column.endswith("_rig_groups"):
        return json.dumps([str(k) for k in (value or [])])
    if value is None and column in _MARKET_DEFAULTS:
        return _MARKET_DEFAULTS[column]
    if column in ("price_from", "build_mfg", "build_rx", "me_rig", "te_rig", "rx_me_rig", "rx_te_rig"):
        # Booleans arrive from JSON as true/false and the columns are INTEGER; SQLite is relaxed
        # about that and Postgres is not.
        try:
            return int(value)
        except (TypeError, ValueError):
            return _MARKET_DEFAULTS[column]
    return value


def _inserted_id(con, context_id: int, location_id: int) -> int:
    """The row id just inserted. `lastrowid` is not portable across this app's two backends
    (`_PgCursor` answers None), so ask for the row that was written — newest first, because the
    merge guarantees only that this is the LAST structure written at that location."""
    row = con.execute("SELECT id FROM pp_markets WHERE owner_kind='account' AND owner_id=? "
                      "AND location_id=? AND kind='structure' ORDER BY id DESC",
                      (context_id, location_id)).fetchone()
    return int(row["id"]) if row else -1


def _import_reaction_settings(context_id: int, doc: dict, dry_run: bool) -> str | None:
    """The personal freight/tax override, written through the reactions module's own upsert.

    Nothing is checked here: `_validate_reaction_settings` already built and system-checked this
    same object before the first write of the import happened, which is the only order in which an
    unrecognised solar system does not 400 with the structures already committed."""
    rx = doc.get("reaction_settings")
    if not isinstance(rx, dict):
        return None
    from app.reactions.settings import (_upsert_settings, ensure_account_reaction_settings_table,
                                        _invalidate_reaction_settings_cache)
    req = _reaction_settings_update(rx)
    if dry_run:
        return "would be replaced"
    ensure_account_reaction_settings_table()
    _upsert_settings("pp_account_reaction_settings", "context_id", context_id, req)
    _invalidate_reaction_settings_cache(context_id)
    return "replaced"


def _import_sources(context_id: int, doc: dict, dry_run: bool) -> dict:
    """Tick the containers the file names, and restore the named sets over them.

    A key this account has never scanned is DROPPED and counted, never written: it would be a source
    the planner is told it may spend and cannot find, which surfaces later as a plan quietly missing
    stock. Sets are keyed by name, so re-importing updates rather than growing a second copy —
    `save_source_set`'s own rule."""
    src = doc.get("sources") or {}
    if not isinstance(src, dict):
        return {"enabled": 0, "unknown": 0, "sets": 0}
    try:
        from app.industry.assets import list_sources, set_sources, save_source_set
        known = {s["key"] for s in list_sources(context_id)}
    except Exception:
        return {"enabled": 0, "unknown": 0, "sets": 0}
    wanted = [str(k) for k in (src.get("enabled_keys") or [])]
    ok = [k for k in wanted if k in known]
    report = {"enabled": len(ok), "unknown": len(wanted) - len(ok), "sets": 0}
    sets = [s for s in (src.get("sets") or []) if isinstance(s, dict) and (s.get("name") or "").strip()]
    report["sets"] = len(sets)
    if dry_run:
        return report
    if ok:
        set_sources(context_id, ok, True)
    for s in sets:
        save_source_set(context_id, s["name"], [k for k in (s.get("keys") or []) if k in known])
    return report


def _import_placeholders(context_id: int, doc: dict, dry_run: bool) -> dict:
    """Recreate the declared-capacity characters, matched by NAME.

    By name because that is the only identity they have that survives leaving one account: their
    `character_id` is a synthetic negative allocated from this install's own sequence. Matching means
    importing twice does not double the roster — the same rule the structure merge follows, and the
    reason neither operation has to be done exactly once."""
    from app.esi_data import _clamp
    incoming = [p for p in (doc.get("placeholders") or [])
                if isinstance(p, dict) and str(p.get("name") or "").strip()]
    con = get_connection()
    try:
        have = {r["character_name"]: r["character_id"] for r in con.execute(
            "SELECT character_id, character_name FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=1", (context_id,)).fetchall()}
        report = {"added": 0, "updated": 0}
        if dry_run:
            for p in incoming:
                report["updated" if str(p["name"]).strip()[:60] in have else "added"] += 1
            return report
        nxt = min(0, con.execute("SELECT MIN(character_id) AS m FROM pp_characters")
                  .fetchone()["m"] or 0) - 1
        for p in incoming:
            name = str(p["name"]).strip()[:60]
            ipc = _clamp(p.get("max_planets"), 1, 6, 6) - 1
            ccu = _clamp(p.get("ccu"), 1, 5, 5)
            mfg = _clamp(p.get("mfg_slots"), 0, 11, 0)
            rx = _clamp(p.get("rx_slots"), 0, 11, 0)
            if name in have:
                con.execute("UPDATE pp_characters SET interplanetary_consolidation=?, "
                            "command_center_upgrades=?, dummy_mfg_slots=?, dummy_rx_slots=? "
                            "WHERE character_id=? AND context_id=?",
                            (ipc, ccu, mfg, rx, have[name], context_id))
                report["updated"] += 1
            else:
                con.execute(
                    "INSERT INTO pp_characters (character_id, character_name, "
                    "interplanetary_consolidation, command_center_upgrades, context_id, "
                    "dummy_mfg_slots, dummy_rx_slots, is_dummy) VALUES (?,?,?,?,?,?,?,1)",
                    (nxt, name, ipc, ccu, context_id, mfg, rx))
                nxt -= 1
                report["added"] += 1
        con.commit()
    finally:
        con.close()
    if not dry_run and (report["added"] or report["updated"]):
        try:
            from app.cache import cache_invalidate, charlist_key
            cache_invalidate(charlist_key(context_id))
        except Exception:
            pass
    return report


def import_config(context_id: int, req: ConfigImport) -> dict:
    """Validate the whole document, then apply it section by section.

    **The validation is up front because the writes cannot be one transaction.** They span four
    stores, each opening its own connection: structures commit, then the industry patch, then the
    reaction settings, then sources and placeholders. So anything a later writer would reject has to
    be caught before the first one runs, or a refused import leaves the account half-changed — which
    is worse than a refused one, because nothing tells the user which half took. `_validate` is
    where every such check lives, and `dry_run` runs exactly the same one.

    What that does NOT promise: an unexpected failure inside a writer (a dropped connection, a
    constraint nobody anticipated) can still stop the run partway. The answer to that is that every
    section is idempotent — re-importing the same file finishes the job rather than doubling it.

    Order matters exactly once: structures are written before the industry patch, because the pins in
    that patch are resolved against the structure rows the merge has just created."""
    problems = _validate(req.config)
    if problems:
        raise HTTPException(status_code=400, detail={"problems": problems})

    from app.industry.build_setup import BuildSetupPatch, apply_patch, _available as _sections_available
    available = _sections_available(context_id)
    doc = req.config

    structures, loc_to_row, skipped = _import_structures(
        context_id, doc, req.replace_structures, req.dry_run)
    patch_body, patch_skipped = _industry_patch(doc, available, loc_to_row)
    skipped += patch_skipped

    # `per_order_plans` is not a `BuildSetupPatch` section — it has its own endpoint because turning
    # it on re-prices every queued build. Written here directly for the same reason it is exported:
    # a config that restores every rule except how the queue is planned has not restored the config.
    per_order = (doc.get("industry") or {}).get("per_order_plans")
    per_order_written = None
    if isinstance(per_order, dict) and "enabled" in per_order:
        if available.get("per_order_plans"):
            per_order_written = bool(per_order["enabled"])
            if not req.dry_run:
                from app.industry.settings import set_per_order_plans
                set_per_order_plans(context_id, per_order_written)
        else:
            skipped.append("per-order plans — not enabled for this account")

    written: list[str] = []
    if patch_body:
        if req.dry_run:
            written = sorted(patch_body)
        else:
            written = apply_patch(context_id, BuildSetupPatch(**patch_body))

    rx = _import_reaction_settings(context_id, doc, req.dry_run)
    # Gated like every other section, and for the same reason: `apply_patch` refuses a `sources`
    # write when `industry_plan_sources` is off, and an import that wrote it anyway would be the one
    # way round a gate the rest of the app enforces.
    if available.get("sources"):
        sources = _import_sources(context_id, doc, req.dry_run)
    else:
        sources = {"enabled": 0, "unknown": 0, "sets": 0}
        if isinstance(doc.get("sources"), dict) and (doc["sources"].get("enabled_keys")
                                                     or doc["sources"].get("sets")):
            skipped.append("stock sources — not enabled for this account")
    placeholders = _import_placeholders(context_id, doc, req.dry_run)

    return {
        "dry_run": req.dry_run,
        "sections": written,
        "per_order_plans": per_order_written,
        "structures": structures,
        "reaction_settings": rx,
        "sources": sources,
        "placeholders": placeholders,
        "skipped": skipped,
    }


# ── Endpoints ─────────────────────────────────────────────────────────────────────────────────

@router.get("/api/config/export")
def api_export_config(ctx: int = Depends(require_context)):
    """This account's whole build configuration as one portable object.

    `discloses` travels with it so the download warning is built from the same list the exporter
    fills — a file that identifies you should say so from the code that made it identifying."""
    _require_available(ctx)
    return {"config": export_config(ctx), "discloses": EXPORT_DISCLOSES}


@router.post("/api/config/import")
def api_import_config(req: ConfigImport, ctx: int = Depends(require_context)):
    """Apply a configuration file. `dry_run` reports exactly what would change and writes nothing."""
    _require_available(ctx)
    return import_config(ctx, req)
