"""`BuildParams` — every knob a plan is resolved against — plus the two blueprint helpers that
reason about ME/TE, and the CCP constants both engines share."""
import math
from dataclasses import dataclass, field

from fastapi import Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection
from app.markets import resolve_market_data
from app.industry_cost import fetch_system_cost_index, fetch_adjusted_prices
from app.esi import require_context

from app.industry._router import router

# Flat 4% SCC surcharge CCP applies to every industry job's estimated item value (EIV), on top of
# the system cost index and facility tax — same constant app.reactions.graph uses.
SCC_SURCHARGE_PCT = 0.04

# Reaction material bonus from the T1 reactor rig (Standup L-Set Reactor Efficiency I): -2.2%
# materials. Matches app.reactions.graph.REACTION_ME_REDUCTION so the two engines agree on
# reaction input quantities. Manufacturing ME comes from the (per-player) blueprint library later;
# for now it's a plan-time parameter.
REACTION_ME_REDUCTION = 0.022


@dataclass
class BuildParams:
    """Knobs the resolver prices against. Phase 1 defaults reproduce a bare, un-researched build
    (ME/TE 0, no structure/rig material bonus) plus the always-on 4% SCC. Later phases populate
    ME/TE per blueprint from the library and the cost indices from the configured build system."""
    me_pct: float = 0.0                       # manufacturing material efficiency (0–10)
    te_pct: float = 0.0                       # time efficiency (0–20)
    struct_material_mult: float = 1.0         # structure+rig MATERIAL multiplier (facility ME bonus)
    struct_time_mult: float = 1.0             # structure+rig TIME multiplier (facility TE bonus)
    # Industry-skill time reduction. These DEFAULTS are the maxed case (manufacturing = Industry V
    # −20% × Advanced Industry V −15% = 0.68; reactions get Advanced Industry V only = 0.85), but
    # nothing in the app relies on them: every plan goes through resolve_build_params(), which
    # overwrites both from the account's REAL scanned skills via account_industry_time_mults().
    # They are the value a bare BuildParams() gets in a test or a REPL, not a planning assumption.
    mfg_skill_time_mult: float = 0.68
    rx_skill_time_mult: float = 0.85
    reaction_material_mult: float = 1.0 - REACTION_ME_REDUCTION
    mfg_cost_index: float = 0.0               # system manufacturing cost index (0.0 = unknown)
    rx_cost_index: float = 0.0                # system reaction cost index
    facility_tax_pct: float = 0.0             # structure facility tax %
    build_margin: float = 0.0                 # build must beat buy by this fraction to be chosen
    max_build_hours: float = 0.0              # >0 → buy any component whose batch would take longer
                                              # than this wall-clock to build (time-priority mode)
    # Marginal-saving buy: don't bother building a component when the ISK it saves over buying is
    # trivial — either as a fraction of the WHOLE product's build cost (many tiny savings add up,
    # but each one isn't worth a job) or as a fraction of the component's own buy price.
    marginal_pct_of_total: float = 0.0        # buy if build saves < this % of total product cost
    min_saving_isk: float = 0.0               # buy if build saves < this absolute ISK (build isn't
                                              # worth the extra job/time for less)
    min_saving_pct: float = 0.0               # buy if build saves < this % of the component's buy price
    # Per-product ME/TE from the account's actual owned blueprints (product_type_id -> (me, te)).
    # When a product is here, its real researched efficiency is used instead of the global me_pct/
    # te_pct fallback. `owned` carries the same map's ownership detail for display.
    me_by_product: dict = field(default_factory=dict)
    owned: dict = field(default_factory=dict)
    # How much of the account's blueprint picture we can actually SEE: {characters, cached}.
    # `owned` unions only the characters that have a cached blueprint list, so on a partly-connected
    # account every count in it is a FLOOR, not a total — the other characters may hold more copies
    # and more formulas of the same types. Anything that reasons about how many prints exist has to
    # know that (see `prints_known`). None = not stated, which is a hand-built params (a test, a
    # REPL) where `owned` IS the holding by construction.
    blueprint_coverage: dict | None = None
    # Products whose holding the user DECLARED by hand (`owned_blueprints` marks them
    # `source: "manual"`; `blueprints.declared_products` reads the mark). A declaration is not a
    # scan — it is the user stating what they own — so it is KNOWN for that product whatever the
    # account-wide coverage above says. See `prints_known`. Empty = nothing declared, which is the
    # behaviour that shipped before hand-declaration existed.
    declared_prints: set = field(default_factory=set)
    # product_type_id -> EXTRA concurrent reactions the account's ENABLED stock proves, over and
    # above the copies in `owned` (app.industry.blueprints.stock_formula_prints, which does the
    # de-duplication against the blueprint cache). A formula found in a hangar raises the print CAP
    # and nothing else: an asset row has no ME, no TE and no runs, so it never reaches `owned`,
    # `copies_for` or `me_te_for`. Empty = exactly the behaviour before stock was read at all.
    stock_prints: dict = field(default_factory=dict)
    # ME/TE of the copy the plan would BUY (from the contract index) — what runs past the owned
    # copies are built at. Falls back to the global me_pct/te_pct when nothing is listed.
    buy_me_te: dict = field(default_factory=dict)
    # product_type_id -> "owned" | "contract" | "override". Purely for reporting: a plan that
    # silently assumed ME 10 off a contract is one the user can't sanity-check.
    me_source: dict = field(default_factory=dict)
    # Blueprint acquisition cost for types NOT owned — {type_id: {kind, price, runs_per_copy}}.
    # Empty means 'unknown', which leaves the old behaviour untouched.
    bp_acquire: dict = field(default_factory=dict)
    # Per-component override: build these no matter what the shortcuts say. The shortcuts are
    # heuristics about what's WORTH a job, and that's the user's call to make item by item — the
    # cost engine's own verdict (buying is outright cheaper) is untouched by this.
    force_build_ids: set = field(default_factory=set)
    # The mirror of force_build_ids: NEVER build these, whatever the cost engine works out. Some
    # things a player simply always buys — a component whose blueprint they don't intend to own, a
    # material they can get delivered — and that's a standing preference about how they operate, not
    # a judgement the cost math can reach. Only honoured when the item can actually be bought (no
    # price = no alternative), and `force_build_ids` wins: a per-order "build it anyway" is a
    # deliberate exception to the standing rule, so the more specific choice takes precedence.
    never_build_ids: set = field(default_factory=set)
    # ── Reaction build policy (per ACCOUNT) ───────────────────────────────────────────────────
    # A builder who simply doesn't run reactions had to blacklist every output by hand. This is the
    # same standing-way-of-operating idea as `never_build_ids`, one rung coarser: it speaks for a
    # whole ACTIVITY or a whole FAMILY instead of a type. Both resolve to "buy", so they cannot
    # contradict each other — only an override can beat either.
    #
    # Two overrides, at the two grains the choice is actually made at, and it is worth being explicit
    # about why there are two: `force_build_ids` is "build THIS component anyway" (per order, per
    # type, the finest escape) and `build_reactions_anyway` is the same sentence at the level this
    # policy operates on — "this order makes its own reactions". Neither is a second policy; both
    # simply exempt something from the standing rule.
    buy_all_reactions: bool = False           # the account doesn't run reactions at all
    buy_reaction_categories: set = field(default_factory=set)   # …or not THESE families
    build_reactions_anyway: bool = False      # per order: this build makes them regardless
    # ── How long ONE reaction job may run (hours). 0 = no ceiling, which is every plan before this.
    # A reaction has no per-job run cap, so 5,000 runs fit in one slot and sit there for weeks; this
    # lets a builder say "never more than two days" and have the batch spread over the reactor slots
    # they have. It is a CEILING, never a target: it can only ever make jobs shorter (see
    # `_packed_jobs`), a consumer's deadline still wins, and it can never conjure a slot or a formula
    # — the split stays bounded by `n_wide`, which already holds the pool size and the formula cap.
    # REACTIONS ONLY, deliberately: splitting a manufacturing batch spends blueprint COPIES, which
    # cost real ISK, while a formula is durable and reused by every later build.
    max_reaction_job_hours: float = 0.0
    # product_type_id -> SDE group_id, which is how a produced type is matched to a category. Empty
    # (a hand-built params, a REPL) means no type can be categorised — so a category rule matches
    # nothing and the plan is exactly today's. `buy_all_reactions` needs no groups at all.
    type_groups: dict = field(default_factory=dict)
    # Ordering a reaction is the newer, more specific instruction — the same carve-out the blacklist
    # gets from `prepare_plan_inputs`. Filled with the plan's targets there.
    reaction_policy_exempt_ids: set = field(default_factory=set)

    def reaction_policy_buys(self, type_id: int, activity: str | None,
                             ignore_override: bool = False) -> bool:
        """Does the account's standing reaction policy say to buy this one?

        `ignore_override=True` answers the counterfactual — what the policy WOULD have said — which
        is what an order that overrides it reports its ISK delta against.
        """
        if activity != "reaction" or type_id in self.reaction_policy_exempt_ids:
            return False
        if self.build_reactions_anyway and not ignore_override:
            return False
        if self.buy_all_reactions:
            return True
        if not self.buy_reaction_categories:
            return False
        from app.industry.categories import category_for
        key = category_for(self.type_groups.get(type_id))
        return key is not None and key in self.buy_reaction_categories
    # What to charge over cost when quoting a customer. Priced off NET cost — the leftovers a build
    # over-produces stay with the builder, so billing them to the customer would charge twice.
    margin_pct: float = 0.0
    # The system the build is costed in, or None when the account has never configured one. Job
    # installation fees are EIV x (system cost index + facility tax + 4% SCC); with no system the
    # index term is simply absent, so the fee is understated by exactly that share (the SCC and tax
    # still apply — this is NOT a zero job cost). Carried so the plan can say so out loud.
    build_system_id: int | None = None
    # Where that system came from: "configured" (the user set it), "structure" (a building they
    # described), "reference" (Jita, we know nothing), "request" (this call named one), "none" (no
    # system at all — the fee is quoted without an index). Reported, never assumed silently.
    build_system_basis: str = "none"
    # "real" when the job-time skills came from a scanned character, "assumed" when nothing
    # on the account has been scanned and V/V was used. Surfaced on the plan so a guess is
    # never presented as a measurement — same principle as me_source per build step.
    skill_time_basis: str = "assumed"

    # Per-JOB build site, when the account runs more than one build structure and its rigs are
    # group-specific: type_id -> {key, name, system_id, me_pct, te_pct, material_mult, time_mult,
    # cost_index, tax_pct}. Empty = the single flat facility above, which is exactly how every
    # plan behaved before routing existed. See app.industry.structures.route_job.
    job_sites: dict = field(default_factory=dict)
    # Pins the account set that this build could NOT honour — the structure is gone, doesn't run
    # that activity, or routing is off entirely. Set by `resolve_job_sites` beside `job_sites`,
    # because only the routing knows which candidates a job really had. Empty = nothing to say.
    pin_notes: list = field(default_factory=list)

    def site_for(self, type_id: int, activity: str) -> dict | None:
        """Where this job is installed, or None when the plan isn't routed (one flat facility)."""
        return self.job_sites.get(type_id) if activity else None

    def struct_mults_for(self, type_id: int, activity: str) -> tuple[float, float]:
        """(material_mult, time_mult) for ONE job — the single place cost, materials, job time and
        the scheduler all read, so they cannot disagree about which structure a job ran in.

        A half-threaded version of this is worse than none: quoting materials off a capital rig the
        schedule doesn't know about produces a plan whose ISK and whose ETA describe two different
        factories. Every consumer goes through here.
        """
        site = self.job_sites.get(type_id)
        if site is not None:
            return site["material_mult"], site["time_mult"]
        if activity == "manufacturing":
            return self.struct_material_mult, self.struct_time_mult
        return self.reaction_material_mult, 1.0

    def job_fee_rate(self, type_id: int, activity: str) -> float:
        """The multiplier on EIV that gives the job installation fee: system cost index + facility
        tax + the flat SCC surcharge. Routed jobs use THEIR OWN system's index and structure's tax
        — the fee is per-system, so it has to follow the routing or the cost is fiction."""
        site = self.job_sites.get(type_id)
        if site is not None:
            return site["cost_index"] + site["tax_pct"] / 100.0 + SCC_SURCHARGE_PCT
        ci = self.mfg_cost_index if activity == "manufacturing" else self.rx_cost_index
        return ci + self.facility_tax_pct / 100.0 + SCC_SURCHARGE_PCT

    def prints_known(self, type_id: int | None = None) -> bool:
        """Whether the blueprint holding may be read as a TOTAL rather than a floor.

        **Asked per PRODUCT when a product is named**, because the evidence comes in two kinds and
        only one of them is account-wide. A product the user DECLARED by hand is known outright: a
        declaration is a statement of what they own, not a partial reading of it, and
        `owned_blueprints` already lets it REPLACE the ESI reading for that product. Gating it on
        account-wide coverage answered a per-product question with an account-wide one and cost a
        real user the cap they had just declared — 238 formulas declared, 10 held of the product
        they ordered, 20 concurrent jobs assigned, because 12 of their 14 characters had never
        granted the blueprints scope. Called with no `type_id` (the reporting sites) it still
        answers for the account as a whole.

        Otherwise: only when EVERY character has a cached blueprint list. `owned` is a union over the cached
        ones, so a partly-connected account's counts are a floor — and a cap built on a floor
        serialises jobs the builder can really run side by side, which is a plan made materially
        worse on incomplete evidence. Measured in prod: one account has 2 of 14 characters cached and
        still shows prints for 159 types; another has 3 of 3 and 31 of its 50 formula types are
        genuinely held singly. Same numbers, opposite meanings — only the coverage tells them apart.

        A character without the blueprints scope can never have a cache, so such an account is
        permanently "unknown" until it connects one. That is the honest answer, and it is deliberately
        NOT a partial-credit scheme: crediting the characters we can see is the same guess in
        smaller print.
        """
        if type_id is not None and type_id in (self.declared_prints or ()):
            return True
        cov = self.blueprint_coverage
        if cov is None:
            return True                    # not stated — `owned` is the holding, by construction
        chars = int(cov.get("characters") or 0)
        return chars > 0 and int(cov.get("cached") or 0) >= chars

    def copies_for(self, type_id: int, activity: str) -> list[dict]:
        """The owned copies a job for this product may run off, best-researched first.

        Empty for a reaction (no blueprint), for a product the account owns nothing for, and for one
        the user has explicitly overridden — an override is the user telling us which print they
        will actually use, so it wins over every copy we can see.
        """
        if activity != "manufacturing" or self.me_source.get(type_id) == "override":
            return []
        own = self.owned.get(type_id)
        if not own:
            return []
        # `owned` carries every copy (see app.industry.blueprints.owned_blueprints). A hand-built
        # params — a test, a REPL — may carry only the summary; that is one copy, described.
        return own.get("copies") or [{"me": own.get("me") or 0, "te": own.get("te") or 0,
                                      "kind": own.get("kind") or "bpc",
                                      "runs": own.get("runs", -1)}]

    def buy_me_te_for(self, type_id: int) -> tuple[float, float]:
        """ME/TE of the copy the plan would buy — what runs past the owned copies are built at."""
        return self.buy_me_te.get(type_id) or (self.me_pct, self.te_pct)

    def me_te_for(self, type_id: int, activity: str, runs: int | None = None) -> tuple[float, float]:
        """(me_pct, te_pct) for a manufacturing product. Reactions have no blueprint ME/TE
        (rig-based, via the material mult), so they return (0, 0).

        **ME/TE is per JOB, off the copy that job runs on** — so with several owned copies of mixed
        research this is an AGGREGATE, and the caller says how big the batch is so the aggregate can
        be honest. `runs` given: a runs-weighted figure over exactly the copies best-first
        consumption will spend on that many runs, plus whatever the plan buys for the remainder.
        Using the best copy for the whole batch instead would over-credit every run after the first
        copy runs out; the per-job values themselves come from `copies_for` in build_tasks.
        """
        if activity != "manufacturing":
            return (0.0, 0.0)
        copies = self.copies_for(type_id, activity)
        if not copies:
            if type_id in self.me_by_product:
                return self.me_by_product[type_id]
            return (self.me_pct, self.te_pct)
        return blend_me_te(copies, runs, self.buy_me_te_for(type_id))


def blueprint_summary(own: dict | None) -> dict | None:
    """What a payload says about the blueprint(s) you hold for a product — everything except the
    per-copy list, which is a planning input rather than something a page needs to render."""
    if not own:
        return own
    return {k: v for k, v in own.items() if k != "copies"}


def blend_me_te(copies: list[dict], runs: int | None,
                fallback: tuple[float, float]) -> tuple[float, float]:
    """Runs-weighted (me, te) over the copies a batch of `runs` will actually be built off.

    Copies arrive best-researched first and are consumed in that order; anything past what they
    cover is built off `fallback` — the copy the plan would buy. An ORIGINAL (`runs < 0`) has no
    limit, so it answers for everything left.

    `runs=None` means the batch size isn't known at this point (a bare call, or the representative
    single-run costing). The whole holding is then weighted, which is the conservative reading: it
    never credits the best copy with runs it cannot carry.
    """
    if not copies:
        return fallback
    left = runs
    if left is None:
        left = sum(c.get("runs") or 0 for c in copies if (c.get("runs") or -1) >= 0)
    used: list[tuple[float, float, float]] = []      # (n, me, te)
    for c in copies:
        if left <= 0:
            break
        cr = c.get("runs")
        n = left if cr is None or cr < 0 else min(left, int(cr))
        if n <= 0:
            continue
        used.append((n, c["me"], c["te"]))
        left -= n
    if left > 0:
        used.append((left, fallback[0], fallback[1]))
    total = sum(n for n, _m, _t in used)
    if total <= 0:                       # nothing to weigh (e.g. an original only) — it is the copy
        return (copies[0]["me"], copies[0]["te"])
    return (sum(n * m for n, m, _t in used) / total,
            sum(n * t for n, _m, t in used) / total)
