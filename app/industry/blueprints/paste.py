"""The industry window, pasted: the parser, and the batches it replaces."""
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

from app.industry.blueprints.esi import (
    _PASTE_BATCH_DEFAULT,
    _STACK_CAP,
    _batch_key,
    _blueprint_product_index,
)
from app.industry.blueprints.manual import (
    _FORMULA_SUFFIX,
    _record_unresolved,
    ensure_manual_blueprints_table,
    ensure_paste_unresolved_table,
)
# ── The industry window, pasted ───────────────────────────────────────────────────────────────
# Declaring prints one at a time is unusable at the scale a real builder has: ~100 reaction formulas
# on one character and more on the others. EVE's own industry window copies to the clipboard as
# exactly the data this table stores, so the whole library arrives in one paste:
#
#     [N x ]<name> TAB <ME> TAB <TE> TAB <runs> TAB <category>
#
# ...in the SHORT layout. The window copies in TWO layouts, depending on whether a container is
# selected in its tree — the long one names WHERE each print is:
#
#   SHORT (a container IS selected, so the window is already scoped and says nothing about where):
#     4 x Nanotransistors Reaction Formula<TAB>0<TAB>0<TAB>-1<TAB>Composite
#
#   LONG (nothing selected — every row carries its structure and its container):
#     [N x ]<name> TAB <ME> TAB <TE> TAB <runs> TAB <?> TAB <structure> TAB <container> TAB <category>
#     Warp Core Stabilizer I Blueprint<TAB>10<TAB>20<TAB>-1<TAB>0<TAB>MTO2-2 - Ctrl C<TAB>Santo BPO<TAB>Warp Core Stabilizer
#
# Both must parse, and mixed in one paste. See `_split_location` for how the location columns are
# found and why they are counted from the END of the line.
#
# Nothing about the format needs translating — `runs = -1` is already this module's encoding for an
# original, and the ME/TE columns are the numbers a corp-hangar print could otherwise not state.
#
# **Each paste is a NAMED BATCH, exactly like a pasted stock source** (`add_pasted_source` in
# assets.py), and for the same reason: the user pastes ONCE PER CHARACTER. A paste that replaced the
# whole library would wipe the previous character's prints; one that appended would double the
# holding the moment the same window is pasted again after buying a print. Replacing only its own
# batch is the rule that survives both.
#
# **ONE PASTE IS ONE BATCH, and its identity is its NAME — never where its prints are.** The long
# layout is read for what it says (structure and container land on every row, and suggest the batch's
# default name), but a batch is not a place. Keying a batch on its container was tried and reverted:
# prints MOVE between containers, so a window re-pasted after a move replaced the new container and
# left the old container's batch standing — the same five formulas counted twice. That fails in the
# dangerous direction, because an over-counted print cap lets the planner schedule parallel jobs off
# prints the user does not have. A re-paste under the same name replaces EVERYTHING that batch last
# declared, whatever containers it named this time, which is what makes a move track correctly.

_STACK_RE = _re.compile(r"^(\d[\d,]*)\s*[x×]\s+(.+)$", _re.IGNORECASE)



def _batch_label(structure: str, container: str) -> str:
    """How a LOCATION reads for display, and the default name offered for a batch found in one.

    Qualified by the structure, because a container name alone ("Santo BPO") is not unique across
    an account's structures and two batches reading identically in the list is indistinguishable
    from a bug. A label, never a key — see `_batch_key`.
    """
    structure, container = (structure or "").strip(), (container or "").strip()
    if container and structure:
        return f"{container} — {structure}"
    return container or structure


def _default_batch_name(locations: list[dict]) -> str:
    """The batch name to offer when the user typed none, derived from where the paste says its
    prints are. **A default, not a dependency** — it saves typing, and the moment it is written to a
    row it is just a name like any other.

    Stable for the same window, which is what makes an un-named re-paste land back on the same
    batch: one container gives its qualified label, several containers in one structure give the
    structure (so re-shuffling prints between cans inside a structure keeps the name), and several
    structures give the first structure alphabetically plus a count. A paste with no location at all
    gets the generic default, exactly as before.

    The honest caveat: a builder who never names their batches and then moves prints to a DIFFERENT
    STRUCTURE gets a different default, and so a second batch. Naming the batch once removes the
    ambiguity for good, which is why the UI offers the default in the name box rather than hiding
    it — a name the user can see is a name they can keep.
    """
    if not locations:
        return _PASTE_BATCH_DEFAULT
    if len(locations) == 1:
        return _batch_label(locations[0]["structure"], locations[0]["container"]) \
            or _PASTE_BATCH_DEFAULT
    structs = sorted({(l["structure"] or "").strip() for l in locations if (l["structure"] or "")})
    if len(structs) == 1:
        return structs[0]
    if not structs:
        return _PASTE_BATCH_DEFAULT
    return f"{structs[0]} +{len(structs) - 1} more"


def _is_number(s: str) -> bool:
    try:
        float((s or "").replace(",", "").replace(" ", ""))
        return True
    except ValueError:
        return False


def _split_location(parts: list[str]) -> tuple[str, str]:
    """(structure, container) for one industry-window row — `('', '')` when the row carries none.

    **Counted from the END of the line, because this column layout is INFERRED, not documented.**
    All we have is two real copies out of the client (see the header above): a short one ending
    `… runs TAB category`, and a long one with `? TAB structure TAB container` wedged in before the
    same trailing category. The one thing both samples agree on is that the CATEGORY IS LAST, so
    that is the only thing worth anchoring to: `[-3]` is the structure and `[-2]` the container.
    Read that way, a column being added, a column being dropped, or the unknown `0` at index 4
    moving all leave the location intact — absolute indices would silently start reading a
    different field. Nothing here depends on that `0`; we do not know what it means.

    Fewer than 7 fields is the short layout, which has no location in it at all. (The long sample
    has 8; 7 is the floor at which `[-3]`/`[-2]` can still be past the four leading columns this
    module does understand.)

    **Guard:** both fields must be non-empty and neither may be a bare number. If the layout ever
    changes such that this rule lands on the wrong columns, what it lands on is overwhelmingly
    likely to be one of the numeric columns — and refusing then degrades to "ask the user where
    these prints are", which is the honest answer. Inventing a structure called `0` is not.
    """
    while len(parts) > 1 and parts[-1] == "":
        parts = parts[:-1]                  # a trailing tab must not shift what "last" means
    if len(parts) < 7:
        return "", ""
    structure, container = parts[-3].strip(), parts[-2].strip()
    if not structure or not container:
        return "", ""
    if _is_number(structure) or _is_number(container):
        return "", ""
    return structure, container


def _parse_paste_line(line: str) -> dict | None:
    """One industry-window row → `{name, me, te, runs, quantity, structure, container}`, or None if
    it is not one. `structure`/`container` are `''` for a short-layout row (see `_split_location`).

    None covers the section headers (`Formulas:`, `Blueprints:`) and anything else pasted along with
    the window: they are counted and reported, never guessed at. A real row is tab-separated and its
    second column is the ME number, which is the cheapest test that a header can never pass.
    """
    parts = [p.strip() for p in line.split("\t")]
    if len(parts) < 2 or not parts[0]:
        return None

    def _num(idx: int, dflt: int) -> int | None:
        if idx >= len(parts) or parts[idx] == "":
            return dflt
        try:
            return int(float(parts[idx].replace(",", "").replace(" ", "")))
        except ValueError:
            return None

    me = _num(1, None)
    if me is None:
        return None                      # column 2 is not a number — not an industry-window row
    te, runs = _num(2, 0), _num(3, -1)
    if te is None or runs is None:
        return None
    name, qty = parts[0], 1
    m = _STACK_RE.match(name)
    if m:
        # "4 x Nanotransistors Reaction Formula" — a STACK of four separate physical prints. Names
        # that merely start with a digit ("1MN Afterburner I Blueprint") do not match: the x and the
        # space after it are required.
        try:
            qty = max(1, int(m.group(1).replace(",", "")))
        except ValueError:
            qty = 1
        name = m.group(2).strip()
    structure, container = _split_location(parts)
    return {"name": name, "me": me, "te": te, "runs": runs, "quantity": qty,
            "structure": structure, "container": container}


def parse_blueprint_paste(text: str) -> dict:
    """A copied EVE industry window → the rows `pp_industry_blueprints` stores, plus what was not
    understood. Pure: reads the SDE, writes nothing, so the preview and the import see one answer.

    Grouping: **a repeated line is a separate physical print.** The window lists one line per item,
    so four identical `Photonic Metamaterials Reaction Formula` lines are four formulas, and the
    stack prefix multiplies each. Quantities are summed per (product, ME, TE, runs) — prints that
    differ in research are genuinely different prints and stay separate rows.

    Names resolve through the SDE `types` table and then `_blueprint_product_index()`, the same index
    the ESI reader uses, so a declared print files under the product a plan actually asks about.
    Anything unresolved is REPORTED: a paste that matched nothing is almost always the wrong window,
    and dropping it silently would leave the user staring at an unchanged list.
    """
    parsed: list[dict] = []
    ignored: list[str] = []
    for raw in (text or "").splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        row = _parse_paste_line(line)
        if row is None:
            ignored.append(line.strip())
            continue
        parsed.append(row)

    empty = {"entries": [], "unknown": [], "no_product": [], "ignored": ignored, "locations": [],
             "suggested_name": _PASTE_BATCH_DEFAULT,
             "prints": 0, "formulas": 0, "blueprints": 0, "products": 0, "lines": len(parsed)}
    if not parsed:
        return empty

    con = get_connection()
    try:
        lookup: dict[str, int] = {}
        names = sorted({r["name"] for r in parsed})
        for i in range(0, len(names), 400):
            chunk = names[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for r in con.execute(
                f"SELECT type_id, name FROM types WHERE LOWER(name) IN ({marks})",
                tuple(n.lower() for n in chunk),
            ).fetchall():
                lookup[r["name"].lower()] = int(r["type_id"])

        # ── Fallback: a formula named after its PRODUCT ──────────────────────────────────────
        # Real case, 2026-08-07: a client copy carried "Fullerides Reaction Formula" while this SDE
        # calls the item "Fulleride Reaction Formula" — singular. CCP renames things and our SDE
        # snapshot lags it, so an exact-name miss is NOT proof the print does not exist, and telling
        # a user who pasted 238 real formulas that one of them isn't a thing is the wrong answer.
        #
        # When a name ends in "Reaction Formula" the stem is the PRODUCT's name, and a product
        # identifies its reaction uniquely (`reactions.output_type_id`). So: strip, look the stem up
        # as a product, take the reaction that makes it.
        #
        # Deliberately narrow. It runs ONLY after an exact match has failed, and only resolves when
        # the stem is a real type that some reaction actually outputs — so it can neither override a
        # good match nor invent a print. 78 of 111 formulas in this SDE are exactly
        # "<product> Reaction Formula" and never reach here; the 33 that differ (the "Pure …"
        # boosters) match on their real name and never reach here either.
        missing_names = [n for n in names if n.lower() not in lookup]
        stems: dict[str, str] = {}          # product-name (lower) -> the name as pasted
        for n in missing_names:
            low = n.lower()
            if low.endswith(_FORMULA_SUFFIX):
                stem = low[:-len(_FORMULA_SUFFIX)].strip()
                if stem:
                    stems.setdefault(stem, n)
        if stems:
            out_to_reaction: dict[int, int] = {}
            try:
                for r in con.execute("SELECT reaction_id, output_type_id FROM reactions"):
                    out_to_reaction.setdefault(int(r["output_type_id"]), int(r["reaction_id"]))
            except Exception:
                out_to_reaction = {}        # manufacturing-only SDE: nothing to fall back to
            keys = sorted(stems)
            for i in range(0, len(keys), 400):
                chunk = keys[i:i + 400]
                marks = ",".join("?" * len(chunk))
                for r in con.execute(
                    f"SELECT type_id, name FROM types WHERE LOWER(name) IN ({marks})",
                    tuple(chunk),
                ).fetchall():
                    reaction_id = out_to_reaction.get(int(r["type_id"]))
                    pasted = stems.get(r["name"].lower())
                    if reaction_id and pasted:
                        lookup[pasted.lower()] = reaction_id

        bp2prod = _blueprint_product_index(con)
        try:
            formula_ids = {int(r["reaction_id"])
                           for r in con.execute("SELECT reaction_id FROM reactions")}
        except Exception:
            formula_ids = set()          # a manufacturing-only SDE knows no formulas; cosmetic only
    finally:
        con.close()

    groups: dict[tuple, dict] = {}
    unknown: list[str] = []
    no_product: list[str] = []
    for r in parsed:
        tid = lookup.get(r["name"].lower())
        if tid is None:
            if r["name"] not in unknown:
                unknown.append(r["name"])
            continue
        prod = bp2prod.get(tid)
        if prod is None:
            # A real item that this SDE cannot turn into a product — a blueprint for something we
            # do not know how to build. Filing it under itself would invent a product no plan asks
            # for, so it is reported instead.
            if r["name"] not in no_product:
                no_product.append(r["name"])
            continue
        me = max(0, min(10, int(r["me"])))
        te = max(0, min(20, int(r["te"])))
        runs = -1 if int(r["runs"]) < 0 else int(r["runs"])
        # The LOCATION is part of the grouping key so the same print in two containers stays two
        # rows and each one keeps the place it was found in — the columns exist to be RECORDED.
        # It changes no total: a holding sums every row of a product (`manual_blueprints`), so two
        # rows of 2 and 3 and one row of 5 are the same five prints. Short-layout rows all carry
        # ('', '') and so group exactly as they always did.
        struct, cont = r.get("structure") or "", r.get("container") or ""
        key = (int(prod), me, te, runs, struct, cont)
        g = groups.setdefault(key, {
            "product_type_id": int(prod), "name": r["name"], "me": me, "te": te, "runs": runs,
            "quantity": 0, "kind": "bpo" if runs < 0 else "bpc",
            "formula": tid in formula_ids, "structure": struct, "container": cont,
        })
        g["quantity"] += int(r["quantity"])

    entries = list(groups.values())
    for e in entries:
        e["quantity"] = min(e["quantity"], _STACK_CAP)
    # Every distinct place this paste named — reported so the preview can say where the prints are
    # and so a default batch name can be offered. It is a DESCRIPTION of the paste; the import files
    # all of it under one batch whatever this says.
    locs: dict[tuple, dict] = {}
    for e in entries:
        if not (e["structure"] or e["container"]):
            continue
        lk = (e["structure"], e["container"])
        loc = locs.setdefault(lk, {"structure": e["structure"], "container": e["container"],
                                   "name": _batch_label(e["structure"], e["container"]),
                                   "prints": 0, "products": set()})
        loc["prints"] += e["quantity"]
        loc["products"].add(e["product_type_id"])
    locations = [{**v, "products": len(v["products"])}
                 for v in sorted(locs.values(), key=lambda x: x["name"].lower())]
    return {"entries": entries, "unknown": unknown, "no_product": no_product, "ignored": ignored,
            "locations": locations, "suggested_name": _default_batch_name(locations),
            "prints": sum(e["quantity"] for e in entries),
            "formulas": sum(e["quantity"] for e in entries if e["formula"]),
            "blueprints": sum(e["quantity"] for e in entries if not e["formula"]),
            "products": len({e["product_type_id"] for e in entries}),
            "lines": len(parsed)}


def replace_blueprint_batch(context_id: int, name: str, text: str,
                            structure: str = "", container: str = "") -> dict:
    """Import one pasted industry window as ONE batch, REPLACING that whole batch and nothing else.

    **A batch is its NAME.** Every row the paste yields is filed under `_batch_key(name)`, and the
    import deletes everything that key held first — regardless of which containers the previous
    paste named or this one does. That is precisely what makes a MOVE track: paste five formulas in
    "Santo BPO", move them into "New Can" in game, re-paste the same window under the same name, and
    the holding is still five. Keying per container (tried, reverted) left the old container's batch
    standing and made it ten, which is the dangerous direction — an over-counted print cap lets the
    planner run parallel jobs off prints that do not exist.

    Where the prints are is still read and STORED per row: the long layout's own structure/container
    when the row carried them, otherwise the `structure`/`container` the UI asked for. Display and
    future use only. The one thing location does decide is the DEFAULT NAME when the user typed none
    (`_default_batch_name`), which saves typing without becoming an identity.

    Re-pasting the same window after buying a print updates it; pasting a second character's window
    under its own name adds to the library beside the first. Rows typed in on the form carry an empty
    batch and are never touched by any paste.
    """
    ensure_manual_blueprints_table()
    ensure_paste_unresolved_table()
    res = parse_blueprint_paste(text)
    ask_struct, ask_cont = (structure or "").strip(), (container or "").strip()
    label = (name or "").strip() or res.get("suggested_name") or _PASTE_BATCH_DEFAULT
    if not (name or "").strip() and not res.get("locations") and (ask_struct or ask_cont):
        # Nothing typed and the paste named no place of its own — the place the user picked in the
        # "Where are these?" box is the most useful name we can offer them.
        label = _batch_label(ask_struct, ask_cont) or _PASTE_BATCH_DEFAULT
    key = _batch_key(label)
    if not res["entries"]:
        res.update({"added": 0, "batch": key, "name": label,
                    "error": "unrecognized" if (res["unknown"] or res["no_product"]) else "empty"})
        return res

    con = get_connection()
    try:
        # The BPO-vs-BPC choice is a property of the PRODUCT (see `edit_manual_blueprint`), so a
        # paste must carry forward one the user already made rather than blanking it.
        prefer = {int(r["type_id"]): str(r["prefer"] or "") for r in con.execute(
            "SELECT type_id, prefer FROM pp_industry_blueprints WHERE context_id=? AND prefer<>''",
            (context_id,)).fetchall()}
        # The batch replaces itself WHOLE — one DELETE by key, before a single row is written. Not
        # per container: a container this window no longer mentions is a container the user emptied,
        # and leaving its rows behind is exactly the double-count this replaced.
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, key))
        nxt = int(con.execute("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM pp_industry_blueprints "
                              "WHERE context_id=?", (context_id,)).fetchone()["n"])
        now = _time.time()
        for e in res["entries"]:
            # The row's own place if the window stated one, else the place the user was asked for.
            e_struct = e.get("structure") or ""
            e_cont = e.get("container") or ""
            if not (e_struct or e_cont):
                e_struct, e_cont = ask_struct, ask_cont
            con.execute(
                "INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                "quantity, prefer, updated_at, batch, batch_name, structure, container) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (context_id, nxt, e["product_type_id"], e["me"], e["te"], e["runs"],
                 e["quantity"], prefer.get(e["product_type_id"], ""), now,
                 key, label, e_struct, e_cont))
            nxt += 1
        # What this window named and we could not resolve. Kept, not just reported once — see
        # `ensure_paste_unresolved_table`.
        _record_unresolved(con, context_id, key, label,
                           list(res["unknown"]) + list(res["no_product"]))
        con.commit()
    finally:
        con.close()
    res.update({"added": res["prints"], "batch": key, "name": label})
    return res


def list_blueprint_batches(context_id: int) -> list[dict]:
    """The pasted batches this account holds — one per pasted window, in practice.

    Grouped on the batch KEY only. The location columns are summarised, never grouped on: one batch
    routinely spans several containers (a window copied with nothing selected in the tree names all
    of them), and grouping by place here would report one paste as several batches sharing a key —
    which is how the ✕ and the print counts would start disagreeing with what a re-paste replaces.
    `places` is that span; `structure`/`container` are filled in only when there is exactly one, so
    a caller can display a location without having to guess whether it is representative. The span
    is folded in PYTHON from a second small query rather than as a `COUNT(DISTINCT a || sep || b)`,
    which would need a separator literal that behaves identically on SQLite and Postgres and could
    still be a real character in a container name.
    """
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        rows = con.execute(
            "SELECT COALESCE(batch,'') AS batch, COALESCE(batch_name,'') AS batch_name, "
            "COUNT(*) AS rows_n, SUM(quantity) AS prints, COUNT(DISTINCT type_id) AS products "
            "FROM pp_industry_blueprints WHERE context_id=? AND COALESCE(batch,'')<>'' "
            "GROUP BY COALESCE(batch,''), COALESCE(batch_name,'') ORDER BY batch_name",
            (context_id,)).fetchall()
        places: dict[str, set] = {}
        for r in con.execute(
            "SELECT DISTINCT COALESCE(batch,'') AS batch, COALESCE(structure,'') AS structure, "
            "COALESCE(container,'') AS container FROM pp_industry_blueprints "
            "WHERE context_id=? AND COALESCE(batch,'')<>''", (context_id,)).fetchall():
            if r["structure"] or r["container"]:
                # ('', '') is "this row claims no place", which is not a place — counting it would
                # report a mixed batch as one place more than it actually names.
                places.setdefault(r["batch"], set()).add((r["structure"], r["container"]))
    except Exception:
        return []
    finally:
        con.close()
    out = []
    for r in rows:
        seen = places.get(r["batch"], set())
        only = next(iter(seen)) if len(seen) == 1 else ("", "")
        out.append({"batch": r["batch"], "name": r["batch_name"] or _PASTE_BATCH_DEFAULT,
                    "structure": only[0], "container": only[1], "places": len(seen),
                    "rows": int(r["rows_n"] or 0), "prints": int(r["prints"] or 0),
                    "products": int(r["products"] or 0)})
    return out


def delete_blueprint_batch(context_id: int, batch: str) -> None:
    """Drop one pasted batch. Every other batch, and everything typed in by hand, stays."""
    ensure_manual_blueprints_table()
    if not (batch or "").strip():
        return                            # '' is the hand-typed rows — never deletable in bulk
    ensure_paste_unresolved_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=? AND batch=?",
                    (context_id, batch))
        # Its unresolved names go with it: they described THAT paste, and a warning about a batch
        # the user has deleted is a warning they cannot act on.
        con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=? AND batch=?",
                    (context_id, batch))
        con.commit()
    finally:
        con.close()
