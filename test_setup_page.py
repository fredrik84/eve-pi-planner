"""The PI planner's setup page: the sections a user must be able to find.

Written for bug 3 (filed 2026-07-14): *"Went into planning mode and I only see the production target
and constellation filter parts of the setup page. The character part is missing."*

It was not missing in the sense of unrendered — `#ppRolesCard` shipped with `style="display:none"`
and was revealed only by `_loadProductConfig`, which runs when a product resolves. So on a fresh
page, and on any typed product that failed to resolve, `onProductChange` set it straight back to
`display:none` and the character section silently vanished mid-edit.

String matching, and weak by construction — but the invariant is not: **a section of the setup page
may explain that it is waiting for something, and may not disappear.** A user cannot report "the
thing I need is hidden" as usefully as they can report "it's missing", and the reporter proved that.

Run: python3 test_setup_page.py
"""
import re
import sys

sys.path.insert(0, ".")

_failures = []


def check(cond: bool, msg: str) -> bool:
    print(f"  {'PASS' if cond else 'FAIL'}: {msg}")
    if not cond:
        _failures.append(msg)
    return bool(cond)


def _section(html: str, element_id: str) -> str:
    """The markup of the <section> carrying an id, up to its close."""
    at = html.index(f'id="{element_id}"')
    start = html.rindex("<section", 0, at)
    return html[start:html.index("</section>", start)]


def test_the_character_section_never_disappears() -> bool:
    print(f"\n{'='*60}\n  the setup page's character section is always present\n{'='*60}")
    ok = True
    html = open("static/index.html", encoding="utf-8").read()
    js = open("static/planetary.js", encoding="utf-8").read()

    card = _section(html, "ppRolesCard")
    opening = card[:card.index(">") + 1]
    ok &= check("display:none" not in opening.replace(" ", ""),
                "the card is not hidden in the markup it ships with")

    # It cannot show the per-character ROWS without a product — they are stored per product — so
    # what it must do instead is say so, on the title line, because the body is collapsed.
    ok &= check("pick a production target" in card.lower(),
                "and its hint says what it is waiting for, on the visible title line")

    # The regression path: the handler that runs on every keystroke in the product box.
    at = js.index("async function onProductChange(")
    body = js[at:js.index("\n}\n", at)]
    ok &= check("ppRolesCard" not in body or "display = 'none'" not in body,
                "onProductChange no longer hides the card when nothing resolves")
    ok &= check("_ppRolesWaiting()" in body,
                "...it restores the waiting state instead")

    # One writer for that state, so the first paint and a cleared product cannot disagree.
    ok &= check(js.count("function _ppRolesWaiting(") == 1,
                "the waiting state has exactly one writer")
    return ok


def test_every_setup_section_the_reporter_named_is_reachable() -> bool:
    """The three cards the report is about, and the rule that separates them: a card may be hidden
    only when something OUTSIDE the user's control decides it is irrelevant."""
    print(f"\n{'='*60}\n  the setup page's cards, and which may hide\n{'='*60}")
    ok = True
    html = open("static/index.html", encoding="utf-8").read()
    for element_id, may_hide, why in [
        ("ppRolesCard", False, "characters are the planner's main input"),
        # The Planet DB may genuinely have no constellations to filter by; the card is meaningless
        # then, and `renderConstellations` hides it for that reason and only that reason.
        ("ppLocationCard", True, "there may be no constellation data at all"),
    ]:
        opening = _section(html, element_id)
        opening = opening[:opening.index(">") + 1]
        hidden = "display:none" in opening.replace(" ", "")
        ok &= check(hidden == may_hide,
                    f"#{element_id} {'may' if may_hide else 'must not'} ship hidden — {why}")
    return ok


def main() -> int:
    results = [
        test_the_character_section_never_disappears(),
        test_every_setup_section_the_reporter_named_is_reachable(),
    ]
    print(f"\n{'='*60}")
    if all(results) and not _failures:
        print("  All setup-page checks passed.")
        return 0
    for f in _failures:
        print(f"  FAILED: {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
