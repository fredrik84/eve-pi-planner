"""Industry structures — the ME/TE (and job-cost) bonuses a player's build structure gives, and
which JOBS each one actually gives them to.

The unified structure list lives in `pp_markets` (extended with build columns): one row can be
"price from here" (a followed market — see app.markets) and/or "build here" for manufacturing
and/or reactions, with the rig tiers the player fitted. ESI can auto-detect a structure's HULL
type (→ role bonus), its SYSTEM (→ job cost index) and that system's SECURITY (→ rig multiplier)
but NOT its fitting — no ESI endpoint exposes structure rigs — so the rig tiers, and which rig
families they are, are a per-structure manual pick.

**Rigs are not generic.** A Standup M-Set rig covers ONE family of things (Capital Ship
Manufacturing, Advanced Component Manufacturing, Equipment, Ammunition, …), so a structure rigged
for capital parts gives a battleship hull nothing but its hull role bonus. Modelling every rig as
applying to every job is what made the planner overstate ME/TE savings on any build that spans
families — exactly the case a capital builder lives in, running parts in one structure and the
hull in another. So a rig carries the **families it covers** and the bonus is resolved per JOB
against the produced type's SDE group.

Standard EVE figures (approximate, T2 rig set):
  Engineering Complex (Raitaru/Azbel/Sotiyo) role bonus: +1% material efficiency.
  Refinery (Athanor/Tatara) reaction role bonus: rig-driven here.
  Manufacturing rigs (Standup M-Set): ME T1 2.0% / T2 2.4%; TE T1 20% / T2 24%.
  Reaction rigs (Standup L-Set Reactor Efficiency): ME T1 2.0% / T2 2.4%; TE T1 20% / T2 24%.
  Rig security multiplier: hi-sec ×1.0, low-sec ×1.9, null-sec / WH ×2.1.

**What the SDE actually supports for family matching.** `scripts/build_sde.py` imports
`types(type_id, name, group_id, pi_tier, volume)` — the real EVE `groupID` — and NOTHING else of
the taxonomy: no `groups` table, so no group NAME and no `categoryID`, and no meta level / tech
level anywhere. Consequences, stated rather than papered over:
  * Family membership has to be a curated group-id map in code (`_G_*` below, generated from
    ESI's `/universe/categories/{id}/` — the ids are stable SDE groupIDs).
  * The M-Set line splits ships by SIZE and by BASIC vs ADVANCED (T1 vs T2): "Basic Medium Ship
    Manufacturing", "Advanced Large Ship Manufacturing", and so on. Size is not a column we have
    and tech level is not in our SDE subset at all, so those cannot be told apart here. Sub-capital
    hulls are therefore ONE family (`ship`); a structure carrying only "Advanced Large Ship" rigs
    is modelled as covering every sub-capital hull, which OVER-states its coverage. Capital hulls
    are their own family and are matched exactly.
  * A group that is in no family is simply not covered by a narrowed rig, which understates rather
    than overstates — the safe direction, and visible (the job reports the site it was costed in).
"""
from dataclasses import dataclass, field

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


# ── Rig families → SDE group ids ───────────────────────────────────────────────────────────────
# Generated from ESI `/universe/categories/{id}/` (Ship 6, Module 7, Charge 8, Drone 18,
# Fighter 87, Structure 65, Structure Module 66) plus the named Commodity component groups. These
# are SDE groupIDs, which is all `types` carries — see the module docstring.

# Capital hulls, matched exactly: Titan, Dreadnought, Lancer Dreadnought, Carrier, Command Carrier,
# Supercarrier, Force Auxiliary, Capital Industrial Ship, Freighter, Jump Freighter.
_G_CAPITAL_SHIP = frozenset({30, 485, 4594, 547, 5120, 659, 1538, 883, 513, 902})

# Every other hull in category 6. One family — size/tech can't be told apart (see docstring).
_G_SHIP = frozenset({
    25, 26, 27, 28, 29, 31, 237, 324, 358, 380, 381, 419, 420, 463, 540, 541, 543, 830, 831,
    832, 833, 834, 893, 894, 898, 900, 906, 941, 963, 1022, 1201, 1202, 1283, 1305, 1527, 1534,
    1972, 2001, 4902, 5087,
})

_G_MODULE = frozenset({
    38, 39, 40, 41, 43, 46, 47, 48, 49, 52, 53, 54, 55, 56, 57, 59, 60, 61, 62, 63, 65, 67, 68,
    71, 72, 74, 76, 77, 78, 80, 82, 96, 98, 201, 202, 203, 205, 208, 209, 210, 211, 212, 213,
    225, 285, 289, 290, 291, 295, 302, 308, 309, 315, 316, 317, 321, 325, 326, 328, 329, 330,
    338, 339, 341, 353, 357, 367, 378, 379, 405, 406, 407, 464, 472, 475, 481, 483, 499, 501,
    506, 507, 508, 509, 510, 511, 512, 514, 515, 518, 524, 538, 546, 585, 586, 588, 589, 590,
    638, 642, 644, 645, 646, 647, 650, 658, 660, 737, 753, 762, 763, 764, 765, 766, 767, 768,
    769, 770, 771, 773, 774, 775, 776, 777, 778, 779, 781, 782, 786, 815, 842, 862, 878, 896,
    899, 901, 904, 905, 1122, 1150, 1154, 1156, 1189, 1199, 1223, 1226, 1232, 1233, 1234, 1238,
    1245, 1289, 1292, 1299, 1306, 1308, 1313, 1395, 1396, 1533, 1672, 1673, 1674, 1697, 1698,
    1699, 1700, 1706, 1770, 1815, 1894, 1969, 1986, 1988, 2003, 2004, 2008, 2013, 2018, 4060,
    4067, 4117, 4127, 4138, 4174, 4184, 4769, 4807,
})

_G_CHARGE = frozenset({
    83, 85, 86, 87, 88, 89, 90, 92, 372, 373, 374, 375, 376, 377, 384, 385, 386, 387, 394, 395,
    396, 425, 476, 479, 482, 492, 497, 498, 500, 548, 648, 653, 654, 655, 656, 657, 663, 772,
    863, 864, 892, 907, 908, 909, 910, 911, 916, 972, 1010, 1019, 1153, 1158, 1400, 1546, 1547,
    1548, 1549, 1550, 1551, 1559, 1569, 1677, 1678, 1701, 1702, 1769, 1771, 1772, 1773, 1774,
    1976, 1987, 1989, 4061, 4062, 4088, 4186, 4808, 4905,
})

# Drones (category 18) and fighters (category 87) — one rig covers both in the M-Set line.
_G_DRONE = frozenset({
    97, 100, 101, 299, 470, 544, 545, 549, 639, 640, 641, 1023, 1159, 1537, 1652, 1653, 4777,
    4778, 4779,
})

# Upwell structures (category 65) + structure modules/rigs/services (category 66).
_G_STRUCTURE = frozenset({
    1321, 1322, 1323, 1324, 1325, 1326, 1327, 1328, 1329, 1330, 1331, 1332, 1333, 1404, 1405,
    1406, 1407, 1408, 1409, 1410, 1415, 1416, 1429, 1430, 1441, 1442, 1535, 1562, 1570, 1579,
    1580, 1581, 1582, 1583, 1584, 1585, 1586, 1587, 1588, 1589, 1590, 1591, 1592, 1593, 1594,
    1595, 1596, 1598, 1599, 1600, 1601, 1602, 1603, 1604, 1605, 1606, 1607, 1608, 1609, 1610,
    1611, 1612, 1613, 1614, 1615, 1616, 1617, 1618, 1619, 1620, 1621, 1622, 1629, 1630, 1631,
    1632, 1633, 1634, 1635, 1639, 1640, 1641, 1642, 1647, 1648, 1649, 1657, 1693, 1694, 1695,
    1696, 1717, 1719, 1816, 1819, 1820, 1821, 1822, 1823, 1824, 1825, 1826, 1827, 1828, 1829,
    1830, 1831, 1832, 1833, 1834, 1835, 1836, 1837, 1838, 1839, 1840, 1841, 1842, 1843, 1844,
    1845, 1846, 1847, 1848, 1849, 1850, 1851, 1852, 1853, 1854, 1855, 1856, 1857, 1858, 1859,
    1860, 1861, 1862, 1863, 1864, 1865, 1866, 1867, 1868, 1869, 1870, 1876, 1887, 1912, 1913,
    1914, 1924, 1933, 1934, 1935, 1936, 1937, 1938, 1939, 1941, 1942, 1943, 1944, 1945, 1962,
    1966, 1967, 1968, 1974, 1984, 2015, 2016, 2017, 4086, 4603, 4644, 4744,
})

# Commodity component groups (category 17), named individually because the rigs are:
#   873 Capital Construction Components          — "Basic Capital Component Manufacturing"
#   913 Advanced Capital Construction Components } "Advanced Component Manufacturing"
#   334 Construction Components (T2 components)  }
#   964 Hybrid Tech Components                   }
#   536 Structure Components                     — "Structure Component Manufacturing"
_G_CAPITAL_COMPONENT = frozenset({873})
_G_ADVANCED_COMPONENT = frozenset({913, 334, 964})
_G_STRUCTURE_COMPONENT = frozenset({536})

# Reaction outputs (category 4 Material), one family per Standup L-Set reactor rig.
_G_COMPOSITE = frozenset({429, 428})        # Composite + Intermediate Materials
_G_HYBRID_POLYMER = frozenset({974})
_G_BIOCHEMICAL = frozenset({712, 20})       # Biochemical Material + Drug (boosters)


# key → what the UI calls it, which activity it belongs to, and the groups it covers. The key is
# what a structure row stores, so DON'T rename one without a migration.
RIG_FAMILIES: dict[str, dict] = {
    "capital_ship":        {"label": "Capital Ship Manufacturing",
                            "activity": "manufacturing", "groups": _G_CAPITAL_SHIP},
    "capital_component":   {"label": "Basic Capital Component Manufacturing",
                            "activity": "manufacturing", "groups": _G_CAPITAL_COMPONENT},
    "advanced_component":  {"label": "Advanced Component Manufacturing",
                            "activity": "manufacturing", "groups": _G_ADVANCED_COMPONENT},
    "ship":                {"label": "Ship Manufacturing (sub-capital)",
                            "activity": "manufacturing", "groups": _G_SHIP},
    "equipment":           {"label": "Equipment Manufacturing",
                            "activity": "manufacturing", "groups": _G_MODULE},
    "ammunition":          {"label": "Ammunition Manufacturing",
                            "activity": "manufacturing", "groups": _G_CHARGE},
    "drone":               {"label": "Drone and Fighter Manufacturing",
                            "activity": "manufacturing", "groups": _G_DRONE},
    "structure":           {"label": "Structure Manufacturing",
                            "activity": "manufacturing", "groups": _G_STRUCTURE},
    "structure_component": {"label": "Structure Component Manufacturing",
                            "activity": "manufacturing", "groups": _G_STRUCTURE_COMPONENT},
    "composite":           {"label": "Composite Reactor Efficiency",
                            "activity": "reaction", "groups": _G_COMPOSITE},
    "hybrid_polymer":      {"label": "Hybrid Reactor Efficiency",
                            "activity": "reaction", "groups": _G_HYBRID_POLYMER},
    "biochemical":         {"label": "Biochemical Reactor Efficiency",
                            "activity": "reaction", "groups": _G_BIOCHEMICAL},
}


def family_registry(activity: str | None = None) -> list[dict]:
    """The pickable rig families, for the UI. Never hardcode this list in the frontend — same rule
    as ALERT_KINDS: one registry, echoed back, so labels can't drift."""
    return [{"key": k, "label": v["label"], "activity": v["activity"]}
            for k, v in RIG_FAMILIES.items() if activity is None or v["activity"] == activity]


def covers(families, group_id: int | None) -> bool:
    """Does a rig covering `families` apply to a product in SDE group `group_id`?

    **An empty / missing selection means EVERY group.** That is the whole compatibility promise:
    a structure that already has rig tiers set and has never been told what they are for keeps
    quoting exactly what it quotes today. Narrowing is an explicit choice — silently cutting
    everyone's efficiency on deploy would be the worst possible way to ship this.
    """
    if not families:
        return True
    if group_id is None:
        return False        # narrowed rig + unknown group → don't claim a bonus we can't justify
    for key in families:
        fam = RIG_FAMILIES.get(key)
        if fam and group_id in fam["groups"]:
            return True
    return False


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


def manufacturing_bonus(hull: str | None, me_rig: int, te_rig: int, security,
                        me_applies: bool = True, te_applies: bool = True) -> tuple[float, float]:
    """(material_reduction_pct, time_reduction_pct) for ONE manufacturing job in this structure.

    `me_applies` / `te_applies` are the per-JOB half: a rig whose families don't cover the product
    contributes nothing, and the job keeps only the hull ROLE bonus (which is a structure-wide
    bonus, not a rig, so it always applies). Both default True = the old whole-structure answer.
    """
    smult = SECURITY_RIG_MULT.get(_sec_band(security), 1.0)
    role_me, role_te = _MFG_HULL_ROLE.get((hull or "").lower(), (0.0, 0.0))
    me = role_me + (_ME_RIG.get(me_rig, 0.0) * smult if me_applies else 0.0)
    te = role_te + (_TE_RIG.get(te_rig, 0.0) * smult if te_applies else 0.0)
    return round(me, 2), round(te, 2)


def reaction_bonus(hull: str | None, me_rig: int, te_rig: int, security,
                   me_applies: bool = True, te_applies: bool = True) -> tuple[float, float]:
    """(material_reduction_pct, time_reduction_pct) for ONE reaction job. Same per-job rule as
    `manufacturing_bonus` — an L-Set Composite rig does nothing for a biochemical reaction."""
    smult = SECURITY_RIG_MULT.get(_sec_band(security), 1.0)
    role_me, role_te = _RX_HULL_ROLE.get((hull or "").lower(), (0.0, 0.0))
    me = role_me + (_ME_RIG.get(me_rig, 0.0) * smult if me_applies else 0.0)
    te = role_te + (_TE_RIG.get(te_rig, 0.0) * smult if te_applies else 0.0)
    return round(me, 2), round(te, 2)


# ── Per-job routing across the account's build structures ─────────────────────────────────────

@dataclass(frozen=True)
class BuildSite:
    """One place a job could be installed: a configured structure, or the account's currently
    selected facility (the preset dropdown), which is always in the running so routing can only
    ever improve on what the plan quotes today."""
    key: str                         # "s:<market row id>" | "account"
    name: str
    activity: str                    # "manufacturing" | "reaction"
    hull: str | None = None
    security: str | float | None = None
    me_rig: int = 0
    te_rig: int = 0
    me_families: tuple = ()
    te_families: tuple = ()
    system_id: int | None = None
    cost_index: float = 0.0          # system cost index for THIS site's system
    tax_pct: float = 0.0             # facility tax % charged here
    # A flat structure whose bonuses are given, not computed from hull+rigs (the preset dropdown,
    # and the reaction baseline that bakes in the T1 reactor rig the engine has always assumed).
    flat: tuple | None = None        # (me_pct, te_pct)

    def bonus_for(self, group_id: int | None) -> tuple[float, float]:
        if self.flat is not None:
            return self.flat
        fn = manufacturing_bonus if self.activity == "manufacturing" else reaction_bonus
        return fn(self.hull, self.me_rig, self.te_rig, self.security,
                  covers(self.me_families, group_id), covers(self.te_families, group_id))


# Below this many percentage points of ME (or TE) two sites are the same site as far as a builder
# is concerned. Routing a job away from where its consumer is built means HAULING the output
# there, and a trip for a rounding error is not a saving — so a difference this small loses to
# staying put. Free savings are still taken: anything above the band wins normally.
ROUTE_NOISE_PCT = 0.1


def route_job(sites: list[BuildSite], group_id: int | None, fee_extra: float = 0.0,
              prefer: str | None = None) -> dict | None:
    """Pick the site ONE job is costed, timed and installed in. Returns None with no candidates.

    The rule, in order: **most material reduction, then most time reduction, then stay where the
    consumer already is, then the cheapest job fee.** Materials are what net cost is made of and
    net cost is the floor under the quote, so ME leads; TE only moves when the delivery lands,
    which the scheduler then re-prices for free; `prefer` (the site this job's consumer was routed
    to) breaks a tie towards not moving the parts at all; the fee settles what is left.
    This is the math deciding, not a knob (CLAUDE.md rule 3) — there is no user-facing choice of
    where a component is built, only the structures they told us they have.

    Freight is deliberately NOT priced in. The plan reports every station change it introduced
    (see `plan_queue`'s `moves`) and leaves the "is 2% ME worth the trip" call to the builder — a
    rejection rule built on volumes we have never verified would refuse sensible moves and be
    impossible to debug.
    """
    q = lambda v: round(v / ROUTE_NOISE_PCT)     # differences inside the band tie
    best = None
    for s in sites:
        me, te = s.bonus_for(group_id)
        fee = s.cost_index + s.tax_pct / 100.0 + fee_extra
        rank = (q(me), q(te), 1 if (prefer and s.key == prefer) else 0, -fee)
        if best is None or rank > best[0]:
            best = (rank, {
                "key": s.key, "name": s.name, "system_id": s.system_id,
                "me_pct": me, "te_pct": te,
                "material_mult": 1.0 - me / 100.0, "time_mult": 1.0 - te / 100.0,
                "cost_index": s.cost_index, "tax_pct": s.tax_pct,
            })
    return best[1] if best else None
