# Contributing to EVE PI Planner

Thanks for considering a contribution. This doc is the short version of "what we expect from a
PR" — for deep implementation detail (how the planner algorithm works, table schemas, etc.) see
[CLAUDE.md](CLAUDE.md).

## Design philosophy

1. **Minimize planet interactions, maximize automation.** The whole point of this tool is that
   the player does less manual PI busywork, not more. The best UI is **read-only**: compute and
   show an answer rather than adding a knob. Only add a configurable field when the math genuinely
   can't decide for the user — if you're adding a checkbox, ask first whether it could instead be
   inferred or defaulted.
2. **Static data first, live data when it matters.** Prefer SDE/Fuzzwork (static, doesn't change
   per-player) over ESI calls. Use ESI for live per-character state. If a value can be reliably
   derived from a known formula, compute it — don't scrape/store what you can calculate.
3. **No ads, no tracking, no third-party data sharing.** No analytics scripts, ad networks, or
   third-party JS. No user data (characters, systems, plans, usage patterns) goes anywhere except
   this app's own database, ESI (CCP's official API), and Fuzzwork (static game data). This is a
   hard line, not a style preference — PRs that add any of the above will be rejected outright.

## Privacy is a requirement, not a nice-to-have

Every endpoint that returns character names, systems, planets, or anything locatable **must** be
gated by `require_context` (caller's own data only) or `require_admin`. Default to session-scoped
for anything new. Never add a publicly reachable endpoint that returns per-user data — including
aggregates that could be re-identified. See the "Access control" and "Share privacy" sections of
CLAUDE.md for the patterns already in place (anonymized shares, admin gating, etc.) — follow them
rather than inventing a new access pattern.

## Code style

- **Backend:** Python 3.12, FastAPI. Reuse existing helpers and write generic endpoints — but
  don't bolt `if mode == ...` branches onto a shared endpoint to serve two callers. Prefer a clean
  shared helper called by two thin endpoints, or a parameter that's genuinely orthogonal, over a
  flag that forks the function body.
- **Frontend:** vanilla JS, no framework, no build step. Functions are global and load-order
  matters — see the "Frontend JS is split across files" note in CLAUDE.md before adding a new JS
  file. Cache-busting is automatic: `index.html` ships `?v=dev` and the server stamps the running
  build's commit onto every asset URL, so there is no version number to bump by hand.
- Match the existing tone: dense, comment-sparse code; comments explain *why* (a gotcha, a
  calibration, a non-obvious constraint), not *what*.

## New features ship behind a flag

There's no staging environment — the feature-flag system (`app/features.py`) is how we stage.
Any **new** feature should be added to `FEATURE_REGISTRY` defaulting to `False` (admin-preview),
then rolled out publicly from the Admin → Features tab once it's been run against real data.
Bug fixes and hot-patches to *existing* features don't need a flag — fix them in place.

## Testing

Run `tests/test_distribution.py` (planner correctness) and `tests/test_features.py` (feature-flag/API surface)
against a running instance before opening a PR touching the planner or public API:

```bash
python tests/test_distribution.py --url http://localhost:8000
python tests/test_features.py --url http://localhost:8000
```

Add to these, or add a new `tests/test_*.py` in the same plain-`urllib`/`--url` style, for new endpoints.
Assert durable invariants (things that are always true), not runtime state an admin can change —
e.g. don't assert a feature flag's enabled value equals its code default.

## Commit messages

Single line, no body: `feat: add X`, `fix: correct Y`, `chore: bump Z`. Keep it to what changed
and why in one sentence — the diff shows the how.

## Opening a PR

- Keep it scoped — one feature or fix per PR is easier to review and roll back if needed.
- If your change touches anything user-data-adjacent, call out in the PR description how it's
  gated (which `Depends(...)` guard, or why it's safe to be public).

## Questions / bugs

Use the in-app **Report bug** button, or open a [GitHub issue](https://github.com/fredrik84/eve-pi-planner/issues).
