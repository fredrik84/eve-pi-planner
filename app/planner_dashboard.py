"""
Dashboard endpoint — the logged-in overview.

`/api/dashboard` is a single ~400-line read-only aggregation (per-colony fill/TTE, fleet totals,
alerts, maintenance routine, the pad-fill meter) that pulled in most of the planner's surface.
It lives here rather than in `planner.py` because it computes no plan — it only reads state and
re-groups what the shared alert engine and advisor already produce.

Top of the import chain: planner_algo <- planner <- planner_advisor <- planner_dashboard.
"""
import json as _json
import time as _time

from fastapi import APIRouter, Cookie

from app.sde import load_pi_data, get_connection
from app.market import fetch_prices
from app.esi import session_context_id, ensure_char_tables
from app.alert_settings import get_alert_settings
from app.alerts import compute_alerts, _extractor_program_lengths
from app.planner import _compute_p1_fracs, _effective_fph, factory_drain
from app.planner_advisor import _expansion_capacity

router = APIRouter()

def _pad_fill_meter(parsed, pi, types):
    """How far the P1 sitting in EXTRACTOR launchpads would go toward FILLING every factory's 30,000 m³
    (3-LP) input buffer. `have` = projected extractor-pad P1 per material; `need` = each factory's buffer
    split by consumption ratio (units = 30000 m³ × normalised frac ÷ 0.19 m³/unit). The headline % is the
    BINDING material (you need them all), with a per-material breakdown (weakest first)."""
    from app.pi_sim import project
    VOL, LP_M3 = 0.19, 30000.0
    have: dict[int, float] = {}        # P1 in extractor launchpads, per type_id
    need: dict[int, float] = {}        # P1 to fill every factory buffer, per type_id
    nfac = 0
    for (r, prods, inputs, pads) in parsed:
        if r["is_ext"]:
            src = None
            if r["sim_state"]:
                try:
                    src = project(_json.loads(r["sim_state"]))   # forward-projected output
                except Exception:
                    src = None
            for it in (src if src is not None else (pads or [])):
                tid, amt = it.get("type_id"), (it.get("amount", 0) or 0)
                if tid and amt > 0:
                    have[tid] = have.get(tid, 0) + amt
        elif prods:
            fr = _compute_p1_fracs(prods[0]["type_id"], pi)   # P1-per-product recipe quantities
            if fr:
                nfac += 1
                tot = sum(fr.values()) or 1.0                 # → consumption ratio (sums to 1)
                for pid, frac in fr.items():
                    need[pid] = need.get(pid, 0) + LP_M3 * (frac / tot) / VOL
    if not need:
        return None
    mats = []
    for pid, nd in need.items():
        hv = have.get(pid, 0)
        mats.append({"type_id": pid, "name": types.get(pid, {}).get("name") or f"#{pid}",
                     "have": round(hv), "need": round(nd),
                     "pct": round(min(1.0, hv / nd) if nd > 0 else 1.0, 4)})
    mats.sort(key=lambda m: m["pct"])
    return {"fill_pct": mats[0]["pct"], "binding": mats[0]["name"], "factories": nfac,
            "target_units": round(sum(need.values())), "materials": mats}


@router.get("/api/dashboard")
def dashboard(pp_session: str = Cookie(default=None)):
    """Logged-in overview: per-factory launchpad fill %, time-to-empty and current-run value,
    plus fleet totals (soonest refill, current-run value, value/day) and the most valuable
    highest-tier PI sitting in launchpads. Strictly scoped to the session's own context."""
    context_id = session_context_id(pp_session)
    if not context_id:
        return {"logged_in": False, "factories": [], "totals": {}, "top_pi": None}
    ensure_char_tables()        # make sure the pad_inputs column exists before we read it
    pi = load_pi_data()
    types = pi["types"]
    con = get_connection()
    rows = con.execute("""
        SELECT c.character_name AS ch, c.character_id AS cid, cp.planet_id AS planet_id,
               cp.planet_num AS pn, s.name AS system,
               cp.is_extractor AS is_ext, cp.products AS products,
               cp.pad_inputs AS pad_inputs, cp.pad_contents AS pad_contents, cp.drain AS drain,
               cp.issues AS issues, cp.sim_state AS sim_state,
               cp.scanned_at AS scanned_at, cp.checkpoint_at AS checkpoint_at, cp.storage AS storage
        FROM pp_char_planets cp
        JOIN pp_characters c ON c.character_id = cp.character_id
        LEFT JOIN solar_systems s ON s.system_id = cp.solar_system_id
        WHERE c.context_id = ? AND COALESCE(c.is_dummy, 0) = 0
    """, (context_id,)).fetchall()
    con.close()

    now = _time.time()
    VOL = 0.19           # m³ per PI unit (verified in-game)
    LP_M3 = 30000.0      # 3 launchpads of P1 input buffer

    parsed, price_tids, pad_all = [], set(), []
    for r in rows:
        prods = _json.loads(r["products"] or "[]")
        inputs = _json.loads(r["pad_inputs"] or "[]")
        pads = _json.loads(r["pad_contents"] or "[]")
        parsed.append((r, prods, inputs, pads))
        for p in prods:
            price_tids.add(p["type_id"])
        for it in pads:
            price_tids.add(it["type_id"])
            if not r["is_ext"]:        # "In pads now" = sellable FACTORY product only; an extractor's
                pad_all.append(it)     # P1 in its launchpad is intermediate (hauled to factories, not sold)
    prices = fetch_prices(list(price_tids)) if price_tids else {}

    factories = []
    chars_in_view: set[int] = set()    # characters actually surfaced on the dashboard (factory tile or
                                       # warning) — the rescan button scopes to these, not the whole fleet
    total_run_value = total_value_per_day = 0.0
    soonest_h = None
    refill_fac_h = None; refill_fac_loc = None  # tightest from-full factory input buffer (refill cadence)
    refill_due_h = None; refill_due_loc = None  # soonest factory to run its inputs dry (refill deadline)
    refill_due_at = None                       # ...as an INSTANT (epoch s), so the client shows a clock
    refill_due_src = None                      # "pins" (real consumption) or "model" (planner average)
    refill_due_ckpt = None                     # checkpoint it was read from — how old the reading is
    cur_units_by_prod: dict[str, float] = {}   # current units/day by product (for the expansion estimate)
    produced_by_tid: dict[int, float] = {}     # product made since checkpoint (projected up)
    for (r, prods, inputs, pads) in parsed:
        if r["is_ext"] or not prods:
            continue
        prod = prods[0]
        tid = prod["type_id"]
        fracs = _compute_p1_fracs(tid, pi)
        if not fracs:
            continue
        try:
            drain = _json.loads(r["drain"] or "null")
        except Exception:
            drain = None
        fph24 = _effective_fph(tid, pi) * 24.0            # products/day for one factory
        # Drain the launchpad buffer forward: the factory keeps eating P1 between ESI updates, so
        # subtract consumption since the colony CHECKPOINT (last_cycle_start — what the reported
        # contents are actually "as of", not our fetch time). `factory_drain` owns that arithmetic
        # for everyone; it prefers the planet's real pin rates and falls back to the modelled ones.
        anchor = r["checkpoint_at"] or r["scanned_at"]
        dr = factory_drain(tid, inputs, anchor, drain=drain, now=now)
        if not dr:
            continue
        onhand, tte_h = dr["onhand"], dr["tte_h"]
        elapsed_h = max(0.0, (now - dr["t0"]) / 3600.0)
        # Product made since the checkpoint = rate × hours the factory was actually fed (it stops
        # once an input runs out). Feeds the rising "In pads now" so value flows inputs → product.
        prod_h = max(0.0, min(elapsed_h, (dr["runs_dry_at"] - dr["t0"]) / 3600.0))
        produced = fph24 * prod_h / 24.0 if prod_h > 0 else 0.0
        if produced > 0:
            produced_by_tid[tid] = produced_by_tid.get(tid, 0.0) + produced
        # Over the drain's OWN input set, not the modelled P1 list — a hybrid planet importing a
        # P2 has real inputs `fracs` never mentions, and summing over `fracs` would read it empty.
        in_m3 = sum(v * VOL for v in onhand.values())
        makeable = tte_h * fph24 / 24.0        # what the inputs on hand can still make
        price = prices.get(tid, 0.0)
        run_value = makeable * price
        vpd = fph24 * price
        total_run_value += run_value
        total_value_per_day += vpd
        cur_units_by_prod[prod.get("name") or f"#{tid}"] = cur_units_by_prod.get(prod.get("name") or f"#{tid}", 0.0) + fph24
        soonest_h = tte_h if soonest_h is None else min(soonest_h, tte_h)
        # Finished product ready to haul off THIS planet now: what's in the pad + what's been made
        # since the checkpoint. The actionable per-planet figure (pairs with "runs out").
        haul_units = round(sum((it.get("amount", 0) or 0) for it in pads) + produced)
        haul_value = sum((it.get("amount", 0) or 0) * prices.get(it["type_id"], 0.0) for it in pads) + produced * price
        if r["cid"] is not None:
            chars_in_view.add(r["cid"])
        loc = f"{r['ch']} · {r['system'] or '?'}" + (f" P{r['pn']}" if r["pn"] is not None else "")
        # Refill cadence = how long a FULL P1 input buffer (3 launchpads) lasts at this factory's
        # consumption — the interval you must top it up on. Keep the tightest (fastest-draining).
        day_in_m3 = sum(dr["rate_h"].values()) * 24.0 * VOL
        if day_in_m3 > 0:
            rc_h = LP_M3 / day_in_m3 * 24.0
            if refill_fac_h is None or rc_h < refill_fac_h:
                refill_fac_h, refill_fac_loc = rc_h, loc
        if refill_due_h is None or tte_h < refill_due_h:    # soonest factory to empty = refill deadline
            refill_due_h, refill_due_loc = tte_h, loc
            refill_due_at, refill_due_src, refill_due_ckpt = dr["runs_dry_at"], dr["source"], dr["t0"]
        factories.append({
            "loc": loc, "product": prod.get("name") or f"#{tid}",
            "tier": types.get(tid, {}).get("pi_tier") or 0,
            "haul_units": haul_units, "haul_value": round(haul_value, 2),
            "fill_pct": round(min(100.0, in_m3 / LP_M3 * 100.0), 1),
            "hours_left": round(tte_h, 1),
            "value_per_day": round(vpd, 2),
        })
    factories.sort(key=lambda x: x["hours_left"])         # soonest to empty first

    # Finished product in launchpads right now = the scan snapshot PLUS what's been produced since
    # the checkpoint (projected up, mirroring the inputs draining down). Gives "In pads now" + top PI.
    agg = {}
    for it in pad_all:
        t = it["type_id"]
        a = agg.setdefault(t, {"type_id": t, "name": it.get("name") or f"#{t}",
                               "tier": types.get(t, {}).get("pi_tier") or 0, "amount": 0.0})
        a["amount"] += it.get("amount", 0) or 0
    for t, units in produced_by_tid.items():
        a = agg.setdefault(t, {"type_id": t, "name": types.get(t, {}).get("name") or f"#{t}",
                               "tier": types.get(t, {}).get("pi_tier") or 0, "amount": 0.0})
        a["amount"] += units
    for a in agg.values():
        a["amount"] = round(a["amount"])
        a["value"] = round(a["amount"] * prices.get(a["type_id"], 0.0), 2)
    top_pi = max(agg.values(), key=lambda a: (a["tier"], a["value"])) if agg else None
    pads_value = round(sum(a["value"] for a in agg.values()), 2)

    # Colony warnings, grouped PER CHARACTER and counted (so a fleet of expiring extractors is one
    # "12 extractions expiring" line, not 12 rows). All actual DETECTION (thresholds, mutes,
    # severity) now lives in app.alerts.compute_alerts() — the same engine the
    # notification scheduler consumes, so a push and what's shown here can't drift apart. This
    # function only re-groups the flat alert list into display cards.
    _alert = get_alert_settings(context_id)   # thresholds only — muting is applied inside compute_alerts()
    _all_alerts = compute_alerts(context_id, rows=rows, now=now)
    for a in _all_alerts:
        if a["character_id"] is not None:
            chars_in_view.add(a["character_id"])

    by_char: dict[str, dict[str, list]] = {}      # char -> kind -> [planet labels]
    expired: dict[str, int] = {}                  # char -> count (extraction cycle events collapse
    expiring: dict[str, int] = {}                 # char -> count   into one global line each)
    factory_refills: dict[str, int] = {}          # char -> count
    factory_refill_high = False                   # any instance escalated to "high" (imminent)?
    fulls = []                                    # storage_full instances, for the grouped card below
    _CORRECTNESS_KINDS = {"ext_unrouted", "fac_unfed", "fac_output", "p0_mismatch"}
    for a in _all_alerts:
        ch = a["character_name"]
        if a["kind"] in _CORRECTNESS_KINDS:
            by_char.setdefault(ch, {}).setdefault(a["kind"], []).append(a["location"])
        elif a["kind"] == "expired":
            expired[ch] = expired.get(ch, 0) + 1
        elif a["kind"] == "expiring":
            expiring[ch] = expiring.get(ch, 0) + 1
        elif a["kind"] == "factory_refill":
            factory_refills[ch] = factory_refills.get(ch, 0) + 1
            if a["severity"] == "high":
                factory_refill_high = True
        elif a["kind"] == "storage_full":
            fulls.append({"ch": ch, "loc": a["location"], "pct": a["pct"], "ttf": a["hours_left"]})

    KIND = {                                       # severity, singular, plural
        "ext_unrouted": ("high", "extractor not routed", "extractors not routed"),
        "fac_unfed":    ("high", "factory has no input route", "factories with no input route"),
        "fac_output":   ("high", "factory output not routed", "factory outputs not routed"),
        "p0_mismatch":  ("high", "extracting something the factories don't use — piling up",
                                 "extracting things the factories don't use — piling up"),
    }
    issues = []
    for ch in sorted(by_char):
        items = []
        for k, locs in by_char[ch].items():
            sev, sg, pl = KIND.get(k, ("warn", k, k))
            n = len(locs)
            msg = f"{n} {sg if n == 1 else pl}"
            if n <= 4:
                msg += f" ({', '.join(locs)})"
            items.append({"severity": sev, "msg": msg})
        items.sort(key=lambda x: 0 if x["severity"] == "high" else 1)
        issues.append({"char": ch, "severity": "high" if any(i["severity"] == "high" for i in items) else "warn", "items": items})

    # Extraction cycle events / factory refills come in fleets, so collapse each state into ONE
    # line (char ×count) rather than one row per planet.
    def _collapse(tally, sev, header, noun, verb):
        total = sum(tally.values())
        parts = ", ".join(f"{c} ×{n}" for c, n in sorted(tally.items(), key=lambda x: -x[1]))
        return {"char": header, "severity": sev,
                "items": [{"severity": sev, "msg": f"{total} {noun}{'s' if total != 1 else ''} {verb} — {parts}"}]}
    _expiring_h_str = ("%g" % _alert["expiring_hours"])
    if expired:
        issues.append(_collapse(expired, "high", "Extractions expired", "extractor", "expired"))
    if expiring:
        issues.append(_collapse(expiring, "warn", "Extractions expiring soon", "extractor", f"expiring within {_expiring_h_str}h"))
    if factory_refills:
        issues.append(_collapse(factory_refills, "high" if factory_refill_high else "warn",
                                 "Factories due for refill", "factory", f"due within {_expiring_h_str}h"))

    if fulls:
        fulls.sort(key=lambda x: (x["ttf"] if x["ttf"] is not None else 1e9, -x["pct"]))

        def _ttf_str(t):
            if t is None:
                return ""
            if t < 1:
                return " · full within the hour"
            if t >= 24:
                return f" · ~{round(t / 24)}d to full"
            return f" · ~{round(t)}h to full"
        # Grouped: a count in the header + only the few most-urgent pads, so a big fleet shows one tidy
        # card (e.g. "62 launchpads ≥80% full (8 within 3h)") instead of dozens of rows.
        n = len(fulls)
        urgent = sum(1 for f in fulls if f["ttf"] is not None and f["ttf"] < _alert["storage_urgent_hours"])
        head = f"Storage filling up — {n} launchpad{'s' if n != 1 else ''} ≥{round(_alert['storage_warn_pct'])}% full"
        if urgent:
            head += f" ({urgent} within {'%g' % _alert['storage_urgent_hours']}h)"
        items = [{"severity": "high" if (f["pct"] >= _alert["storage_high_pct"]
                                          or (f["ttf"] is not None and f["ttf"] < _alert["storage_high_ttf_hours"]))
                  else "warn",
                  "msg": f"{f['ch']} · {f['loc']} — {f['pct']}% full{_ttf_str(f['ttf'])}"} for f in fulls[:5]]
        if n > len(items):
            items.append({"severity": "warn", "msg": f"+ {n - len(items)} more pad{'s' if n - len(items) != 1 else ''} ≥{round(_alert['storage_warn_pct'])}%"})
        issues.append({"char": head,
                       "severity": "high" if any(i["severity"] == "high" for i in items) else "warn",
                       "items": items})
    issues.sort(key=lambda c: 0 if c["severity"] == "high" else 1)

    # The maintenance-routine countdown stats below (restart/empty/refill cadence) are always-on
    # numbers, not muteable alerts, so they're computed separately from compute_alerts() —
    # they still need every extractor's storage/program data regardless of any account's alert
    # thresholds or mutes.
    empty_pads_h = None; empty_pads_loc = None   # tightest empty→full launchpad time (emptying CADENCE)
    empty_due_h = None; empty_due_loc = None     # soonest pad to cap from its CURRENT fill (DEADLINE)
    restart_due_h = None; restart_due_loc = None  # soonest expiry among IN-SYNC extractors (the fleet batch) + which
    for (r, prods, inputs, pads) in parsed:
        if not r["is_ext"]:
            continue
        sysloc = (r["system"] or "?") + (f" P{r['pn']}" if r["pn"] is not None else "")
        cloc = f"{r['ch']} · {sysloc}"
        st = _json.loads(r["storage"] or "null")
        if not st:
            continue
        fill_h = st.get("fill_m3_h", 0) or 0
        # Emptying cadence = how long a freshly-emptied launchpad takes to cap (10k m³ → extraction
        # stalls); track the fastest-filling pad. Deadline = soonest pad to cap from its CURRENT fill.
        if fill_h > 0 and st.get("cap_m3"):
            cad_h = st["cap_m3"] / fill_h
            if empty_pads_h is None or cad_h < empty_pads_h:
                empty_pads_h, empty_pads_loc = cad_h, cloc
        anchor = r["checkpoint_at"] or r["scanned_at"]
        el_h = max(0.0, (now - anchor) / 3600.0) if anchor else 0.0
        cap = st["cap_m3"] or 1
        vol = min(cap, st["vol_m3"] + fill_h * el_h)
        ttf = ((cap - vol) / fill_h) if fill_h > 0 and vol < cap else None
        if ttf is not None and (empty_due_h is None or ttf < empty_due_h):
            empty_due_h, empty_due_loc = ttf, cloc

    # Dashboard shows spare capacity as STATUS only (free slots / idle / trained-up counts); the
    # concrete "deploy this here" advice lives in Setup Analysis (GET /api/expansion). Keeps the
    # dashboard a status surface and avoids the expensive per-product deploy search on every load.
    expansion = _expansion_capacity(context_id)

    # Fleet program-length norm (most common, 0.5h bins) — shared with the schedule_sync alert
    # (see compute_alerts()) so the two can't disagree; computed unconditionally here
    # (unlike the alert) since the restart-due countdown below needs it regardless of mutes.
    ext_progs, norm = _extractor_program_lengths(rows)

    # Restart-due = the MEDIAN expiry of the in-sync batch (extractors on the common program length).
    # Median, not the soonest, is deliberate: the player would rather be told to come back a touch LATE
    # and restart the whole batch in one go than log in early for the first straggler and wait. One
    # off-schedule planet is excluded (it's surfaced as the schedule_sync alert) so it can't drag this
    # to "due now".
    in_sync_exp = sorted(e["expiry"] for e in ext_progs
                         if e.get("expiry") and (norm is None or abs(e["prog_hours"] - norm) <= 0.4))
    if in_sync_exp:
        median_exp = in_sync_exp[len(in_sync_exp) // 2]
        restart_due_h = max(0.0, (median_exp - now) / 3600.0)
        restart_due_loc = None   # fleet-wide batch, not a single colony

    # Out-of-sync extractors: sourced from _all_alerts (computed above), so muting schedule_sync
    # in Settings > Alerts hides this card too, same as every other alert kind.
    _sync_items = [a for a in _all_alerts if a["kind"] == "schedule_sync"]
    sync_warn = None
    if _sync_items:
        _sync_items.sort(key=lambda a: a["prog_hours"])
        sync_warn = {"norm_hours": _sync_items[0]["norm_hours"],
                     "off": [{"cid": a["character_id"], "char": a["character_name"], "loc": a["location"],
                              "hours": a["prog_hours"]} for a in _sync_items]}

    # Reactions alerts (see app.alerts._reaction_alerts) — unrelated to PI colonies, so
    # they're not folded into `issues`/`by_char` above; a flat pass-through list is enough, the
    # Dashboard only needs to show them exist (the Reactions tab itself is where the detail —
    # which product, which character — already lives via its own pending/todo display).
    reaction_alerts = [
        {"kind": a["kind"], "severity": a["severity"], "location": a["location"],
         "character_name": a["character_name"], "runs": a.get("runs"), "hours_left": a.get("hours_left"),
         # Only reaction_stage_ready carries these — which stage became startable and what to
         # install — so the Dashboard can say it without opening the Reactions tab.
         "stage": a.get("stage"), "names": a.get("names")}
        for a in _all_alerts
        if a["kind"] in ("reaction_finishing_soon", "reaction_completed", "reaction_stage_ready")
    ]

    # Reactions summary for the main Overview/Maintenance cards — naturally empty/None for
    # anyone who hasn't opted into job tracking (same "empty is safe" shape reaction_alerts
    # above already relies on). Calling get_industry_jobs directly (not through its own route)
    # bypasses its require_context FastAPI dependency, which is fine here: it's always the
    # caller's OWN context_id anyway, so there's no privacy boundary being skipped. Wrapped
    # defensively — a problem on the Reactions side must never take down the core PI dashboard.
    reactions_tracked = False
    reactions_time_left_hours = reactions_net_profit = reactions_isk_committed = None
    reactions_net_profit_per_day = None
    reactions_time_left_loc = None
    reactions_progress_pct = None
    try:
        from app.reactions import get_industry_jobs
        rx = get_industry_jobs(context_id)
        if rx.get("tracked"):
            reactions_tracked = True
            reactions_net_profit = rx.get("pending_net_profit", 0.0)
            reactions_net_profit_per_day = rx.get("pending_net_profit_per_day", 0.0)
            reactions_isk_committed = rx.get("pending_isk_committed", 0.0)
            reactions_progress_pct = rx.get("running_progress_pct")
            # "Middle of the road" completion, not the soonest: the earliest-finishing job
            # badly under-represents when the whole batch is actually done. Use the MEDIAN
            # running job (a real job, so the reported time + product name stay honest) —
            # matches the Reactions tab's own "Time left" tile.
            timed = sorted((r for r in (rx.get("running") or []) if r.get("hours_left") is not None),
                           key=lambda r: r["hours_left"])
            if timed:
                med = timed[len(timed) // 2]
                reactions_time_left_hours = round(med["hours_left"], 1)
                reactions_time_left_loc = med.get("name") or types.get(
                    med["product_type_id"], {}).get("name", str(med["product_type_id"]))
    except Exception:
        pass

    # Soonest manufacturing job completion (Industry planner) — same defensive shape as reactions
    # above; a problem on the Industry side must never take down the core PI dashboard.
    manufacturing_tracked = False
    manufacturing_time_left_hours = None
    manufacturing_time_left_loc = None
    try:
        from app.industry.jobs import next_manufacturing_completion
        mf = next_manufacturing_completion(context_id)
        if mf:
            manufacturing_tracked = True
            manufacturing_time_left_hours = mf["hours_left"]
            manufacturing_time_left_loc = mf.get("name")
    except Exception:
        pass

    return {
        "logged_in": True,
        "factories": factories,
        "sync_warn": sync_warn,
        "reaction_alerts": reaction_alerts,
        "char_ids_in_view": sorted(chars_in_view),
        "issues": issues,
        "expansion": expansion,
        "pad_fill": _pad_fill_meter(parsed, pi, types),
        "totals": {
            "factory_count": len(factories),
            "runtime_hours": round(soonest_h, 1) if soonest_h is not None else None,
            "pads_value": pads_value,
            "current_run_value": round(total_run_value, 2),
            "value_per_day": round(total_value_per_day, 2),
            # Maintenance routine — DUE = countdown to the next time the job is needed (from current
            # state); HOURS = the cadence (how often it comes due once on schedule).
            "restart_due_hours": round(restart_due_h, 1) if restart_due_h is not None else None,
            "restart_due_loc": restart_due_loc,
            "restart_extractors_hours": round(sorted(e["prog_hours"] for e in ext_progs)[len(ext_progs) // 2], 1) if ext_progs else None,
            "empty_due_hours": round(empty_due_h, 1) if empty_due_h is not None else None,
            "empty_due_loc": empty_due_loc,
            "empty_pads_hours": round(empty_pads_h, 1) if empty_pads_h is not None else None,
            "empty_pads_loc": empty_pads_loc,
            "refill_due_hours": round(refill_due_h, 1) if refill_due_h is not None else None,
            "refill_due_loc": refill_due_loc,
            # The deadline as an instant, plus where it came from. Derived from the last scan, so
            # it needs no client-side store and the server can quote the same time the page does.
            "refill_due_at": round(refill_due_at) if refill_due_at is not None else None,
            "refill_due_source": refill_due_src,
            "refill_due_checkpoint": round(refill_due_ckpt) if refill_due_ckpt is not None else None,
            "refill_factories_hours": round(refill_fac_h, 1) if refill_fac_h is not None else None,
            "refill_factories_loc": refill_fac_loc,
            "reactions_tracked": reactions_tracked,
            "reactions_progress_pct": reactions_progress_pct,
            "reactions_time_left_hours": reactions_time_left_hours,
            "reactions_time_left_loc": reactions_time_left_loc,
            "reactions_net_profit": reactions_net_profit,
            "reactions_net_profit_per_day": reactions_net_profit_per_day,
            "reactions_isk_committed": reactions_isk_committed,
            "manufacturing_tracked": manufacturing_tracked,
            "manufacturing_time_left_hours": manufacturing_time_left_hours,
            "manufacturing_time_left_loc": manufacturing_time_left_loc,
        },
        "top_pi": top_pi,
    }

