# Production workflow contract

Reactions and Manufacturing should behave alike wherever the game rules are alike. They should not
share a conditional planner that conceals genuine differences between formulas and blueprints.

## Shared behavior

- Metrics lead each Overview and remain visible.
- A successful create/add action closes its modal, refreshes the affected plan, returns to Overview,
  and reports the outcome once.
- “More” menus are transient: actions close them and reloads never restore them open.
- Capacity is reported as `capacity.reservation_model` plus named `capacity.pools`. Each pool has
  `total`, `available`, and `committed`; old flat fields remain only as migration compatibility.
- Automatic work reports a capacity shortfall instead of silently dropping work. Manual management
  remains an escape hatch, not the normal path.
- Reaction-job cadence has one effective value across both tools. An unset/cleared value is the
  seven-day default; the advisor and every post-assignment/background planning pass use that same
  value, so a weekly preview cannot turn into longer jobs when Overview refreshes.
- Ready Manufacturing reaction work crosses the boundary as a linked reaction order. Reactions
  owns its priority, chain expansion, cadence, reservations and runtime matching; Manufacturing
  keeps later waves scheduled until their dependencies make them ready.
- Linked demand follows safe lifecycle rules: quantity changes reconcile down only as far as work
  already committed, terminal Manufacturing orders release pending reservations, and an ESI-running
  reaction is preserved with a visible decision instead of being silently orphaned.
- Aggregated Manufacturing keeps its shared-batch savings. The build trees attribute each ready
  reaction's runs across the orders that consume it in `pp_reaction_order_sources`; Reactions still
  receives one physical order, while resize/cancel can remove one owner's share without guessing.

## Intentional differences

| Concern | Reactions | Manufacturing |
| --- | --- | --- |
| Capacity model | `reserved`: a saved assignment claims a reactor before ESI sees installation | `scheduled`: queued future waves do not reserve today’s EVE slots |
| Concurrency item | One formula per simultaneous reaction job | One blueprint/formula per simultaneous job, with copy-run limits |
| Dependency flow | Sequential reaction tiers; complete chains stay with a host where required | Routed manufacturing/reaction DAG; waves can span characters and structures |
| Recurrence | Customer cadence can create and assign another reaction batch | No recurring manufacturing-order behavior exists yet |
| Placement | Reaction host capacity and formula availability | Skills, manufacturing/reaction pools, blueprint limits, and structure rig routing |

## Refactoring boundary

Share pure game-rule arithmetic, response shapes, navigation, refresh outcomes, warnings, and other
workflow plumbing. Keep advisor/graph/allocation engines inside their activity packages. If sharing
requires `if mode == reactions` branches through the body, the abstraction is at the wrong level.
