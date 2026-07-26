"""Industry / manufacturing make-or-buy planner (see docs/industry-planner-spec.md).

Given a target buildable + quantity, decides build-vs-buy for every component, prices a shopping
list off the account's markets (local → Jita, reusing the Reactions pricing stack), and reports
cost + time metrics. The recipe graph spans manufacturing blueprints AND reactions so capital /
T2 / T3 builds that mix both are costed honestly.

Structured to mirror app/reactions/: the shared `router` lives in _router.py so submodules can
register on it without a circular import through this package.

This package exports exactly one name — `router`, which app.main mounts. The submodule imports
below exist for their SIDE EFFECT: importing a submodule runs its @router decorators and thereby
registers its endpoints. Everything else imports what it needs straight from the module that
defines it (`from app.industry.graph import build_plan`), which is why there's no re-export list
here — the one this file used to carry named 25 symbols that no caller ever imported from it, so
it was pure surface area to keep in sync.
"""
from app.industry._router import router  # noqa: F401 — mounted by app.main

from app.industry import (  # noqa: F401 — imported for endpoint registration only
    graph,          # /api/industry/plan, /api/industry/search
    slots,          # /api/industry/slots
    blueprints,     # /api/industry/blueprints[/refresh]
    jobs,           # /api/industry/jobs[/refresh], /api/industry/lifetime
    orders,         # the build queue: /api/industry/orders*, queue-plan, to-install
    assets,         # /api/industry/assets*
    bpc,            # /api/industry/bpc[/scan]
    progress,       # /api/industry/progress
)
