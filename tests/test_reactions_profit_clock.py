#!/usr/bin/env python3
"""Reactions: profit truth and the clock (WS1 of docs/reactions-repair-2026-08.md).

Three invariants, in the order the repair spec ranks them.

  1. **No user-facing profit field derives from `sell_price`.** CLAUDE.md has said this since before
     the code that broke it existed, and three live surfaces drifted past it unnoticed for days —
     the completions ledger (lifetime net profit), the dashboard tiles, and the running-job modal.
     A rule nothing enforces is a rule that gets re-broken, so this is a SOURCE scan over the whole
     `app/reactions` package: every sell-side reference must be on an explicit allowlist with a
     recorded reason. A new one fails the test. This is the most valuable assertion in the file.

  2. **With a measurement present, the quoted duration uses it.** `time_efficiency_pct` used to be
     a hand-typed 0%-by-default knob while `reaction_time_mult_for` already measured the real
     multiplier off ESI job durations — two clocks, and every user-facing duration read the wrong
     one (~2.14x slow). Pinned end to end: a measured job -> the derived setting -> the graph's
     effective cycle_time.

  3. **The wizard's totals equal the dashboard's for the same plan.** Both must apply one rule —
     makespan = sum over stages of the longest job in each — via the same function.

Durable invariants, not runtime state: nothing here asserts a market price, a flag's rung, or an
account's actual numbers.

In-process; run inside the container against a NON-PROD database.

    docker compose cp tests/test_reactions_profit_clock.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_reactions_profit_clock.py
"""
import ast
import json
import sys
import time

sys.path.insert(0, ".")

FAKE_CTX = 777931
FAKE_CID = 990931

_fails = 0


def check(cond: bool, msg: str) -> bool:
    global _fails
    if not cond:
        _fails += 1
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    return bool(cond)


# ── 1. The pricing guard ───────────────────────────────────────────────────────────────────────
#
# The rule: sell prices may price what you BUY, may charge courier collateral, and may be reported
# as explicitly-labelled market context — they may never REACH a field the user reads as profit or
# as achievable value.
#
# **Enforced semantically, not by grepping a function name.** The first version of this test keyed
# on the enclosing `def` and allow-listed whole functions, which meant a sell-priced term injected
# into an already-listed function (`_plan_totals`, say) stayed green — and `get_industry_jobs`, the
# function the audit found three of its five disagreements in, was exempted wholesale. What is
# checked now is every *assignment or dict entry whose target is a profit field*, against whether
# its expression draws on the sell side: the `sell_price` argument, a market row's `sell_price`, or
# the sell-side keys of a `_value_reaction_batch` result (`output_value` / `net_profit`) read off a
# variable that call actually returned into. Drift inside any function fails.
#
# `test_the_guard_can_fail` below proves the detector on synthetic source, because a guard nobody
# has watched fail is a guard nobody knows works.

# Fields a user reads as profit or achievable value. A key not on this list (`sell_order_value`,
# `net_profit_order`, `instant_sell_value`, `job_fees`…) is free to be sell-priced — that is the
# market-context half of open question (b), and the labels are what keep it honest.
_PROFIT_SINKS = {
    "net_profit", "output_value", "out_val", "reward", "row_output_value", "instant_value",
    "net_profit_per_day", "profit_per_day", "pending_net_profit", "pending_output_value",
    "total_output_value", "turnover", "net_profit_instant", "isk_committed", "total_cost",
}
_SELL_RESULT_KEYS = {"output_value", "net_profit"}

# The only places a sell-side expression may reach a profit field, each with the reason. Keyed
# (file, function, sink field) — as narrow as the language allows, so an exemption cannot cover a
# second violation that happens to land in the same function.
_SELL_SINK_EXEMPTIONS = {
    ("graph.py", "_value_reaction_batch", "output_value"):
        "the one place the sell/buy split is computed — this IS the sell figure, and the same "
        "call returns instant_value/net_profit_instant beside it",
    ("graph.py", "reactions_job_detail", "net_profit"):
        "the no-live-price fallback: with m None no buy_price is passed, sell_price is 0.0, so "
        "v['net_profit'] there is cost-only and `priced: false` tells the modal to say so",
}

# Sell-priced profit fields that are KNOWN wrong and not yet fixed. Empty as of 2026-08-14 — the
# completions ledger, the dashboard tiles, the job modal and the adopted-orphan row were the four,
# and `get_industry_jobs`' dead per-row `output_value` was deleted with them. The count is pinned
# to a literal below precisely so adding an entry cannot pass unnoticed.
_KNOWN_OPEN: dict[tuple[str, str, str], str] = {}
_MAX_KNOWN_OPEN = 0


def _batch_result_vars(fn) -> set[str]:
    """Names that hold a `_value_reaction_batch(...)` result inside this function."""
    out = set()
    for n in ast.walk(fn):
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Call):
            f = n.value.func
            name = f.id if isinstance(f, ast.Name) else getattr(f, "attr", None)
            if name == "_value_reaction_batch":
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        out.add(t.id)
    return out


def _sell_side_reason(node, batch_vars: set[str]) -> str | None:
    """Why this expression draws on the sell side of the order book, or None."""
    for n in ast.walk(node):
        if isinstance(n, ast.Name) and n.id == "sell_price":
            return "the sell_price argument"
        if isinstance(n, ast.keyword) and n.arg == "sell_price":
            return "sell_price= passed on"
        if isinstance(n, ast.Subscript) and isinstance(n.slice, ast.Constant) \
                and n.slice.value == "sell_price":
            return 'a market row\'s ["sell_price"]'
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                and n.args and isinstance(n.args[0], ast.Constant) \
                and n.args[0].value == "sell_price":
            return 'a market row\'s .get("sell_price")'
        if isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) \
                and n.value.id in batch_vars and isinstance(n.slice, ast.Constant) \
                and n.slice.value in _SELL_RESULT_KEYS:
            return f'{n.value.id}["{n.slice.value}"] — the batch\'s SELL figure'
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get" \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in batch_vars \
                and n.args and isinstance(n.args[0], ast.Constant) \
                and n.args[0].value in _SELL_RESULT_KEYS:
            return f'{n.func.value.id}.get("{n.args[0].value}") — the batch\'s SELL figure'
    return None


def _sink_name(target) -> str | None:
    if isinstance(target, ast.Name):
        return target.id
    if isinstance(target, ast.Subscript) and isinstance(target.slice, ast.Constant):
        return target.slice.value
    if isinstance(target, ast.Attribute):
        return target.attr
    return None


def _sink_pairs(node) -> list[tuple]:
    """(sink field, expression) for every assignment target and dict entry on this node."""
    pairs = []
    if isinstance(node, (ast.Assign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            if isinstance(t, ast.Tuple):
                for i, el in enumerate(t.elts):
                    v = (node.value.elts[i]
                         if isinstance(node.value, ast.Tuple) and i < len(node.value.elts)
                         else node.value)
                    pairs.append((_sink_name(el), v))
            else:
                pairs.append((_sink_name(t), node.value))
    elif isinstance(node, ast.Dict):
        for k, v in zip(node.keys, node.values):
            if isinstance(k, ast.Constant):
                pairs.append((k.value, v))
    return pairs


def _scan_source(src: str, fname: str) -> list[tuple]:
    """Every (file, function, sink, reason, line) where a sell-side expression feeds a profit field."""
    tree = ast.parse(src)
    found = []
    for fn in [n for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
        bvars = _batch_result_vars(fn)
        for node in ast.walk(fn):
            for sink, expr in _sink_pairs(node):
                if sink not in _PROFIT_SINKS or expr is None:
                    continue
                why = _sell_side_reason(expr, bvars)
                if why:
                    found.append((fname, fn.name, sink, why, getattr(expr, "lineno", 0)))
    return found


def test_pricing_guard() -> None:
    print("\ntest_pricing_guard — no user-facing profit field derives from sell_price")
    import os
    pkg = os.path.join("app", "reactions")
    found = []
    for fname in sorted(os.listdir(pkg)):
        if fname.endswith(".py"):
            found += _scan_source(open(os.path.join(pkg, fname), encoding="utf-8").read(), fname)

    unexplained = [f for f in found
                   if (f[0], f[1], f[2]) not in _SELL_SINK_EXEMPTIONS
                   and (f[0], f[1], f[2]) not in _KNOWN_OPEN]
    for fl, fn, sink, why, line in unexplained:
        print(f"    VIOLATION {fl}:{line} in {fn}() — `{sink}` is fed by {why}")
    check(not unexplained,
          "no profit field in app/reactions is fed a sell-side expression (a failure here means a "
          "profit figure has drifted back to sell orders — read docs/reactions-repair-2026-08.md "
          "1a before exempting anything)")

    still_open = [f for f in found if (f[0], f[1], f[2]) in _KNOWN_OPEN]
    for fl, fn, sink, why, line in still_open:
        print(f"    still open: {fl}:{line} in {fn}() — `{sink}` via {why}")
    # Pinned to a LITERAL, not to len(_KNOWN_OPEN) — comparing the dict against itself is a
    # tautology that passes however many entries someone adds, which is how the first version of
    # this assertion could never fail.
    check(len(_KNOWN_OPEN) <= _MAX_KNOWN_OPEN,
          f"the known-open list holds at most {_MAX_KNOWN_OPEN} entr"
          f"{'y' if _MAX_KNOWN_OPEN == 1 else 'ies'} (got {len(_KNOWN_OPEN)})")
    # An exemption that no longer matches anything is a comment pretending to be a rule.
    live = {(f[0], f[1], f[2]) for f in found}
    stale = [k for k in _SELL_SINK_EXEMPTIONS if k not in live]
    check(not stale, f"every exemption still describes real code (stale: {stale})")

    # ...and the positive half: the shared valuation function really does net the FULL cost base
    # against buy orders, which is what every repriced site now reads.
    from app.reactions.graph import _value_reaction_batch
    node = {"unit_cost": 100.0, "job_cost": 5.0}
    settings = {"export_isk_per_m3": 10.0, "export_collateral_pct": 0.01}
    v = _value_reaction_batch(node, total_out=1000, sell_price=200.0, volume=2.0,
                              settings=settings, buy_price=150.0)
    check(abs(v["instant_value"] - 150_000.0) < 1e-6, "instant_value is total_out x BUY price")
    check(abs(v["fixed_costs"] - (100_000 + 5_000 + 20_000 + 2_000)) < 1e-6,
          "fixed_costs is materials + job fees + freight + collateral (the full base)")
    check(abs(v["net_profit_instant"] - (150_000 - 127_000)) < 1e-6,
          "net_profit_instant nets buy value against the FULL cost base")
    check(v["net_profit_instant"] < v["net_profit"],
          "instant profit is below order profit whenever buy < sell — the two are not interchangeable")


def test_the_guard_can_fail() -> None:
    """A guard nobody has watched fail is a guard nobody knows works.

    Each case is a real drift shape: repricing a tile, repricing the ledger, and reading the sell
    key off the batch inside a function that legitimately calls it for other reasons. The last one
    is exactly what the first version of this test missed."""
    print("\ntest_the_guard_can_fail — the detector fires on injected drift")
    cases = {
        "a tile repriced to a market row's sell price":
            'def f(m, runs, oq):\n    out = {}\n    out["output_value"] = runs * oq * m["sell_price"]\n',
        "the ledger's turnover taken from the batch's sell figure":
            'def f(node, n, s, vol, st):\n    v = _value_reaction_batch(node, n, s, vol, st)\n'
            '    out_val = v["output_value"]\n    return out_val\n',
        "profit read off the batch's sell key inside a function that also uses it properly":
            'def _plan_totals(node, n, s, vol, st, out):\n'
            '    v = _value_reaction_batch(node, n, s, vol, st, buy_price=1.0)\n'
            '    out["job_fees"] += v["job_cost"]\n'
            '    out["net_profit"] += v["net_profit"]\n',
        "a profit-per-day rate built on sell_price":
            'def f(sell_price, runs, hours):\n    net_profit_per_day = runs * sell_price / hours\n'
            '    return net_profit_per_day\n',
    }
    for label, src in cases.items():
        hits = _scan_source(src, "synthetic.py")
        check(bool(hits), f"caught: {label}")
    # ...and does NOT fire on the shapes that are deliberately allowed, or it would be useless.
    clean = {
        "sell-order value kept as labelled market context":
            'def f(node, n, s, vol, st):\n    v = _value_reaction_batch(node, n, s, vol, st, buy_price=2.0)\n'
            '    return {"sell_order_value": v["output_value"], "net_profit_order": v["net_profit"]}\n',
        "a purchasable material priced at what it costs to acquire":
            'def f(m, freight):\n    purchasable = m["sell_price"] + freight\n    return purchasable\n',
        "profit netted from the plan's own already-instant totals":
            'def f(out):\n    out["net_profit"] = out["output_value"] - out["total_cost"]\n',
    }
    for label, src in clean.items():
        hits = _scan_source(src, "synthetic.py")
        check(not hits, f"quiet on: {label}")


# ── 2. The clock ───────────────────────────────────────────────────────────────────────────────

def _clock_fixture(mult: float):
    """A context whose only reaction job ran at exactly `mult` of its raw SDE cycle."""
    from app.db import get_connection
    from app.reactions.jobs import ensure_industry_jobs_table, _reaction_cycle_times
    ensure_industry_jobs_table()
    cyc = _reaction_cycle_times()
    if not cyc:
        return None
    tid = sorted(cyc)[0]
    raw_h = cyc[tid]
    runs = 10
    jobs = [{"job_id": 1, "product_type_id": tid, "runs": runs,
             "duration": raw_h * runs * mult * 3600.0, "status": "active"}]
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (FAKE_CID,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
        con.execute(
            "INSERT INTO pp_characters (character_id, context_id, character_name) VALUES (?,?,?)",
            (FAKE_CID, FAKE_CTX, "Clock Fixture"))
        con.execute(
            "INSERT INTO pp_char_industry_jobs (character_id, jobs_json, fetched_at) VALUES (?,?,?)",
            (FAKE_CID, json.dumps(jobs), time.time()))
        con.commit()
    finally:
        con.close()
    return tid, raw_h


def _clock_cleanup():
    from app.db import get_connection
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_char_industry_jobs WHERE character_id=?", (FAKE_CID,))
        con.execute("DELETE FROM pp_characters WHERE character_id=?", (FAKE_CID,))
        try:
            con.execute("DELETE FROM pp_industry_settings WHERE context_id=?", (FAKE_CTX,))
        except Exception:
            pass
        con.commit()
    finally:
        con.close()


def test_clock_uses_the_measurement() -> None:
    print("\ntest_clock_uses_the_measurement — a measured job sets the clock every duration reads")
    from app.db import get_connection
    from app.reactions.jobs import reaction_time_mult_for
    from app.reactions.settings import (derived_time_efficiency, effective_reaction_settings,
                                        _invalidate_reaction_settings_cache)
    from app.reactions.graph import _load_reaction_graph

    MULT = 0.4680                       # the measured Carbon Fiber figure from docs/reactions.md
    fx = _clock_fixture(MULT)
    if fx is None:
        print("  SKIP: no `reactions` rows in this database")
        return
    tid, raw_h = fx
    try:
        _invalidate_reaction_settings_cache(FAKE_CTX)
        measured = reaction_time_mult_for(FAKE_CTX)
        check(abs(measured - MULT) < 1e-3,
              f"reaction_time_mult_for reads the measurement back ({measured:.4f} ~ {MULT})")

        pct, source = derived_time_efficiency(FAKE_CTX)
        check(source == "measured", f"the derivation reports its source as a measurement (got {source!r})")
        check(abs(pct - (1.0 - MULT)) < 1e-3,
              f"time efficiency is 1 - multiplier ({pct:.4f} ~ {1 - MULT:.4f})")

        eff = effective_reaction_settings(FAKE_CTX)
        check(abs(eff["time_efficiency_pct"] - (1.0 - MULT)) < 1e-3,
              "effective_reaction_settings serves the DERIVED figure, not the typed 0% default")
        check(eff.get("time_efficiency_source") == "measured",
              "...and says so, so a surface can tell a measurement from an estimate")

        # The end of the chain: this is the number the graph builds every user-facing duration from.
        con = get_connection()
        try:
            by_out, _ = _load_reaction_graph(con, eff["time_efficiency_pct"])
        finally:
            con.close()
        quoted_h = by_out[tid][0]["cycle_time"] / 3600.0
        check(abs(quoted_h - raw_h * MULT) < 1e-3,
              f"the graph's quoted cycle time IS the measured one ({quoted_h:.3f}h, "
              f"raw {raw_h:.3f}h) — not the raw SDE clock")
        check(quoted_h < raw_h * 0.9,
              "...and is materially faster than raw SDE, which is the whole defect (~2.14x)")

        # An explicit override still wins — the knob survives as an override and only as one.
        eff2 = dict(eff)
        _invalidate_reaction_settings_cache(FAKE_CTX)
        from app.reactions import settings as _st
        real = _st._account_reaction_settings_override
        _st._account_reaction_settings_override = lambda ctx: {
            "import_isk_per_m3": 0.0, "export_isk_per_m3": 0.0, "export_collateral_pct": 0.0,
            "reaction_system": None, "facility_tax_pct": 0.0, "time_efficiency_pct": 0.25}
        try:
            over = effective_reaction_settings(FAKE_CTX)
        finally:
            _st._account_reaction_settings_override = real
            _invalidate_reaction_settings_cache(FAKE_CTX)
        check(abs(over["time_efficiency_pct"] - 0.25) < 1e-9 and over["time_efficiency_source"] == "override",
              "a typed non-zero value still overrides the measurement, and is labelled as one")
        assert eff2  # keep the earlier reading referenced for clarity
    finally:
        _clock_cleanup()
        _invalidate_reaction_settings_cache(FAKE_CTX)


def test_clock_override_is_one_clock() -> None:
    """A typed override must move BOTH clocks, not just the graph's.

    This is the two-clocks defect surviving in the path a user reaches for deliberately. The graph
    reduces `cycle_time` by `time_efficiency_pct`; the leveller, the order cadence split and the
    per-product ceiling all size jobs off `reaction_time_mult_for` / `_reaction_time_mult`. Before
    2026-08-14 the override moved the first and not the second, so a typed 53.2% gave the graph
    0.468 against the leveller's 0.85 — a 1.82x disagreement, and every plan split ~1.8x finer than
    the window the account had just said it could fill.

    The invariant is the equality, not either value: **the multiplier the leveller sizes against is
    1 − the time efficiency the graph reduces by, always.**"""
    print("\ntest_clock_override_is_one_clock — an override moves the leveller's clock too")
    from app.reactions import settings as _st
    from app.reactions.jobs import reaction_time_mult_for, _reaction_time_mult
    from app.reactions.settings import (effective_reaction_settings,
                                        _invalidate_reaction_settings_cache)

    MULT = 0.4680
    fx = _clock_fixture(MULT)
    if fx is None:
        print("  SKIP: no `reactions` rows in this database")
        return
    real = _st._account_reaction_settings_override
    try:
        for typed in (0.20, 0.532, 0.75):
            _st._account_reaction_settings_override = lambda ctx, _p=typed: {
                "import_isk_per_m3": 0.0, "export_isk_per_m3": 0.0, "export_collateral_pct": 0.0,
                "reaction_system": None, "facility_tax_pct": 0.0, "time_efficiency_pct": _p}
            _invalidate_reaction_settings_cache(FAKE_CTX)
            graph_te = effective_reaction_settings(FAKE_CTX)["time_efficiency_pct"]
            for label, got in (("reaction_time_mult_for", reaction_time_mult_for(FAKE_CTX, 16654)),
                               ("_reaction_time_mult", _reaction_time_mult(FAKE_CTX))):
                check(abs(got - (1.0 - graph_te)) < 1e-9,
                      f"typed {typed:.3f}: {label} = {got:.4f} = 1 - graph's {graph_te:.4f} "
                      f"(measurement {MULT} is deliberately NOT used)")
        # ...and with the override cleared, both fall back to the measurement together.
        _st._account_reaction_settings_override = lambda ctx: None
        _invalidate_reaction_settings_cache(FAKE_CTX)
        graph_te = effective_reaction_settings(FAKE_CTX)["time_efficiency_pct"]
        check(abs(reaction_time_mult_for(FAKE_CTX, 16654) - (1.0 - graph_te)) < 1e-9
              and abs(reaction_time_mult_for(FAKE_CTX, 16654) - MULT) < 1e-3,
              "and with no override both clocks are the MEASUREMENT, still equal to each other")
        # The measurement-only path must stay blind to the override, or the settings resolver that
        # derives the efficiency from it would be reading its own output back.
        _st._account_reaction_settings_override = lambda ctx: {
            "import_isk_per_m3": 0.0, "export_isk_per_m3": 0.0, "export_collateral_pct": 0.0,
            "reaction_system": None, "facility_tax_pct": 0.0, "time_efficiency_pct": 0.9}
        _invalidate_reaction_settings_cache(FAKE_CTX)
        check(abs(_reaction_time_mult(FAKE_CTX, _derive=False) - MULT) < 1e-3,
              "the measurement-only path ignores the override — no loop through the resolver")
    finally:
        _st._account_reaction_settings_override = real
        _clock_cleanup()
        _invalidate_reaction_settings_cache(FAKE_CTX)


# ── 3. Wizard totals vs dashboard totals ───────────────────────────────────────────────────────

def test_wizard_and_dashboard_reconcile() -> None:
    print("\ntest_wizard_and_dashboard_reconcile — one makespan rule, two surfaces")
    from app.reactions.advisor import sequential_makespan_hours
    from app.reactions import jobs as J
    from app.reactions import graph as G

    # A two-stage chain: stage 0 runs two jobs side by side (10h and 6h), stage 1 runs one (8h).
    # Sequential stages, parallel within a stage -> 10 + 8 = 18h, and nothing else.
    TOP, MID_A, MID_B = 5001, 5002, 5003
    reached = {
        TOP:   {"unit_cost": 100.0, "job_cost": 4.0, "cycle_time": 3600,
                "via": {"output_qty": 1, "inputs": [{"type_id": MID_A, "quantity": 1},
                                                    {"type_id": MID_B, "quantity": 1}]}},
        MID_A: {"unit_cost": 40.0, "job_cost": 1.0, "cycle_time": 3600,
                "via": {"output_qty": 1, "inputs": []}},
        MID_B: {"unit_cost": 30.0, "job_cost": 1.0, "cycle_time": 3600,
                "via": {"output_qty": 1, "inputs": []}},
    }
    rows = [
        {"type_id": MID_A, "runs": 10, "tier_order": 0, "order_id": None},
        {"type_id": MID_B, "runs": 6,  "tier_order": 0, "order_id": None},
        {"type_id": TOP,   "runs": 8,  "tier_order": 1, "order_id": None},
    ]
    cycle_hours = {TOP: 1.0, MID_A: 1.0, MID_B: 1.0}
    out_qty = {TOP: 1.0, MID_A: 1.0, MID_B: 1.0}
    market = {TOP: {"sell_price": 500.0, "buy_price": 400.0},
              MID_A: {"sell_price": 60.0, "buy_price": 50.0},
              MID_B: {"sell_price": 60.0, "buy_price": 50.0}}
    types = {TOP: {"volume": 1.0}, MID_A: {"volume": 1.0}, MID_B: {"volume": 1.0}}
    settings = {"export_isk_per_m3": 10.0, "export_collateral_pct": 0.02}

    saved = (J._load_goo_and_reached, J.reaction_stock_pool, J.effective_reaction_settings,
             G._plan_materials)
    J._load_goo_and_reached = lambda ctx, *a, **k: ({}, reached, {}, {}, types)
    J.reaction_stock_pool = lambda ctx: {}
    J.effective_reaction_settings = lambda ctx: dict(settings)
    G._plan_materials = lambda subset, r, pool, in_house: {9001: 1000.0}
    reached[9001] = {"unit_cost": 1.0}
    try:
        totals = J._plan_totals(0, rows, {}, cycle_hours, out_qty, market)
    finally:
        (J._load_goo_and_reached, J.reaction_stock_pool, J.effective_reaction_settings,
         G._plan_materials) = saved

    top_row = {"reward": 0.0}
    wizard_steps = [
        {"tier": 0, "runs": 10, "jobs": 1, "cycle_hours": 1.0, "top_row": top_row, "row": {}},
        {"tier": 0, "runs": 6,  "jobs": 1, "cycle_hours": 1.0, "top_row": top_row, "row": {}},
        {"tier": 1, "runs": 8,  "jobs": 1, "cycle_hours": 1.0, "top_row": top_row, "row": top_row},
    ]
    wizard_hours = max(sequential_makespan_hours(wizard_steps).values(), default=0.0)

    check(abs(wizard_hours - 18.0) < 1e-9,
          f"the wizard's makespan sums stage maxima, chain tiers included (got {wizard_hours}h, want 18h)")
    check(abs(totals["makespan_hours"] - 18.0) < 1e-9,
          f"the dashboard's makespan applies the same rule (got {totals['makespan_hours']}h)")
    check(abs(totals["makespan_hours"] - wizard_hours) < 1e-9,
          "THE RECONCILIATION: the wizard and the dashboard agree about the same plan")

    # ...and while this fixture is up, the other half of WS1: profit is netted against the FULL
    # cost base and valued at buy orders. TOP is the only end product (both mids are consumed).
    check(abs(totals["output_value"] - 8 * 400.0) < 1e-6,
          "output value is the end product at BUY orders, intermediates excluded")
    check(abs(totals["sell_order_value"] - 8 * 500.0) < 1e-6,
          "...with the sell-order figure kept separately as market context")
    check(totals["total_cost"] > totals["materials_committed"],
          "the cost base profit is netted against is larger than the shopping list")
    check(abs(totals["total_cost"] - (totals["materials_committed"] + totals["job_fees"]
                                      + totals["freight"] + totals["collateral"])) < 1e-6,
          "total cost = materials + job fees + freight + collateral")
    check(abs(totals["net_profit"] - (totals["output_value"] - totals["total_cost"])) < 1e-6,
          "profit is derived from the FULL cost, never from the materials-only figure")
    check(abs(totals["net_profit_per_day"] - totals["net_profit"] / (18.0 / 24.0)) < 1e-6,
          "profit per day divides by the makespan, one definition for the whole plan")


def test_rate_and_ranking_share_one_window_rule() -> None:
    """The displayed ISK/day and the reason a chain is ranked where it is must be the same rule.

    The wizard briefly carried two: the sort key and the LP objective divided by `depth` (a window
    COUNT), while the rate divided by `max(cadence, chain_hours)` (job TIME). On the common shape —
    a deep chain whose intermediates are short, since only the top batch is cadence-capped — those
    disagree badly: 3 windows against ~1.29, so the rate read 2.33x high on a chain the ranking was
    already discounting by 3.

    `_earning_window_h` is the one rule. A stage costs a whole window even when its jobs are short,
    because the next stage cannot start until the player logs in again."""
    print("\ntest_rate_and_ranking_share_one_window_rule — one definition of 'how many windows'")
    from app.reactions.advisor import _earning_window_h

    CAD = 168.0
    # The reported shape: 3 sequential stages, short intermediates, one week-long top batch.
    chain_h = 216.7
    win = _earning_window_h(chain_h, 3, CAD)
    check(abs(win - 3 * CAD) < 1e-9,
          f"a 3-stage weekly chain earns over 3 windows, not over its job time "
          f"({win:.1f}h, not {max(CAD, chain_h):.1f}h)")
    check(abs(win / CAD - 3) < 1e-9,
          "so the rate's divisor in windows IS the ranking's divisor — the two cannot disagree")
    # The old rule, kept here as the thing that must never come back.
    old = max(CAD, chain_h)
    check(abs(win / old - 2.33) < 0.01,
          f"and it is {win / old:.2f}x more conservative than dividing by job time alone")

    # Job time still wins when the work genuinely runs longer than the rhythm — the rule is a max,
    # not a substitution, or a slow single-stage batch would report a rate it cannot sustain.
    slow = _earning_window_h(900.0, 1, CAD)
    check(abs(slow - 900.0) < 1e-9,
          f"a single stage running 900h earns over 900h, not over one 168h window (got {slow})")
    # Depth 1 is unpenalised, and no cadence means no rhythm to bound anything.
    check(abs(_earning_window_h(100.0, 1, CAD) - CAD) < 1e-9,
          "a shallow batch that finishes early still earns over the window it idles through")
    check(abs(_earning_window_h(100.0, 3, 0.0) - 100.0) < 1e-9,
          "with no cadence set there is no rhythm — the chain's own time is the whole answer")


def test_order_invoice_counted_once() -> None:
    """1e: a tier whose only consumer was trimmed away must not book a second slice of the invoice."""
    print("\ntest_order_invoice_counted_once — an order's price is apportioned once")
    from app.reactions import jobs as J
    from app.reactions import graph as G

    TOP, ORPHAN_TIER = 6001, 6002
    reached = {
        TOP: {"unit_cost": 10.0, "job_cost": 0.0, "cycle_time": 3600,
              "via": {"output_qty": 1, "inputs": []}},
        ORPHAN_TIER: {"unit_cost": 5.0, "job_cost": 0.0, "cycle_time": 3600,
                      "via": {"output_qty": 1, "inputs": []}},
    }
    # The order's top product sits at stage 1; the stranded tier at stage 0 is no longer eaten by
    # anything, so `_plan_intermediates` does not see it as consumed.
    rows = [
        {"type_id": ORPHAN_TIER, "runs": 5, "tier_order": 0, "order_id": 42},
        {"type_id": TOP, "runs": 10, "tier_order": 1, "order_id": 42},
    ]
    order_meta = {42: {"client_price": 1_000_000.0, "top_level_runs": 10}}
    cycle_hours = {TOP: 1.0, ORPHAN_TIER: 1.0}
    out_qty = {TOP: 1.0, ORPHAN_TIER: 1.0}
    market = {TOP: {"sell_price": 1.0, "buy_price": 1.0},
              ORPHAN_TIER: {"sell_price": 1.0, "buy_price": 1.0}}
    settings = {"export_isk_per_m3": 0.0, "export_collateral_pct": 0.0}

    saved = (J._load_goo_and_reached, J.reaction_stock_pool, J.effective_reaction_settings,
             G._plan_materials)
    J._load_goo_and_reached = lambda ctx, *a, **k: ({}, reached, {}, {}, {})
    J.reaction_stock_pool = lambda ctx: {}
    J.effective_reaction_settings = lambda ctx: dict(settings)
    G._plan_materials = lambda subset, r, pool, in_house: {}
    try:
        totals = J._plan_totals(0, rows, order_meta, cycle_hours, out_qty, market)
    finally:
        (J._load_goo_and_reached, J.reaction_stock_pool, J.effective_reaction_settings,
         G._plan_materials) = saved

    # One full invoice (10/10 assigned) plus 5 units of stranded stock at 1 ISK — never 2m.
    check(abs(totals["output_value"] - 1_000_005.0) < 1e-6,
          f"the invoice is booked once, not twice (got {totals['output_value']:,.0f}, want 1,000,005)")


if __name__ == "__main__":
    test_pricing_guard()
    test_the_guard_can_fail()
    test_clock_uses_the_measurement()
    test_clock_override_is_one_clock()
    test_wizard_and_dashboard_reconcile()
    test_rate_and_ranking_share_one_window_rule()
    test_order_invoice_counted_once()
    print(f"\n{'ALL PASS' if not _fails else str(_fails) + ' FAILURE(S)'}")
    sys.exit(1 if _fails else 0)
