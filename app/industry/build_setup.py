"""One read/write surface for everything that shapes a build.

**This owns no storage.** Every field here still lives in the store that already owned it —
`pp_industry_settings` (and its own columns for the blacklist, the reaction policy, the pins and the
job-length ceiling), `pp_source_sets`, and the order row. What was missing was never a schema; it
was a *place to look*. Six separate write paths grew because the settings PUT is a debounced save of
the whole plan form, so anything that could be changed from elsewhere on the page had to escape it
(see `set_reaction_policy`) — and the result was eight inline strips on the plan card, none of which
read as configuration. A user who wrote this app could not find the reaction control.

So: one GET that answers "what is in force, and where did it come from", one POST that writes a
SPARSE patch back to whichever store owns each section. Deliberately NOT a new settings table — see
TODO 18 Half A, whose open question about a keyed config blob this is designed to leave untouched.
Consolidating the surface and consolidating the schema are different jobs and only one of them has
been shown to be worth doing.

`order_id` is an orthogonal parameter, not a mode switch (CLAUDE.md rule 4): the SECTIONS are
identical either way, and it only decides which store a section is read from and written to. An
account read answers "my standing rules"; an order read answers the same question plus "…and which
of them this build has overridden".
"""
from __future__ import annotations

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.esi import require_context

from app.industry._router import router

# Every section this surface speaks for, and the feature that gates each. `None` = always available
# (it predates the ladder or has no flag of its own). The UI renders exactly the sections it is told
# are available, so a flag that is off costs a section rather than showing a control that 403s.
_SECTION_FEATURES: dict[str, str | None] = {
    "facility": None,
    "threshold": None,
    "margin": None,
    "reactions": "industry_reaction_policy",
    "components": "industry_blacklist",
    "job_length": "industry_job_length_policy",
    "pins": None,              # rides on the routing flag — see settings._pins_available
    "sources": "industry_plan_sources",
}


def _available(context_id: int) -> dict[str, bool]:
    """Which sections this account may see. Asked once here so the modal and every write agree —
    a control the UI offers and the POST refuses is the failure this whole module exists to end."""
    from app.features import feature_enabled_for
    from app.industry.settings import _pins_available
    out: dict[str, bool] = {}
    for section, key in _SECTION_FEATURES.items():
        if section == "pins":
            out[section] = _pins_available(context_id)
        else:
            out[section] = True if key is None else feature_enabled_for(key, context_id)
    return out


def account_setup(context_id: int) -> dict:
    """The account's standing rules, section by section, with the pick-lists they are expressed in.

    Reuses each store's own reader rather than re-querying: `_policy_payload` already knows that a
    category registry travels with the policy, `_pins_payload` already knows its families, and
    duplicating either here is how the modal and the old strip would come to disagree.
    """
    from app.industry.settings import (get_settings, get_max_reaction_job_days,
                                       _policy_payload, _pins_payload)
    s = get_settings(context_id) or {}
    policy = _policy_payload(context_id)
    pins = _pins_payload(context_id)
    return {
        "facility": {"facility_id": s.get("facility_id") or "",
                     "struct_material_pct": s.get("struct_material_pct"),
                     "struct_time_pct": s.get("struct_time_pct")},
        # `force_build` sits with the threshold because it IS the threshold's off switch — it drops
        # the percentage and the absolute floor together, which the slider alone cannot express.
        "threshold": {"marginal_pct": s.get("marginal_pct"),
                      "force_build": bool(s.get("force_build")),
                      "prioritize_speed": bool(s.get("prioritize_speed"))},
        "margin": {"margin_pct": s.get("margin_pct")},
        "reactions": {"policy": policy["policy"], "categories": policy["categories"],
                      # Still on the code default rather than a choice — the one thing that made
                      # composites get bought on accounts that never opened this control.
                      "defaulted": policy["defaulted"]},
        "components": _components_section(context_id),
        "job_length": {"max_reaction_job_days": get_max_reaction_job_days(context_id)},
        "pins": {"pins": pins["pins"], "families": pins["families"]},
        "sources": _sources_section(context_id),
    }


def _components_section(context_id: int) -> dict:
    """The per-component overrules as ONE list, each row carrying which rule it is under.

    They were two lists — "never build" and (per order only) "build anyway" — and that is one list
    wearing two hats: a component is always-buy, always-build, or left to the cost engine, and it
    cannot be two of those. Two lists let a type appear in both and made the user reconcile a
    contradiction the resolver would silently break a tie on. One row per type with a `rule` makes
    the exclusion visible and makes "change my mind about this component" one click instead of a
    delete in one list and an add in another.

    Names come along because a list of type ids is not something anyone can read.
    """
    from app.industry.settings import get_blacklist, get_always_build
    never, always = get_blacklist(context_id), get_always_build(context_id)
    ids = sorted(set(never) | set(always))
    names = _type_names(ids)
    return {"items": [{"type_id": t, "name": names.get(t, str(t)),
                       # `always` wins a collision, matching the resolver's own precedence — the
                       # store keeps them disjoint, so this only matters for a row written before
                       # `set_component_rules` became the only writer.
                       "rule": "build" if t in set(always) else "buy"} for t in ids]}


def _type_names(ids: list[int]) -> dict[int, str]:
    if not ids:
        return {}
    from app.db import get_connection
    con = get_connection()
    try:
        rows = con.execute(
            f"SELECT type_id, name FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
            tuple(ids)).fetchall()
        return {r["type_id"]: r["name"] for r in rows}
    finally:
        con.close()


def _sources_section(context_id: int) -> dict:
    """Which boxes a build may spend, plus the saved sets. Tolerant by design: an account with no
    asset scan has no sources yet, and that is a normal empty state rather than an error."""
    try:
        from app.industry.assets import list_sources, list_source_sets
        return {"sources": list_sources(context_id), "sets": list_source_sets(context_id)}
    except Exception:
        return {"sources": [], "sets": []}


# Order-level overrides. A short list on purpose: these are the decisions worth arguing with per
# build (CLAUDE.md's "open a knob only where the judgement is genuinely the user's"), and every one
# of them already existed on the order row — this only gives them a home beside the defaults they
# override, so the two can be read together instead of on two different screens.
_ORDER_FIELDS = ("force_build_ids", "me_te_overrides", "margin_pct", "build_reactions",
                 "source_keys")


def order_overrides(context_id: int, order_id: int) -> dict:
    """This order's overrides, and WHICH of them are actually set.

    `overridden` is the load-bearing half: without it the modal cannot tell "the user chose 10%"
    from "10% is what the account default happens to be", and an inherited value shown as a choice
    is how an override gets made by accident.
    """
    from app.db import get_connection
    from app.industry.orders import _order_row
    con = get_connection()
    try:
        o = _order_row(con, order_id, context_id)      # 404s on another account's order
    finally:
        con.close()
    values, overridden = {}, []
    for f in _ORDER_FIELDS:
        v = o.get(f)
        values[f] = v
        # An empty list/map is "nothing overridden here", not an override to an empty value — the
        # order row stores '' / '[]' for both, so absence is the only honest reading.
        if v not in (None, "", [], {}, False):
            overridden.append(f)
    return {"order_id": order_id, "label": o.get("label") or "",
            "product_type_id": o.get("product_type_id"), "quantity": o.get("quantity"),
            "values": values, "overridden": overridden}


class BuildSetupPatch(BaseModel):
    """A SPARSE patch: only the sections present are written. Sending the whole object back would
    re-save every section on every edit, which is exactly the clobbering that pushed the reaction
    policy, the blacklist and the pins out into write paths of their own in the first place."""
    facility: dict | None = None
    threshold: dict | None = None
    margin: dict | None = None
    reactions: dict | None = None
    components: dict | None = None
    job_length: dict | None = None
    pins: dict | None = None
    sources: dict | None = None


def apply_patch(context_id: int, patch: BuildSetupPatch) -> list[str]:
    """Write each present section through the setter that already owns it. Returns the sections
    written, so the caller can report what changed rather than implying everything did.

    A section the account may not see is REFUSED, not ignored: silently dropping a write is how a
    user comes to believe a setting took effect when it did not.
    """
    from app.industry.settings import (set_component_rules, set_reaction_policy,
                                       get_reaction_policy, set_max_reaction_job_days,
                                       set_build_pins, save_settings)
    avail = _available(context_id)
    sent = {k: v for k, v in patch.model_dump(exclude_unset=True).items() if v is not None}
    denied = [k for k in sent if not avail.get(k, False)]
    if denied:
        raise HTTPException(status_code=403,
                            detail=f"not enabled for this account: {', '.join(sorted(denied))}")

    written: list[str] = []
    # The three that live on the settings row go in ONE save — they are the fields the plan form
    # already debounced together, so writing them separately would just reintroduce the race.
    form = {}
    for section, fields in (("facility", ("facility_id", "struct_material_pct", "struct_time_pct")),
                            ("threshold", ("marginal_pct", "force_build", "prioritize_speed")),
                            ("margin", ("margin_pct",))):
        if section in sent:
            form.update({f: sent[section][f] for f in fields if f in sent[section]})
            written.append(section)
    if form:
        save_settings(context_id, form)

    if "reactions" in sent:
        cur = get_reaction_policy(context_id)
        p = sent["reactions"]
        set_reaction_policy(context_id,
                            p.get("build_reactions", cur["build_reactions"]),
                            p.get("buy_categories", cur["buy_categories"]))
        written.append("reactions")
    if "components" in sent:
        # Both lists in ONE write — see set_component_rules. Splitting them here would recreate
        # exactly the window where a type can be in both.
        items = sent["components"].get("items") or []
        set_component_rules(context_id,
                            [int(i["type_id"]) for i in items if i.get("rule") != "build"],
                            [int(i["type_id"]) for i in items if i.get("rule") == "build"])
        written.append("components")
    if "job_length" in sent:
        set_max_reaction_job_days(context_id, sent["job_length"].get("max_reaction_job_days"))
        written.append("job_length")
    if "pins" in sent:
        set_build_pins(context_id, sent["pins"].get("pins") or {})
        written.append("pins")
    if "sources" in sent:
        from app.industry.assets import set_sources
        s = sent["sources"]
        set_sources(context_id, [str(k) for k in (s.get("keys") or [])], bool(s.get("enabled", True)))
        written.append("sources")
    return written


@router.get("/api/industry/build-setup")
def read_build_setup(order_id: int | None = None, ctx: int = Depends(require_context)):
    """Everything that shapes a build, in one read. With `order_id`, also what that order overrides."""
    out = {"account": account_setup(ctx), "available": _available(ctx), "order": None}
    if order_id is not None:
        out["order"] = order_overrides(ctx, order_id)
    return out


@router.post("/api/industry/build-setup")
def write_build_setup(patch: BuildSetupPatch, ctx: int = Depends(require_context)):
    """Apply a sparse patch to the account's standing rules and read the result back, so the caller
    never has to guess what the store made of what it sent."""
    written = apply_patch(ctx, patch)
    return {"written": written, "account": account_setup(ctx), "available": _available(ctx)}
