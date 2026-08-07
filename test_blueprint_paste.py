#!/usr/bin/env python3
"""The industry window, pasted (`parse_blueprint_paste` / `replace_blueprint_batch`).

Declaring prints one at a time is unusable at the scale a real builder has — ~100 reaction formulas
on one character and more on the others — so the whole library arrives as a copy of EVE's own
industry window. This file pins what a paste is allowed to mean:

  * `[N x ]<name> TAB ME TAB TE TAB runs TAB category`, and the trailing category is ignored;
  * a leading `N x ` is a STACK of N prints, and **a repeated line is a separate physical print** —
    the window lists one line per item, so four identical lines are four formulas;
  * `runs = -1` is a BPO, anything else a BPC with that many runs (already this module's encoding);
  * section headers and blank lines are skipped, and a paste with no headers reads identically;
  * a name we cannot place is REPORTED — a paste that matched nothing is usually the wrong window,
    and silence would leave the user staring at an unchanged list;
  * **each paste is a NAMED BATCH**: re-pasting a name replaces that batch and nothing else, so the
    second character's window cannot wipe the first one's and a re-paste cannot double a holding;
  * the window also copies in a LONG layout that names WHERE each print is
    (`… runs TAB ? TAB structure TAB container TAB category`) — read by counting back from the
    trailing category, never from absolute indices, since the layout is inferred from two real
    samples and not documented;
  * **a batch is then a PLACE**: one batch per container, keyed on structure+container so
    re-pasting the same window replaces each container independently and two cans of the same name
    in two structures never collide. A short-layout paste has no location, so the user is asked for
    a structure — or may still just type a batch name.

In-process; run inside the container against a NON-PROD database. Seeds rows under a fabricated
context id and removes them in a finally.

    docker compose cp test_blueprint_paste.py web:/srv/app/ && \
      docker compose exec web python3 test_blueprint_paste.py
"""
import json
import sys

sys.path.insert(0, ".")
from app.db import get_connection                                      # noqa: E402
from app.industry import blueprints as bp                              # noqa: E402
from app.industry.blueprints import (                                  # noqa: E402
    ensure_char_blueprints_table, ensure_manual_blueprints_table, parse_blueprint_paste,
    replace_blueprint_batch, list_blueprint_batches, delete_blueprint_batch, manual_blueprints,
    owned_blueprints, _batch_key, _location_batch_key, _split_location,
)

CTX = -98882
CHAR = -9342

# The real thing, copied out of the client — headers, a stack prefix, repeated lines and all.
SAMPLE = (
    "Formulas:\n"
    "4 x Nanotransistors Reaction Formula\t0\t0\t-1\tComposite\n"
    "Phenolic Composites Reaction Formula\t0\t0\t-1\tComposite\n"
    "Photonic Metamaterials Reaction Formula\t0\t0\t-1\tComposite\n"
    "Photonic Metamaterials Reaction Formula\t0\t0\t-1\tComposite\n"
    "Platinum Technite Reaction Formula\t0\t0\t-1\tIntermediate Materials\n"
    "5 x Platinum Technite Reaction Formula\t0\t0\t-1\tIntermediate Materials\n"
    "\n"
    "Blueprints:\n"
    "'Augmented' Valkyrie Blueprint\t0\t0\t60\tCombat Drone\n"
    "'Integrated' Hammerhead Blueprint\t0\t0\t60\tCombat Drone\n"
    "'Packrat' Mobile Tractor Unit Blueprint\t0\t0\t1\tMobile Tractor Unit\n"
    "Amarr Shuttle Blueprint\t10\t20\t-1\tShuttle\n"
    "Amarr Shuttle Blueprint\t10\t20\t-1\tShuttle\n"
)

# The OTHER layout the client copies in. With no container selected in the industry window's tree,
# every row carries where it lives — `… runs TAB ? TAB structure TAB container TAB category` — and
# the column at index 4 is a `0` whose meaning we do not know and do not read. Copied out of the
# client verbatim; the extra rows below it put two containers in one structure and the SAME
# container name in a second structure, which is the collision the batch key has to survive.
MTO, JITA = "MTO2-2 - Ctrl C", "Jita 4-4 - Ctrl V"
LONG = (
    f"Warp Core Stabilizer I Blueprint\t10\t20\t-1\t0\t{MTO}\tSanto BPO\tWarp Core Stabilizer\n"
    f"4 x Nanotransistors Reaction Formula\t0\t0\t-1\t0\t{MTO}\tSanto BPO\tComposite\n"
    f"Phenolic Composites Reaction Formula\t0\t0\t-1\t0\t{MTO}\tRx cans\tComposite\n"
    f"Amarr Shuttle Blueprint\t10\t20\t-1\t0\t{JITA}\tSanto BPO\tShuttle\n"
)

NANO, PHENOLIC, PHOTONIC, PLATINUM = 16681, 16680, 33359, 16662     # reaction OUTPUTS
VALKYRIE, HAMMERHEAD, PACKRAT, SHUTTLE = 28296, 28270, 33700, 11134  # manufactured products

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


_real_enabled = bp._manual_enabled


def _force_flag(on: bool):
    bp._manual_enabled = (lambda ctx: on)


def _reset(con):
    con.execute("DELETE FROM pp_industry_blueprints WHERE context_id=?", (CTX,))
    con.execute("DELETE FROM pp_char_blueprints WHERE character_id=?", (CHAR,))
    con.execute("DELETE FROM pp_characters WHERE context_id=?", (CTX,))
    con.commit()


def _character(con, esi_rows):
    con.execute("INSERT INTO pp_characters (character_id, character_name, context_id, scopes) "
                "VALUES (?,?,?,?)", (CHAR, "PasteTester", CTX, "esi-characters.read_blueprints.v1"))
    con.execute("INSERT INTO pp_char_blueprints (character_id, blueprints_json, fetched_at) "
                "VALUES (?,?,?)", (CHAR, json.dumps(esi_rows), 1.0))
    con.commit()


def _by_product(res):
    out = {}
    for e in res["entries"]:
        out.setdefault(e["product_type_id"], []).append(e)
    return out


def main():
    ensure_char_blueprints_table()
    ensure_manual_blueprints_table()
    con = get_connection()
    try:
        # ── parsing ───────────────────────────────────────────────────────────────────────────
        print("the real sample parses into the prints it names:")
        res = parse_blueprint_paste(SAMPLE)
        g = _by_product(res)
        check(not res["unknown"] and not res["no_product"],
              "every name in the sample resolves — apostrophes included")
        check(sorted(res["ignored"]) == ["Blueprints:", "Formulas:"],
              "the two section headers are the only lines skipped, and they are reported")
        check(res["prints"] == 18 and res["products"] == 8,
              "18 physical prints across 8 products")
        check(res["formulas"] == 13 and res["blueprints"] == 5,
              "13 of them formulas and 5 manufacturing blueprints")

        print("a leading 'N x ' is a stack of N separate prints:")
        check(g[NANO][0]["quantity"] == 4, "'4 x Nanotransistors' is four formulas, not one")
        check(g[PHENOLIC][0]["quantity"] == 1, "and a line with no prefix is one")

        print("a repeated line is a separate physical print:")
        check(len(g[PHOTONIC]) == 1 and g[PHOTONIC][0]["quantity"] == 2,
              "two identical Photonic Metamaterials lines are two formulas in one row")
        check(g[PLATINUM][0]["quantity"] == 6,
              "a bare line plus a '5 x' line of the same print is six — prefix and repeat combine")

        print("runs decides BPO vs BPC, and ME/TE carry through:")
        check(g[SHUTTLE][0]["kind"] == "bpo" and g[SHUTTLE][0]["runs"] == -1,
              "runs -1 is an original")
        check(g[SHUTTLE][0]["me"] == 10 and g[SHUTTLE][0]["te"] == 20,
              "with the ME and TE the window stated — the whole point of declaring it")
        check(g[SHUTTLE][0]["quantity"] == 2, "and two identical original lines are two originals")
        check(g[VALKYRIE][0]["kind"] == "bpc" and g[VALKYRIE][0]["runs"] == 60,
              "a run count is a copy carrying that many runs")
        check(g[PACKRAT][0]["kind"] == "bpc" and g[PACKRAT][0]["runs"] == 1,
              "including a 1-run copy")

        print("prints that differ in research stay different prints:")
        two_me = parse_blueprint_paste("Amarr Shuttle Blueprint\t10\t20\t-1\tShuttle\n"
                                       "Amarr Shuttle Blueprint\t0\t0\t-1\tShuttle\n")
        check(len(two_me["entries"]) == 2 and two_me["prints"] == 2,
              "an ME10 original and an un-researched one are not merged into a stack of two")

        print("the section headers are not required:")
        headerless = "\n".join(ln for ln in SAMPLE.splitlines()
                               if ln.strip() and not ln.endswith(":"))
        hres = parse_blueprint_paste(headerless)
        check(hres["prints"] == res["prints"] and hres["products"] == res["products"]
              and not hres["ignored"],
              "a paste of one section, or of no headers at all, reads identically")

        print("the trailing category column is ignored, and may be absent:")
        no_cat = parse_blueprint_paste("Amarr Shuttle Blueprint\t10\t20\t-1\n")
        check(no_cat["prints"] == 1 and no_cat["entries"][0]["me"] == 10,
              "a row with no category is still a row")
        check("category" not in (no_cat["entries"][0] or {}),
              "and nothing is stored from it")

        print("names we cannot place are reported, never dropped:")
        junk = parse_blueprint_paste("Nanotransistors Reaction Formula\t0\t0\t-1\tComposite\n"
                                     "Definitely Not A Blueprint\t0\t0\t-1\tNonsense\n")
        check(junk["unknown"] == ["Definitely Not A Blueprint"],
              "the unrecognised name comes back by name")
        check(junk["prints"] == 1, "and the recognised print is still imported")
        wrong_window = parse_blueprint_paste("Tritanium\t1 000 000\n")
        check(wrong_window["prints"] == 0,
              "a stock paste declares no prints (the wrong window is the common mistake)")

        # ── batches ───────────────────────────────────────────────────────────────────────────
        _reset(con)
        _force_flag(True)

        print("a paste is a NAMED BATCH — a second character's window does not wipe the first:")
        a = replace_blueprint_batch(CTX, "Main", SAMPLE)
        check(a["added"] == 18 and not a.get("error"), "the first window imports 18 prints")
        alt = ("3 x Nanotransistors Reaction Formula\t0\t0\t-1\tComposite\n"
               "Amarr Shuttle Blueprint\t0\t0\t-1\tShuttle\n")
        b = replace_blueprint_batch(CTX, "Alt", alt)
        check(b["added"] == 4, "the second window imports its own 4")
        held = manual_blueprints(CTX)
        check(len(held[PHOTONIC]["copies"]) == 2,
              "the first character's prints are still there after the second paste")
        check(len(held[NANO]["copies"]) == 7,
              "and a product BOTH characters hold sums across batches: 4 + 3")
        check(len(held[SHUTTLE]["copies"]) == 3,
              "same for the shuttle print — two ME10 originals and the alt's un-researched one")
        names = sorted(x["name"] for x in list_blueprint_batches(CTX))
        check(names == ["Alt", "Main"], "and both batches are listed by name")

        print("re-pasting the same batch REPLACES it — it never doubles:")
        again = replace_blueprint_batch(CTX, "Main", SAMPLE)
        check(again["added"] == 18, "the same window imports the same 18 prints")
        held = manual_blueprints(CTX)
        check(len(held[PHOTONIC]["copies"]) == 2,
              "and the holding is unchanged — two formulas, not four")
        check(len(held[NANO]["copies"]) == 7, "the alt's batch is untouched by the re-paste")
        check(len(list_blueprint_batches(CTX)) == 2, "still two batches")

        print("...and a batch is matched by NAME, whatever process imported it:")
        check(_batch_key("Main") == _batch_key(" main "),
              "the key is a stable digest of the trimmed, case-folded name")

        print("a re-paste tracks the window it was copied from:")
        sold = SAMPLE.replace("Photonic Metamaterials Reaction Formula\t0\t0\t-1\tComposite\n"
                              "Photonic Metamaterials Reaction Formula\t0\t0\t-1\tComposite\n",
                              "Photonic Metamaterials Reaction Formula\t0\t0\t-1\tComposite\n", 1)
        replace_blueprint_batch(CTX, "Main", sold)
        held = manual_blueprints(CTX)
        check(len(held[PHOTONIC]["copies"]) == 1,
              "selling one and re-pasting leaves one — a replace can go DOWN as well as up")

        print("a hand-typed row is not part of any batch and no paste may delete it:")
        con.execute("INSERT INTO pp_industry_blueprints (context_id, id, type_id, me, te, runs, "
                    "quantity, prefer, updated_at, batch, batch_name) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (CTX, 9000, PACKRAT, 7, 14, -1, 1, "", 1.0, "", ""))
        con.commit()
        replace_blueprint_batch(CTX, "Main", SAMPLE)
        typed = [r for r in con.execute(
            "SELECT id FROM pp_industry_blueprints WHERE context_id=? AND COALESCE(batch,'')=''",
            (CTX,)).fetchall()]
        check(len(typed) == 1, "the typed row survives a re-paste of the batch beside it")
        check(len(manual_blueprints(CTX)[PACKRAT]["copies"]) == 2,
              "and counts on top of the pasted one — two prints of that product")

        print("deleting a batch takes that batch only:")
        delete_blueprint_batch(CTX, _batch_key("Main"))
        held = manual_blueprints(CTX)
        check(PHOTONIC not in held, "the deleted batch's prints are gone")
        check(len(held[NANO]["copies"]) == 3, "the other character's batch is intact")
        check(len(held[PACKRAT]["copies"]) == 1, "and so is the hand-typed row")
        delete_blueprint_batch(CTX, "")
        check(len(manual_blueprints(CTX)[PACKRAT]["copies"]) == 1,
              "an empty batch key deletes nothing — '' is the hand-typed rows")

        # ── where the prints are ──────────────────────────────────────────────────────────────
        _reset(con)

        print("the long layout carries a LOCATION, read from the END of the line:")
        lres = parse_blueprint_paste(LONG)
        check(not lres["unknown"] and not lres["no_product"],
              "every name in the long sample resolves too")
        check(lres["prints"] == 7 and lres["products"] == 4,
              "7 prints across 4 products — the extra columns change nothing about the counts")
        lg = _by_product(lres)
        check(lg[NANO][0]["structure"] == MTO and lg[NANO][0]["container"] == "Santo BPO",
              "structure is fields[-3] and container fields[-2], anchored on the trailing category")
        check(lg[NANO][0]["me"] == 0 and lg[SHUTTLE][0]["me"] == 10
              and lg[SHUTTLE][0]["runs"] == -1,
              "and ME/TE/runs still come off the leading columns, not the shifted ones")
        check(sorted(l["name"] for l in lres["locations"]) ==
              [f"Rx cans — {MTO}", f"Santo BPO — {JITA}", f"Santo BPO — {MTO}"],
              "three distinct containers are reported, each qualified by its structure")

        print("the short layout has no location — that is what 'short' means:")
        sres = parse_blueprint_paste(SAMPLE)
        check(all(not e["structure"] and not e["container"] for e in sres["entries"]),
              "a 5-column row names no place at all")
        check(sres["locations"] == [], "and reports no locations")

        print("the column rule is guarded, and a wrong guess degrades to 'no location':")
        check(_split_location(["Name", "0", "0", "-1", "0", "7", "8", "Cat"]) == ("", ""),
              "numbers where a structure and a container should be are not a location")
        check(_split_location(["Name", "0", "0", "-1", "0", MTO, "Santo BPO", "Cat", ""])
              == (MTO, "Santo BPO"),
              "a trailing empty field does not shift what 'last' means")
        check(_split_location(["Name", "0", "0", "-1", "Cat"]) == ("", ""),
              "fewer than 7 fields is the short layout")

        print("a located paste becomes ONE BATCH PER CONTAINER:")
        imp = replace_blueprint_batch(CTX, "ignored name", LONG)
        check(imp["added"] == 7 and len(imp.get("batches") or []) == 3,
              "one window spanning three containers imports as three batches")
        listed = {b["name"]: b for b in list_blueprint_batches(CTX)}
        check(sorted(listed) == [f"Rx cans — {MTO}", f"Santo BPO — {JITA}", f"Santo BPO — {MTO}"],
              "and all three are listed, named from the container and qualified by the structure")
        check(listed[f"Santo BPO — {MTO}"]["prints"] == 5
              and listed[f"Rx cans — {MTO}"]["prints"] == 1,
              "each carries only its own container's prints — 5 and 1")
        check(listed[f"Santo BPO — {MTO}"]["structure"] == MTO
              and listed[f"Santo BPO — {MTO}"]["container"] == "Santo BPO",
              "the structure and the container are stored as their own fields, not just in the name")

        print("two containers of the SAME NAME in different structures do not collide:")
        check(_location_batch_key(MTO, "Santo BPO") != _location_batch_key(JITA, "Santo BPO"),
              "the structure is part of the key, so 'Santo BPO' twice is two keys")
        check(listed[f"Santo BPO — {MTO}"]["batch"] != listed[f"Santo BPO — {JITA}"]["batch"],
              "and the two really are separate batches after an import")
        check(listed[f"Santo BPO — {JITA}"]["prints"] == 1,
              "the second structure's can holds its own one print")

        print("re-pasting the same window REPLACES each container — it never duplicates:")
        again = replace_blueprint_batch(CTX, "", LONG)
        check(again["added"] == 7, "the same window imports the same 7 prints")
        check(len(list_blueprint_batches(CTX)) == 3, "still three batches, not six")
        check(len(manual_blueprints(CTX)[NANO]["copies"]) == 4,
              "and the holding is unchanged — four Nanotransistors formulas, not eight")
        check({b["batch"] for b in list_blueprint_batches(CTX)}
              == {b["batch"] for b in imp["batches"]},
              "the keys are identical across the two pastes — this is what makes it a replace")
        check(_location_batch_key(f"  {MTO}  ", " Santo BPO ")
              == _location_batch_key(MTO, "Santo BPO"),
              "the key is a digest of the trimmed, case-folded LOCATION, not of the display label")

        print("...and one container emptying is tracked, like any other re-paste:")
        sold = LONG.replace("4 x Nanotransistors", "2 x Nanotransistors")
        replace_blueprint_batch(CTX, "", sold)
        check(len(manual_blueprints(CTX)[NANO]["copies"]) == 2,
              "selling two and re-pasting leaves two")
        check(len(manual_blueprints(CTX)[PHENOLIC]["copies"]) == 1,
              "and the OTHER container in the same structure is untouched")

        print("a short-layout paste can be TOLD where it is, and that place is its batch:")
        _reset(con)
        asked = replace_blueprint_batch(CTX, "", SAMPLE, structure=MTO)
        check(asked["added"] == 18 and asked["name"] == MTO,
              "the structure the user picked names the batch")
        check(asked["batch"] == _location_batch_key(MTO, ""),
              "and keys it — a location batch with no container")
        again2 = replace_blueprint_batch(CTX, "", SAMPLE, structure=MTO)
        check(again2["batch"] == asked["batch"] and len(list_blueprint_batches(CTX)) == 1,
              "re-picking the same structure hits the same batch and replaces it")
        check(list_blueprint_batches(CTX)[0]["structure"] == MTO,
              "the asked-for structure is stored on the batch")
        check(replace_blueprint_batch(CTX, "", SAMPLE, structure=JITA)["batch"]
              != asked["batch"],
              "a different structure is a different batch")

        print("...and the typed-name path still works, untouched:")
        _reset(con)
        typed_b = replace_blueprint_batch(CTX, "Main", SAMPLE)
        check(typed_b["batch"] == _batch_key("Main") and typed_b["name"] == "Main",
              "no location anywhere means the batch is exactly the name that was typed")

        print("a MIXED paste splits by what each line knows:")
        _reset(con)
        mixed = replace_blueprint_batch(CTX, "Main", LONG + SAMPLE)
        made = {b["name"]: b for b in mixed["batches"]}
        check(len(made) == 4, "three located containers plus the typed batch for the rest")
        check(made["Main"]["prints"] == 18 and made["Main"]["structure"] == "",
              "the short-layout rows land in the typed batch, with no place claimed for them")
        check(made[f"Santo BPO — {MTO}"]["prints"] == 5,
              "and the located rows land in their own containers regardless")
        check(len(manual_blueprints(CTX)[NANO]["copies"]) == 8,
              "a product in both halves sums across the batches: 4 located + 4 typed")

        # ── the merge rule, across batches ────────────────────────────────────────────────────
        print("what a batch declares REPLACES the ESI reading for that product:")
        _reset(con)
        # ESI reports two Photonic copies and one Packrat copy the user never pastes.
        _character(con, [{"type_id": 46217, "me": 0, "te": 0, "quantity": -1, "runs": -1},
                         {"type_id": 46217, "me": 0, "te": 0, "quantity": -1, "runs": -1},
                         {"type_id": 33701, "me": 3, "te": 6, "quantity": -2, "runs": 9}])
        esi_only = owned_blueprints(CTX)
        check(len(esi_only[PHOTONIC]["copies"]) == 2, "control: ESI reads two of that formula")
        replace_blueprint_batch(CTX, "Main", SAMPLE)
        merged = owned_blueprints(CTX)
        check(len(merged[PHOTONIC]["copies"]) == 2 and merged[PHOTONIC].get("source") == "manual",
              "the two the paste declares ARE the holding — the two ESI read are dropped, not added")
        check(merged[SHUTTLE]["me"] == 10 and merged[SHUTTLE]["te"] == 20,
              "and a product only the paste knows about is declared at its stated research")
        check(merged[PACKRAT].get("source") == "manual",
              "the Packrat print the paste also names is declared too...")
        check(len(merged[PACKRAT]["copies"]) == 1 and merged[PACKRAT]["runs"] == 1,
              "...so the ESI-read 9-run copy of it is NOT counted on top — this is the surprise the"
              " UI warns about, and it applies to every character's ESI reading")

        print("with the flag off a pasted library is invisible to the planner:")
        _force_flag(False)
        check(manual_blueprints(CTX) == {}, "nothing is read while the feature is off")
        check(len(owned_blueprints(CTX)[PHOTONIC]["copies"]) == 2
              and "source" not in owned_blueprints(CTX)[PHOTONIC],
              "and the holding is exactly what ESI said")

    finally:
        _reset(con)
        con.close()
        bp._manual_enabled = _real_enabled

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
