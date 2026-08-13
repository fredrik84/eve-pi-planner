"""Feature-flag lookup for the reactions package.

Plumbing module, like `_router` — every layer may import it, it imports no layer, so there is no
cycle. `app.features` is imported lazily inside the call for the same reason it always was: the
features registry reads the DB, and a flag check must never be what breaks a request. An
unreachable/failing registry reads as OFF, which is the pre-feature behaviour by construction
(CLAUDE.md rule 2).

Each layer keeps its own named predicate (`_tidy_runs_on`, `_pack_hosts_on`, …) — the docstrings on
those are the rollout rationale for that specific flag and are load-bearing. This only removes the
five identical lines underneath each one.
"""


def flag_on(key: str, context_id: int) -> bool:
    try:
        from app.features import feature_enabled_for
        return bool(feature_enabled_for(key, context_id))
    except Exception:
        return False
