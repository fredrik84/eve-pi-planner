"""Industry planner — per-character manufacturing (and reaction) slot pools.

The scheduler (schedule.py) fills two parallel slot pools. This module derives their sizes from
the account's characters' real ESI skills, the same shape reactions uses:

  manufacturing_slots = 1 base + 1/level Mass Production + 1/level Advanced Mass Production (≤11)
  reaction_slots      = 1 base + 1/level Mass Reactions   + 1/level Advanced Mass Reactions   (≤11)

Placeholder ("dummy") characters count too, behind `industry_placeholder_slots`: they have no ESI
skills to read, so they contribute the slot counts their owner DECLARED (dummy_mfg_slots /
dummy_rx_slots) and nothing else. That is the same promise as the skill path — capacity is never
invented, it is either measured or stated — reached from the other end.

Phase 3 (this): total pool = sum across the account's characters. A later slice subtracts
currently-running ESI manufacturing jobs (activity_id 1) to get *free* slots per character, the
same way app/reactions computes free reaction slots — see the TODO in _slot_pool. Reaction slots
are read straight off the character rows (already fetched for the Reactions tool) rather than
importing app.reactions, keeping this module free of a cross-package dependency.
"""
from fastapi import Depends

from app.sde import get_connection
from app.esi import require_context
from app.production import skill_slot_count

from app.industry._router import router


def manufacturing_slots(row) -> int:
    """1 base + 1/level of Mass Production + 1/level of Advanced Mass Production, capped at the
    game's real max of 11 (5+5+1)."""
    return skill_slot_count(row["mass_production"], row["advanced_mass_production"])


def reaction_slots(row) -> int:
    """1 base + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at 11."""
    return skill_slot_count(row["mass_reactions"], row["advanced_mass_reactions"])


SKILLS_SCOPE = "esi-skills.read_skills.v1"

# Placeholder ("dummy") characters declare their slots instead of having skills scanned — see
# app/esi.py's dummy_mfg_slots note for why they are their own columns and not implied skill levels.
PLACEHOLDER_FEATURE = "industry_placeholder_slots"


def _placeholders_on(context_id: int) -> bool:
    # Local import: app.features imports app.esi, and this module is imported by the industry
    # router. Role-aware so a rollout to testers actually reaches them.
    from app.features import feature_enabled_for
    return feature_enabled_for(PLACEHOLDER_FEATURE, context_id)


def declared_slots(row, key: str) -> int:
    """A placeholder's user-declared slot count for one pool, clamped to the game's real range.
    0 is meaningful and common — a placeholder that manufactures but never reacts, or vice versa."""
    return max(0, min(11, int(row[key] or 0)))


def _placeholder_eligibility(row) -> tuple[bool, bool, str]:
    """The placeholder mirror of `_eligibility`. Same promise — never invent capacity — enforced
    from the other end: a placeholder has no skills to read, so the only capacity it can contribute
    is what its owner explicitly typed in. Declaring 0 for a pool keeps it out of that pool exactly
    like an untrained multiplier skill does for a real character."""
    mfg = declared_slots(row, "dummy_mfg_slots")
    rx = declared_slots(row, "dummy_rx_slots")
    if not mfg and not rx:
        return False, False, "placeholder with no job slots declared — set them on the Characters tab"
    return mfg > 0, rx > 0, ""


def _eligibility(row) -> tuple[bool, bool, str]:
    """(usable for manufacturing, usable for reactions, why not) — REAL characters only.
    Placeholders go through `_placeholder_eligibility`; this path is untouched by them.

    Two filters, both automatic — no knob:

    * **No skills scope** → we have no skill data at all, so we'd be inventing capacity. This also
      catches a wallet-only character, which isn't an industry character in the first place.
    * **Never trained the pool's multiplier skill** → they have only the free base slot. Assigning
      capital work to a toon that has never trained Mass Production produces an instruction they
      can't act on, and it inflates the slot pool so every estimate comes out optimistic.

    Judged PER POOL, which matters: a character with Mass Production 0 but Mass Reactions 4 is a
    reaction pilot, and should keep their 5 reaction slots while staying out of manufacturing.
    """
    if SKILLS_SCOPE not in (row["scopes"] or "").split():
        return False, False, "no skill data — connect this character, or it's a wallet-only account"
    mfg_ok = (row["mass_production"] or 0) > 0 or (row["advanced_mass_production"] or 0) > 0
    rx_ok = (row["mass_reactions"] or 0) > 0 or (row["advanced_mass_reactions"] or 0) > 0
    if not mfg_ok and not rx_ok:
        return False, False, "no industry or reaction slot skills trained"
    return mfg_ok, rx_ok, ""


def _slot_pool(context_id: int) -> dict:
    """Per-character + total manufacturing and reaction slots for the account's real characters,
    plus how many are FREE right now (total − currently-running ESI jobs). Free counts fall back to
    total for characters with no cached jobs (nothing known to be running)."""
    placeholders_on = _placeholders_on(context_id)
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, scopes, COALESCE(is_dummy,0) AS is_dummy, "
            "COALESCE(mass_production,0) AS mass_production, "
            "COALESCE(advanced_mass_production,0) AS advanced_mass_production, "
            "COALESCE(mass_reactions,0) AS mass_reactions, "
            "COALESCE(advanced_mass_reactions,0) AS advanced_mass_reactions, "
            "COALESCE(dummy_mfg_slots,0) AS dummy_mfg_slots, "
            "COALESCE(dummy_rx_slots,0) AS dummy_rx_slots "
            "FROM pp_characters WHERE context_id=?" + ("" if placeholders_on else
                                                       " AND COALESCE(is_dummy,0)=0"),
            (context_id,),
        ).fetchall()
    finally:
        con.close()

    from app.industry.jobs import running_counts   # local import avoids a slots↔jobs cycle
    running = running_counts(context_id)

    per_char = []
    excluded = []
    mfg_total = rx_total = mfg_free = rx_free = 0
    for c in chars:
        is_ph = bool(c["is_dummy"])
        mfg_ok, rx_ok, why = _placeholder_eligibility(c) if is_ph else _eligibility(c)
        if not mfg_ok and not rx_ok:
            excluded.append({"character_id": c["character_id"],
                             "character_name": c["character_name"], "reason": why,
                             "is_placeholder": is_ph})
            continue
        if is_ph:
            ms = declared_slots(c, "dummy_mfg_slots") if mfg_ok else 0
            rs = declared_slots(c, "dummy_rx_slots") if rx_ok else 0
        else:
            ms = manufacturing_slots(c) if mfg_ok else 0
            rs = reaction_slots(c) if rx_ok else 0
        # A placeholder is never connected, so nothing of it can be running in ESI — running_counts
        # is keyed by real character ids and simply has no row for it.
        run = running.get(c["character_id"], {"manufacturing": 0, "reaction": 0})
        mf = max(0, ms - run["manufacturing"])
        rf = max(0, rs - run["reaction"])
        mfg_total += ms; rx_total += rs; mfg_free += mf; rx_free += rf
        per_char.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "manufacturing_slots": ms, "reaction_slots": rs,
            "manufacturing_free": mf, "reaction_free": rf,
            # Carried everywhere the pool is shown: a placeholder must never read as a connected
            # character, and its slots are a claim its owner made, not something we measured.
            "is_placeholder": is_ph,
        })
    return {
        "characters": per_char,
        "excluded": excluded,
        "manufacturing_slots": mfg_total, "reaction_slots": rx_total,
        "manufacturing_free": mfg_free, "reaction_free": rx_free,
    }


@router.get("/api/industry/slots")
def industry_slots(ctx: int = Depends(require_context)):
    """The account's manufacturing + reaction slot pool, per character and totalled — the parallel
    capacity the queue scheduler fills. Own-account scoped."""
    return _slot_pool(ctx)
