"""
Moon-goo reaction profitability + job-tracking tool (see app/moon_goo.py for the group-scoped
alliance price sheet it reads from). NOT part of the PI planner's extractor/factory distribution
algorithm — a separate, read-only advisory tool.

This package is the former single-file app/reactions.py, split into layers (each depends only on
those above it — a strict DAG, no cycles):

    settings.py  group + per-account pricing settings (shipping/collateral, reaction system, tax,
                 time efficiency). effective_reaction_settings() is what everything prices with.
    graph.py     the reaction graph + pricing: cost every reachable product from priced leaves,
                 _value_reaction_batch() (the single source of truth for batch economics), the
                 opportunity ranking, and shopping lists.
    jobs.py      live ESI industry-job tracking, the persistent plan (slots/orphans) and
                 per-character slot capacity.
    advisor.py   the wizard suggestion engine (knapsack over what to run, then bin-packing onto
                 real characters' free slots). Depends on jobs.py, never the reverse.
    orders.py    fixed-unit customer orders (target-quantity client jobs) built on top.

This __init__ is pure wiring: it imports each layer (which registers its endpoints on the shared
`router` from _router.py) and re-exports the public surface — `router` (mounted by app.main),
`get_industry_jobs` (used by app.planner), and the internals the test suite pins.
"""
# The shared router lives in _router so submodules register endpoints on it without importing this
# __init__ (which imports them — that would be circular). Each `from ... import` below also executes
# the submodule, registering its endpoints, and re-exports its public names for the package surface.
from app.reactions._router import router  # noqa: F401
from app.reactions.settings import (  # noqa: F401 — re-exported for the package's public surface
    _RXS_DEFAULTS, _GLOBAL_SETTINGS_GROUP_ID,
    ensure_reaction_settings_table, get_reaction_settings, ensure_account_reaction_settings_table,
    _account_reaction_settings_override, effective_reaction_settings, ReactionSettingsUpdate,
    _resolve_system_id, _validate_reaction_system,
)
# Graph/pricing layer — importing it registers its endpoints (opportunities/fuel-blocks/shopping-
# list) and re-exports the recipe/cost helpers this module's jobs+orders code (and the tests) use.
from app.reactions.graph import (  # noqa: F401 — re-exported for the package's public surface
    SCC_SURCHARGE_PCT, REACTION_ME_REDUCTION,
    _load_reaction_graph, _resolve_reachable, _value_reaction_batch, _load_goo_and_reached,
    _build_opportunities, _build_opportunities_uncached, _fuel_block_ids,
    _explode_shopping_list, _explode_chain_tiers, _materials_report,
)
# Jobs/plan layer — importing it registers those endpoints and re-exports the helpers the
# customer-order endpoints below build on (slot allocation, assignment inserts, capacities).
from app.reactions.jobs import (  # noqa: F401 — re-exported for the package's public surface
    get_industry_jobs, reaction_slots, refresh_industry_jobs, fetch_industry_jobs,
    fetch_corp_industry_jobs, ensure_industry_jobs_table, ensure_reaction_assignments_table,
    ensure_reaction_orders_table, _insert_assignment_rows, _character_capacities,
    _allocate_and_insert, _unplanned_running_totals,
    assign_reaction, adopt_orphan_job, unassign_reaction, unassign_all_reactions,
    ChainTier, AssignRequest, AdoptOrphanRequest,
)
# Suggestion engine — imported after jobs (it depends on it, never the reverse). Importing
# registers /api/reactions/suggest.
from app.reactions.advisor import (  # noqa: F401
    _suggest_reactions, _build_advisor, SuggestRequest,
)
# Orders layer (top of the stack) — importing registers the customer-order endpoints.
from app.reactions import orders as _orders  # noqa: F401

