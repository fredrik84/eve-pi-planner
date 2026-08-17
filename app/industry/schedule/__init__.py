"""Industry planner — Phase 2: demand aggregation + the parallel-slot scheduler.

Split from one 2,056-line module (TODO 34). The public surface is unchanged: every name this
package's modules define is re-exported here, so `from app.industry.schedule import X` works
exactly as it did, private names included.

Read `scripts/symbols.sh app/industry/schedule` (the DIRECTORY) for the map — the old
`symbols.sh app/industry/schedule.py` no longer resolves.

Pure and deliberately I/O-free: every function takes prebuilt graphs + params, and no module here
owns an endpoint, DB handle or market lookup."""

from app.industry.schedule.demand import (  # noqa: F401
    _depths,
    aggregate_demand,
    marginal_threshold,
)

from app.industry.schedule.splitting import (  # noqa: F401
    Task,
    _ALIGN_FLOOR,
    _DELIVERY_OVERSHOOT,
    _PACE_OVERSHOOT,
    _align_cohorts,
    _balanced,
    _copy_limits,
    _jobs_on_copies,
    _packed_duration,
    _packed_jobs,
    _print_limits,
    _tightest,
)

from app.industry.schedule.tasks import (  # noqa: F401
    build_tasks,
)

from app.industry.schedule.scheduler import (  # noqa: F401
    _built_deps,
    _critical_priority,
    _fifo_priority,
    order_ranks,
    schedule,
)

from app.industry.schedule.plan import (  # noqa: F401
    MARGINAL_SWEEP_PCTS,
    _finish_of,
    _job_length_limits,
    _sites_used,
    assign_characters,
    plan_queue,
    skill_tier,
    sweep_marginal,
)

from app.industry.schedule.per_order import (  # noqa: F401
    _merge_reaction_reports,
    _order_cost,
    _order_params,
    plan_queue_per_order,
)

from app.industry.schedule import (  # noqa: F401  submodules, for tests that patch a module attribute
    demand,
    splitting,
    tasks,
    scheduler,
    plan,
    per_order,
)
