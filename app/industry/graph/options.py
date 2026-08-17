"""What a caller may ask for (`BuildOptions`, `IndustryPlanRequest`) and what the ACCOUNT
answers when they do not: time multipliers, build defaults, the fallback system."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

# ── Endpoint ──────────────────────────────────────────────────────────────────────────────────

class BuildOptions(BaseModel):
    """Everything that shapes HOW a build is costed and scheduled, independent of WHAT is being
    built. Shared base for the one-shot plan (a product + quantity) and the whole-queue plan (the
    account's queued orders), which had drifted into two hand-maintained copies of this list."""
    me_pct: float = 0.0
    te_pct: float = 0.0
    system_id: int | None = None       # None → derive from the account's Reactions build system
    facility_tax_pct: float | None = None
    prioritize_speed: bool = True      # buy slow-to-build bulk components to minimize makespan
    struct_material_pct: float = 0.0   # facility ME bonus (material reduction %)
    struct_time_pct: float = 0.0       # facility TE bonus (time reduction %)
    # Which facility the two percentages above came from ("s:<market id>" = one of the account's
    # own structures, "p:<preset>" = a generic preset). The engine needs the identity, not just the
    # numbers, to know whether that structure is already a routing candidate — see routing.py.
    facility_id: str | None = None
    use_stock: bool = True             # net owned materials off the demand (needs an asset scan)
    marginal_pct: float | None = None  # build only if it saves >= this % of the build (None = default)
    force_build: bool = False          # build everything buildable, ignoring small-saving shortcuts
    force_build_ids: list[int] = []    # build THESE components regardless of the shortcuts
    # Never build these, whatever the cost engine decides — the account's standing "I always buy
    # that" list. `force_build_ids` on an order overrides it for that build.
    never_build_ids: list[int] = []
    # The account's standing reaction policy (app.industry.categories). `buy_all_reactions` is "this
    # account doesn't run reactions"; `buy_reaction_categories` narrows that to families. Filled from
    # the saved policy in apply_account_build_options, so every plan path — share link, checklist —
    # follows the same standing rule the user's screen does.
    buy_all_reactions: bool = False
    buy_reaction_categories: list[str] = []
    # …and the per-ORDER escape hatch: this build makes its own reactions regardless. Unioned across
    # the queue exactly like force_build_ids, because the queue builds ONE shared batch per
    # component — if any order in it reacts, the shared batch is reacted.
    build_reactions_anyway: bool = False
    # Per-product ME/TE the user wants assumed, {"<type_id>": [me, te]} — JSON keys are strings.
    # Wins over everything: it's the user telling us which print they'll actually use.
    me_te_overrides: dict[str, list[int]] = {}
    margin_pct: float | None = None    # markup over net cost for the customer price (None = default)
    # The longest a single REACTION job may run, in DAYS (None/0 = no ceiling, today's plan). Filled
    # from the account's saved setting in apply_account_build_options, so a share link and the
    # start-now checklist split a batch into the same jobs the user's screen showed.
    max_reaction_job_days: float | None = None
    # Where a whole rig FAMILY is built, whatever the routing would have scored:
    # {"capital_ship": "s:<market row id>", …}. Filled from the account's saved pins in
    # apply_account_build_options, so a share link and the start-now checklist install the jobs in
    # the same buildings the user's screen named. `{}` = infer everything, today's plan.
    build_pins: dict[str, str] = {}


class IndustryPlanRequest(BuildOptions):
    type_id: int
    quantity: int = 1
    # The boxes this PLAN may spend, when the user has already picked them in the modal. A plan owns
    # its own sources, so the preview has to be costed against exactly the stock the resulting order
    # would count — otherwise the preview promises a shopping list the queued build then contradicts.
    # Omitted (None) = the account-wide tick list, which is what every existing caller sends.
    source_keys: list[str] | None = None


# Wall-clock cap (hours) a single component's batch may take to build before the time-priority
# make-or-buy buys it instead. ~1 day: builds fast components, buys the multi-day bulk marathons.
SPEED_BUILD_CAP_HOURS = 24.0

# Default markup when quoting a build to a customer. A starting point, not a recommendation — it's
# the one number here only the builder can know, so it's a knob with a sane default.
MARGIN_DEFAULT_PCT = 10.0

# Marginal-saving threshold (always on): buy a component the cost engine would build if building it
# saves less than this % of the TOTAL product cost. Measured against the whole build — NOT a
# per-component percentage, which doesn't scale (4% of an Ishtar component is a sub-1m difference
# that shouldn't drive a decision, while 4% of a Revelation component is real money). This one
# lens auto-scales: it trims the long tail on a 2.35b capital but barely touches a 147m cruiser.
#
# Raised 0.1% -> 3% once the market-pricing fix let components actually be costed. At 0.1% the
# threshold was ~2m on a Revelation, under the absolute floor below, so it never bound: the planner
# built 18 component types / 471 jobs to save ~70m on a 2.4b hull. 3% (~72m there) keeps the builds
# that genuinely move the number and buys the long tail, which is the stated priority — time first,
# cost competitive but not at the price of days of clicking.
MARGINAL_BUILD_PCT_OF_TOTAL = 3.0
# Absolute floor: a build step isn't worth its slot/time unless it saves at least this much ISK,
# so anything cheaper to build by less than this is bought (prioritize time). This is what makes a
# cheap ship — where most components each save only a little — mostly BUY-and-assemble rather than
# a multi-day build. The effective threshold is max(this, MARGINAL_BUILD_PCT_OF_TOTAL × total): the
# floor governs small hulls (3% of a 147m Ishtar is ~4m, under it) while the percentage governs
# capitals, so one rule covers both ends without a per-component percentage.
MIN_BUILD_SAVING_ISK = 5_000_000


def account_industry_time_mults(context_id: int, with_basis: bool = False):
    """(manufacturing, reaction) job-time multipliers from the account's best Industry / Advanced
    Industry levels — Industry −4%/level (manufacturing), Advanced Industry −3%/level (all jobs).
    Uses the highest across the account's characters (you build on your best-skilled toon).

    **Real levels win wherever we can prove we have them.** This used to read
    `if ind == 0 and adv == 0: ind = adv = 5`, which silently upgraded an untrained account to V/V
    and quoted it a build ~47% faster than it can actually do.

    The hard part is that a 0 in these columns has TWO meanings, and getting it wrong is expensive
    in both directions. `industry`/`advanced_industry` were added to the character table after
    `interplanetary_consolidation`/`command_center_upgrades`/`mass_reactions`, so a character last
    scanned before that migration reads 0 for the newer columns while the older ones are populated —
    a stale gap, not an untrained pilot. Measured on prod 2026-08-01: 79 characters across 13 of 26
    accounts show exactly that shape (Mass Reactions V, Industry 0), and believing those zeros would
    have inflated their job times by 47% overnight. The ESI skills scope does NOT separate the two
    cases: it proves the character was scanned at some point, not that THESE columns were filled.

    The only sound evidence is a `pp_char_skills` row set for the character — the full ESI skill
    list written by the required-skills feature. It is authoritative because it records ABSENCE as
    well as presence: a skill missing from it is genuinely untrained, so a 0 derived from it is a
    fact. Without it we cannot tell a real 0 from a stale column, so the account keeps the V/V
    fallback and the basis is reported as "assumed".

    A tempting shortcut was "believe the columns if ANY industry-era column is non-zero, since that
    proves a post-migration scan". It is WRONG, and prod proved it: two accounts show Mass
    Production V with Industry 0, which the game does not allow — the SDE lists Industry III as a
    prerequisite of Mass Production. `mass_production` was evidently added to the scan before
    `industry`, so one populated column says nothing about its neighbour. Trusting that shortcut
    would have inflated those accounts' job times by 47% on data that is provably impossible.

    Practical effect: until required-skills is enabled and characters are rescanned, every account
    reads "assumed" and nothing changes. As `pp_char_skills` fills in, accounts switch to their real
    numbers automatically, one rescan at a time.

    Pass `with_basis=True` to also get "real" or "assumed", so callers can say which they used
    instead of presenting a guess as a measurement.
    """
    ind = adv = 0
    known = False
    try:
        con = get_connection()
        rows = con.execute(
            "SELECT character_id, COALESCE(industry,0) AS i, COALESCE(advanced_industry,0) AS a "
            "FROM pp_characters WHERE context_id=? AND COALESCE(is_dummy,0)=0", (context_id,),
        ).fetchall()
        scanned_ids: set = set()
        if rows:
            try:
                ids = [r["character_id"] for r in rows]
                placeholders = ",".join("?" for _ in ids)
                scanned_ids = {r["character_id"] for r in con.execute(
                    f"SELECT DISTINCT character_id FROM pp_char_skills "
                    f"WHERE character_id IN ({placeholders})", tuple(ids))}
            except Exception:
                scanned_ids = set()      # table absent (feature never enabled) → nothing is proven
        con.close()
        for r in rows:
            if r["character_id"] not in scanned_ids:
                continue          # no authoritative list → its columns can't be trusted either way
            known = True
            ind = max(ind, int(r["i"] or 0))
            adv = max(adv, int(r["a"] or 0))
    except Exception:
        known = False
    if not known:
        ind = adv = 5   # no provable data → assume trained, corrected once skills are scanned
    mults = ((1 - 0.04 * ind) * (1 - 0.03 * adv), (1 - 0.03 * adv))
    return (*mults, "real" if known else "assumed") if with_basis else mults


# The reference system used only when an account has told us nothing about where it builds. Jita is
# the honest choice for a REFERENCE rather than a guess: its index is at the top of the range
# (0.1715 measured against a 0.055 no-index rate — 76% of the whole fee), so a quote built on it is
# conservative, and being conservative about a floor price is the safe direction to be wrong in.
_REFERENCE_SYSTEM_ID = 30000142            # Jita


def account_build_defaults(context_id: int, with_basis: bool = False):
    """Where this account builds — (system_id, facility_tax_pct[, basis]).

    Job installation fee is EIV x (system cost index + facility tax + 4% SCC), so with NO system the
    index term is simply missing: manufacturing is understated by the index share and reactions are
    quoted with no install fee at all. Measured in prod, **1 of 26 accounts had a system set**, so
    that was very nearly everybody.

    Three sources, most specific first, and the basis is reported so the number is never a silent
    assumption (same rule as `skill_time_basis`):

    * `"configured"` — the system the account set for Reactions. Unchanged behaviour, and it stays
      first: it is the only one the user actually chose.
    * `"structure"` — the system of a structure the account has told us it BUILDS in, with that
      structure's own facility tax. Not a guess at all: they described the building, and it is a
      better answer than nothing for the 25 accounts that never filled in the Reactions field.
    * `"reference"` — Jita, when we know nothing whatsoever. Explicitly labelled, because a wrong
      default is harder to notice than an absent one, and this one WILL be wrong for a null-sec
      builder — it is a floor to quote against, not a claim about where they live.

    The last two are behind `industry_default_build_system`: they change the cost of every existing
    account's build, which is not something to do to a live quote without the switch being visible.
    """
    basis = "none"
    sid, tax = None, 0.0
    try:
        from app.reactions.settings import effective_reaction_settings, _resolve_system_id
        s = effective_reaction_settings(context_id)
        sid = _resolve_system_id(s.get("reaction_system"))
        tax = s.get("facility_tax_pct") or 0.0
        if sid:
            basis = "configured"
    except Exception:
        sid, tax = None, 0.0
    if sid is None and _default_system_on(context_id):
        sid, tax, basis = _fallback_build_system(context_id, tax)
    return (sid, tax, basis) if with_basis else (sid, tax)


def _default_system_on(context_id: int) -> bool:
    """Industry's own gate on the inference — this flag and no other.

    It briefly ORed in `reactions_default_system` (2026-08-14) so a reactions-only account could
    reach the inference. That was wrong and was reverted the same day: these are ROLLOUT-LADDER
    flags, not per-account opt-ins, so promoting the Reactions one would have moved job fees on
    every Industry user's quote through a switch that says nothing about Industry. Reactions reaches
    the same inference through `app.reactions.graph._reaction_fee_system`, which asks its own flag
    and calls `_fallback_build_system` itself — the shared machinery without the shared gate.
    """
    try:
        from app.features import feature_enabled_for
        return feature_enabled_for("industry_default_build_system", context_id)
    except Exception:
        return False


def _fallback_build_system(context_id: int, tax: float) -> tuple[int, float, str]:
    """A system the account has actually told us about, else the reference one.

    Prefers a structure it BUILDS in over one it only prices from — "where do you install jobs" is
    the question being answered — and takes that structure's own facility tax with it, since the two
    belong to the same building. Imported inside the function: `app.markets` reaches back into
    `app.industry.structures` for the rig bonuses, and this module is on that path.
    """
    try:
        from app.markets import build_structures, _list_markets
        sites = [m for m in build_structures(context_id) if m.get("system_id")]
        if sites:
            site = sites[0]
            return int(site["system_id"]), float(site.get("facility_tax_pct") or tax), "structure"
        priced = [m for m in _list_markets("account", context_id)
                  if m.get("kind") == "structure" and m.get("system_id")]
        if priced:
            return int(priced[0]["system_id"]), tax, "structure"
    except Exception:
        pass
    return _REFERENCE_SYSTEM_ID, tax, "reference"
