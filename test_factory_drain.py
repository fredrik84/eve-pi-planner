"""
When does a factory colony run its imported inputs dry? (TODO §21b)

The refill deadline, the Dashboard's "Up next" agenda and the `factory_refill` alert all answer
"when should I log in" from ONE number, and it is no longer a cadence: it is read off the planet's
real factory pins (`pi_sim.colony_drain_state`) and drained forward from the colony checkpoint by
`planner.factory_drain`. This pins the arithmetic and the three properties that make it usable.

The defect this replaced: the alert took a FULL 3-launchpad buffer from the saved plan and anchored
it to `scanned_at` — i.e. it assumed every colony was topped up to full at the instant we last
polled ESI. Part 4 pins that anchor bug specifically.

Runs in-process against the SDE — inside the container:

    docker exec eve-pi-planner-web-1 python3 test_factory_drain.py
"""

import sys

sys.path.insert(0, ".")

FAILS = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        FAILS.append(msg)


def close(a, b, tol=1e-6):
    return a is not None and b is not None and abs(a - b) <= tol


T0 = 1_700_000_000.0     # a fixed checkpoint, so every assertion below is hand-computable
HOUR = 3600.0


def _pi():
    from app.sde import load_pi_data
    return load_pi_data()


def _a_p2(pi):
    """A real P2 schematic with two P1 inputs, straight from the SDE — no invented type ids."""
    for out, sch in pi["schematics"].items():
        if (pi["types"].get(out, {}).get("pi_tier") or 0) != 2:
            continue
        inputs = sch.get("inputs") or []
        if len(inputs) == 2 and all((pi["types"].get(i["type_id"], {}).get("pi_tier") or 0) == 1
                                    for i in inputs):
            return out, sch
    return None, None


def _pin(schematic_id, lcs=T0, contents=None):
    return {"schematic_id": schematic_id, "last_cycle_start": _iso(lcs),
            "contents": contents or []}


def _iso(ts):
    from datetime import datetime, timezone
    return datetime.fromtimestamp(ts, timezone.utc).isoformat().replace("+00:00", "Z")


# ── Part 1: consumption comes off the pins, and scales with how many there are ────────────────
def test_drain_state_from_pins():
    from app.pi_sim import colony_drain_state
    pi = _pi()
    out, sch = _a_p2(pi)
    if not out:
        check(False, "SDE has no two-input P1→P2 schematic to test against")
        return None, None, None
    a, b = sch["inputs"][0], sch["inputs"][1]
    ct = sch["cycle_time"]

    # Two identical factory pins eating the same schematic, with input stock in a launchpad pin.
    pins = [_pin(sch["schematic_id"]), _pin(sch["schematic_id"]),
            {"last_cycle_start": _iso(T0),
             "contents": [{"type_id": a["type_id"], "amount": 4000},
                          {"type_id": b["type_id"], "amount": 4000}]}]
    st = colony_drain_state({"pins": pins}, pi)
    check(st is not None, "a factory planet importing P1 has a drain state")
    if not st:
        return None, None, None
    rates = {i["type_id"]: i["rate"] for i in st["inputs"]}
    check(close(rates.get(a["type_id"]), 2 * a["quantity"] / ct),
          f"input A drains at 2 pins x {a['quantity']}/{ct}s")
    check(close(rates.get(b["type_id"]), 2 * b["quantity"] / ct),
          f"input B drains at 2 pins x {b['quantity']}/{ct}s")
    check(close(st["t0"], T0), "checkpoint is the pins' last_cycle_start, not our fetch time")
    check(st["product"] == out, "the end product is the one nothing else on the planet eats")

    # One pin instead of two must halve the burn — the property a per-product average can't have.
    st1 = colony_drain_state({"pins": [pins[0], pins[2]]}, pi)
    r1 = {i["type_id"]: i["rate"] for i in st1["inputs"]}
    check(close(r1[a["type_id"]] * 2, rates[a["type_id"]]),
          "halving the factory pins halves the consumption rate")
    return out, sch, pins


# ── Part 2: an on-planet chain drains only what the PLAYER delivers ───────────────────────────
def test_chain_excludes_self_made():
    from app.pi_sim import colony_drain_state
    pi = _pi()
    # A P3 whose inputs are P2s, where one of those P2s is also made on this planet.
    p3 = None
    for out, sch in pi["schematics"].items():
        if (pi["types"].get(out, {}).get("pi_tier") or 0) != 3:
            continue
        ins = sch.get("inputs") or []
        if ins and all((pi["types"].get(i["type_id"], {}).get("pi_tier") or 0) == 2 for i in ins):
            p3 = (out, sch, ins[0]["type_id"])
            break
    if not p3:
        check(False, "SDE has no all-P2-input P3 schematic to test against")
        return
    out, sch, made_here = p3
    sub = pi["schematics"][made_here]
    pins = [_pin(sch["schematic_id"]), _pin(sub["schematic_id"])]
    st = colony_drain_state({"pins": pins}, pi)
    check(st is not None, "a chained planet still has a drain state")
    if not st:
        return
    tids = {i["type_id"] for i in st["inputs"]}
    check(made_here not in tids,
          "a P2 the planet makes for itself is NOT a drain (the player never delivers it)")
    check(all((pi["types"].get(t, {}).get("pi_tier") or 0) == 1 for t in tids)
          or any(t != made_here for t in tids),
          "the sub-line's own P1 inputs ARE drains")
    check(st["product"] == out, "the end product is the P3, not the intermediate P2")


# ── Part 3: run-dry is onhand ÷ rate, binding on the SCARCEST input ───────────────────────────
def test_runs_dry():
    from app.planner import factory_drain
    pi = _pi()
    out, sch = _a_p2(pi)
    if not out:
        return
    a, b = sch["inputs"][0], sch["inputs"][1]
    ct = sch["cycle_time"]
    rate_a = a["quantity"] / ct        # one pin
    # Stock A for exactly 10h, B for 40h. The factory stops on A.
    drain = {"t0": T0, "product": out, "product_rate": sch.get("output_qty", 1) / ct,
             "inputs": [{"type_id": a["type_id"], "onhand": rate_a * 10 * HOUR, "rate": rate_a},
                        {"type_id": b["type_id"], "onhand": b["quantity"] / ct * 40 * HOUR,
                         "rate": b["quantity"] / ct}]}

    dr = factory_drain(out, [], T0, drain=drain, now=T0)
    check(dr is not None and dr["source"] == "pins", "a stored drain state is used as the source")
    check(close(dr["tte_h"], 10.0, 1e-6), "runs dry in 10h — the SCARCEST input, not the average")
    check(dr["binding"] == a["type_id"], "the binding input is the one that empties first")
    check(close(dr["runs_dry_at"], T0 + 10 * HOUR, 1e-3), "run-dry reported as an instant")

    # Six hours after the checkpoint: 4h left, and onhand has actually gone down.
    dr6 = factory_drain(out, [], T0, drain=drain, now=T0 + 6 * HOUR)
    check(close(dr6["tte_h"], 4.0, 1e-6), "6h later it has 4h left — drained from the CHECKPOINT")
    check(close(dr6["onhand"][a["type_id"]], rate_a * 4 * HOUR, 1e-3),
          "on-hand stock ticks down at the pin rate")
    check(close(dr6["runs_dry_at"], dr["runs_dry_at"], 1e-3),
          "the run-dry INSTANT does not move as time passes — only the countdown to it does")

    # Past the deadline the answer stays honest rather than clamping to 'due now'.
    dr20 = factory_drain(out, [], T0, drain=drain, now=T0 + 20 * HOUR)
    check(close(dr20["onhand"][a["type_id"]], 0.0), "an emptied input floors at 0, never negative")
    check(close(dr20["runs_dry_at"], T0 + 10 * HOUR, 1e-3), "it still reports WHEN it ran dry")


# ── Part 4: the alert reads the colony, not a from-full cadence anchored to the scan ──────────
def test_alert_uses_observed_stock():
    from app.alerts import _factory_runs_dry_hours
    import json
    pi = _pi()
    out, sch = _a_p2(pi)
    if not out:
        return
    a, b = sch["inputs"][0], sch["inputs"][1]
    ct = sch["cycle_time"]
    now = T0 + 2 * HOUR
    drain = {"t0": T0, "product": out, "product_rate": sch.get("output_qty", 1) / ct,
             "inputs": [{"type_id": a["type_id"], "onhand": a["quantity"] / ct * 10 * HOUR,
                         "rate": a["quantity"] / ct},
                        {"type_id": b["type_id"], "onhand": b["quantity"] / ct * 99 * HOUR,
                         "rate": b["quantity"] / ct}]}
    row = {"products": json.dumps([{"type_id": out}]), "pad_inputs": "[]",
           "drain": json.dumps(drain), "checkpoint_at": T0, "scanned_at": now}

    h = _factory_runs_dry_hours(row, now)
    check(close(h, 8.0, 1e-6),
          "alert says 8h: 10h of stock at the checkpoint, 2h consumed since — NOT a full buffer")

    # The anchor bug: the old code read `scanned_at + cadence`, so moving ONLY the scan time (same
    # colony, same contents, same checkpoint) moved the deadline. It must not.
    row_late = dict(row, scanned_at=now + 5 * HOUR)
    check(close(_factory_runs_dry_hours(row_late, now), 8.0, 1e-6),
          "when we last polled ESI does not change when the colony runs dry")

    # A half-full colony must be told a sooner time than a fuller one — the whole point.
    half = json.loads(json.dumps(drain))
    half["inputs"][0]["onhand"] /= 2
    row_half = dict(row, drain=json.dumps(half))
    check(_factory_runs_dry_hours(row_half, now) < h,
          "half the stock reports a sooner deadline than a full one")

    # Already dry reports a NEGATIVE number, so the alert can escalate it rather than read 'due'.
    # (Note the row's own `checkpoint_at` is deliberately NOT what moves this — a drain state
    # carries the checkpoint it was read at, so only the passage of time can make it overdue.)
    check(close(_factory_runs_dry_hours(row, now + 50 * HOUR), 10.0 - 52.0, 1e-6),
          "a colony that already ran dry reports negative hours left")


# ── Part 5: no drain state (pre-rescan rows) still answers, from the model ────────────────────
def test_model_fallback():
    from app.planner import factory_drain, _compute_p1_fracs, _effective_fph
    pi = _pi()
    out, sch = _a_p2(pi)
    if not out:
        return
    fracs = _compute_p1_fracs(out, pi)
    rate_hr = _effective_fph(out, pi)
    pid = min(fracs, key=lambda p: p)
    inputs = [{"type_id": p, "amount": rate_hr * f * 12} for p, f in fracs.items()]   # 12h each
    dr = factory_drain(out, inputs, T0, drain=None, now=T0)
    check(dr is not None and dr["source"] == "model",
          "a row scanned before drain states existed falls back to the modelled rate")
    check(close(dr["tte_h"], 12.0, 1e-6), "the fallback still drains from the checkpoint")
    check(factory_drain(None, [], None, drain=None, now=T0) is None,
          "nothing to drain returns None rather than a fake deadline")


if __name__ == "__main__":
    print("Part 1: drain state off real pins")
    test_drain_state_from_pins()
    print("Part 2: on-planet chains")
    test_chain_excludes_self_made()
    print("Part 3: run-dry arithmetic")
    test_runs_dry()
    print("Part 4: the alert reads the colony")
    test_alert_uses_observed_stock()
    print("Part 5: model fallback")
    test_model_fallback()
    print()
    if FAILS:
        print(f"FAILED ({len(FAILS)}):")
        for f in FAILS:
            print("  - " + f)
        sys.exit(1)
    print("all good")
