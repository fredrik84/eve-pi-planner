"""Industry structures — the ME/TE (and job-cost) bonuses a player's build structure gives.

The unified structure list lives in `pp_markets` (extended with build columns): one row can be
"price from here" (a followed market — see app.markets) and/or "build here" for manufacturing
and/or reactions, with the rig tiers the player fitted. ESI can auto-detect a structure's HULL
type (→ role bonus) and SYSTEM security (→ rig multiplier) but NOT its fitting — no ESI endpoint
exposes structure rigs — so the rig tiers are a per-structure manual pick. From hull + rigs +
security this module computes the material/time reductions the planner applies.

Standard EVE figures (approximate, T2 rig set):
  Engineering Complex (Raitaru/Azbel/Sotiyo) role bonus: +1% material efficiency.
  Refinery (Athanor/Tatara) reaction role bonus: Tatara +0/… (rig-driven here).
  Manufacturing rigs (Standup M-Set): ME T1 2.0% / T2 2.4%; TE T1 20% / T2 24%.
  Reaction rigs (Standup L-Set Reactor Efficiency): ME T1 2.0% / T2 2.4%; TE T1 20% / T2 24%.
  Rig security multiplier: hi-sec ×1.0, low-sec ×1.9, null-sec / WH ×2.1.
"""

SECURITY_RIG_MULT = {"high": 1.0, "low": 1.9, "null": 2.1, "wh": 2.1}

# rig tier → base % reduction (before the security multiplier)
_ME_RIG = {0: 0.0, 1: 2.0, 2: 2.4}
_TE_RIG = {0: 0.0, 1: 20.0, 2: 24.0}

# hull → (role material %, role time %). Engineering complexes give a flat 1% ME; TE is rig-driven.
_MFG_HULL_ROLE = {"raitaru": (1.0, 0.0), "azbel": (1.0, 0.0), "sotiyo": (1.0, 0.0)}
_RX_HULL_ROLE = {"athanor": (0.0, 0.0), "tatara": (0.0, 0.0)}

# The manufacturing engineering complexes, by hull name (for the UI + auto-detect classification).
MFG_HULLS = ("raitaru", "azbel", "sotiyo")
RX_HULLS = ("athanor", "tatara")


def _sec_band(security: str | float | None) -> str:
    """Normalise a system security status (or an already-banded string) to high/low/null."""
    if isinstance(security, str):
        s = security.lower()
        if s in SECURITY_RIG_MULT:
            return "null" if s == "wh" else s
    try:
        v = float(security)
    except (TypeError, ValueError):
        return "high"
    if v >= 0.45:
        return "high"
    if v > 0.0:
        return "low"
    return "null"


def manufacturing_bonus(hull: str | None, me_rig: int, te_rig: int, security) -> tuple[float, float]:
    """(material_reduction_pct, time_reduction_pct) for a manufacturing structure."""
    smult = SECURITY_RIG_MULT.get(_sec_band(security), 1.0)
    role_me, role_te = _MFG_HULL_ROLE.get((hull or "").lower(), (0.0, 0.0))
    me = role_me + _ME_RIG.get(me_rig, 0.0) * smult
    te = role_te + _TE_RIG.get(te_rig, 0.0) * smult
    return round(me, 2), round(te, 2)


def reaction_bonus(hull: str | None, me_rig: int, te_rig: int, security) -> tuple[float, float]:
    """(material_reduction_pct, time_reduction_pct) for a reaction structure."""
    smult = SECURITY_RIG_MULT.get(_sec_band(security), 1.0)
    role_me, role_te = _RX_HULL_ROLE.get((hull or "").lower(), (0.0, 0.0))
    me = role_me + _ME_RIG.get(me_rig, 0.0) * smult
    te = role_te + _TE_RIG.get(te_rig, 0.0) * smult
    return round(me, 2), round(te, 2)


def account_build_structures(context_id: int, purpose: str) -> list[dict]:
    """The structures the account can BUILD `purpose` ('manufacturing'|'reaction') in — its own
    rows first, else its group's — each with its computed (me, te) bonus. Reads the unified
    pp_markets store (structure rows flagged build_mfg / build_rx)."""
    from app.sde import get_connection
    from app.markets import ensure_markets_table
    from app.groups import member_group
    ensure_markets_table()
    bcol = "build_mfg" if purpose == "manufacturing" else "build_rx"
    cols = "id, name, hull, security, me_rig, te_rig, rx_me_rig, rx_te_rig"
    con = get_connection()
    try:
        rows = con.execute(
            f"SELECT {cols} FROM pp_markets WHERE owner_kind='account' AND owner_id=? "
            f"AND kind='structure' AND {bcol}=1", (context_id,),
        ).fetchall()
        if not rows:
            g = member_group(context_id)
            if g:
                rows = con.execute(
                    f"SELECT {cols} FROM pp_markets WHERE owner_kind='group' AND owner_id=? "
                    f"AND kind='structure' AND {bcol}=1", (g["id"],),
                ).fetchall()
    finally:
        con.close()
    out = []
    for r in rows:
        if purpose == "manufacturing":
            me, te = manufacturing_bonus(r["hull"], r["me_rig"], r["te_rig"], r["security"])
        else:
            me, te = reaction_bonus(r["hull"], r["rx_me_rig"], r["rx_te_rig"], r["security"])
        out.append({"id": r["id"], "name": r["name"], "hull": r["hull"],
                    "security": r["security"], "me": me, "te": te})
    return out


def account_manufacturing_bonus(context_id: int, structure_id: int | None = None) -> tuple[float, float]:
    """(material %, time %) for the chosen build structure (or the best-bonus one if unspecified),
    or (0, 0) if the account has no manufacturing structure."""
    structs = account_build_structures(context_id, "manufacturing")
    if not structs:
        return (0.0, 0.0)
    if structure_id is not None:
        for s in structs:
            if s["id"] == structure_id:
                return (s["me"], s["te"])
    best = max(structs, key=lambda s: s["me"] + s["te"])
    return (best["me"], best["te"])
