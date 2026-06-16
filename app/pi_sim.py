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
that import P1 can't be simulated (the import schedule is unknown) → caller falls back to the
raw checkpoint contents.
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

    outputs = []
    for out, f in lines.items():
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
        ext_refined = ext_rate.get(f["input_type"], 0.0) * f["output_qty"] / (f["input_qty"] or 1)
        rate_sustained = min(rate, ext_refined) if ext_refined > 0 else rate
        outputs.append({"type_id": out, "name": types.get(out, {}).get("name") or f"#{out}",
                        "tier": types.get(out, {}).get("pi_tier") or 1,
                        "base": base.get(out, 0.0), "rate": rate, "rate_sustained": rate_sustained,
                        "batch": f["count"] * f["output_qty"]})
    if not outputs:
        # Pure extractor (no on-planet factories): the P0 itself piles up in the launchpad.
        for p0, rate in ext_rate.items():
            outputs.append({"type_id": p0, "name": types.get(p0, {}).get("name") or f"#{p0}",
                            "tier": types.get(p0, {}).get("pi_tier") or 0,
                            "base": base.get(p0, 0.0), "rate": rate, "rate_sustained": rate,
                            "batch": ext_batch.get(p0, 0.0)})
    if not outputs or t0 <= 0:
        return None
    # install = when the current program started; peak_p0_day = the colony's game-true average P0/day
    # (program total / runtime × 86400 via the EVE formula — constant for a program, so re-scanning
    # the same program doesn't drift it). Used to log a per-program yield sample so the
    # tool can show the MEASURED yield trend across reseats (a single snapshot can't).
    peak_p0_day = sum(ext_rate.values()) * 86400.0
    return {"t0": t0, "expiry": expiry, "install": install, "program_days": program_days,
            "peak_p0_day": peak_p0_day, "outputs": outputs}


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
