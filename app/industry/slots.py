"""Industry planner — per-character manufacturing (and reaction) slot pools.

The scheduler (schedule.py) fills two parallel slot pools. This module derives their sizes from
the account's characters' real ESI skills, the same shape reactions uses:

  manufacturing_slots = 1 base + 1/level Mass Production + 1/level Advanced Mass Production (≤11)
  reaction_slots      = 1 base + 1/level Mass Reactions   + 1/level Advanced Mass Reactions   (≤11)

Phase 3 (this): total pool = sum across the account's characters. A later slice subtracts
currently-running ESI manufacturing jobs (activity_id 1) to get *free* slots per character, the
same way app/reactions computes free reaction slots — see the TODO in _slot_pool. Reaction slots
are read straight off the character rows (already fetched for the Reactions tool) rather than
importing app.reactions, keeping this module free of a cross-package dependency.
"""
from fastapi import Depends

from app.sde import get_connection
from app.esi import require_context

from app.industry._router import router


def manufacturing_slots(row) -> int:
    """1 base + 1/level of Mass Production + 1/level of Advanced Mass Production, capped at the
    game's real max of 11 (5+5+1)."""
    return min(11, 1 + (row["mass_production"] or 0) + (row["advanced_mass_production"] or 0))


def reaction_slots(row) -> int:
    """1 base + 1/level of Mass Reactions + 1/level of Advanced Mass Reactions, capped at 11."""
    return min(11, 1 + (row["mass_reactions"] or 0) + (row["advanced_mass_reactions"] or 0))


SKILLS_SCOPE = "esi-skills.read_skills.v1"


def _eligibility(row) -> tuple[bool, bool, str]:
    """(usable for manufacturing, usable for reactions, why not).

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
    con = get_connection()
    try:
        chars = con.execute(
            "SELECT character_id, character_name, scopes, "
            "COALESCE(mass_production,0) AS mass_production, "
            "COALESCE(advanced_mass_production,0) AS advanced_mass_production, "
            "COALESCE(mass_reactions,0) AS mass_reactions, "
            "COALESCE(advanced_mass_reactions,0) AS advanced_mass_reactions "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0",
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
        mfg_ok, rx_ok, why = _eligibility(c)
        if not mfg_ok and not rx_ok:
            excluded.append({"character_id": c["character_id"],
                             "character_name": c["character_name"], "reason": why})
            continue
        ms = manufacturing_slots(c) if mfg_ok else 0
        rs = reaction_slots(c) if rx_ok else 0
        run = running.get(c["character_id"], {"manufacturing": 0, "reaction": 0})
        mf = max(0, ms - run["manufacturing"])
        rf = max(0, rs - run["reaction"])
        mfg_total += ms; rx_total += rs; mfg_free += mf; rx_free += rf
        per_char.append({
            "character_id": c["character_id"], "character_name": c["character_name"],
            "manufacturing_slots": ms, "reaction_slots": rs,
            "manufacturing_free": mf, "reaction_free": rf,
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
