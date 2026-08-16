"""
Forward-simulate a PI colony from the ESI checkpoint to "now".

ESI's `GET /characters/{id}/planets/{planet_id}/` reports a colony's stored contents
only as of its last server checkpoint (`last_cycle_start`) — it does NOT stream live
storage. The in-game client (and tools like Rift) reproduce the live launchpad numbers by
running the PI production simulation forward from that checkpoint. This module does the same
with an aggregate flow model (good enough to match the game within a few %):

  output rate (units/sec) = conversion of the binding feed —
      extraction rate of the input P0  vs  the factories' processing capacity, whichever is lower.
  amount(now) = checkpoint contents + rate × (min(now, program expiry) − checkpoint time)

It covers EXTRACTOR planets (ECU → basic facilities → P1, or pure ECU → P0). Factory planets
that import P1 can't have their OUTPUT simulated past the point their inputs run out (the import
schedule is unknown) → caller falls back to the raw checkpoint contents.

Their INPUT side, though, is fully determined: `colony_drain_state` reads the planet's real
factory pins and reports what each imported input is consumed at (units/sec) and how much of it
was on the planet at the checkpoint. Consumption is a fixed `quantity / cycle_time` per pin — no
decay, no noise — so "when does this colony run dry" is arithmetic, not an estimate. That is what
the refill deadline, the Up-next agenda and the `factory_refill` alert answer from.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone


def _epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def extractor_program_total(qty_per_cycle: float, cycle_time_s: float, num_cycles: int) -> float:
    """EVE's exact extractor program output. The per-cycle yield decays (decay 0.012, dogma 1683)
    and oscillates with a deterministic noise wave (noise 0.8, dogma 1687) whose phase is seeded by
    qty_per_cycle**0.7; the program total is the floored sum. Validated against in-game program
    totals to the unit. IMPORTANT: qty_per_cycle/cycle_time UNDER-counts real extraction by ~16-20%
    — the true rate is this total / program runtime (what the game shows as units/hour)."""
    if qty_per_cycle <= 0 or cycle_time_s <= 0 or num_cycles <= 0:
        return 0.0
    decay_factor, noise_factor = 0.012, 0.8
    bar_width = cycle_time_s / 900.0
    phase = pow(qty_per_cycle, 0.7)
    total = 0.0
    for cycle in range(num_cycles):
        t = (cycle + 0.5) * bar_width
        decay_value = qty_per_cycle / (1 + t * decay_factor)
        sin_stuff = max((math.cos(phase + t * (1.0 / 12.0))
                         + math.cos(phase / 2.0 + t * 0.2)
                         + math.cos(t * 0.5)) / 3.0, 0.0)
        total += math.floor(bar_width * decay_value * (1 + noise_factor * sin_stuff))
    return total


def colony_sim_state(detail: dict, pi_data: dict) -> dict | None:
    """Build a forward-simulation state from one planet's ESI detail, or None if the planet
    has no extractor (can't be simulated). Shape:
      {"t0": <checkpoint epoch>, "expiry": <epoch|None>,
       "outputs": [{"type_id", "name", "tier", "base": <checkpoint amount>, "rate": <units/sec>}]}"""
    pins = detail.get("pins", [])
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    sch_by_id = {v["schematic_id"]: out for out, v in schematics.items()}

    ext_rate: dict[int, float] = {}     # P0 type_id -> extracted units/sec (install headline rate)
    ext_batch: dict[int, float] = {}    # P0 type_id -> extracted units per ECU cycle
    t0 = 0.0
    expiry: float | None = None
    install: float | None = None
    has_ecu = False
    for pin in pins:
        ed = pin.get("extractor_details")
        if ed and ed.get("product_type_id") and ed.get("cycle_time"):
            has_ecu = True
            p0 = ed["product_type_id"]
            _qpc = ed.get("qty_per_cycle", 0) or 0
            _cyc = ed["cycle_time"]
            ex = _epoch(pin.get("expiry_time"))
            inst = _epoch(pin.get("install_time"))
            # True extraction rate = EVE's program total / runtime (decay+noise formula). The headline
            # qty_per_cycle/cycle_time under-counts it by ~16-20%; fall back to it if the program
            # window is missing so we never divide by zero.
            if inst and ex and ex > inst and _cyc > 0:
                _ncyc = int((ex - inst) // _cyc)
                _rate = extractor_program_total(_qpc, _cyc, _ncyc) / (ex - inst)
            else:
                _rate = _qpc / _cyc
            ext_rate[p0] = ext_rate.get(p0, 0.0) + _rate
            ext_batch[p0] = ext_batch.get(p0, 0.0) + _qpc
            lcs = _epoch(pin.get("last_cycle_start"))
            if lcs:
                t0 = max(t0, lcs)
            if ex:
                expiry = ex if expiry is None else max(expiry, ex)
            if inst:
                install = inst if install is None else min(install, inst)
    if not has_ecu:
        return None
    # Full extraction-program length the player chose (install → expiry), in days — the anchor for
    # the "how long can I run extractors" advice (the program's avg yield already reflects this).
    program_days = (expiry - install) / 86400.0 if (expiry and install and expiry > install) else None

    # Basic/advanced factory lines on the planet, grouped by output product.
    lines: dict[int, dict] = {}
    for pin in pins:
        sid = pin.get("schematic_id") or (pin.get("factory_details") or {}).get("schematic_id")
        if not sid:
            continue
        out = sch_by_id.get(sid)
        if out is None:
            continue
        sch = schematics.get(out) or {}
        inputs = sch.get("inputs") or []
        if not inputs:
            continue
        f = lines.setdefault(out, {"count": 0, "input_type": inputs[0]["type_id"],
                                   "input_qty": inputs[0]["quantity"],
                                   "output_qty": sch.get("output_qty", 1),
                                   "cycle_time": sch.get("cycle_time", 3600)})
        f["count"] += 1
        lcs = _epoch(pin.get("last_cycle_start"))
        if lcs:
            t0 = max(t0, lcs)

    base: dict[int, float] = {}          # checkpoint stored contents per type
    for pin in pins:
        for c in (pin.get("contents") or []):
            tid, amt = c.get("type_id"), c.get("amount", 0) or 0
            if tid:
                base[tid] = base.get(tid, 0.0) + amt

    # A hand-built colony can chain a P1 line straight into an on-planet P2 (or higher) line —
    # something our own generated templates never produce, but ESI happily reports as two separate
    # `lines` entries. Process tier-ascending (P1 before P2 before P3...) so a downstream line can
    # look up its upstream on-planet line's OWN rate_sustained, not just raw P0 extraction — without
    # this, a chained line's `input_type` (a P1/P2 type_id) is never a key in `ext_rate` (which only
    # ever holds P0 type_ids from ECU pins), so ext_refined was always 0 and rate_sustained silently
    # fell back to the full uncapped nominal rate, ignoring that its real feed is itself
    # extraction-limited. `consumed_as_input` flags which on-planet outputs are somebody else's
    # input (self-consumed, not exportable to the rest of the account) vs the planet's real end
    # product(s).
    consumed_as_input = {f["input_type"] for f in lines.values()}
    rate_sustained_by_out: dict[int, float] = {}
    outputs = []
    for out, f in sorted(lines.items(), key=lambda kv: types.get(kv[0], {}).get("pi_tier") or 0):
        # `rate` = the FULL factory rate (all facilities running). The launchpad fills at this rate
        # because extraction decay front-loads P0 and storage buffers the facilities — it matches the
        # in-game launchpad, which accrues whole batches (count × output_qty, e.g. 8 × 20 = 160) every
        # cycle. Good for "what's in my pad now".
        # `rate_sustained` = the long-run SUSTAINABLE rate = min(factory capacity, extraction refined).
        # A poor planet whose extraction can't keep the basics fed reports the (lower) extraction rate;
        # a rich planet stays factory-limited (full rate). We use the install-time extraction rate (NOT
        # a decay-averaged one): the launchpad/storage buffers the front-loaded extraction, so an
        # actively-cycled colony holds the factory rate as long as PEAK extraction covers it — matches
        # the in-game numbers. (A decay average under-counted every factory-limited colony.)
        rate = f["count"] * f["output_qty"] / f["cycle_time"]    # product per sec
        chain_input_type = None
        chain_need_per_day = None
        if f["input_type"] in ext_rate:
            ext_refined = ext_rate[f["input_type"]] * f["output_qty"] / (f["input_qty"] or 1)
        elif f["input_type"] in rate_sustained_by_out:
            # Chained: this line's real feed is an upstream on-planet line's own sustained output,
            # not raw extraction.
            ext_refined = rate_sustained_by_out[f["input_type"]] * f["output_qty"] / (f["input_qty"] or 1)
            chain_input_type = f["input_type"]
            # Full/uncapped appetite for the input, independent of whether it's currently starved —
            # "how much they'd need to end up with" for a reseat suggestion to target.
            chain_need_per_day = rate * (f["input_qty"] or 1) / f["output_qty"] * 86400
        else:
            ext_refined = 0.0   # imported input, schedule unknown — same fallback as before
        rate_sustained = min(rate, ext_refined) if ext_refined > 0 else rate
        rate_sustained_by_out[out] = rate_sustained
        # `ext_refined` is what the heads can refine BEFORE the factory clips it. When it exceeds `rate`
        # the colony is factory-limited and the surplus is the player's decay/overshoot buffer — invisible
        # in rate_sustained (which clamps at the factory rate), so we surface it separately.
        entry = {"type_id": out, "name": types.get(out, {}).get("name") or f"#{out}",
                 "tier": types.get(out, {}).get("pi_tier") or 1,
                 "base": base.get(out, 0.0), "rate": rate, "rate_sustained": rate_sustained,
                 "ext_refined": ext_refined if ext_refined > 0 else rate,
                 "batch": f["count"] * f["output_qty"],
                 "exportable": out not in consumed_as_input}
        if chain_input_type is not None:
            entry["chain_input_type"] = chain_input_type
            entry["chain_need_per_day"] = chain_need_per_day
        outputs.append(entry)
    if not outputs:
        # Pure extractor (no on-planet factories): the P0 itself piles up in the launchpad.
        for p0, rate in ext_rate.items():
            outputs.append({"type_id": p0, "name": types.get(p0, {}).get("name") or f"#{p0}",
                            "tier": types.get(p0, {}).get("pi_tier") or 0,
                            "base": base.get(p0, 0.0), "rate": rate, "rate_sustained": rate,
                            "ext_refined": rate, "batch": ext_batch.get(p0, 0.0),
                            "exportable": True})
    if not outputs or t0 <= 0:
        return None
    # install = when the current program started; peak_p0_day = the colony's game-true average P0/day
    # (program total / runtime × 86400 via the EVE formula — constant for a program, so re-scanning
    # the same program doesn't drift it). Used to log a per-program yield sample so the
    # tool can show the MEASURED yield trend across reseats (a single snapshot can't).
    peak_p0_day = sum(ext_rate.values()) * 86400.0
    return {"t0": t0, "expiry": expiry, "install": install, "program_days": program_days,
            "peak_p0_day": peak_p0_day, "outputs": outputs}


def colony_drain_state(detail: dict, pi_data: dict) -> dict | None:
    """Per-input CONSUMPTION for a planet that imports its inputs, read off its real factory pins,
    or None if nothing on it imports anything (a pure extractor, or a self-contained chain).

    This is the deterministic half of a colony. A factory pin eats `quantity` of each input every
    `cycle_time` seconds — a constant, unlike extraction, which decays. So the drain rate here is
    the planet's TRUE burn, not the modelled `_effective_fph × frac` the planner uses when sizing a
    plan (which is a per-product average and, for P4, a deliberate 0.5/hr approximation of a whole
    on-planet chain). Run-dry time = onhand ÷ rate, exact to the pin.

    Only IMPORTED inputs are counted: a P2 the planet makes itself to feed its own P3 line is not
    something the player delivers, so it can't be what the colony runs dry of. Shape:
      {"t0": <checkpoint epoch>, "product": <end-product type_id|None>,
       "inputs": [{"type_id", "name", "tier", "onhand": <at checkpoint>, "rate": <units/sec>}]}"""
    pins = detail.get("pins", [])
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    sch_by_id = {v["schematic_id"]: out for out, v in schematics.items()}

    rate: dict[int, float] = {}        # input type_id -> units consumed/sec across every pin eating it
    produced: set[int] = set()         # what this planet makes for itself
    out_rate: dict[int, float] = {}    # output type_id -> units/sec (to pick the end product)
    t0 = 0.0
    for pin in pins:
        sid = pin.get("schematic_id") or (pin.get("factory_details") or {}).get("schematic_id")
        if not sid:
            continue
        out = sch_by_id.get(sid)
        if out is None:
            continue
        sch = schematics.get(out) or {}
        ct = sch.get("cycle_time") or 3600
        if ct <= 0:
            continue
        produced.add(out)
        out_rate[out] = out_rate.get(out, 0.0) + sch.get("output_qty", 1) / ct
        for inp in (sch.get("inputs") or []):
            tid, qty = inp.get("type_id"), inp.get("quantity", 0) or 0
            if tid and qty > 0:
                rate[tid] = rate.get(tid, 0.0) + qty / ct
        lcs = _epoch(pin.get("last_cycle_start"))
        if lcs:
            t0 = max(t0, lcs)

    imported = {tid: r for tid, r in rate.items() if tid not in produced and r > 0}
    if not imported or t0 <= 0:
        return None

    onhand: dict[int, float] = {}      # every container on the planet, not just launchpads — a pin
    for pin in pins:                   # buffering its own input still feeds the line
        for c in (pin.get("contents") or []):
            tid, amt = c.get("type_id"), c.get("amount", 0) or 0
            if tid in imported and amt:
                onhand[tid] = onhand.get(tid, 0.0) + amt

    # The end product is what nothing else on the planet eats — the thing hauled off it.
    ends = [t for t in produced if t not in rate]
    product = max(ends, key=lambda t: types.get(t, {}).get("pi_tier") or 0) if ends else None
    return {"t0": t0, "product": product,
            "inputs": sorted(
                [{"type_id": tid, "name": types.get(tid, {}).get("name") or f"#{tid}",
                  "tier": types.get(tid, {}).get("pi_tier") or 1,
                  "onhand": onhand.get(tid, 0.0), "rate": r}
                 for tid, r in imported.items()], key=lambda x: x["type_id"]),
            "product_rate": out_rate.get(product, 0.0) if product else 0.0}


def drain_runs_dry(state: dict, now_ts: float | None = None) -> dict | None:
    """When does a drain state run dry → {"hours": <from now, may be negative>, "at": <epoch>,
    "binding": <type_id>, "onhand": {type_id: units now}}. None if it never does.

    The binding input is the one that empties FIRST — a factory stops on its scarcest input, so
    that, not the average or the total, is the deadline. Negative hours mean it already ran dry
    (and the checkpoint stopped advancing when it did, which is why the number stays honest)."""
    if not state or not state.get("inputs"):
        return None
    if now_ts is None:
        now_ts = datetime.now(timezone.utc).timestamp()
    t0 = state.get("t0") or now_ts
    dt = max(0.0, now_ts - t0)
    dry_at = None
    binding = None
    onhand_now: dict[int, float] = {}
    for it in state["inputs"]:
        r = it.get("rate", 0) or 0
        base = it.get("onhand", 0) or 0
        onhand_now[it["type_id"]] = max(0.0, base - r * dt)
        if r <= 0:
            continue
        at = t0 + base / r
        if dry_at is None or at < dry_at:
            dry_at, binding = at, it["type_id"]
    if dry_at is None:
        return None
    return {"hours": (dry_at - now_ts) / 3600.0, "at": dry_at,
            "binding": binding, "onhand": onhand_now}


def project(state: dict, now_ts: float | None = None) -> list[dict]:
    """Project a sim state forward to now → [{type_id, name, tier, amount}]. Production stops at
    the extraction program's expiry."""
    if not state or not state.get("outputs"):
        return []
    if now_ts is None:
        now_ts = datetime.now(timezone.utc).timestamp()
    t0 = state.get("t0") or now_ts
    expiry = state.get("expiry")
    end = now_ts if not expiry else min(now_ts, expiry)
    dt = max(0.0, end - t0)
    out = []
    for o in state["outputs"]:
        base = o.get("base", 0) or 0
        produced = (o.get("rate", 0) or 0) * dt
        batch = o.get("batch", 0) or 0
        if batch > 0:
            produced = (int(produced // batch)) * batch     # only whole cycle batches have landed
        amt = base + produced
        if amt >= 1:
            out.append({"type_id": o["type_id"], "name": o["name"],
                        "tier": o.get("tier", 1), "amount": int(round(amt))})
    out.sort(key=lambda x: -x["amount"])
    return out
