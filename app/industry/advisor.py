"""Industry skill advisor — what to train next to raise actual output.

The planner tells you what a build costs and how long it takes. This answers the other question:
given the characters you have, which single skill level buys the most throughput, and what does it
cost in SP?

**One comparable metric.** Slots and job-time look unrelated, but both resolve to the same thing —
percentage more jobs finished per day on a given pool — so they can be ranked against each other
honestly:

  * `+1 slot` on a pool of N slots is worth `+1/N` throughput, but ONLY while the pool is
    saturated. A slot you don't fill produces nothing, which is why an idle pool suppresses the
    suggestion entirely instead of quietly ranking it low.
  * `Industry` cuts manufacturing job time 4%/level, `Advanced Industry` 3%/level on manufacturing
    AND reactions. Going L → L+1 multiplies throughput by `(1-0.04L)/(1-0.04(L+1))`, which is a
    real gain regardless of utilisation.

Ranking is by gain per SP, so a rank-2 skill that adds 8% beats a rank-8 skill that adds 10% — the
question is what to train NEXT, and cheap-and-good wins that.

**It suppresses rather than nags.** No suggestion is emitted for a skill already at V, for a slot
pool with idle capacity (that's an `enough` entry saying so — deploy before you train), or for a
character with no skill data. The PI half is not reimplemented here: `skill_roi_for` already
answers it and is called directly, so both pages agree by construction.
"""
import logging

from fastapi import Depends

from app.sde import get_connection
from app.esi import require_context

from app.industry._router import router
from app.industry.slots import _slot_pool

log = logging.getLogger(__name__)

FEATURE_KEY = "industry_skill_advisor"

# Skill ranks (SDE dogma attribute 275, `skillTimeConstant`), hardcoded for exactly the skills this
# advisor reasons about. They are NOT read from the SDE: the only file carrying them is
# fsd/typeDogma.yaml, and parsing it costs ~1.8 GB peak RSS and ~19s (measured 2026-08-01) against
# a 2Gi container limit — an OOM risk at pod startup for eight integers that have never changed.
# Verified against the live SDE on the same date.
SKILL_RANK = {
    3380: 1,    # Industry
    3388: 3,    # Advanced Industry
    3387: 2,    # Mass Production
    24625: 8,   # Advanced Mass Production
    45748: 2,   # Mass Reactions
    45749: 8,   # Advanced Mass Reactions
}
SKILL_NAME = {
    3380: "Industry", 3388: "Advanced Industry",
    3387: "Mass Production", 24625: "Advanced Mass Production",
    45748: "Mass Reactions", 45749: "Advanced Mass Reactions",
}
# pp_characters column backing each skill, so levels come from the same place the slot maths uses.
SKILL_COLUMN = {
    3380: "industry", 3388: "advanced_industry",
    3387: "mass_production", 24625: "advanced_mass_production",
    45748: "mass_reactions", 45749: "advanced_mass_reactions",
}

# CCP's skill-point curve: SP for level L = 250 × rank × √32^(L-1).
_SP_MULT = [1, 5.656854249492381, 32, 181.01933598375618, 1024]

# A slot pool this busy or busier counts as saturated — i.e. another slot would actually get used.
# Below it, the honest advice is "fill what you have", not "train".
SATURATION = 0.7

# Per-level job-time reduction. Industry is manufacturing-only; Advanced Industry covers reactions
# too, which is why it can be worth more than its higher rank suggests for a reaction-heavy account.
TIME_SKILLS = {
    3380: {"pct": 0.04, "pools": ("manufacturing",)},
    3388: {"pct": 0.03, "pools": ("manufacturing", "reaction")},
}
SLOT_SKILLS = {
    "manufacturing": (3387, 24625),     # Mass Production, Advanced Mass Production
    "reaction": (45748, 45749),         # Mass Reactions, Advanced Mass Reactions
}


def _feature_on(context_id: int | None = None) -> bool:
    # Role-aware: a feature on the `testers` rung must work for a tester, not just show up in the
    # Admin tab. See app.features.feature_enabled_for.
    from app.features import feature_enabled_for
    return feature_enabled_for(FEATURE_KEY, context_id)


def sp_for_level(rank: int, level: int) -> int:
    """Total SP to hold `level` in a skill of this rank. Level 0 is 0."""
    if level <= 0:
        return 0
    return int(round(250 * rank * _SP_MULT[min(level, 5) - 1]))


def sp_to_next(skill_id: int, have: int) -> int | None:
    """SP from `have` to the next level, or None at V (nothing left to train)."""
    if have >= 5:
        return None
    rank = SKILL_RANK.get(skill_id)
    if not rank:
        return None
    return sp_for_level(rank, have + 1) - sp_for_level(rank, have)


def _time_gain_pct(pct_per_level: int | float, have: int) -> float:
    """Throughput gain from one more level of a job-time skill, as a percentage.

    Job time scales by (1 - pct·level), so throughput — jobs per day — scales by its reciprocal.
    Expressed as a gain over the CURRENT level, not over an untrained character: the question is
    what the next level buys, and the levels already trained are sunk.
    """
    cur = 1.0 - pct_per_level * have
    nxt = 1.0 - pct_per_level * (have + 1)
    if nxt <= 0 or cur <= 0:
        return 0.0
    return (cur / nxt - 1.0) * 100.0


def _character_rows(context_id: int) -> list[dict]:
    con = get_connection()
    try:
        cols = ", ".join(f"COALESCE({c},0) AS {c}" for c in sorted(set(SKILL_COLUMN.values())))
        rows = con.execute(
            f"SELECT character_id, character_name, scopes, {cols} "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0 "
            "ORDER BY character_id", (context_id,)).fetchall()
    finally:
        con.close()
    return [dict(r) for r in rows]


def _suggest(char: dict, skill_id: int, have: int, gain_pct: float, pool: str, detail: str):
    """One ranked suggestion, or None when it isn't worth showing (already V, or no real gain)."""
    sp = sp_to_next(skill_id, have)
    if sp is None or gain_pct <= 0.01:
        return None
    return {
        "character_id": char["character_id"],
        "character_name": char["character_name"],
        "skill_id": skill_id,
        "skill": SKILL_NAME.get(skill_id, str(skill_id)),
        "from_lvl": have,
        "to_lvl": have + 1,
        "pool": pool,
        "detail": detail,
        "gain_pct": round(gain_pct, 2),
        "sp": sp,
        # The ranking key, and the reason a cheap rank-2 skill outranks a pricey rank-8 one: this
        # is throughput bought per SP spent. Scaled per million SP purely for readability.
        "gain_per_msp": round(gain_pct / (sp / 1_000_000.0), 3) if sp else 0.0,
    }


def industry_skill_advice(context_id: int) -> dict | None:
    """Ranked "train this next" advice for the account, or None when the feature is off."""
    if not _feature_on(context_id):
        return None
    pool = _slot_pool(context_id)
    by_id = {c["character_id"]: c for c in pool.get("characters") or []}
    chars = _character_rows(context_id)

    suggestions: list[dict] = []
    enough: list[dict] = []

    # Account-level pool utilisation. Slots are per character, but the decision "do we need more
    # slots at all" is an account-level one — a fleet with idle slots somewhere does not need more.
    for pname, (basic_id, adv_id) in SLOT_SKILLS.items():
        total = pool.get(f"{pname}_slots") or 0
        free = pool.get(f"{pname}_free") or 0
        if total <= 0:
            continue
        used = max(0, total - free)
        util = used / total
        if util < SATURATION:
            enough.append({
                "pool": pname,
                "skill": SKILL_NAME[basic_id],
                "detail": f"{free} of {total} {pname} slot{'s' if total != 1 else ''} are idle — "
                          f"fill the slots you already have before training more",
                "slots": total, "free": free, "utilisation_pct": round(util * 100, 1),
            })
            continue
        # Saturated: one more slot is worth 1/total of current throughput. Offer it on whichever
        # character can buy that slot most cheaply — the basic skill is rank 2 and the advanced one
        # rank 8, so "which skill" is really "which is cheaper from where you are".
        best = None
        for c in chars:
            if c["character_id"] not in by_id:
                continue            # excluded from the pool (no skill data / no slot skills)
            for sid in (basic_id, adv_id):
                have = int(c.get(SKILL_COLUMN[sid]) or 0)
                s = _suggest(c, sid, have, 100.0 / total, pname,
                             f"+1 {pname} slot (pool is {round(util * 100)}% busy)")
                if s and (best is None or s["gain_per_msp"] > best["gain_per_msp"]):
                    best = s
        if best:
            best["slots"] = total
            best["utilisation_pct"] = round(util * 100, 1)
            suggestions.append(best)

    # Job-time skills. Unlike slots these always pay, so they need no saturation test — but they're
    # only offered for a character that actually has slots in an affected pool, since shaving job
    # time on a toon that installs nothing is worth nothing.
    for c in chars:
        pooled = by_id.get(c["character_id"])
        if not pooled:
            continue
        for sid, spec in TIME_SKILLS.items():
            have = int(c.get(SKILL_COLUMN[sid]) or 0)
            active = [p for p in spec["pools"] if (pooled.get(f"{p}_slots") or 0) > 0]
            if not active:
                continue
            gain = _time_gain_pct(spec["pct"], have)
            where = " + ".join(active)
            s = _suggest(c, sid, have, gain,
                         "manufacturing" if len(active) == 1 else "both",
                         f"-{int(spec['pct'] * 100)}% job time on {where}")
            if s:
                suggestions.append(s)

    suggestions.sort(key=lambda s: s["gain_per_msp"], reverse=True)
    return {
        "suggestions": suggestions,
        "enough": enough,
        "pools": {"manufacturing": {"slots": pool.get("manufacturing_slots") or 0,
                                    "free": pool.get("manufacturing_free") or 0},
                  "reaction": {"slots": pool.get("reaction_slots") or 0,
                               "free": pool.get("reaction_free") or 0}},
        "excluded": pool.get("excluded") or [],
    }


@router.get("/api/industry/skill-advisor")
def industry_skill_advisor(ctx: int = Depends(require_context)):
    """What to train next, industry AND planetary, ranked by throughput bought per SP.

    The PI half comes from `skill_roi_for` — the Setup Analysis advisor — rather than a second
    implementation, so the two pages can never drift into disagreeing about the same account. It is
    reported under its own key because its gains are ISK/day, which is NOT comparable to a
    throughput percentage; merging them into one ranked list would invent a common currency that
    doesn't exist.
    """
    if not _feature_on(ctx):
        return {"enabled": False}
    out = industry_skill_advice(ctx) or {}
    try:
        from app.planner_advisor import skill_roi_for
        pi = skill_roi_for(ctx)
    except Exception:
        log.exception("PI skill-roi half failed")
        pi = {"suggestions": [], "enough": [], "note": None}
    return {"enabled": True, **out, "planetary": pi}
