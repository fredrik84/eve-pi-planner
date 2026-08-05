#!/usr/bin/env python3
"""A build structure described BY HAND — no ESI character, no structure-search scopes.

Adding a structure used to start at `GET /api/markets/search`, which needs a character holding the
market scope; everything after that (hull, rigs, families, tax) was already typed. `POST
/api/markets/manual` closes that gap by minting the location id ourselves. What must stay true:

  * it works with NO character in the context at all — that is the whole point;
  * the minted id is NEGATIVE and globally below every existing one, so it can never be confused
    with (or collide with) a real EVE structure id, on this account or any other;
  * the system is stored and the SECURITY BAND is DERIVED from it, never asked for;
  * `price_from` is off on creation and CANNOT be turned on — a minted id resolves to no market,
    so pricing from it would silently return an empty book. It must also stay out of
    `effective_markets` even if a row somehow carries the flag;
  * it is a real build structure: `build_structures()` returns it, which is what the Industry
    facility dropdown's `s:` entries are built from;
  * `_detect_structure_meta` (the ESI read) is never called for a manual row, and the ESI-scanned
    path is completely unchanged — same detection, same price_from toggle;
  * delete and reorder work on it through the existing endpoints.

In-process; run inside the container against a NON-PROD database. Seeds under fabricated context
ids and removes everything in a finally.

    docker compose cp test_manual_structures.py web:/srv/app/ && \
      docker compose exec web python3 test_manual_structures.py
"""
import sys

sys.path.insert(0, ".")
from app.db import get_connection                       # noqa: E402
import app.markets as M                                 # noqa: E402
from app.industry.structures import MFG_HULLS, RX_HULLS  # noqa: E402

CTX = -98801
OTHER_CTX = -98802
SYSTEMS = [
    ("ZZmanual-Hi", "ZZ Manual Constellation", 0.91, 39980001),
    ("ZZmanual-Lo", "ZZ Manual Constellation", 0.34, 39980002),
    ("ZZmanual-Null", "ZZ Manual Constellation", -0.19, 39980003),
]

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _rows(ctx=CTX):
    con = get_connection()
    try:
        return [dict(r) for r in con.execute(
            "SELECT * FROM pp_markets WHERE owner_kind='account' AND owner_id=? ORDER BY id",
            (ctx,)).fetchall()]
    finally:
        con.close()


def _seed_geo():
    con = get_connection()
    for name, constel, sec, sid in SYSTEMS:
        con.execute("INSERT INTO system_geo (system, constellation, security, system_id) "
                    "VALUES (?,?,?,?)", (name, constel, sec, sid))
    con.commit()
    con.close()


def _cleanup():
    con = get_connection()
    for ctx in (CTX, OTHER_CTX):
        con.execute("DELETE FROM pp_markets WHERE owner_kind='account' AND owner_id=?", (ctx,))
    for name, *_ in SYSTEMS:
        con.execute("DELETE FROM system_geo WHERE system=?", (name,))
    con.commit()
    con.close()


class _Env:
    """Flag forced on (its live rung is an admin setting, so a test must not assert against it) and
    `_detect_structure_meta` replaced by a spy — every manual path must leave the spy untouched."""

    def __init__(self, flag=True, detect=("azbel", "low", 30000142)):
        self.flag, self.detect, self.calls = flag, detect, []

    def __enter__(self):
        self._real = (M._manual_structures_on, M._detect_structure_meta, M.member_group)
        M._manual_structures_on = lambda ctx: self.flag
        def _spy(ctx, sid):
            self.calls.append(sid)
            return self.detect
        M._detect_structure_meta = _spy
        M.member_group = lambda ctx: None      # no alliance group in a fabricated context
        return self

    def __exit__(self, *a):
        M._manual_structures_on, M._detect_structure_meta, M.member_group = self._real


def _add(name="ZZ Manual Azbel", hull="azbel", system="ZZmanual-Null", ctx=CTX):
    return M.add_manual_structure(
        M.ManualStructureAdd(name=name, hull=hull, system=system), context_id=ctx)


def main():
    print("Manual (hand-described) build structures")
    _cleanup()
    M.ensure_markets_table()
    _seed_geo()
    try:
        # ── Created with nothing connected ──────────────────────────────────────────────
        print("\nCreation with no ESI character")
        with _Env() as env:
            con = get_connection()
            chars = con.execute("SELECT COUNT(*) AS c FROM pp_characters WHERE context_id=?",
                                (CTX,)).fetchone()["c"]
            con.close()
            check(chars == 0, "the test context has no characters at all")
            payload = _add()
            rows = _rows()
            check(len(rows) == 1, "one structure row created")
            r = rows[0]
            check(r["kind"] == "structure", "stored as a structure")
            check(r["name"] == "ZZ Manual Azbel", "keeps the typed name")
            check(r["hull"] == "azbel", "keeps the picked hull")
            check(not env.calls, "_detect_structure_meta was NOT called for a manual row")
            check(any(m["id"] == r["id"] for m in payload.get("markets", [])),
                  "_markets_payload lists it without raising")

            # ── The synthetic id ────────────────────────────────────────────────────────
            print("\nThe minted location id")
            check(r["location_id"] < 0, f"location_id is negative ({r['location_id']})")
            con = get_connection()
            gmin = con.execute("SELECT MIN(location_id) AS m FROM pp_markets").fetchone()["m"]
            con.close()
            check(r["location_id"] == gmin, "it is the globally minimum location id")
            _add(name="ZZ Manual Two", system="ZZmanual-Hi")
            _add(name="ZZ Manual Other Account", system="ZZmanual-Hi", ctx=OTHER_CTX)
            ids = [x["location_id"] for x in _rows()] + [x["location_id"] for x in _rows(OTHER_CTX)]
            check(len(set(ids)) == 3, "ids are unique ACROSS accounts, not just within one")
            check(all(i < 0 for i in ids), "every minted id stays negative")
            check(M.is_manual_location(min(ids)) and not M.is_manual_location(60003760),
                  "is_manual_location tells a minted id from a real structure id")

            # ── System + derived security ───────────────────────────────────────────────
            print("\nSystem, and the security derived from it")
            check(r["system_id"] == 39980003, "the picked system's id is stored")
            check(r["security"] == "null", "-0.19 sec system → null band, derived not asked")
            hi = [x for x in _rows() if x["name"] == "ZZ Manual Two"][0]
            check(hi["security"] == "high" and hi["system_id"] == 39980001,
                  "0.91 sec system → high band")
            lo = _add(name="ZZ Manual Low", system="ZZmanual-Lo") and \
                [x for x in _rows() if x["name"] == "ZZ Manual Low"][0]
            check(lo["security"] == "low", "0.34 sec system → low band")

            # ── Pricing is refused ──────────────────────────────────────────────────────
            print("\nprice_from can never be turned on")
            check(all(x["price_from"] == 0 for x in _rows()), "created with price_from off")
            M.set_market_build(r["id"], M.MarketBuildConfig(build_mfg=True, me_rig=2, te_rig=1,
                                                            price_from=True), context_id=CTX)
            again = [x for x in _rows() if x["id"] == r["id"]][0]
            check(again["price_from"] == 0, "asking for price_from=True through /build is refused")
            check(again["me_rig"] == 2 and again["te_rig"] == 1,
                  "the SAME build endpoint still sets rigs (no second config form)")
            check(not env.calls, "/build did not call _detect_structure_meta for a manual row")
            con = get_connection()
            con.execute("UPDATE pp_markets SET price_from=1 WHERE id=?", (r["id"],))
            con.commit()
            con.close()
            eff = [m["id"] for m in M.effective_markets(CTX)]
            check(r["id"] not in eff,
                  "even a row forced price_from=1 in the DB stays out of the pricing chain")
            con = get_connection()
            con.execute("UPDATE pp_markets SET price_from=0 WHERE id=?", (r["id"],))
            con.commit()
            con.close()

            # ── It is a build structure ─────────────────────────────────────────────────
            print("\nIt reaches the Industry facility dropdown")
            bs = M.build_structures(CTX)
            mine = [b for b in bs if b["id"] == r["id"]]
            check(len(mine) == 1, "build_structures() returns it (the source of the 's:' options)")
            check(mine[0].get("mfg_bonus", {}).get("me", 0) > 0,
                  f"it carries a real ME bonus ({mine[0].get('mfg_bonus')}) — hull + rigs + derived security")
            rx = _add(name="ZZ Manual Tatara", hull="tatara", system="ZZmanual-Null")
            trow = [x for x in _rows() if x["name"] == "ZZ Manual Tatara"][0]
            check(trow["build_rx"] == 1 and trow["build_mfg"] == 0,
                  "a refinery hull defaults to reactions, an engineering complex to manufacturing")

            # ── Reorder + delete ────────────────────────────────────────────────────────
            print("\nReorder and delete")
            ids_now = [x["id"] for x in _rows()]
            M.reorder_markets(M.MarketReorder(order=list(reversed(ids_now))), context_id=CTX)
            after = [x["id"] for x in _rows()]
            check(after == ids_now, "reorder accepts a manual row (rows come back priority-ordered)")
            prios = {x["id"]: x["priority"] for x in _rows()}
            check(prios[ids_now[0]] == len(ids_now) - 1, "its priority really moved")
            M.delete_market(r["id"], scope="account", context_id=CTX)
            check(all(x["id"] != r["id"] for x in _rows()),
                  "the existing DELETE endpoint removes a manual structure")

        # ── The ESI path is unchanged ───────────────────────────────────────────────────
        print("\nThe ESI-scanned path is untouched")
        with _Env() as env:
            M.add_market(M.MarketAdd(kind="structure", location_id=60003760, name="ZZ Real Struct",
                                     price_from=True, build_mfg=True), context_id=CTX)
            real = [x for x in _rows() if x["name"] == "ZZ Real Struct"][0]
            check(env.calls == [60003760], "_detect_structure_meta IS still called for a real id")
            check(real["hull"] == "azbel" and real["security"] == "low"
                  and real["system_id"] == 30000142, "its detected hull/security/system are stored")
            check(real["location_id"] == 60003760, "the real id is stored as given")
            check(real["price_from"] == 1, "a real structure can still be priced from")
            M.set_market_build(real["id"], M.MarketBuildConfig(build_mfg=True, price_from=True),
                               context_id=CTX)
            check([x for x in _rows() if x["id"] == real["id"]][0]["price_from"] == 1,
                  "and /build can still turn price_from on for it")
            try:
                M.add_market(M.MarketAdd(kind="structure", location_id=-5, name="ZZ Sneaky"),
                             context_id=CTX)
                check(False, "POST /api/markets refuses a negative (minted-looking) location id")
            except Exception as e:
                check(getattr(e, "status_code", None) == 400,
                      f"POST /api/markets refuses a negative location id ({e})")

        # ── Gating and validation ───────────────────────────────────────────────────────
        print("\nFlag and input validation")
        with _Env(flag=False):
            try:
                _add(name="ZZ Flag Off")
                check(False, "the feature flag gates creation")
            except Exception as e:
                check(getattr(e, "status_code", None) == 403,
                      f"flag off → 403 rather than a silent create ({e})")
        with _Env():
            for bad, why in ((dict(system="ZZ Nowhere At All"), "an unknown system is rejected"),
                             (dict(hull="deathstar"), "an unknown hull is rejected"),
                             (dict(name="   "), "a blank name is rejected")):
                try:
                    _add(**{**dict(name="ZZ Bad", hull="azbel", system="ZZmanual-Hi"), **bad})
                    check(False, why)
                except Exception as e:
                    check(getattr(e, "status_code", None) == 400, f"{why} ({e})")
            check(set(MFG_HULLS) == {"raitaru", "azbel", "sotiyo"} and set(RX_HULLS) == {"athanor", "tatara"},
                  "the hull list served to the UI is the structures registry, not a JS copy")
            served = {h["key"] for h in M.list_structure_hulls(context_id=CTX)["hulls"]}
            check(served == set(MFG_HULLS) | set(RX_HULLS), "GET /api/markets/hulls serves exactly it")
    finally:
        _cleanup()

    print(f"\n{'FAILED: ' + str(len(failures)) if failures else 'All checks passed'}")
    for f in failures:
        print("  - " + f)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
