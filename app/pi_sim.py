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

# EVE extraction decays over a program: a flat qty_per_cycle × cycles overestimates the real haul
# by ~25% (community-verified). CCP's per-cycle yield (EVE developer docs, dogma attrs 1683/1687) is
#   barWidth = cycle_time/900;  t = (cycle+0.5)·barWidth;  decayValue = qty_per_cycle/(1 + t·0.012)
#   output   = barWidth · decayValue · (1 + 0.8·noise),  noise = max(mean of three cosines, 0)
# The headline qty_per_cycle is the t≈0 (peak) value; the program AVERAGE relative to it is what a
# colony actually sustains. Crucially `t` only depends on ELAPSED time (num_cycles·barWidth =
# program_seconds/900), so the decay term has a clean closed-form mean — and the noise term is a
# small (mostly cancelling) bounded ripple on top, so we use the analytic decay average:
#   mean over [0,T] of 1/(1+0.012·t)  =  ln(1+0.012·T) / (0.012·T)
# This lands at ~0.79 for a ~12 h program (matching the "~25% overestimate") and falls off for long
# programs (a 2-week program's tail yields little) — the right "fraction of peak you sustain".
_DECAY_FACTOR = 0.012


def program_decay_factor(qty_per_cycle: float, cycle_time: float, num_cycles: int) -> float:
    """Fraction of the install-time (peak) extraction rate a program sustains on average over its
    life. 1.0 when the program length is unknown (no decay applied)."""
    if num_cycles <= 1 or cycle_time <= 0:
        return 1.0
    t_max = (num_cycles * cycle_time) / 900.0      # CCP bar-units; = program_seconds / 900
    k = _DECAY_FACTOR * t_max
    if k <= 0:
        return 1.0
    return max(0.3, min(1.0, math.log(1 + k) / k))


def _epoch(s: str | None) -> float | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def colony_sim_state(detail: dict, pi_data: dict) -> dict | None:
    """Build a forward-simulation state from one planet's ESI detail, or None if the planet
    has no extractor (can't be simulated). Shape:
      {"t0": <checkpoint epoch>, "expiry": <epoch|None>,
       "outputs": [{"type_id", "name", "tier", "base": <checkpoint amount>, "rate": <units/sec>}]}"""
    pins = detail.get("pins", [])
    types = pi_data["types"]
    schematics = pi_data["schematics"]
    sch_by_id = {v["schematic_id"]: out for out, v in schematics.items()}

    ext_rate: dict[int, float] = {}     # P0 type_id -> peak extracted units/sec (install headline)
    ext_rate_avg: dict[int, float] = {} # P0 type_id -> decay-averaged units/sec over the program
    ext_batch: dict[int, float] = {}    # P0 type_id -> extracted units per ECU cycle
    t0 = 0.0
    expiry: float | None = None
    has_ecu = False
    for pin in pins:
        ed = pin.get("extractor_details")
        if ed and ed.get("product_type_id") and ed.get("cycle_time"):
            has_ecu = True
            p0 = ed["product_type_id"]
            ct = ed["cycle_time"]
            _qpc = ed.get("qty_per_cycle", 0) or 0
            ext_rate[p0] = ext_rate.get(p0, 0.0) + _qpc / ct
            # Program-average (decayed) extraction: needs the program length (install→expiry).
            inst = _epoch(pin.get("install_time"))
            ex = _epoch(pin.get("expiry_time"))
            ncyc = int((ex - inst) / ct) if (inst and ex and ex > inst) else 0
            dfac = program_decay_factor(_qpc, ct, ncyc)
            ext_rate_avg[p0] = ext_rate_avg.get(p0, 0.0) + (_qpc * dfac) / ct
            ext_batch[p0] = ext_batch.get(p0, 0.0) + _qpc
            lcs = _epoch(pin.get("last_cycle_start"))
            if lcs:
                t0 = max(t0, lcs)
            if ex:
                expiry = ex if expiry is None else max(expiry, ex)
    if not has_ecu:
        return None

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
        # Uses the DECAY-AVERAGED extraction (ext_rate_avg) so a poor or just-reset planet reports the
        # rate it holds over the whole program, not the optimistic install-time peak. That's the right
        # number for "can this colony meet a daily quota".
        rate = f["count"] * f["output_qty"] / f["cycle_time"]    # product per sec
        ext_refined = ext_rate_avg.get(f["input_type"], 0.0) * f["output_qty"] / (f["input_qty"] or 1)
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
                            "base": base.get(p0, 0.0), "rate": rate,
                            "rate_sustained": ext_rate_avg.get(p0, rate),
                            "batch": ext_batch.get(p0, 0.0)})
    if not outputs or t0 <= 0:
        return None
    return {"t0": t0, "expiry": expiry, "outputs": outputs}


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
