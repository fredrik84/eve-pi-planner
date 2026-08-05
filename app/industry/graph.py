"""Industry (manufacturing) make-or-buy engine — Phase 1.

Given a target buildable and a quantity, walk the recipe graph and decide, for every intermediate,
whether it's cheaper to BUILD it (manufacture / react from inputs) or BUY it off the market — then
produce a priced shopping list, a build tree, and cost + time metrics.

The producer graph spans TWO SDE recipe sources so cost totals are honest for capital / T2 / T3
builds that mix both:
  - manufacturing blueprints (`blueprints` / `blueprint_materials`, built by scripts/build_sde.py)
  - reactions (`reactions` / `reaction_inputs` — the same tables app/reactions reads)
Anything with neither recipe (minerals, moon goo, PI, datacores, raw items) is a terminal BUY node.

Pricing reuses the Reactions stack verbatim: materials are priced by
`app.markets.resolve_market_data` (followed local/alliance markets in priority order → Jita
fallback), and job installation fees reuse `app.industry_cost` (EIV × (system cost index +
facility tax + 4% SCC), EIV valued at CCP's adjusted prices).

This module is the read-only cost core. Scheduling, slots, queueing, alerting, and actually
*spawning* reaction orders into the Reactions service are later phases — see
docs/industry-planner-spec.md. For now a reaction node is costed generically here; Phase 3 routes
it through app.reactions for the authoritative economics + slot allocation.
"""
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

    def prints_known(self) -> bool:
        """Whether the account's blueprint holding may be read as a TOTAL rather than a floor.

        Only when EVERY character has a cached blueprint list. `owned` is a union over the cached
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


# ── SDE recipe graph loaders ──────────────────────────────────────────────────────────────────

# The two recipe graphs are STATIC SDE data — ~4,800 blueprints and every material row — and they
# were rebuilt from scratch on every plan call: 68ms locally, more against Postgres, and the
# manufacturing page runs several plans per load. They're read-only to every consumer (verified: no
# caller mutates a recipe), so one process-level copy serves all of them. The TTL is only a backstop
# for an SDE rebuild under a long-lived process; a deploy restarts the pod anyway.
_GRAPH_CACHE: dict[str, tuple[float, dict]] = {}
_GRAPH_TTL = 900.0


def _cached_graph(key: str, con, loader) -> dict[int, dict]:
    import time as _t
    hit = _GRAPH_CACHE.get(key)
    now = _t.time()
    if hit and now - hit[0] < _GRAPH_TTL:
        return hit[1]
    graph = loader(con)
    _GRAPH_CACHE[key] = (now, graph)
    return graph


def clear_graph_cache():
    """Drop the cached recipe graphs — for an SDE rebuild that has to take effect immediately."""
    _GRAPH_CACHE.clear()


def load_manufacturing_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`. `_load_manufacturing_graph` does the real work."""
    return _cached_graph("mfg", con, _load_manufacturing_graph)


def load_reaction_graph(con) -> dict[int, dict]:
    """Cached wrapper — see `_cached_graph`."""
    return _cached_graph("rx", con, _load_reaction_graph)


def _load_manufacturing_graph(con) -> dict[int, dict]:
    """product_type_id -> {blueprint_type_id, output_qty, base_time, max_runs, inputs}. If a
    product is (rarely) made by more than one blueprint, keep the lowest blueprint id so the graph
    is deterministic."""
    mats: dict[int, list[dict]] = {}
    for r in con.execute("SELECT blueprint_type_id, type_id, quantity FROM blueprint_materials"):
        mats.setdefault(r["blueprint_type_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute(
        "SELECT blueprint_type_id, product_type_id, output_qty, base_time, max_runs FROM blueprints"
    ):
        prod = r["product_type_id"]
        if prod in graph and graph[prod]["blueprint_type_id"] <= r["blueprint_type_id"]:
            continue
        graph[prod] = {
            "blueprint_type_id": r["blueprint_type_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["base_time"] or 0,
            "max_runs": r["max_runs"] or 0,
            "inputs": mats.get(r["blueprint_type_id"], []),
        }
    return graph


def _load_reaction_graph(con) -> dict[int, dict]:
    """product_type_id -> {reaction_id, output_qty, base_time, inputs}. Same tables app/reactions
    reads; loaded directly here (no cross-package import) to keep the make-or-buy core standalone.
    First formula wins on the rare double-output product."""
    inputs: dict[int, list[dict]] = {}
    for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
        inputs.setdefault(r["reaction_id"], []).append(
            {"type_id": r["type_id"], "quantity": r["quantity"]})
    graph: dict[int, dict] = {}
    for r in con.execute("SELECT reaction_id, output_type_id, output_qty, cycle_time FROM reactions"):
        prod = r["output_type_id"]
        if prod in graph:
            continue
        graph[prod] = {
            "reaction_id": r["reaction_id"],
            "output_qty": r["output_qty"] or 1,
            "base_time": r["cycle_time"] or 0,
            "inputs": inputs.get(r["reaction_id"], []),
        }
    return graph


# ── EVE material / time formulas ──────────────────────────────────────────────────────────────

def effective_material_qty(base_qty: int, runs: int, me_pct: float, bonus_mult: float) -> int:
    """CCP's per-job material formula: max(runs, ceil(round(baseQty·runs·(1−ME/100)·bonus, 2))).
    The max(runs, …) floor means a material never drops below 1 per run however good the ME."""
    if base_qty <= 0 or runs <= 0:
        return 0
    q = base_qty * runs * (1 - me_pct / 100.0) * bonus_mult
    return max(runs, math.ceil(round(q, 2)))


def _producer(type_id: int, mfg: dict, rx: dict) -> tuple[str | None, dict | None]:
    if type_id in mfg:
        return "manufacturing", mfg[type_id]
    if type_id in rx:
        return "reaction", rx[type_id]
    return None, None


def collect_reachable(target: int, mfg: dict, rx: dict) -> set[int]:
    """Every type_id reachable from the target through the producer graph (target + all recipe
    inputs, recursively), cycle-guarded — the set to price in one market call."""
    seen: set[int] = set()

    def walk(tid: int):
        if tid in seen:
            return
        seen.add(tid)
        _, recipe = _producer(tid, mfg, rx)
        if recipe:
            for inp in recipe["inputs"]:
                walk(inp["type_id"])

    walk(target)
    return seen


# ── Make-or-buy resolution ────────────────────────────────────────────────────────────────────

def resolve_unit_costs(mfg: dict, rx: dict, prices: dict, adjusted: dict,
                       params: BuildParams) -> dict[int, dict]:
    """Bottom-up unit cost + build/buy decision for every reachable node. Memoized per type_id,
    cycle-guarded (a recipe cycle degrades to buy). Unit build cost uses a representative single
    run for the decision; the top-down explode uses the real per-job quantities."""
    memo: dict[int, dict] = {}

    def unit(type_id: int, stack: frozenset) -> dict:
        if type_id in memo:
            return memo[type_id]
        buy = (prices.get(type_id) or {}).get("sell_price") or None
        activity, recipe = _producer(type_id, mfg, rx)
        build_uc = None
        if recipe and type_id not in stack:
            me, _te = params.me_te_for(type_id, activity)
            mult, _tm = params.struct_mults_for(type_id, activity)
            fee_rate = params.job_fee_rate(type_id, activity)
            inner = stack | {type_id}
            total_in = 0.0
            eiv = 0.0
            ok = True
            for inp in recipe["inputs"]:
                child = unit(inp["type_id"], inner)
                if child["unit_cost"] is None:
                    ok = False
                    break
                qty = effective_material_qty(inp["quantity"], 1, me, mult)
                total_in += child["unit_cost"] * qty
                eiv += inp["quantity"] * adjusted.get(inp["type_id"], 0.0)
            if ok:
                job = eiv * fee_rate
                build_uc = (total_in + job) / recipe["output_qty"]

        # Blacklisted: the user always buys this one. Applied HERE rather than at demand time so the
        # parent's cost is the price of buying it too — deciding to buy but costing the parent as if
        # it were built is the mismatch that makes a plan's total unbelievable.
        blacklisted = (type_id in params.never_build_ids
                       and type_id not in params.force_build_ids and buy is not None)
        # The account's reaction policy, applied at the SAME layer and for the same reason: it makes
        # the whole subtree under a bought reaction disappear on its own, with nothing pruned by
        # hand. `force_build_ids` still wins (a per-type "build it anyway" is the more specific
        # instruction) and a reaction with no buy price is still BUILT — refusing to build what
        # can't be bought would leave the plan no way to get one at all.
        by_policy = (params.reaction_policy_buys(type_id, activity)
                     and type_id not in params.force_build_ids and buy is not None)
        if blacklisted or by_policy:
            decision = "buy"
        elif build_uc is not None and buy is not None:
            decision = "build" if build_uc < buy * (1 - params.build_margin) else "buy"
        elif build_uc is not None:
            decision = "build"
        elif buy is not None:
            decision = "buy"
        else:
            decision = "unresolved"
        unit_cost = (build_uc if decision == "build"
                     else buy if decision == "buy" else None)
        node = {
            "type_id": type_id, "activity": activity, "buildable": recipe is not None,
            "decision": decision, "unit_cost": unit_cost, "blacklisted": blacklisted,
            "reaction_policy": by_policy,
            "build_unit_cost": build_uc, "buy_unit_cost": buy,
            "source": (prices.get(type_id) or {}).get("source"),
        }
        memo[type_id] = node
        return node

    return memo, unit


def reaction_policy_report(memo: dict, params: BuildParams, names: dict[int, str],
                           rows: list[tuple[int, float, bool]]) -> dict | None:
    """What the reaction policy did to this plan, in ISK.

    Buying reaction outputs instead of running them is the same shape of trade as the
    marginal-saving threshold, so it follows the same rule: report what the shortcut cost rather
    than quietly taking it. A builder quoting against a competitor has to see that not reacting
    moved their floor.

    `isk` is signed the same way `marginal_saving` is — **what BUILDING these would save over
    buying them** — which is what makes it read correctly in both directions:
      * policy in force  → the reactions were bought, so a positive figure is what the convenience
        cost this build;
      * order overriding it → the reactions were built, so the same positive figure is what
        building them saved against the standing rule.
    `None` when the policy touched nothing, so the UI has nothing to draw.

    `rows` is (type_id, quantity, was_built) — the caller knows which of its own rows are which.
    """
    items = []
    total = 0.0
    for tid, qty, built in rows:
        # The ACTIVITY comes off the resolved node, never assumed from the row: a raw material has
        # no producer at all, and asking the policy about it as if it were a reaction reports every
        # mineral in the build as something the policy bought.
        node = memo.get(tid) or {}
        if qty <= 0 or not params.reaction_policy_buys(tid, node.get("activity"),
                                                       ignore_override=True):
            continue
        if tid in params.force_build_ids:
            continue          # exempted per type; not the policy's doing either way
        buc, byc = node.get("build_unit_cost"), node.get("buy_unit_cost")
        if built and not params.build_reactions_anyway:
            continue          # built despite the policy (no buy price) — nothing was traded away
        saving = None if buc is None or byc is None else round((byc - buc) * qty, 2)
        if saving is not None:
            total += saving
        items.append({"type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                      "built": built, "saving": saving})
    if not items:
        return None
    items.sort(key=lambda r: -(r["saving"] or 0.0))
    return {
        "overridden": bool(params.build_reactions_anyway),
        "all": bool(params.buy_all_reactions),
        "categories": sorted(params.buy_reaction_categories),
        "items": items,
        "isk": round(total, 2),
    }


def build_plan(target: int, quantity: int, mfg: dict, rx: dict, prices: dict, adjusted: dict,
               params: BuildParams, names: dict[int, str]) -> dict:
    """Full read-only plan: resolve unit costs, then explode the target quantity top-down into a
    build tree, an aggregated priced shopping list, a job list, and cost + time metrics. The root
    is always built (that's what the user asked to make), even if buying it would be cheaper —
    that comparison is still surfaced on the root node."""
    memo, unit = resolve_unit_costs(mfg, rx, prices, adjusted, params)
    unit(target, frozenset())  # populate the memo from the target down

    shopping: dict[int, float] = {}
    jobs: list[dict] = []
    totals = {"materials_cost": 0.0, "job_cost": 0.0, "job_seconds": 0.0, "leftover_value": 0.0}

    def explode(type_id: int, qty: float, is_root: bool = False) -> dict:
        node = memo[type_id]
        activity, recipe = _producer(type_id, mfg, rx)
        do_build = node["decision"] == "build" or (is_root and node["buildable"])
        if not do_build or recipe is None:
            price = node["buy_unit_cost"]
            line = (price or 0.0) * qty
            if node["decision"] != "unresolved":
                shopping[type_id] = shopping.get(type_id, 0.0) + qty
                totals["materials_cost"] += line
            return {
                "type_id": type_id, "name": names.get(type_id, str(type_id)),
                "decision": "unresolved" if node["decision"] == "unresolved" else "buy",
                "qty": qty, "unit_cost": price, "line_cost": line if price else None,
                "source": node.get("source"),
            }

        mult, st = params.struct_mults_for(type_id, activity)
        output_qty = recipe["output_qty"]
        runs = max(1, math.ceil(qty / output_qty))
        # Runs first: ME/TE depends on how many copies this batch consumes.
        me, te = params.me_te_for(type_id, activity, runs)
        produced = runs * output_qty
        if produced > qty:   # batch-rounding overproduction is reusable inventory, credit it back
            totals["leftover_value"] += (produced - qty) * (node["build_unit_cost"] or 0.0)

        children = []
        eiv = 0.0
        for inp in recipe["inputs"]:
            need = effective_material_qty(inp["quantity"], runs, me, mult)
            # EIV (the job-cost basis) uses BASE quantities × runs — ME never reduces it.
            eiv += inp["quantity"] * runs * adjusted.get(inp["type_id"], 0.0)
            children.append(explode(inp["type_id"], need))
        job_cost = eiv * params.job_fee_rate(type_id, activity)
        skill = params.mfg_skill_time_mult if activity == "manufacturing" else params.rx_skill_time_mult
        job_seconds = recipe["base_time"] * runs * (1 - te / 100.0) * st * skill
        totals["job_cost"] += job_cost
        totals["job_seconds"] += job_seconds
        jobs.append({
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "activity": activity, "runs": runs, "output_qty": output_qty,
            "produced": produced, "job_cost": job_cost, "job_seconds": job_seconds,
        })
        return {
            "type_id": type_id, "name": names.get(type_id, str(type_id)),
            "decision": "build", "activity": activity, "qty": qty, "runs": runs,
            "produced": produced, "excess": produced - qty,
            "unit_cost": node["build_unit_cost"], "buy_unit_cost": node["buy_unit_cost"],
            "job_cost": job_cost, "owned": blueprint_summary(params.owned.get(type_id)),
            "inputs": children,
            # Which of your structures this step was costed in — the rigs there are what the ME/TE
            # above came from, so the number is unreadable without it.
            "site": (params.site_for(type_id, activity) or {}).get("name"),
        }

    tree = explode(target, quantity, is_root=True)

    shopping_list = sorted(
        (
            {
                "type_id": tid, "name": names.get(tid, str(tid)), "qty": qty,
                "unit_price": (prices.get(tid) or {}).get("sell_price"),
                "source": (prices.get(tid) or {}).get("source"),
                "line_cost": ((prices.get(tid) or {}).get("sell_price") or 0.0) * qty,
                # Bought because of the account's standing always-buy rule rather than because
                # building lost on cost — the same flag the queue's list carries, since the two
                # lists render through the same row.
                "blacklisted": tid in params.never_build_ids,
                # …and the same for the account's reaction policy: a standing rule has to be
                # visible where it takes effect, or the plan looks like it got make-or-buy wrong.
                "reaction_policy": bool((memo.get(tid) or {}).get("reaction_policy")),
            }
            for tid, qty in shopping.items()
        ),
        key=lambda r: r["line_cost"], reverse=True,
    )
    unresolved = [s["type_id"] for s in shopping_list if s["unit_price"] is None]
    total_cost = totals["materials_cost"] + totals["job_cost"]
    return {
        "target": {"type_id": target, "name": names.get(target, str(target)), "quantity": quantity},
        "tree": tree,
        "shopping_list": shopping_list,
        "jobs": jobs,
        "metrics": {
            "materials_cost": round(totals["materials_cost"], 2),
            "job_cost": round(totals["job_cost"], 2),
            "total_cost": round(total_cost, 2),
            "leftover_value": round(totals["leftover_value"], 2),
            "net_cost": round(total_cost - totals["leftover_value"], 2),
            "job_count": len(jobs),
            "total_job_hours": round(totals["job_seconds"] / 3600.0, 2),
        },
        "unresolved": unresolved,
        # What the account's reaction policy cost (or, when this build overrides it, saved).
        "reaction_policy": reaction_policy_report(
            memo, params, names,
            [(s["type_id"], s["qty"], False) for s in shopping_list]
            + [(j["type_id"], j["produced"], True) for j in jobs]),
    }


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


def resolve_build_params(context_id: int, me_pct: float, te_pct: float,
                         system_id: int | None, facility_tax_pct: float | None,
                         max_build_hours: float = 0.0,
                         struct_material_pct: float = 0.0, struct_time_pct: float = 0.0,
                         marginal_pct: float | None = None, force_build: bool = False,
                         force_build_ids: list[int] | None = None,
                         margin_pct: float | None = None,
                         never_build_ids: list[int] | None = None) -> BuildParams:
    """Build the resolver's params, auto-deriving the build system + tax from the account's
    Reactions settings when the request didn't override them — so the caller needn't supply a
    system id or tax by hand."""
    d_sid, d_tax, basis = account_build_defaults(context_id, with_basis=True)
    sid = system_id if system_id is not None else d_sid
    tax = facility_tax_pct if facility_tax_pct is not None else d_tax
    if system_id is not None:
        basis = "request"          # the caller named a system; nothing was defaulted
    # Auto per-product ME/TE from the account's real owned blueprints (empty if not connected).
    try:
        from app.industry.blueprints import owned_blueprints, blueprint_coverage
        owned = owned_blueprints(context_id)
        # ...and how much of the account that holding actually covers. Everything that counts PRINTS
        # (how many jobs of a type can run at once) is only allowed to trust it when every character
        # is cached — see BuildParams.prints_known.
        coverage = blueprint_coverage(context_id)
    except Exception:
        owned, coverage = {}, {"characters": 0, "cached": 0, "missing": 0, "complete": False}
    # Reaction formulas the account keeps in a hangar or container instead of a personal blueprint
    # list — counted from ENABLED stock and from the prints their real industry jobs were installed
    # on. Same cap, different evidence; see formula_print_floor for the precedence and the
    # de-duplication.
    stock_prints: dict = {}
    try:
        from app.features import feature_enabled_for
        if feature_enabled_for("industry_formulas_from_stock", context_id):
            from app.industry.blueprints import formula_print_floor
            stock_prints = formula_print_floor(context_id, owned)
    except Exception:
        stock_prints = {}
    # The per-product aggregate is a runs-weighted figure over the WHOLE holding, not the best copy:
    # crediting a 20-run batch with the ME of a 5-run copy under-states its materials. The per-JOB
    # value comes off the copies themselves (BuildParams.me_te_for / build_tasks). This map is only
    # the fallback for a product with no copies to read.
    me_by_product = {p: blend_me_te(o.get("copies") or [o], None, (me_pct, te_pct))
                     for p, o in owned.items()}
    mfg_skill, rx_skill, skill_basis = account_industry_time_mults(context_id, with_basis=True)
    return BuildParams(
        me_pct=me_pct, te_pct=te_pct,
        mfg_skill_time_mult=mfg_skill, rx_skill_time_mult=rx_skill, skill_time_basis=skill_basis,
        mfg_cost_index=fetch_system_cost_index(sid, "manufacturing"),
        rx_cost_index=fetch_system_cost_index(sid, "reaction"),
        facility_tax_pct=tax, me_by_product=me_by_product, owned=owned,
        blueprint_coverage=coverage, stock_prints=stock_prints,
        build_system_id=sid, build_system_basis=basis,
        max_build_hours=max_build_hours,
        struct_material_mult=1.0 - struct_material_pct / 100.0,
        struct_time_mult=1.0 - struct_time_pct / 100.0,
        # User-tunable: how much of the build's value a component must save to be worth building.
        # None keeps the default. This is a genuine time-vs-cost preference the math can't settle,
        # which is why it's a knob at all. min_saving_pct stays 0 (per-component % doesn't scale).
        #
        # force_build drops BOTH shortcuts — the percentage and the absolute floor. Note the floor
        # is why the slider alone can't express this: at 0% the 5m floor still applies, so small
        # components keep getting bought. Building at an outright LOSS is still refused; "ignore
        # marginal savings" means small gains count, not that paying more to build is sensible.
        marginal_pct_of_total=(0.0 if force_build else
                               (MARGINAL_BUILD_PCT_OF_TOTAL if marginal_pct is None
                                else max(0.0, min(25.0, float(marginal_pct))))),
        min_saving_isk=(0.0 if force_build else MIN_BUILD_SAVING_ISK),
        force_build_ids=set(force_build_ids or ()),
        never_build_ids=set(never_build_ids or ()),
        margin_pct=(MARGIN_DEFAULT_PCT if margin_pct is None
                    else max(0.0, min(100.0, float(margin_pct)))),
    )


@dataclass
class PlanInputs:
    """Everything a scheduler run needs, resolved once: recipe graphs, names, live prices and the
    build params + slot pools for this account."""
    mfg: dict
    rx: dict
    names: dict[int, str]
    ids: list[int]
    prices: dict
    adjusted: dict
    params: BuildParams
    pools: dict[str, int]


def prepare_plan_inputs(ctx: int, targets: list[tuple[int, int]], opts: BuildOptions,
                        mfg_slots: int | None = None, rx_slots: int | None = None,
                        missing_recipe_detail=None) -> PlanInputs:
    """Resolve the graphs, prices and parameters for a set of build targets.

    /api/industry/plan and the queue endpoints ran identical 35-line preambles — load both graphs,
    validate the targets, collect the reachable type ids, fetch names, price them, resolve the
    build params, look up blueprint acquisition costs, size the slot pools. Three copies meant a
    fix like the bp_acquire best-effort block had to be made three times (and once wasn't). One
    resolver, and the endpoints are left holding only what actually differs between them.

    `missing_recipe_detail(type_id)` builds the 400 message, since the wording is caller-specific.
    """
    from app.industry.slots import _slot_pool     # local: avoids a graph↔slots import cycle
    from app.industry.settings import apply_account_build_options

    # Anything the caller didn't explicitly set comes from the account's saved build options, so a
    # plan run without a browser (share link, checklist) matches what the user actually builds with.
    opts = apply_account_build_options(ctx, opts)

    con = get_connection()
    try:
        mfg = load_manufacturing_graph(con)
        rx = load_reaction_graph(con)
        ids: set[int] = set()
        for tid, _qty in targets:
            if tid not in mfg and tid not in rx:
                detail = (missing_recipe_detail(tid) if missing_recipe_detail
                          else f"No manufacturing or reaction recipe for type {tid}")
                raise HTTPException(status_code=400, detail=detail)
            ids |= collect_reachable(tid, mfg, rx)
        names = {}
        groups = {}
        # group_id comes along for the ride: it's what decides whether a structure's rigs cover a
        # given job (app.industry.structures), and it is the ONLY taxonomy the SDE build imports —
        # there is no groups table, so no category and no tech level.
        for r in con.execute(
                f"SELECT type_id, name, group_id FROM types WHERE type_id IN ({','.join('?' * len(ids))})",
                tuple(ids)):
            names[r["type_id"]] = r["name"]
            groups[r["type_id"]] = r["group_id"]
    finally:
        con.close()

    id_list = list(ids)
    prices = resolve_market_data(ctx, id_list)
    adjusted = fetch_adjusted_prices(id_list)
    # force_build also drops the speed shortcut: that one buys slow bulk components, which is
    # exactly what someone asking to build everything does not want.
    mbh = 0.0 if opts.force_build else (SPEED_BUILD_CAP_HOURS if opts.prioritize_speed else 0.0)
    params = resolve_build_params(ctx, opts.me_pct, opts.te_pct, opts.system_id,
                                  opts.facility_tax_pct, mbh, opts.struct_material_pct,
                                  opts.struct_time_pct, opts.marginal_pct, opts.force_build,
                                  opts.force_build_ids, opts.margin_pct,
                                  # A target is never blacklisted out of its own build: you asked for
                                  # it. Only components can be forced onto the shopping list.
                                  [t for t in opts.never_build_ids
                                   if t not in {tid for tid, _q in targets}])
    # The account's standing reaction policy. Set on the resolved params rather than threaded
    # through resolve_build_params' positional list, same as job_sites and bp_acquire. `type_groups`
    # is what a produced type is matched to a category by, and it is the map already fetched above —
    # the SDE's only taxonomy, shared with rig routing.
    params.buy_all_reactions = bool(opts.buy_all_reactions)
    params.buy_reaction_categories = {str(k) for k in (opts.buy_reaction_categories or ())}
    params.build_reactions_anyway = bool(opts.build_reactions_anyway)
    params.type_groups = groups
    # The reaction job-length ceiling, in hours. THE one flag gate for the feature, placed here
    # because every plan path resolves its params through this function: with the flag off the field
    # stays 0.0 and the scheduler is byte-for-byte the one that shipped before it existed. Days in,
    # hours out — days is the unit a builder says it in, hours is what the scheduler already thinks
    # in everywhere else.
    if opts.max_reaction_job_days:
        from app.features import feature_enabled_for
        if feature_enabled_for("industry_job_length_policy", ctx):
            params.max_reaction_job_hours = max(0.0, float(opts.max_reaction_job_days) * 24.0)
    # Ordering a reaction is the newer, more specific instruction — you asked for it, so the policy
    # does not get to buy it out of its own build. Same carve-out the blacklist gets above.
    params.reaction_policy_exempt_ids = {tid for tid, _q in targets}
    # What an unowned blueprint would cost to acquire, so building can be priced honestly and the
    # margin-saver can see it. Best-effort: an empty index just leaves the old behaviour.
    try:
        from app.industry.bpc import acquisition_costs, representative_me_te
        params.bp_acquire = acquisition_costs(id_list, params.owned)
    except Exception:
        params.bp_acquire = {}
    # A print you don't own was costed at ME 0 / TE 0 — the un-researched worst case — even though
    # the copy you'd buy is usually researched, and its ME/TE is already sitting in the contract
    # index we just priced against. That inflated both materials and job time on every component
    # bought as a BPC. Owned blueprints still win: your own print is what you'd actually use.
    for tid, info in (params.bp_acquire or {}).items():
        if info.get("kind") != "bpc":
            continue
        me_te = representative_me_te(info)
        if not me_te:
            continue
        # Recorded for EVERY type, owned or not: it is what a batch bigger than the copies you hold
        # is built off, so it has to be known even when the owned copies win the first runs.
        params.buy_me_te[tid] = me_te
        if tid not in params.me_by_product:
            params.me_by_product[tid] = me_te
            params.me_source[tid] = "contract"
    for tid in params.owned:
        params.me_source.setdefault(tid, "owned")
    # The user's own call comes last — they know which print they're really using.
    for key, val in (opts.me_te_overrides or {}).items():
        try:
            tid = int(key)
            me, te = float(val[0]), float(val[1])
        except Exception:
            continue
        params.me_by_product[tid] = (max(0.0, min(10.0, me)), max(0.0, min(20.0, te)))
        params.me_source[tid] = "override"

    # Per-job build site. Resolved LAST: it reads the resolved params (build system, tax, the flat
    # facility bonus) and hands back the routing every downstream number is then computed from.
    # `{}` when the flag is off or the account described no build structure — i.e. today's plan.
    from app.industry.routing import resolve_job_sites
    params.job_sites = resolve_job_sites(ctx, targets, mfg, rx, groups, params, opts.facility_id)

    pool = _slot_pool(ctx)
    pools = {
        "manufacturing": max(1, mfg_slots if mfg_slots is not None else pool["manufacturing_slots"]),
        "reaction": max(1, rx_slots if rx_slots is not None else pool["reaction_slots"]),
    }
    return PlanInputs(mfg=mfg, rx=rx, names=names, ids=id_list, prices=prices,
                      adjusted=adjusted, params=params, pools=pools)


@router.get("/api/industry/search")
def industry_search(q: str, ctx: int = Depends(require_context)):
    """Buildable products (manufacturing or reaction) whose name matches `q` — the product picker.
    Shortest names first so an exact-ish match surfaces above longer variants."""
    q = (q or "").strip()
    if len(q) < 2:
        return {"results": []}
    con = get_connection()
    try:
        # LOWER(...) both sides so the match is case-insensitive on Postgres too (its LIKE is
        # case-sensitive, unlike SQLite's) — otherwise "revelation" finds nothing on prod.
        rows = con.execute(
            "SELECT t.type_id, t.name FROM types t "
            "WHERE LOWER(t.name) LIKE ? AND (t.type_id IN (SELECT product_type_id FROM blueprints) "
            "OR t.type_id IN (SELECT output_type_id FROM reactions)) "
            "ORDER BY LENGTH(t.name), t.name LIMIT 25",
            (f"%{q.lower()}%",),
        ).fetchall()
        return {"results": [{"type_id": r["type_id"], "name": r["name"]} for r in rows]}
    finally:
        con.close()


def _cost_basis(params) -> dict:
    """What the job-installation fee was actually costed against, so a missing system cost index is
    visible instead of silently cheap. Job cost is EIV x (index + facility tax + 4% SCC); with no
    configured build system the index term is 0 and the quote is light by that share."""
    return {
        "system_id": params.build_system_id,
        "mfg_index": params.mfg_cost_index,
        "rx_index": params.rx_cost_index,
        "facility_tax_pct": params.facility_tax_pct,
        # A default has to say it is one — a wrong default is harder to notice than an absent one.
        "basis": params.build_system_basis,
    }


def _plan_on_hand(ctx: int, req) -> dict[int, float]:
    """Stock a single-product plan may net off.

    `source_keys` given = this plan owns its sources and counts those boxes only; absent OR EMPTY =
    the account-wide enabled pool, which is what every plan did before plans owned anything. Empty
    counts as absent for the same reason it does on an order: picking no box says nothing about
    where the materials come from, and answering "then you have nothing" would quietly turn the
    picker into a switch that disables stock netting.
    """
    from app.industry.assets import owned_quantities, source_quantities_multi
    keys = getattr(req, "source_keys", None)
    return source_quantities_multi(ctx, keys) if keys else owned_quantities(ctx)


@router.post("/api/industry/plan")
def industry_plan(req: IndustryPlanRequest, ctx: int = Depends(require_context)):
    """Read-only make-or-buy plan for one product+quantity: build tree, priced shopping list, and
    cost/time metrics. Own-account scoped (pricing follows the account's markets)."""
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    targets = [(req.type_id, req.quantity)]
    inp = prepare_plan_inputs(
        ctx, targets, req,
        missing_recipe_detail=lambda _tid: "No manufacturing or reaction recipe for that type")

    # Schedule the single build across the account's real slot pools (manufacturing + separate
    # reaction pool) for an honest MAKESPAN with parallelism — build_plan alone only sums job time
    # serially, which massively overstates wall-clock for a big build (a Nyx's 46 jobs are mostly
    # parallel). plan_queue gives schedule + batched cost + net-cost; build_plan supplies the tree.
    # Local imports avoid a graph↔schedule/assets import cycle.
    from app.industry.schedule import plan_queue
    # Net off what you already own (never the product itself — you asked to build that), from the
    # sources this plan is entitled to: the ones picked for it if any, else the account's tick list.
    on_hand = _plan_on_hand(ctx, req) if req.use_stock else {}
    on_hand.pop(req.type_id, None)
    result = plan_queue(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params, inp.names,
                        inp.pools, on_hand=on_hand)
    result["target"] = {"type_id": req.type_id, "quantity": req.quantity,
                        "name": inp.names.get(req.type_id, str(req.type_id))}
    result["tree"] = build_plan(req.type_id, req.quantity, inp.mfg, inp.rx, inp.prices,
                                inp.adjusted, inp.params, inp.names)["tree"]
    # Name who installs each job, for every stage — not just the ones startable right now.
    from app.industry.schedule import assign_characters
    from app.industry.slots import _slot_pool
    # Which skills the account is missing to actually INSTALL these jobs. Returns None (and the
    # key is omitted) whenever the feature is off, so a disabled flag costs this endpoint nothing
    # — not a query, not a byte of payload. One call yields both the report and the eligibility
    # that keeps the scheduler from handing a job to someone who can't install it.
    from app.industry.skills import analyze_plan_skills
    sk = analyze_plan_skills(ctx, result.get("requirements") or [], inp.mfg, inp.rx)
    assign_characters(result["schedule"]["waves"], _slot_pool(ctx).get("characters") or [],
                      (sk or {}).get("eligibility"))
    if sk is not None:
        result["skill_gaps"] = sk["gaps"]
    # Whether the job times above came from real scanned skills or the V/V fallback. The number is
    # the same shape either way, so without this the user cannot tell a measurement from a guess.
    result["skill_time_basis"] = inp.params.skill_time_basis
    result["cost_basis"] = _cost_basis(inp.params)
    return result


@router.post("/api/industry/plan_sweep")
def industry_plan_sweep(req: IndustryPlanRequest, ctx: int = Depends(require_context)):
    """Cost + makespan for this product at every stop of the marginal-saving slider.

    The slider is one of the few genuine knobs here, and "3%" says nothing about what it costs you
    — so the UI reads the whole curve once and shows the time saved / ISK spent live as you drag,
    instead of firing a full replan per pixel. `marginal_pct` on the request is ignored: the sweep
    covers every value."""
    if req.quantity < 1:
        raise HTTPException(status_code=400, detail="quantity must be ≥ 1")
    targets = [(req.type_id, req.quantity)]
    # "Build everything" zeroes the threshold AND its floor — that's the point of it, but it would
    # flatten the curve the slider is asking about, so the sweep always resolves params with the
    # normal marginal rule in place.
    req = req.model_copy(update={"force_build": False, "marginal_pct": None})
    inp = prepare_plan_inputs(
        ctx, targets, req,
        missing_recipe_detail=lambda _tid: "No manufacturing or reaction recipe for that type")
    from app.industry.schedule import sweep_marginal
    on_hand = _plan_on_hand(ctx, req) if req.use_stock else {}
    on_hand.pop(req.type_id, None)
    points = sweep_marginal(targets, inp.mfg, inp.rx, inp.prices, inp.adjusted, inp.params,
                            inp.names, inp.pools, on_hand=on_hand)
    return {"points": points}
