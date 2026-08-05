"""Industry planner — which skills the account is MISSING to actually install a build.

The planner would happily schedule a Revelation for someone with no capital production skills: it
costed the job, timed it, and named a character to install it, none of which is possible without
the skills. This closes that gap — for every build step, the skills EVE requires to install the
job, checked against the account's real characters.

**Whose skills.** Per build step, the single character who comes closest to being able to install
it. Skills do not pool: one character installs one job, so an account-wide union would cheerfully
report a build as fine when the skills are split across two toons and no single one can start it.
"Closest" is fewest missing skills, then fewest total levels short, then lowest character id for
determinism.

**Data sources.** Requirements come from the SDE (`blueprint_skills`, built by scripts/build_sde.py
from blueprints.yaml's `activities.<activity>.skills`). Character levels come from
`pp_char_skills`, written by `store_character_skills()` off the SAME ESI response the PI skills
already use — `_fetch_skills` was fetching every skill the character has and discarding all but a
handful, so this needs no new ESI call, no new scope, and no extra rate-limit budget.

**Everything here is gated on the `required_skills` feature flag**, including the write path. With
the flag off nothing is stored and nothing is queried, so the feature costs an untouched account
exactly nothing — which is the point: it can be switched off if it misbehaves and the rest of the
planner does not notice.
"""
import logging

from app.sde import get_connection, ensure_once
from app.esi import require_context

from app.industry._router import router
from fastapi import Depends

log = logging.getLogger(__name__)

FEATURE_KEY = "required_skills"

# EVE skill levels run 0-5; a requirement of 0 means "the skill must be injected", which we cannot
# distinguish from "untrained but injected" through ESI (it reports trained_skill_level and simply
# omits skills the character has never injected). A level-0 requirement is therefore treated as
# always satisfied rather than guessed at — see _missing_for().
MAX_SKILL_LEVEL = 5


def _feature_on(context_id: int | None = None) -> bool:
    # Local import: app.features imports from app.esi, and this module is imported by the industry
    # router, so a module-level import risks a cycle.
    # Role-aware: this feature has lived on the `testers` rung, where a public-only gate answers
    # "off" for the very testers it was rolled out to. See app.features.feature_enabled_for.
    from app.features import feature_enabled_for
    return feature_enabled_for(FEATURE_KEY, context_id)


def _context_of(character_id: int) -> int | None:
    """The account a character belongs to — needed because the skill WRITE path is reached from an
    ESI scan that knows the character but not who asked."""
    con = get_connection()
    try:
        row = con.execute("SELECT context_id FROM pp_characters WHERE character_id=?",
                          (character_id,)).fetchone()
    finally:
        con.close()
    return row["context_id"] if row else None


@ensure_once
def ensure_char_skills_table():
    con = get_connection()
    try:
        con.execute("""
            CREATE TABLE IF NOT EXISTS pp_char_skills (
                character_id  INTEGER NOT NULL,
                skill_type_id INTEGER NOT NULL,
                level         INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (character_id, skill_type_id)
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_char_skills_char "
                    "ON pp_char_skills (character_id)")
        con.commit()
    finally:
        con.close()


def store_character_skills(character_id: int, levels: dict[int, int]) -> None:
    """Replace this character's full skill list. No-op unless the feature is on, so the write cost
    only exists while the feature does.

    Replace-not-merge is deliberate: a skill can never be un-trained, but a character can be
    re-scanned after the SDE moved on, and a stale row that survived a refresh would be indis-
    tinguishable from a real one. An empty `levels` is treated as a failed fetch and left alone
    rather than wiping a good list (same reasoning as the PI skill columns in app/esi.py)."""
    if not levels or not _feature_on(_context_of(character_id)):
        return
    ensure_char_skills_table()
    con = get_connection()
    try:
        con.execute("DELETE FROM pp_char_skills WHERE character_id=?", (character_id,))
        rows = [(character_id, int(sid), int(lvl or 0)) for sid, lvl in levels.items()]
        con.executemany("INSERT INTO pp_char_skills (character_id, skill_type_id, level) "
                        "VALUES (?,?,?)", rows)
        con.commit()
    except Exception:
        log.exception("failed storing skills for character %s", character_id)
    finally:
        con.close()


def _blueprint_ids(requirements: list[dict], mfg: dict, rx: dict) -> dict[int, tuple[int, str]]:
    """product type_id -> (blueprint/reaction id, activity) for every step the plan actually builds.
    `reactions.reaction_id` and `blueprints.blueprint_type_id` share one id space (both are
    blueprints.yaml keys), which is why one lookup table serves both activities."""
    out: dict[int, tuple[int, str]] = {}
    for req in requirements or []:
        tid, activity = req.get("type_id"), req.get("activity")
        if tid is None:
            continue
        if activity == "manufacturing":
            node = mfg.get(tid)
            if node:
                out[tid] = (node["blueprint_type_id"], "manufacturing")
        elif activity == "reaction":
            node = rx.get(tid)
            if node:
                out[tid] = (node["reaction_id"], "reaction")
    return out


def _load_requirements(con, pairs: set[tuple[int, str]]) -> dict[tuple[int, str], list[tuple[int, int]]]:
    """(blueprint_id, activity) -> [(skill_type_id, level)]. Returns {} rather than raising if the
    table isn't built yet, so an SDE that predates this feature degrades to "no data" instead of a
    500 (the caller reports that state explicitly)."""
    if not pairs:
        return {}
    out: dict[tuple[int, str], list[tuple[int, int]]] = {}
    ids = sorted({bp for bp, _a in pairs})
    placeholders = ",".join("?" for _ in ids)
    try:
        rows = con.execute(
            f"SELECT blueprint_type_id, activity, skill_type_id, level FROM blueprint_skills "
            f"WHERE blueprint_type_id IN ({placeholders})", tuple(ids)).fetchall()
    except Exception:
        log.warning("blueprint_skills unavailable — SDE not backfilled yet?")
        return {}
    for r in rows:
        key = (r["blueprint_type_id"], r["activity"])
        if key in pairs:
            out.setdefault(key, []).append((r["skill_type_id"], r["level"]))
    return out


def _account_characters(con, context_id: int) -> list[dict]:
    """Real (non-dummy) characters on the account with their skill map. A character with no rows
    yet is kept and flagged, not dropped — "we don't know this toon's skills" is a different
    answer from "this toon can't build it", and only the first is fixed by a rescan."""
    chars = con.execute(
        "SELECT character_id, character_name FROM pp_characters "
        "WHERE context_id=? AND COALESCE(is_dummy,0)=0 ORDER BY character_id", (context_id,)
    ).fetchall()
    if not chars:
        return []
    ids = [c["character_id"] for c in chars]
    placeholders = ",".join("?" for _ in ids)
    levels: dict[int, dict[int, int]] = {}
    try:
        for r in con.execute(
            f"SELECT character_id, skill_type_id, level FROM pp_char_skills "
            f"WHERE character_id IN ({placeholders})", tuple(ids)
        ).fetchall():
            levels.setdefault(r["character_id"], {})[r["skill_type_id"]] = r["level"]
    except Exception:
        log.warning("pp_char_skills unavailable")
    return [{"character_id": c["character_id"],
             "character_name": c["character_name"] or str(c["character_id"]),
             "skills": levels.get(c["character_id"], {}),
             "has_data": bool(levels.get(c["character_id"]))}
            for c in chars]


def _placeholder_ids(context_id: int) -> set[int]:
    """The account's placeholder characters. Its own query rather than a column on
    `_account_characters` so the gap report — which is about REAL toons and what to train on them —
    keeps seeing exactly the rows it saw before."""
    con = get_connection()
    try:
        return {r["character_id"] for r in con.execute(
            "SELECT character_id FROM pp_characters "
            "WHERE context_id=? AND COALESCE(is_dummy,0)=1", (context_id,))}
    except Exception:
        return set()
    finally:
        con.close()


def _missing_for(char: dict, required: list[tuple[int, int]]) -> list[dict]:
    """Which of `required` this character falls short on. A level-0 requirement is satisfied by
    definition (see MAX_SKILL_LEVEL note) — flagging it would produce a gap nobody can close."""
    out = []
    for skill_id, need in required:
        if need <= 0:
            continue
        have = char["skills"].get(skill_id, 0)
        if have < need:
            out.append({"skill_id": skill_id, "need": need, "have": have})
    return out


def _best_character(chars: list[dict], required: list[tuple[int, int]]):
    """The character closest to being able to install this job: fewest missing skills, then fewest
    total levels short, then lowest id. Characters with no skill data are excluded from the
    contest — with an empty skill map they look maximally unskilled and would otherwise never win,
    which would report a confident "missing everything" for a toon we simply haven't scanned."""
    known = [c for c in chars if c["has_data"]]
    if not known:
        return None, []
    scored = []
    for c in known:
        miss = _missing_for(c, required)
        scored.append((len(miss), sum(m["need"] - m["have"] for m in miss), c["character_id"], c, miss))
    scored.sort(key=lambda s: (s[0], s[1], s[2]))
    best = scored[0]
    return best[3], best[4]


def analyze_plan_skills(context_id: int, requirements: list[dict], mfg: dict, rx: dict) -> dict | None:
    """Everything this feature knows about one plan, in ONE database pass: the per-step gap report
    AND the per-step eligibility the scheduler needs to stop handing jobs to characters who cannot
    install them. Returns None when the feature is off.

    Both outputs come from the same query set deliberately — computing them separately would double
    the per-plan cost for two views of one answer.

      {"gaps": <the report the UI renders>,
       "eligibility": {"capable": {type_id: {character_id, ...}},
                       "unknown": {character_id, ...}}}

    `capable` lists only characters PROVEN to meet a step's requirements. A character we hold no
    skill data for lands in `unknown` instead: absence of data is not evidence of incapability, and
    conflating the two would either hide a real problem or invent one.
    """
    if not _feature_on(context_id):
        return None
    ensure_char_skills_table()
    bp = _blueprint_ids(requirements, mfg, rx)
    if not bp:
        return {"gaps": {"steps": [], "missing": [], "blocked_steps": 0,
                         "characters_without_data": [], "sde_ready": True},
                "eligibility": {"capable": {}, "unknown": set()}}
    con = get_connection()
    try:
        reqs = _load_requirements(con, set(bp.values()))
        chars = _account_characters(con, context_id)
        skill_ids = {sid for lst in reqs.values() for sid, _lvl in lst}
        names: dict[int, str] = {}
        if skill_ids:
            placeholders = ",".join("?" for _ in skill_ids)
            for r in con.execute(f"SELECT type_id, name FROM types WHERE type_id IN ({placeholders})",
                                 tuple(sorted(skill_ids))).fetchall():
                names[r["type_id"]] = r["name"]
    finally:
        con.close()

    by_type = {r.get("type_id"): r for r in requirements or []}
    steps: list[dict] = []
    worst: dict[int, int] = {}          # skill_id -> highest level this plan demands
    step_count: dict[int, int] = {}     # skill_id -> how many steps it blocks
    capable: dict[int, set[int]] = {}   # type_id -> characters proven able to install it
    # Placeholders carry no skills BY DEFINITION, so they are neither "capable" nor scannable. They
    # belong in `unknown` (tier 1: skill_ok None), not left out of both sets — a character absent
    # from both scores as PROVEN INCAPABLE in skill_tier(), which is a claim we have no basis for
    # and would make a placeholder with declared slots the last resort for every job. Unknown is the
    # honest answer, and the one this module already gives an unscanned real character. They are
    # deliberately kept out of `characters_without_data`, which tells the user to rescan — there is
    # nothing to rescan on a placeholder.
    unknown = {c["character_id"] for c in chars if not c["has_data"]} | _placeholder_ids(context_id)
    for tid, key in bp.items():
        required = reqs.get(key) or []
        # A step with no listed requirement is installable by anyone — record that explicitly
        # rather than leaving it absent, so the scheduler can tell "no requirement" apart from
        # "not analysed" without a second lookup.
        capable[tid] = {c["character_id"] for c in chars
                        if c["has_data"] and not _missing_for(c, required)}
        if not required:
            continue
        char, missing = _best_character(chars, required)
        if not missing:
            continue
        for m in missing:
            worst[m["skill_id"]] = max(worst.get(m["skill_id"], 0), m["need"])
            step_count[m["skill_id"]] = step_count.get(m["skill_id"], 0) + 1
        req_row = by_type.get(tid) or {}
        steps.append({
            "type_id": tid,
            "name": req_row.get("name") or str(tid),
            "activity": req_row.get("activity"),
            "character_id": char["character_id"] if char else None,
            "character_name": char["character_name"] if char else None,
            "missing": [{**m, "name": names.get(m["skill_id"], str(m["skill_id"]))} for m in missing],
        })
    steps.sort(key=lambda s: (-len(s["missing"]), s["name"]))
    missing_summary = sorted(
        ({"skill_id": sid, "name": names.get(sid, str(sid)), "level": lvl,
          "steps": step_count.get(sid, 0)} for sid, lvl in worst.items()),
        key=lambda m: (-m["steps"], -m["level"], m["name"]),
    )
    return {
        "gaps": {
            "steps": steps,
            "missing": missing_summary,
            "blocked_steps": len(steps),
            # Named so the UI can say "rescan this character" instead of implying they can't build.
            "characters_without_data": [c["character_name"] for c in chars if not c["has_data"]],
            "sde_ready": bool(reqs),
        },
        "eligibility": {"capable": capable, "unknown": unknown},
    }


def plan_skill_gaps(context_id: int, requirements: list[dict], mfg: dict, rx: dict) -> dict | None:
    """Just the gap report — the thin wrapper callers use when they don't also need eligibility."""
    res = analyze_plan_skills(context_id, requirements, mfg, rx)
    return None if res is None else res["gaps"]


@router.get("/api/industry/skill-coverage")
def industry_skill_coverage(ctx: int = Depends(require_context)):
    """Diagnostic: whether the two data sources this feature needs are actually populated, per
    account. Cheap to call and the first thing to check when the panel says nothing — it separates
    "SDE not backfilled" from "characters never rescanned since the flag went on"."""
    if not _feature_on(ctx):
        return {"enabled": False}
    ensure_char_skills_table()
    con = get_connection()
    try:
        try:
            sde_rows = con.execute("SELECT COUNT(*) AS n FROM blueprint_skills").fetchone()["n"]
        except Exception:
            sde_rows = 0
        chars = _account_characters(con, ctx)
    finally:
        con.close()
    return {
        "enabled": True,
        "sde_skill_rows": sde_rows,
        "characters": [{"character_id": c["character_id"], "character_name": c["character_name"],
                        "skills_known": len(c["skills"])} for c in chars],
    }
