#!/usr/bin/env python3
"""Reactions is answerable without ever opening Manufacturing.

The manifesto claims Reactions is a business in its own right (`docs/manifesto.md`, "Honest gap"
#2) and on 2026-08-14 that claim was tested and failed on two counts, both structural rather than
cosmetic:

  * **The cadence — the tool's headline setting — was gated on `industry_job_length_policy`.** A
    Manufacturing flag decided whether a Reactions user could say how often they log in, which is
    the one number the whole plan is shaped around.
  * **`reactions_missing_formulas` was unreachable by construction.** It only reports once a PASTED
    industry window makes the library complete, and the only paste form in the product sat inside
    `#indManualBpSubsec`, hidden behind `industry_manual_blueprints`. A Reactions-group flag could
    not fire for anybody who had not been given an Industry one.

And one control was asked for twice: the wizard's "Run on a…" dropdown (hours, never persisted)
beside the card's "Come back every N days" (days, persisted). One rhythm, two questions, one of
them forgotten every time the wizard opened.

**What these tests pin is reachability and singleness, not a rung.** They never assert a flag
equals its code default — every one of them sets the state it needs and restores it (CLAUDE.md
rule 1: an admin may move any of these tomorrow and that must not break a test):

  1. the cadence resolves with NO Industry flag anywhere;
  2. it is ONE stored number — write it from either side, read it from both;
  3. the wizard's dropdown and the stored setting are that same number;
  4. the Reactions paste route reaches the same store, and makes the missing-formula report
     genuinely able to fire with `industry_manual_blueprints` off.

In-process; run inside the container against a NON-PROD database.

    docker compose cp test_reactions_standalone.py web:/srv/app/ && \
      docker compose exec web python3 test_reactions_standalone.py
"""
import re
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                        # noqa: E402
from app.features import ensure_features_table                           # noqa: E402

CTX = -98793
PROD = 16654                    # Titanium Chromide — a real reaction, so the SDE index resolves
PROD_NAME = "Titanium Chromide"
OTHER = 16655                   # Crystallite Alloy — deliberately NOT pasted
OTHER_NAME = "Crystallite Alloy"
BATCH_NAME = "standalone-test window"

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _set_feature_state(key, state):
    """Force a flag to a state and return what it was, so every test restores it. Reading the
    default and asserting against it is the thing rule 1 forbids — an admin owns these."""
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT state FROM pp_features WHERE key=?", (key,)).fetchone()
    if row:
        con.execute("UPDATE pp_features SET state=?, enabled=? WHERE key=?",
                    (state, 1 if state == "public" else 0, key))
    else:
        con.execute("INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,?)",
                    (key, 1 if state == "public" else 0, state, ""))
    con.commit()
    con.close()
    return (row["state"] if row else None) or "admin"


def test_cadence_needs_no_industry_flag():
    """The cadence is Reactions-owned: its own flag opens it, and the Manufacturing flag being off
    is no longer an answer to "how often do you log in"."""
    from app.reactions.jobs import _reaction_cadence_hours
    from app.industry.settings import get_max_reaction_job_days, set_max_reaction_job_days

    was_rx = _set_feature_state("reactions_cadence", "hidden")
    was_ind = _set_feature_state("industry_job_length_policy", "hidden")
    was_days = get_max_reaction_job_days(CTX)
    try:
        set_max_reaction_job_days(CTX, 3.0)

        # Neither flag: still 0. The gate has not simply been deleted — a plan built before anyone
        # chose a cadence must not be resized by one.
        check(_reaction_cadence_hours(CTX) == 0.0,
              "with neither flag on, no cadence is applied at all")

        _set_feature_state("reactions_cadence", "public")
        check(abs(_reaction_cadence_hours(CTX) - 72.0) < 1e-9,
              f"the cadence resolves on the Reactions flag ALONE, with no Industry flag anywhere "
              f"(got {_reaction_cadence_hours(CTX)}h, want 72h)")

        # ...and the accounts already living on the Industry flag keep it. Removing that half would
        # take the control away from every user who has it today.
        _set_feature_state("reactions_cadence", "hidden")
        _set_feature_state("industry_job_length_policy", "public")
        check(abs(_reaction_cadence_hours(CTX) - 72.0) < 1e-9,
              "the Industry flag still opens the same setting, so nobody loses the control")
    finally:
        set_max_reaction_job_days(CTX, was_days)
        _set_feature_state("reactions_cadence", was_rx)
        _set_feature_state("industry_job_length_policy", was_ind)


def test_one_number_two_doors():
    """Write it from Reactions, read it from Industry, and back. Duplicating the storage is the
    failure this pins against: two rhythms that drift apart is exactly the disagreement the whole
    repair is about."""
    from app.reactions.settings import api_get_reaction_cadence, api_set_reaction_cadence
    from app.reactions.settings import ReactionCadenceUpdate
    from app.reactions.jobs import _reaction_cadence_hours
    from app.industry.build_setup import account_setup
    from app.industry.settings import get_max_reaction_job_days, set_max_reaction_job_days

    was_rx = _set_feature_state("reactions_cadence", "public")
    was_ind = _set_feature_state("industry_job_length_policy", "public")
    was_days = get_max_reaction_job_days(CTX)
    try:
        api_set_reaction_cadence(ReactionCadenceUpdate(max_reaction_job_days=5.0), CTX)
        check(get_max_reaction_job_days(CTX) == 5.0,
              "a write through the Reactions endpoint lands in the ONE stored setting")
        check(account_setup(CTX)["job_length"]["max_reaction_job_days"] == 5.0,
              "...and Industry's Build rules reads that same number back")

        # ...and the other direction.
        set_max_reaction_job_days(CTX, 1.5)
        check(api_get_reaction_cadence(CTX)["max_reaction_job_days"] == 1.5,
              "a write through Build rules is what the Reactions card shows")
        check(abs(_reaction_cadence_hours(CTX) - 36.0) < 1e-9,
              "...and what the leveller caps jobs at")

        # Clearing it is a real state, not an error: no ceiling is the documented default.
        api_set_reaction_cadence(ReactionCadenceUpdate(max_reaction_job_days=None), CTX)
        check(get_max_reaction_job_days(CTX) in (None, 0),
              "clearing the cadence clears the one stored value")
        check(_reaction_cadence_hours(CTX) == 0.0, "...and no ceiling is applied")
    finally:
        set_max_reaction_job_days(CTX, was_days)
        _set_feature_state("reactions_cadence", was_rx)
        _set_feature_state("industry_job_length_policy", was_ind)


def test_the_cadence_gate_is_real():
    """The Reactions cadence endpoint is FLAG-GATED, and removing that gate must be loud.

    Not a cosmetic check. The write lands in `max_reaction_job_days`, which `app/industry/graph.py`
    also reads when it schedules a build — so an ungated write on this side reaches across into
    Industry's scheduling. `_cadence_available` returning a hard `True` used to leave this whole
    suite green; it does not any more.
    """
    from fastapi import HTTPException
    from app.reactions.settings import (ReactionCadenceUpdate, api_get_reaction_cadence,
                                        api_set_reaction_cadence)
    from app.industry.settings import get_max_reaction_job_days, set_max_reaction_job_days

    was_rx = _set_feature_state("reactions_cadence", "hidden")
    was_ind = _set_feature_state("industry_job_length_policy", "hidden")
    was_days = get_max_reaction_job_days(CTX)
    try:
        set_max_reaction_job_days(CTX, 4.0)

        # The WRITE is refused. This is the one the gate exists for.
        refused, status = False, None
        try:
            api_set_reaction_cadence(ReactionCadenceUpdate(max_reaction_job_days=9.0), CTX)
        except HTTPException as e:
            refused, status = True, e.status_code
        check(refused and status == 403,
              f"with neither flag on, writing the cadence is REFUSED (got "
              f"{'403' if refused else 'a successful write'})")
        check(get_max_reaction_job_days(CTX) == 4.0,
              "...and the stored value Industry also schedules against is untouched")

        # ...and the READ does not disclose a setting the account may not see.
        payload = api_get_reaction_cadence(CTX)
        check(payload["available"] is False, "the read reports the surface as unavailable")
        check(payload["max_reaction_job_days"] is None,
              f"...and withholds the value rather than leaking it "
              f"(got {payload['max_reaction_job_days']!r})")

        # One flag is enough, and then the same write goes through — proving the refusal above was
        # the gate and not something else failing.
        _set_feature_state("reactions_cadence", "public")
        api_set_reaction_cadence(ReactionCadenceUpdate(max_reaction_job_days=9.0), CTX)
        check(get_max_reaction_job_days(CTX) == 9.0,
              "with the Reactions flag on, the same write succeeds")
    finally:
        set_max_reaction_job_days(CTX, was_days)
        _set_feature_state("reactions_cadence", was_rx)
        _set_feature_state("industry_job_length_policy", was_ind)


def test_an_explicitly_invalid_cadence_is_still_invalid():
    """OMITTED and INVALID are different answers.

    Making `cadence_hours` optional risked quietly turning "0" — bad input that used to get the
    documented empty result — into "a weekly plan", because the resolver can never return <= 0. A
    caller that explicitly asks for a zero-hour window must not be handed a week it never asked for.
    """
    from app.reactions.advisor import SuggestRequest, suggest_reactions

    was_rx = _set_feature_state("reactions_cadence", "public")
    try:
        for bad in (0.0, -5.0):
            res = suggest_reactions(SuggestRequest(isk_budget=1e9, cadence_hours=bad), CTX)
            check(res["suggestions"] == [] and res["totals"]["net_profit"] == 0.0,
                  f"an explicitly sent cadence of {bad:g} is still rejected, not read as 'unspecified'")
    finally:
        _set_feature_state("reactions_cadence", was_rx)


def test_the_wizard_dropdown_is_the_stored_setting():
    """Change one, read the other. The wizard used to hard-code 168h, so the plan it sold you and
    the plan the leveller re-shaped a page later were built around two different weeks."""
    from app.reactions.advisor import _resolve_cadence_hours, SuggestRequest, _DEFAULT_CADENCE_H
    from app.industry.settings import get_max_reaction_job_days, set_max_reaction_job_days

    was_rx = _set_feature_state("reactions_cadence", "public")
    was_days = get_max_reaction_job_days(CTX)
    try:
        # The request no longer carries a hard-coded week. A client that omits it gets the
        # account's rhythm — that is the merge, expressed in the one place both sides meet.
        check(SuggestRequest(isk_budget=1.0).cadence_hours is None,
              "an unspecified cadence is UNSPECIFIED, not a hard-coded 168h")

        set_max_reaction_job_days(CTX, 14.0)
        check(_resolve_cadence_hours(CTX, None) == 336.0,
              f"the wizard sizes batches off the stored cadence "
              f"(got {_resolve_cadence_hours(CTX, None)}h, want 336h)")
        set_max_reaction_job_days(CTX, 2.0)
        check(_resolve_cadence_hours(CTX, None) == 48.0,
              "change the stored setting and the wizard follows it")

        # An explicit pick still wins — the dropdown IS a control, it just starts from the truth.
        check(_resolve_cadence_hours(CTX, 720.0) == 720.0,
              "an explicitly chosen window still wins over the stored one")

        # With no cadence set at all, weekly remains the fallback rather than "no window", which
        # would divide by zero in every per-day figure the wizard reports.
        set_max_reaction_job_days(CTX, None)
        check(_resolve_cadence_hours(CTX, None) == _DEFAULT_CADENCE_H,
              "with nothing stored, the wizard still has a week to size against")
    finally:
        set_max_reaction_job_days(CTX, was_days)
        _set_feature_state("reactions_cadence", was_rx)

    # The other half of "one number" lives in the browser: the dropdown is seeded from the stored
    # value and writes back when changed. Pinned as source, because the alternative is a browser
    # test suite we do not have (TODO 2e-residual) and the wiring is exactly what silently rots.
    html = open("static/index.html").read()
    js = open("static/reactions.js").read()
    sel = html[html.find('id="wizRCadence"'):html.find('id="wizRCadence"') + 300]
    check("_rxWizCadenceChanged" in sel,
          "the wizard's cadence dropdown writes its pick back to the stored setting")
    check("function _rxSyncWizardCadence" in js and "_rxSyncWizardCadence()" in js,
          "...and is seeded from it, rather than defaulting to Weekly every time")
    # Quote style must not be able to defeat this, and neither must a passing mention in a comment.
    # Matched as a CALL — the route inside an api()/apiSend() argument list, either quoting —
    # because "does reactions.js ever say this string" is not the question; "does it ever ask that
    # endpoint" is. `'…build-setup'` with a trailing apostrophe was the old, defeatable form.
    call = lambda path: re.search(
        r"""(api|apiSend)\s*\([^)]*['"]""" + re.escape(path) + r"""['"]""", js)
    check(bool(call("/api/reactions/cadence")),
          "the Reactions cadence control calls its own route")
    check(not call("/api/industry/build-setup"),
          "...and reactions.js asks the Industry build-setup endpoint nowhere, in either quote style")


def _paste_text(*names):
    """One line per formula, in the shape the client's industry window produces: name, ME, TE,
    runs, group — tab separated."""
    return "\n".join(f"{n} Reaction Formula\t0\t0\t-1\tComposite" for n in names)


def test_reactions_paste_route_reaches_the_same_store():
    """The paste is the act that turns "unknown" into "not owned". Before this it could only be
    performed through a Manufacturing feature, so a Reactions flag could not fire for anyone
    without an Industry one."""
    from app.reactions.library import (_library_state, held_formula_products, missing_formulas,
                                       reactions_import_formula_paste, FormulaPaste)
    from app.reactions.graph import request_memo

    # The whole point: the Industry side is OFF for the duration of this test.
    was_manual = _set_feature_state("industry_manual_blueprints", "hidden")
    was_miss = _set_feature_state("reactions_missing_formulas", "public")
    con = get_connection()
    con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
    con.commit()
    con.close()
    try:
        request_memo.reset() if hasattr(request_memo, "reset") else None

        # Nothing pasted: the report must stay silent. Without this the "complete" check below
        # could pass on a library that is complete for everybody, which would be no test at all.
        check(_library_state(CTX)["complete"] is False,
              "with nothing pasted the library is NOT complete — absence stays unknown")

        res = reactions_import_formula_paste(
            FormulaPaste(name=BATCH_NAME, text=_paste_text(PROD_NAME)), CTX)
        imported = res.get("imported") or {}
        check(int(imported.get("prints") or 0) >= 1,
              f"the Reactions route imports the paste (got {imported.get('prints')} prints)")

        # ...into the SAME store the Industry paste writes, under a batch. One library, two doors.
        con = get_connection()
        rows = con.execute("SELECT type_id, COALESCE(batch,'') AS b FROM pp_industry_blueprints "
                           "WHERE context_id=?", (CTX,)).fetchall()
        con.close()
        check(any(int(r["type_id"]) == PROD and r["b"] for r in rows),
              "the rows land in pp_industry_blueprints as a PASTED batch, not hand-typed rows")

        state = _library_state(CTX)
        check(state["complete"] is True,
              "the library reads as complete with industry_manual_blueprints OFF — the feature is "
              "reachable at last")
        check(state["formulas_declared"] >= 1,
              f"...and it counted the formulas ({state['formulas_declared']})")

        # The dangerous direction, and the reason `held_formula_products` had to change too: a user
        # who pastes their whole window and is then told to go and buy every formula in it.
        held = held_formula_products(CTX)
        check(PROD in held,
              "a formula you just pasted counts as one you HOLD, with the Industry flag off")

        report = missing_formulas(CTX, {PROD: 100, OTHER: 100},
                                  names={PROD: PROD_NAME, OTHER: OTHER_NAME})
        missing = {r["type_id"] for r in report["formulas"]}
        check(report["complete"] is True, "the report fires")
        check(OTHER in missing,
              f"...and names the formula that was never pasted ({OTHER_NAME})")
        check(PROD not in missing,
              f"...while never telling you to buy the one you pasted ({PROD_NAME})")
    finally:
        con = get_connection()
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
        con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=?", (CTX,))
        con.commit()
        con.close()
        _set_feature_state("industry_manual_blueprints", was_manual)
        _set_feature_state("reactions_missing_formulas", was_miss)


def test_the_report_is_still_gated():
    """The paste ROUTE is ungated (it is a door to an existing feature). The REPORT is not — it
    still rolls out on `reactions_missing_formulas`, and turning that off must still silence it."""
    from app.reactions.library import missing_formulas, reactions_import_formula_paste, FormulaPaste

    was_manual = _set_feature_state("industry_manual_blueprints", "hidden")
    was_miss = _set_feature_state("reactions_missing_formulas", "hidden")
    try:
        reactions_import_formula_paste(
            FormulaPaste(name=BATCH_NAME, text=_paste_text(PROD_NAME)), CTX)
        report = missing_formulas(CTX, {OTHER: 100}, names={OTHER: OTHER_NAME})
        check(report["formulas"] == [],
              "with its own flag off the missing-formula report is empty, paste or no paste")
    finally:
        con = get_connection()
        con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
        con.execute("DELETE FROM pp_blueprint_paste_unresolved WHERE context_id=?", (CTX,))
        con.commit()
        con.close()
        _set_feature_state("industry_manual_blueprints", was_manual)
        _set_feature_state("reactions_missing_formulas", was_miss)


def main():
    ensure_features_table()
    for t in (test_cadence_needs_no_industry_flag,
              test_one_number_two_doors,
              test_the_cadence_gate_is_real,
              test_an_explicitly_invalid_cadence_is_still_invalid,
              test_the_wizard_dropdown_is_the_stored_setting,
              test_reactions_paste_route_reaches_the_same_store,
              test_the_report_is_still_gated):
        print(f"\n{t.__name__}\n  {(t.__doc__ or '').strip().splitlines()[0]}")
        t()

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print("  - " + f)
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
