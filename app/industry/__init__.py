"""Industry / manufacturing make-or-buy planner (see docs/industry-planner-spec.md).

Given a target buildable + quantity, decides build-vs-buy for every component, prices a shopping
list off the account's markets (local → Jita, reusing the Reactions pricing stack), and reports
cost + time metrics. The recipe graph spans manufacturing blueprints AND reactions so capital /
T2 / T3 builds that mix both are costed honestly.

Phase 1 (this): read-only cost engine + `/api/industry/plan`. Later phases add scheduling, the
slot system, a persistent queue, alerting, and spawning real reaction orders into the Reactions
service. Structured to mirror app/reactions/: the shared `router` lives in _router.py so submodules
register on it without a circular import through this __init__.
"""
from app.industry._router import router  # noqa: F401 — mounted by app.main
from app.industry.graph import (  # noqa: F401 — importing registers /api/industry/plan
    SCC_SURCHARGE_PCT, REACTION_ME_REDUCTION, BuildParams,
    load_manufacturing_graph, load_reaction_graph, collect_reachable,
    effective_material_qty, resolve_unit_costs, build_plan, IndustryPlanRequest,
)
from app.industry.schedule import (  # noqa: F401 — importing registers /api/industry/plan-queue
    aggregate_demand, build_tasks, schedule, plan_queue, Task,
    IndustryQueueRequest, QueueTarget,
)
from app.industry.slots import (  # noqa: F401 — importing registers /api/industry/slots
    manufacturing_slots, reaction_slots, _slot_pool,
)
from app.industry.blueprints import (  # noqa: F401 — registers the blueprint auto-read endpoints
    ensure_char_blueprints_table, owned_blueprints, fetch_character_blueprints,
)
from app.industry.jobs import (  # noqa: F401 — registers the live manufacturing-job endpoints
    ensure_manufacturing_jobs_table, running_counts, running_jobs, fetch_manufacturing_jobs,
    ensure_manufacturing_completions_table, log_manufacturing_completions,
    log_all_manufacturing_completions,
)
from app.industry import orders as _orders  # noqa: F401 — registers the build-queue endpoints
from app.industry.assets import (  # noqa: F401 — registers the asset endpoints
    ensure_asset_tables, owned_quantities, refresh_assets, list_sources, set_sources,
    add_pasted_source, delete_source,
)
from app.industry.bpc import (  # noqa: F401 — registers the blueprint-contract endpoints
    ensure_bpc_tables, bpc_prices, maybe_scan,
)
from app.industry import progress as _progress  # noqa: F401 — registers /api/industry/progress
