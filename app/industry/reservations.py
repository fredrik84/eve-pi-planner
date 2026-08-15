"""What the account has already PROMISED, so two plans cannot spend the same units.

`owned_quantities` reads the asset tables raw: it says what is in the hangar, not what is still
free. That was fine while stock meant "what you happen to hold", and it stopped being fine the
moment work could be assigned to a slot without the materials leaving the box yet. Between planning
and installing, a unit is claimed but still sitting there — so a second planning run, or the other
service entirely, saw it and promised it again. Reactions documented the gap outright (*"there is no
reservation ledger"*); this is that ledger.

**Derived, not stored.** There is no reservation table and there deliberately is not one: a stored
ledger has to be written on every commit and released on every completion, cancellation, expiry and
manual edit, and every path that forgets one leaks a claim that nothing will ever release. The
claims are already implied by state the app keeps — a pending reaction assignment IS a claim on its
inputs — so deriving them cannot drift out of step with the thing they describe. It costs one query
and a recipe lookup, memoised per request.

**Why reaction assignments are the whole answer, and Industry orders are not missing.** Two
Industry planning runs over the same queue allocate stock first-come-first-served down the same
list and reach the same answer, so Industry cannot double-promise against itself. What it could not
see was the OTHER service: goo already assigned to a reactor still read as free. That is the leak,
and it runs both ways — a second reactions run would re-spend it too. Reaction assignments are also
exactly what the user meant by *"what is assigned to slots"*: a row is a job waiting on a character.

**A claim ends when the materials do.** An assignment whose job is actually running has already
consumed its inputs — they left the container, so the next asset scan reports the truth on its own
and a reservation on top of that would subtract them twice. Only rows with no live job count.
"""
from app.sde import get_connection
from app.cache import request_memo


def _reaction_inputs() -> dict[int, list[tuple[int, int]]]:
    """{output_type_id: [(input_type_id, qty_per_run), …]} — ONE recipe per output.

    A product can be made by more than one reaction, and the rows must NOT be unioned: doing so
    charged a claim for every recipe that could produce the type, which double-counted the inputs of
    anything with two formulas (measured: 1400 units reserved where the assignment needs 700). The
    assignment does not record which formula it will run, so this takes the lowest `reaction_id` —
    the same first-listed formula `_load_reactions` hands the planner, so the reservation matches
    the recipe the plan was built from rather than a second one nobody chose.
    """
    def _build():
        by_out: dict[int, int] = {}
        rows: dict[int, list[tuple[int, int]]] = {}
        con = get_connection()
        try:
            for r in con.execute("SELECT reaction_id, output_type_id FROM reactions "
                                 "ORDER BY reaction_id"):
                d = dict(r)
                by_out.setdefault(int(d["output_type_id"]), int(d["reaction_id"]))
            for r in con.execute("SELECT reaction_id, type_id, quantity FROM reaction_inputs"):
                d = dict(r)
                rows.setdefault(int(d["reaction_id"]), []).append(
                    (int(d["type_id"]), int(d["quantity"] or 0)))
        finally:
            con.close()
        return {out_t: rows.get(rid, []) for out_t, rid in by_out.items()}
    return request_memo(("reaction_inputs_map",), _build)


def _running_type_ids(context_id: int) -> set[int]:
    """Products this account has a LIVE reaction job for, per the cached ESI job list.

    A row whose job is running has already spent its inputs, so reserving them again would subtract
    the same units twice — once here and once by their absence from the next asset scan. Failing to
    read the jobs returns an empty set, which reserves MORE rather than less: the safe direction,
    since over-reserving only makes a plan buy something it might have had, while under-reserving
    promises units that are gone.
    """
    out: set[int] = set()
    try:
        import json
        con = get_connection()
        try:
            rows = con.execute(
                "SELECT j.jobs_json FROM pp_char_industry_jobs j JOIN pp_characters c "
                "ON c.character_id = j.character_id WHERE c.context_id = ?", (context_id,)
            ).fetchall()
        finally:
            con.close()
        for r in rows:
            for j in json.loads(dict(r).get("jobs_json") or "[]"):
                if str(j.get("status") or "").lower() in ("active", "paused", ""):
                    tid = j.get("product_type_id")
                    if tid:
                        out.add(int(tid))
    except Exception:
        return set()
    return out


def reserved_quantities(context_id: int) -> dict[int, float]:
    """{type_id: units} already claimed by work assigned to a slot but not yet installed.

    Empty unless `stock_reservations` is on, and empty on any failure — no reservations is the
    behaviour that shipped before this existed, so it is the safe direction to fall back to.
    """
    def _build() -> dict[int, float]:
        try:
            from app.features import feature_enabled_for
            if not feature_enabled_for("stock_reservations", context_id):
                return {}
        except Exception:
            return {}
        try:
            recipes = _reaction_inputs()
            running = _running_type_ids(context_id)
            con = get_connection()
            try:
                rows = con.execute(
                    "SELECT a.type_id AS type_id, a.runs AS runs FROM pp_reaction_assignments a "
                    "JOIN pp_characters c ON c.character_id = a.character_id "
                    "WHERE c.context_id = ?", (context_id,)
                ).fetchall()
            finally:
                con.close()
            out: dict[int, float] = {}
            for r in rows:
                d = dict(r)
                tid, runs = int(d["type_id"]), int(d["runs"] or 0)
                if runs <= 0 or tid in running:
                    continue
                for in_t, per_run in recipes.get(tid, ()):
                    out[in_t] = out.get(in_t, 0.0) + per_run * runs
            return out
        except Exception:
            return {}
    return request_memo(("reserved_quantities", context_id), _build)


def net_of_reservations(context_id: int, pool: dict[int, float]) -> dict[int, float]:
    """`pool` minus what is already promised, floored at zero and with empties dropped.

    The single place the subtraction happens, so every reader of stock nets the same claims off the
    same pool — two readers disagreeing about what is free is the whole defect.
    """
    res = reserved_quantities(context_id)
    if not res:
        return pool
    out: dict[int, float] = {}
    for tid, qty in pool.items():
        left = qty - res.get(tid, 0.0)
        if left > 0:
            out[tid] = left
    return out
