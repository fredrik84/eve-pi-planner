"""Industry (manufacturing) make-or-buy engine — Phase 1.

Split from one 1,383-line module (TODO 34). The public surface is unchanged: every name defined
across this package is re-exported here, private ones included — `app/reactions/graph.py` imports
`_fallback_build_system`, and `test_cost_basis.py` reaches for `_default_system_on`.

Read `scripts/symbols.sh app/industry/graph` (the DIRECTORY) for the map."""

from app.industry.graph.params import (  # noqa: F401
    BuildParams,
    REACTION_ME_REDUCTION,
    SCC_SURCHARGE_PCT,
    blend_me_te,
    blueprint_summary,
)

from app.industry.graph.sde import (  # noqa: F401
    _GRAPH_CACHE,
    _GRAPH_TTL,
    _cached_graph,
    _load_manufacturing_graph,
    _load_reaction_graph,
    _producer,
    clear_graph_cache,
    collect_reachable,
    effective_material_qty,
    load_manufacturing_graph,
    load_reaction_graph,
)

from app.industry.graph.costs import (  # noqa: F401
    build_plan,
    reaction_policy_report,
    resolve_unit_costs,
)

from app.industry.graph.options import (  # noqa: F401
    BuildOptions,
    IndustryPlanRequest,
    MARGINAL_BUILD_PCT_OF_TOTAL,
    MARGIN_DEFAULT_PCT,
    MIN_BUILD_SAVING_ISK,
    SPEED_BUILD_CAP_HOURS,
    _REFERENCE_SYSTEM_ID,
    _default_system_on,
    _fallback_build_system,
    account_build_defaults,
    account_industry_time_mults,
)

from app.industry.graph.resolve import (  # noqa: F401
    PlanInputs,
    _ACCOUNT_CACHE,
    _ACCOUNT_TTL,
    _account_snapshot,
    clear_account_snapshot,
    prepare_plan_inputs,
    resolve_build_params,
)

from app.industry.graph.routes import (  # noqa: F401
    _cost_basis,
    _plan_on_hand,
    industry_plan,
    industry_plan_sweep,
    industry_search,
)

from app.industry.graph import (  # noqa: F401  submodules, for tests that patch a module attribute
    params,
    sde,
    costs,
    options,
    resolve,
    routes,
)
