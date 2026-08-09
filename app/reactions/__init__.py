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
    library.py   what the account can actually REACT: whether its formula library is complete (a
                 paste says so, nothing else does) and which formulas a plan needs that it does not
                 hold. Reports; never re-plans. Depends on jobs.py's evidence layer at call time.
    advisor.py   the wizard suggestion engine (knapsack over what to run, then bin-packing onto
                 real characters' free slots). Depends on jobs.py, never the reverse.
    orders.py    fixed-unit customer orders (target-quantity client jobs) built on top.

This __init__ is pure wiring, and it does two separate jobs:

  1. **Importing every layer**, in dependency order, because importing a submodule is what registers
     its endpoints on the shared `router`. That is why the bare `from app.reactions import X as _X`
     lines below are load-bearing rather than unused — deleting one silently removes its endpoints
     from the app.
  2. **Re-exporting the handful of names other modules and the tests reach for by package path.**
     That list is deliberately SHORT: anything else should be imported from the submodule that owns
     it (`from app.reactions.jobs import ...`), which is what the package's own code does. A
     re-export here is a second name for the same function and a second thing to keep in step.
"""
# The shared router lives in _router so submodules register endpoints on it without importing this
# __init__ (which imports them — that would be circular).
from app.reactions._router import router  # noqa: F401
# Layers, in dependency order (settings → graph → jobs → library → advisor → orders). Each import
# registers that layer's endpoints; none of them may be dropped as "unused".
from app.reactions import settings as _settings  # noqa: F401
from app.reactions import graph as _graph  # noqa: F401
from app.reactions import jobs as _jobs  # noqa: F401
from app.reactions import library as _library  # noqa: F401
from app.reactions import advisor as _advisor  # noqa: F401
from app.reactions import orders as _orders  # noqa: F401

# The package's public surface: what app.main, app.planner_dashboard and the test suite import by
# package path. Everything else lives on its own layer.
from app.reactions.graph import (  # noqa: F401
    REACTION_ME_REDUCTION, _resolve_reachable, _value_reaction_batch, _load_goo_and_reached,
    _explode_shopping_list, _explode_chain_tiers,
)
from app.reactions.jobs import get_industry_jobs  # noqa: F401

