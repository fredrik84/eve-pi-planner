"""Feature flags: gate features behind an admin-controlled on/off switch.

We have no separate staging environment, so new (or risky) features ship to production
hidden from the public and visible only to admins. An admin flips a flag from the Admin tab
to roll a feature out to everyone. The flag state lives in `pp_features`; the set of known
features is the code-defined `FEATURE_REGISTRY` (a feature missing from the registry can't be
toggled). Admins always see every feature regardless of its flag (so they can preview); the
public sees a feature only when its flag is enabled. Mirrors the auth/table pattern in admin.py.
"""
from datetime import datetime, timezone

from fastapi import APIRouter, Cookie, Depends, HTTPException
from pydantic import BaseModel

from app.sde import get_connection, ensure_once, add_columns
from app.esi import require_admin, admin_and_tester_status

router = APIRouter()

# key, label, one-line description, and the default public-visibility state. New/experimental
# features default False (admin-only until rolled out); already-shipped features default True
# (visible now, but retrofitted with a flag so an admin can pull them if they misbehave).
# `group` clusters related features in the Admin UI (collapsible sections — see admin.js
# loadAdminFeatures) so the list stays scannable as it grows; pick the app area the feature's
# `description` names first (most descriptions already lead with "On the X tab:" / "On X:").
# GROUP_ORDER below controls display order; a group missing from it sorts last.
FEATURE_REGISTRY = [
    {"key": "timeline", "label": "PI process timeline", "group": "Dashboard",
     "description": "Account-level “you are here” timeline on the Dashboard: "
                    "extractors started → haul P1 → refill factories.",
     "default": False},
    {"key": "split_extraction", "label": "Split extraction", "group": "Planner",
     "description": "Plan two P0s on one planet type (2 ECUs share the 10 heads) and "
                    "reinvest the freed planets into more factories.",
     "default": True},
    {"key": "baskets", "label": "Custom production baskets", "group": "Planner",
     "description": "Pick a custom multi-product basket as a planning target in the product picker.",
     "default": True},
    {"key": "skill_roi", "label": "Skill-ROI advisor", "group": "Setup Analysis",
     "description": "On Setup Analysis: which character skills (Interplanetary Consolidation, "
                    "Command Center Upgrades) to train for more output, ranked by ISK/day.",
     "default": False},
    {"key": "move_character", "label": "Move a character to another account", "group": "Setup Analysis",
     "description": "On Setup Analysis: the 1:1 colony-swap tool for moving a character's PI to a "
                    "character on another account.",
     "default": False},
    {"key": "schedule_sync", "label": "Extractor schedule sync warning", "group": "Dashboard",
     "description": "On the Dashboard: warn when an extractor runs a different program length than "
                    "the rest of the fleet (drifts off your batch restart). Mutable per character.",
     "default": False},
    {"key": "pad_fill", "label": "Fill-factories meter", "group": "Dashboard",
     "description": "On the Dashboard: how far the P1 in your extractor pads would go toward filling "
                    "every factory's 30,000 m³ buffer — a binding-material % + per-material breakdown.",
     "default": False},
    {"key": "dummy_characters", "label": "Placeholder characters", "group": "Characters",
     "description": "On the Characters tab: add placeholder toons (no ESI login) that contribute "
                    "planet slots + CCU level to plans without logging the alt in.",
     "default": False},
    {"key": "factory_layout", "label": "Factory Layout tab", "group": "Planner",
     "description": "Show the Factory Layout tab — generates importable EVE PI templates for any "
                    "P1–P4 product.",
     "default": False},
    {"key": "planet_db", "label": "Planet DB tab", "group": "Planet DB",
     "description": "Show the Planet DB tab — the shared planet density database the planner "
                    "uses; also lets users submit and browse planet data.",
     "default": False},
    {"key": "notifications", "label": "Notifications", "group": "Notifications & Alerts",
     "description": "Let users configure Pushover / ntfy.sh / Discord alerts for extractor "
                    "expiry and factory refill reminders.",
     "default": False},
    {"key": "esi_cache_skip", "label": "ESI cache-aware rescan", "group": "Characters",
     "description": "Skip re-fetching a colony/skills from ESI while its cache (Expires header) "
                    "hasn't lapsed yet — faster rescans — and show a “no new data until” hint "
                    "in the UI.",
     "default": False},
    {"key": "measured_yield", "label": "Measured yield in Planet DB", "group": "Planet DB",
     "description": "On the Planet DB tab: show a real measured average extraction yield "
                    "(pooled across all users' actual colonies) alongside a planet's static "
                    "richness value, where enough samples exist.",
     "default": False},
    {"key": "hybrid_colonies", "label": "Hybrid colony analysis", "group": "Setup Analysis",
     "description": "Track hand-built colonies that run extraction + a P1→P2+ factory chain "
                    "on one planet: surfaces their real demand in Setup Analysis and suggests "
                    "reseats to close their own shortfall (never a redeploy).",
     "default": False},
    {"key": "measured_yield_blend", "label": "Measured yield in planning weights", "group": "Planner",
     "description": "Nudge the planner's extractor placement toward planets with real pooled "
                    "yield data, confidence-weighted by sample count — never overrides the "
                    "static richness value, and does nothing for the ~99% of planets with no "
                    "measured data yet. Separate from the Planet DB display flag since this "
                    "changes real plan output, not just a badge.",
     "default": False},
    {"key": "alert_settings", "label": "Configurable Dashboard alerts", "group": "Notifications & Alerts",
     "description": "Settings → Alerts: customize the extractor-expiry warning window and "
                    "storage-fill warning/severity thresholds used by the Dashboard's colony "
                    "warnings, instead of the fixed defaults.",
     "default": False},
    {"key": "extraction_targets", "label": "Extraction targets reference", "group": "Setup Analysis",
     "description": "On Setup Analysis: each material row shows its P0 source name and a comfortable "
                    "P0/hr-per-planet target at a glance — a reference while reseating extractor heads.",
     "default": False},
    {"key": "redeploy_proximity", "label": "Overlapping extraction ranges", "group": "Setup Analysis",
     "description": "On Setup Analysis: flag when two colonies on the same planet have overlapping "
                    "reachable extraction areas for the same P0 — they compete for the same hotspots "
                    "and reseating can't escape it, so it suggests redeploying one command centre to "
                    "a clear area. Overlap % cutoff is set in Settings → General. Needs a rescan to "
                    "capture extractor positions.",
     "default": False},
    {"key": "redeploy_depletion", "label": "Depleting-deposit redeploy advice", "group": "Setup Analysis",
     "description": "On Setup Analysis: flag an extractor colony whose measured install-yield has "
                    "trended down across the last several programs — the deposit is exhausting, so a "
                    "reseat only chases a sinking ceiling and the fix is to redeploy the CC to a fresh "
                    "planet.",
     "default": False},
    {"key": "reactions", "label": "Reactions tracking", "group": "Reactions",
     "description": "Adds a Reactions tab: ranks the most profitable moon-goo reaction chains, "
                    "priced from your alliance's own price sheet if it has one (see Admin → "
                    "Groups) or live market prices otherwise, to ship and sell. This only "
                    "controls whether the nav tab shows — the tool itself is open to any "
                    "logged-in user regardless of this flag's state, so rolling it to 'public' "
                    "just reveals the tab to everyone.",
     "default": False},
    {"key": "reaction_orders", "label": "Reactions: customer orders", "group": "Reactions",
     "description": "On the Reactions tab: track a fixed-unit order for another player — runs "
                    "needed for the whole chain, materials to import, a cost breakdown (no "
                    "markup applied — you decide what to charge), and a rough time estimate. "
                    "Committing to an order occupies real reaction slots the same way the "
                    "suggestion/manual-assign flow does.",
     "default": False},
    {"key": "local_market", "label": "Reactions: local / alliance market pricing", "group": "Reactions",
     "description": "On the Reactions tab: follow one or more markets (a player structure market "
                    "and/or a public region market) in a priority order and price reactions "
                    "against them, falling back to Jita for anything not listed locally. Adds a "
                    "market & freight setup card and a per-line price-source badge.",
     "default": False},
    {"key": "local_sell_hint", "label": "Reactions: local buy-order sell hint", "group": "Reactions",
     "description": "In the 'reactions completed' alert: if one of your followed local/alliance "
                    "markets has a buy order that beats hauling the output to Jita (after jump-freight "
                    "cost), tell you how much you can sell there and the ISK you'd gain. Read-only — "
                    "no auto-sell. Requires local / alliance market pricing to be set up.",
     "default": False},
    {"key": "industry", "label": "Industry / manufacturing planner", "group": "Industry",
     "description": "Adds an Industry tab: pick a buildable (module up to a capital) and a "
                    "quantity, and it decides build-vs-buy for every component, prices a shopping "
                    "list against your followed markets (local → Jita), and reports what it costs "
                    "and how long it takes. Spans manufacturing AND reactions for deep builds. "
                    "Admin-preview while it's built out.",
     "default": False},
    {"key": "required_skills", "label": "Industry: required skills to build", "group": "Industry",
     "description": "On the Industry plan: which skills the account is MISSING to actually install "
                    "every job in the build tree, and which character comes closest. Needs the "
                    "SDE blueprint_skills table and per-character full skill lists — both only "
                    "populated while this is on, so turning it off costs nothing.",
     "default": False},
    {"key": "industry_share", "label": "Industry: customer build-status links", "group": "Industry",
     "description": "Share a queued build with the person who ordered it: a login-free link showing "
                    "what's being built, which stage it's on, a progress bar and an ETA. Deliberately "
                    "carries no character names, systems or ISK — only the build's own progress. "
                    "Revocable at any time.",
     "default": False},
    {"key": "industry_manual_done", "label": "Industry: mark jobs running or done by hand",
     "group": "Industry",
     "description": "Click a build step to move it on: not started → running → done → back again, "
                    "for work the app can't see — a job run on a character that isn't connected, "
                    "or a component acquired by trade instead of built. Counts as progress and "
                    "never overrides what we can measure; never writes to the earnings ledger, so "
                    "it can't inflate your turnover.",
     "default": False},
    {"key": "industry_blacklist", "label": "Industry: always-buy list", "group": "Industry",
     "description": "Items you never build, however the make-or-buy math comes out. Kept per "
                    "account and applied to every plan; an individual order set to build one of "
                    "them anyway still does.",
     "default": False},
    {"key": "industry_corp_assets", "label": "Industry: corp hangars over ESI", "group": "Industry",
     "description": "Read corp hangars and containers directly instead of pasting them, for "
                    "characters with the Director role — EVE allows nothing weaker. They become "
                    "ordinary opt-in stock sources. Everyone else keeps the paste, which needs no "
                    "role at all.",
     "default": False},
    {"key": "industry_sourcing", "label": "Industry: material sourcing per build", "group": "Industry",
     "description": "Per order: which materials are already gathered and which are still to buy. "
                    "Point an order at the container you're hauling into and it keeps itself up to "
                    "date from your assets; anything we can't see can be ticked off by hand.",
     "default": False},
    {"key": "industry_rig_routing", "label": "Industry: rigs per structure, jobs per rig",
     "group": "Industry",
     "description": "Say what each structure's rigs are FOR (capital ships, advanced components, "
                    "ammunition, …) and the planner picks the right structure per job: only jobs a "
                    "rig actually covers get its ME/TE, and each job's fee follows the system it "
                    "lands in. Or pin a family — capital ships, advanced components — to one "
                    "structure and every job in it is installed there whatever the scoring says. "
                    "The plan names the structure per step. A structure with no families set still "
                    "covers everything.",
     # Starts with the testers, alongside every other Industry feature — this was asked for by the
     # builders using the tab, so admin-only would be a rung nobody needed.
     "default": False, "state": "testers"},
    {"key": "industry_plan_sources", "label": "Industry: each build owns its containers",
     "group": "Industry",
     "description": "Per order: bind SEVERAL containers to a build (grouped by station and system), "
                    "save a reusable named set, and count only those boxes as that build's stock — "
                    "sharing a container between builds becomes a deliberate pick rather than a "
                    "side effect of binding it. Orders queued before this keep using the "
                    "account-wide tick list until you edit them.",
     "default": False, "state": "testers"},
    {"key": "reactions_assign_guard", "label": "Reactions: assigning twice doesn't book it twice",
     "group": "Reactions",
     "description": "Re-assigning the same product to the same character REPLACES that plan entry "
                    "instead of adding a second copy of it, and an assignment that would need more "
                    "reaction slots than the character has is refused rather than silently booked. "
                    "Chain tiers run one after another, so a deep chain is still allowed — only "
                    "jobs that would run at the same moment count against the slots.",
     "default": False, "state": "testers"},
    {"key": "industry_install_skill_aware", "label": "Industry: don't tell you to start a job you can't",
     "group": "Industry",
     "description": "The start-now checklist names a character with a free slot — now ranked by "
                    "who can actually install that job, the same way the schedule below it is. "
                    "Before this the two could disagree: the checklist would say 'start it on X' "
                    "above a plan already marking that job blocked for X.",
     "default": False, "state": "testers"},
    {"key": "industry_placeholder_slots", "label": "Industry: placeholder characters carry job slots",
     "group": "Industry",
     "description": "Placeholder characters can declare how many manufacturing and reaction job "
                    "slots they have, and those slots count toward the Industry planner's pool — "
                    "so an alt army can be planned for without connecting every alt over ESI. "
                    "Own flag because it ADDS capacity to accounts that already use placeholders "
                    "for PI, which changes every plan they run.",
     "default": False},
    {"key": "industry_default_build_system", "label": "Industry: assume a build system when none is set",
     "group": "Industry",
     "description": "Job installation fees include the system cost index, which is 76% of the fee "
                    "in Jita — but it only counts once a build system is configured, and almost "
                    "nobody has. This falls back to the system of a structure you build in, or to "
                    "Jita as a labelled reference when we know nothing, so quotes stop being light "
                    "by that share. The plan always says which it used.",
     "default": False, "state": "testers"},
    {"key": "industry_group_structures", "label": "Industry: build in your group's structures",
     "group": "Industry",
     "description": "Structures shared to your alliance group are available to every member as "
                    "build facilities, so one person describes a building — its hull, rigs, system "
                    "and tax — and nobody else has to add it again. Your own row always wins for a "
                    "structure you have described yourself.",
     "default": False, "state": "testers"},
    {"key": "industry_manual_structures", "label": "Industry: describe a build structure by hand",
     "group": "Industry",
     "description": "Add a structure you build in by typing its name and picking its hull and "
                    "system — no connected character and no structure-search scopes needed. Its "
                    "security (and so its rig bonuses) comes from the system you pick. It can be "
                    "built in like any other structure, but never priced from: reading a "
                    "structure's market needs ESI and a real structure id.",
     "default": False, "state": "testers"},
    {"key": "industry_per_order_plans", "label": "Industry: plan each order on its own",
     "group": "Industry",
     "description": "Plan every queued order separately instead of aggregating the queue into one "
                    "shared batch. A job outputs to exactly ONE container, so a batch shared "
                    "between two builds has nowhere to deliver — this is what lets a builder run a "
                    "container per customer. It costs real ISK: shared components are built once "
                    "per order and blueprint copies bought per order. Compare both plans on the "
                    "real queue before switching.",
     "default": False, "state": "testers"},
    {"key": "industry_job_length_policy", "label": "Industry: cap how long one reaction job runs",
     "group": "Industry",
     "description": "Say the longest a single REACTION job may run (in days) and the planner splits a "
                    "big batch across more of your reaction slots instead of parking 5,000 runs in "
                    "one slot for weeks. A ceiling only ever makes jobs shorter — a consumer's "
                    "deadline still wins, and it can never invent slots or formulas, so where the "
                    "concurrency isn't there the plan honours what it can and says so. Manufacturing "
                    "is untouched: splitting a manufacturing batch spends blueprint COPIES, which "
                    "cost ISK, while a reaction formula is durable and reused.",
     # Testers, like every other Industry feature — the ceiling came from a builder using the tab.
     "default": False, "state": "testers"},
    {"key": "industry_reaction_policy", "label": "Industry: which reactions a build runs",
     "group": "Industry",
     "description": "Say once that this account's builds don't run reactions — or don't run certain "
                    "families of them (composites & intermediates, hybrid polymers, biochemicals) — "
                    "instead of blacklisting every output by hand. Those outputs are bought, their "
                    "sub-steps disappear, and the plan says what buying them instead added to the "
                    "cost. A single order can still be set to make its own.",
     # Testers, like every other Industry feature — this came from the builders using the tab.
     "default": False, "state": "testers"},
    {"key": "industry_build_setup", "label": "Industry: one place for build settings",
     "group": "Industry",
     "description": "Collects every rule that shapes a build — facility, build threshold, reactions, "
                    "never-build list, job length, build pins, stock sources — into a single Build "
                    "setup dialog, instead of eight strips scattered down the plan card that don't "
                    "read as settings. The same dialog edits an order's overrides, showing which "
                    "values it inherits and which this build changed. The strips stay as read-only "
                    "summaries of what is in force, each linking into the section that set it.",
     # Testers, like every other Industry feature. Storage is untouched: this is a surface over the
     # stores that already own each field, not a new settings table (see TODO 18 Half A).
     "default": False, "state": "testers"},
    {"key": "industry_manual_blueprints", "label": "Industry: declare a blueprint or formula by hand",
     "group": "Industry",
     "description": "Type in the blueprints and reaction formulas you own — product, ME, TE, runs "
                    "(blank = an original) and how many — for the prints ESI cannot see. The "
                    "blueprint endpoint is PERSONAL-only, so a builder whose prints live in a corp "
                    "hangar was planned at ME 0 / TE 0 and every material and duration figure was "
                    "wrong. A declaration replaces what ESI read for that product (never adds to "
                    "it, which would double-count), is reported as \"declared\" rather than "
                    "measured, and can say whether to spend the original or the copies.",
     "default": False, "state": "testers"},
    {"key": "industry_formulas_from_stock", "label": "Industry: count formulas in your hangars",
     "group": "Industry",
     "description": "Count reaction formulas found in your enabled stock sources (corp hangars, "
                    "containers, pasted hangars) — and the formulas your own industry jobs were "
                    "installed on — toward how many reactions of a product can run at once. ESI "
                    "only reports PERSONAL blueprints, so formulas kept in a corp container were "
                    "invisible and the plan scheduled parallel jobs off a formula you own one of. "
                    "A pasted hangar wins for the formulas it names. Concurrency only — neither an "
                    "asset row nor a job states ME, TE or runs.",
     "default": False},
    {"key": "reactions_formula_cap", "label": "Reactions: a formula is one reaction at a time",
     "group": "Reactions",
     "description": "On Reactions: cap how many jobs of a product the wizard and customer orders "
                    "plan side by side at the number of formulas you actually hold — a formula is "
                    "locked into the reactor while a job runs on it, so ten free slots and one "
                    "Ferrofluid formula is one job, not ten. Chain tiers are unaffected (they run "
                    "in sequence), and a formula we have no evidence about is never capped.",
     "default": False},
    {"key": "reactions_tidy_runs", "label": "Reactions: round intermediate runs to tidy numbers",
     "group": "Reactions",
     "description": "Round the run count of an INTERMEDIATE reaction up to a number you can type "
                    "without checking — 79 stays 80, 41 becomes 45, 213 becomes 225 — instead of "
                    "making you read an exact figure for every job. Never more than 15% over what "
                    "the chain needs, never applied to the end product (its runs are what the "
                    "cost and profit were computed from), and never rounded down, which would "
                    "leave the next stage short. The extra output is stock, and gets spent by "
                    "your next plan.",
     "default": False},
    {"key": "reactions_use_stock", "label": "Reactions: skip stages you already have the materials for",
     "group": "Reactions",
     "description": "Count intermediates sitting in your enabled stock sources against a chain: "
                    "hold enough of a sub-reaction's output and that stage is shortened or dropped "
                    "outright, along with everything below it, instead of being reacted from goo "
                    "you already spent. What the stock covered is always named, so a stage never "
                    "just disappears. Within one plan a unit is spent once; across two separate "
                    "plans there is no reservation, so the same units can be promised twice.",
     "default": False},
    {"key": "reactions_parallel_stages", "label": "Reactions: don't idle reactors waiting on a stage",
     "group": "Reactions",
     "description": "A chain's stages run one after another, so they reuse the same reactor "
                    "instead of each holding one — free slots now show what you can really start, "
                    "not one fewer for every stage queued behind it. Whatever is still idle once "
                    "the wizard has placed everything is spent splitting the slowest step across "
                    "more jobs, so it finishes sooner. Never changes what gets suggested, what it "
                    "costs or what it earns — only how many reactors do the same work.",
     "default": False},
    {"key": "reactions_level_runs", "label": "Reactions: one run count per product, everywhere",
     "group": "Reactions",
     "description": "Every job of a product carries the SAME run count on every character, instead "
                    "of 125 runs on one and 90 and 75 on the others because three plans sized it "
                    "separately. The work is re-split into as many jobs as that one number needs, "
                    "so a stage also finishes in one go and usually in fewer reactors. A product "
                    "is pooled across your characters — the account's requirement, made wherever "
                    "there are reactors free, which assumes you keep the output somewhere every "
                    "character can reach it. Set the job length on the Reactions card to decide "
                    "how long a batch runs; the surplus from rounding up is stock.",
     "default": False},
    {"key": "reactions_pack_hosts", "label": "Reactions: an order fills one character before it uses two",
     "group": "Reactions",
     "description": "A customer order is placed on the FEWEST characters whose reactors can hold "
                    "it, instead of being shared out across every character with a free slot. The "
                    "jobs are the same jobs and they finish at the same time — parallelism comes "
                    "from reactors, not from characters — so the only thing that changes is how "
                    "many characters you log in to install and collect it. An order spills onto a "
                    "second character exactly when the first runs out of reactors, which is the "
                    "point at which spreading it genuinely does make it finish sooner.",
     "default": False},
    {"key": "reactions_stage_pipeline", "label": "Reactions: stages drawn as a pipeline, like builds",
     "group": "Reactions",
     "description": "The 'to install' list becomes the same pipeline the Industry tab draws for a "
                    "build: columns are the stages, rows are your characters, and each cell is what "
                    "that character installs at that stage — so a stage looks and reads the same "
                    "wherever you meet it. Nothing about the plan changes, only how it is drawn: "
                    "the same jobs, run counts and stage order as the table it replaces, with the "
                    "stages you cannot start yet marked as waiting on the one below.",
     "default": False},
    {"key": "reactions_manual_done", "label": "Reactions: mark a reaction installed or done by hand",
     "group": "Reactions",
     "description": "The same three states an Industry build step has, on your reaction plan: "
                    "click a step to say you have installed it, again when it has finished, and "
                    "once more to take it back. For when ESI cannot tell us — the job cache is up "
                    "to five minutes stale, a job installed under a different product than planned "
                    "matches nothing, and a stage reacted before this tool saw it has no job to "
                    "read — so the page stops saying 'after stage 1 finishes' about a stage that "
                    "finished an hour ago. A mark only ever brings a stage forward: it can never "
                    "hide a job ESI can actually see, and it is never counted as ISK earned.",
     "default": False},
    {"key": "reactions_missing_formulas", "label": "Reactions: formulas you need to acquire",
     "group": "Reactions",
     "description": "Once you have PASTED your industry window, a formula you never declared is "
                    "one you don't own — so the wizard, a manual assign and a customer order say "
                    "which formulas a plan needs that you'd have to buy first, instead of quietly "
                    "planning a sub-reaction you can't install. Reported with a contract price, "
                    "never added to any shopping list or cost. Without a paste nothing changes: "
                    "absence stays 'unknown' and unknown never refuses work.",
     "default": False},
]
GROUP_ORDER = ["Dashboard", "Setup Analysis", "Planner", "Planet DB", "Characters",
               "Notifications & Alerts", "Reactions", "Industry"]
_DEFAULTS = {f["key"]: f for f in FEATURE_REGISTRY}
VALID_STATES = {"hidden", "admin", "testers", "public"}


def _default_state(f: dict) -> str:
    """The rung a feature starts on when its row is first created.

    `default: True/False` is the old two-state shorthand (public / admin-only). A feature may
    instead name its starting rung explicitly with `"state"` — which is how something ships
    straight to the testers who asked for it, rather than landing on `admin` and needing a manual
    flip that only takes effect on whichever environment someone remembered to click.
    """
    state = f.get("state")
    if state in VALID_STATES:
        return state
    return "public" if f.get("default") else "admin"


@ensure_once
def ensure_features_table():
    con = get_connection()
    con.execute(
        """CREATE TABLE IF NOT EXISTS pp_features (
               key        TEXT PRIMARY KEY,
               enabled    INTEGER NOT NULL DEFAULT 0,
               updated_at TEXT
           )"""
    )
    con.commit()
    # Add state column and migrate from boolean enabled if needed
    add_columns(con, "pp_features", "state TEXT")
    con.execute(
        "UPDATE pp_features SET state = CASE WHEN enabled=1 THEN 'public' ELSE 'admin' END "
        "WHERE state IS NULL"
    )
    existing = {r["key"] for r in con.execute("SELECT key FROM pp_features")}
    now = datetime.now(timezone.utc).isoformat()
    for f in FEATURE_REGISTRY:
        if f["key"] not in existing:
            con.execute(
                "INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,?)",
                (f["key"], 1 if f["default"] else 0, _default_state(f), now),
            )
    con.commit()
    con.close()


def _state_of(key: str) -> str:
    """The feature's current rung on the rollout ladder."""
    ensure_features_table()
    con = get_connection()
    row = con.execute("SELECT state FROM pp_features WHERE key=?", (key,)).fetchone()
    con.close()
    if row is not None:
        return row["state"] or "admin"
    return _default_state(_DEFAULTS.get(key, {"default": False}))


def feature_enabled(key: str) -> bool:
    """Is this feature public-visible? (For backend gating — ignores admin/tester roles.)

    Use `feature_enabled_for(key, context_id)` instead wherever the caller knows whose request it
    is: this one answers only "is it public", so a feature parked on `admin` or `testers` reads as
    OFF for everybody, including the admins and testers the rung exists to serve.
    """
    return _state_of(key) == "public"


def feature_enabled_for(key: str, context_id: int | None) -> bool:
    """Backend gate that respects the whole rollout ladder for a KNOWN caller.

    `feature_enabled()` alone made the ladder frontend-only: the Admin tab would show a feature
    rolled out to testers while every server-side gate behind it still answered "off", so the
    feature appeared enabled and did nothing. Anything gated in the backend must ask this instead,
    or its rung on the ladder is decoration.

    hidden → nobody. admin → admins. testers → admins and testers. public → everyone.
    """
    state = _state_of(key)
    if state == "public":
        return True
    if state == "hidden" or not context_id:
        return False
    from app.esi import admin_and_tester_status_for_context
    is_admin, is_tester = admin_and_tester_status_for_context(context_id)
    return is_admin if state == "admin" else (is_admin or is_tester)


@router.get("/api/features")
def list_features(pp_session: str = Cookie(default=None)):
    """Public. Returns every registered feature with its state, plus caller's admin/tester role."""
    ensure_features_table()
    con = get_connection()
    db_state = {r["key"]: r["state"] for r in con.execute("SELECT key, state FROM pp_features")}
    con.close()
    feats = [
        {"key": f["key"], "label": f["label"], "description": f["description"],
         "group": f.get("group") or "Other",
         "state": db_state.get(f["key"]) or _default_state(f)}
        for f in FEATURE_REGISTRY
    ]
    from app.main import GIT_COMMIT
    _admin, _tester = admin_and_tester_status(pp_session)
    return {"features": feats, "group_order": GROUP_ORDER, "is_admin": _admin, "is_tester": _tester,
            "git_commit": GIT_COMMIT}


class FeatureStateUpdate(BaseModel):
    state: str


@router.post("/api/features/{key}")
def set_feature(key: str, req: FeatureStateUpdate, _: int = Depends(require_admin)):
    if key not in _DEFAULTS:
        raise HTTPException(status_code=404, detail="Unknown feature")
    if req.state not in VALID_STATES:
        raise HTTPException(status_code=400, detail=f"Invalid state — must be one of: {', '.join(sorted(VALID_STATES))}")
    ensure_features_table()
    con = get_connection()
    con.execute(
        "INSERT INTO pp_features (key, enabled, state, updated_at) VALUES (?,?,?,?) "
        "ON CONFLICT(key) DO UPDATE SET enabled=excluded.enabled, state=excluded.state, updated_at=excluded.updated_at",
        (key, 1 if req.state == "public" else 0, req.state, datetime.now(timezone.utc).isoformat()),
    )
    con.commit()
    con.close()
    return {"ok": True, "key": key, "state": req.state}
