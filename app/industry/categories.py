"""Reaction families, as a taxonomy in their own right.

The SDE subset this app imports carries `types.group_id` and nothing else of the taxonomy (no
`groups` table, no category, no tech level — see app/industry/structures.py), so any notion of
"what KIND of thing is this" has to be a curated group-id map in code. There is exactly one such
map for reactions, and it lives here.

**Why here and not in structures.py.** The rig registry (`RIG_FAMILIES`) already carried these
group sets, because a Standup L-Set rig covers one reaction family. But a *rig family* and a
*build-policy category* are two different ideas that happen to agree today: one answers "does this
structure's rig apply to this job", the other answers "does this account run this kind of reaction
at all". Welding the second to the first by importing a rig constant would make a future divergence
(a rig line that splits, a policy category that merges) a rename in the wrong file. So the GROUP
SETS are shared from here and both readers keep their own labels: the rig registry names rigs, this
one names what a builder calls the goo.

`REACTION_CATEGORIES` is echoed to the frontend by `/api/industry/reaction-policy` — the UI never
hardcodes these labels, same rule as ALERT_KINDS.
"""
from __future__ import annotations

# Reaction outputs (category 4 Material + 20 Drug), by EVE groupID. Curated from ESI's
# /universe/categories/{id}/; the ids are stable SDE groupIDs.
_G_COMPOSITE = frozenset({429, 428})        # Composite + Intermediate moon materials
_G_HYBRID_POLYMER = frozenset({974})        # Hybrid polymers
_G_BIOCHEMICAL = frozenset({712, 20})       # Biochemical materials + boosters

# key → what a builder calls this family, plus the SDE groups its outputs land in. The key is
# STORED (per account, in pp_industry_settings.reaction_policy), so renaming one needs a migration.
REACTION_CATEGORIES: dict[str, dict] = {
    "composite": {"label": "Composites & intermediates",
                  "description": "Moon-goo reactions: intermediate materials and the composites "
                                 "built from them.",
                  "groups": _G_COMPOSITE},
    "hybrid_polymer": {"label": "Hybrid polymers",
                       "description": "Polymer reactions (fullerenes → polymers).",
                       "groups": _G_HYBRID_POLYMER},
    "biochemical": {"label": "Biochemicals & boosters",
                    "description": "Biochemical materials and the boosters made from them.",
                    "groups": _G_BIOCHEMICAL},
}


# What an account buys in when it has NEVER touched the policy control (2026-08-05, tester request).
#
# Hybrid polymers and biochemicals feed later steps of a build DIRECTLY, so reacting them removes a
# purchase without adding a stage to what the builder actually does — the reaction output goes
# straight into the next job that was already scheduled. Composites and intermediates are the family
# where buying is usually the better trade: they are the deepest sub-tree (intermediates feed
# composites feed the build), so building them is where the extra stages, slots and moon-goo sourcing
# pile up, and they are the most liquid to buy.
#
# This is only a DEFAULT. It applies to accounts with no stored policy and to nothing else — an
# account that has used the control keeps exactly what it chose (see `_parse_reaction_policy`). It is
# a families-only split by design: whether a first-stage/second-stage axis is also wanted is an open
# question with the tester, not something this default decides.
DEFAULT_BUY_CATEGORIES: tuple[str, ...] = ("composite",)


def category_registry() -> list[dict]:
    """The pickable categories, for the UI. Never hardcode this list in the frontend."""
    return [{"key": k, "label": v["label"], "description": v["description"]}
            for k, v in REACTION_CATEGORIES.items()]


def category_for(group_id: int | None) -> str | None:
    """Which category a produced type belongs to, by its SDE group — or None when the group is
    unknown or in no category. None is deliberately NOT "everything": a category rule may only
    speak for what it can actually identify, which is why the whole-activity switch is a separate
    thing rather than "all three categories ticked"."""
    if group_id is None:
        return None
    for key, cat in REACTION_CATEGORIES.items():
        if group_id in cat["groups"]:
            return key
    return None
