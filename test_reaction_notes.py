#!/usr/bin/env python3
"""A warning means "act on this". A footnote means "here is how the number was priced".

The reactions overview stacked FOUR amber `!` boxes under its tiles — buy-order valuation, the
fee/freight/collateral breakdown, the unpriced-order note and the mixed-basis ledger note. Not one
of them told the reader to do anything: the jobs were already running. Sitting beside them, styled
identically, are notes the reader MUST act on ("no system set, so job install fees are left out of
every estimate"). Four unactionable warnings per page view is how a reader learns to skip the row,
and the one that mattered went with it.

What is pinned:

  * `.stat-footnote` exists, carries no `!`, and is visually quieter than `.settings-note`;
  * the four basis notes render through `_rxFootnote`, not as `.settings-note`;
  * the notes that DO demand an action are still warnings — this is the half that makes the split
    worth anything, and the easy mistake is to demote them too;
  * `_rxFootnote` renders nothing when every part is empty, so a clean account gets no empty box.

    docker compose cp test_reaction_notes.py web:/srv/app/ && \
      docker compose exec web python3 test_reaction_notes.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _strip_comments(js: str) -> str:
    """Drop `//` comment lines, keeping line structure.

    This file explains itself heavily, and several comments QUOTE the note text they are about
    ("No system set means job install fees are left out"). Scanning raw source finds the comment
    first and reports on prose instead of markup — which is exactly what this test did on its first
    run, passing a check it had not actually performed.
    """
    out = []
    for line in js.splitlines():
        stripped = line.lstrip()
        out.append("" if stripped.startswith("//") else line)
    return "\n".join(out)


def _nearest_classes(js: str, phrase: str) -> list:
    """The wrapper class in effect at each occurrence of `phrase`, nearest-preceding wins."""
    found = []
    for m in re.finditer(phrase, js):
        before = js[max(0, m.start() - 400):m.start()]
        last = None
        for cm in re.finditer(r'class="(settings-note|stat-footnote)"', before):
            last = cm.group(1)
        found.append(last)
    return found


def main() -> int:
    js = _strip_comments(
        open(os.path.join(HERE, "static", "reactions.js"), encoding="utf-8").read())
    css = open(os.path.join(HERE, "static", "style-layout-admin.css"), encoding="utf-8").read()

    print("\nthe quiet class exists and is actually quiet:")
    foot = re.search(r"\.stat-footnote\s*\{([^}]*)\}", css)
    check(bool(foot), ".stat-footnote is defined — a class with no rule styles nothing")
    if foot:
        body = foot.group(1)
        check("color" in body, "...it sets a colour")
        # The whole point is that it does not shout. An amber fill or a left border would make it
        # a warning again by appearance while claiming not to be one.
        check("background" not in body,
              f"...and has no filled background, unlike .settings-note (got {body.strip()[:80]})")
        check("border-left" not in body, "...and no warning rail")
    check(not re.search(r"\.stat-footnote::before\s*\{[^}]*content:\s*[\"']!", css),
          "no ! glyph on a footnote — the glyph is what makes a warning scannable")
    # ...while the real warning keeps its glyph. If this ever fails the split has been applied
    # backwards, which looks tidy and destroys the signal.
    check(bool(re.search(r"\.settings-note::before\s*\{[^}]*content:\s*[\"']!", css)),
          ".settings-note still carries its ! — the split must not disarm the real warnings")

    print("\nthe four unactionable notes are footnotes now:")
    check("function _rxFootnote(" in js, "_rxFootnote exists")
    # Each of the four, by a phrase from its own text, must not be inside a settings-note div.
    for label, phrase in [
        ("buy-order valuation", r"Valued at <b>buy orders</b>"),
        ("cost breakdown", r"Full cost adds"),
        ("unpriced orders", r"priced at <b>market</b>"),
        ("mixed-basis ledger", r"Mixed basis:"),
    ]:
        cls = _nearest_classes(js, phrase)
        check(bool(cls), f"the {label} note is still shown at all")
        # EVERY occurrence must be clean — one demoted copy and one left behind is still four
        # warning boxes on the page.
        check("settings-note" not in cls,
              f"...and no copy of the {label} note wears warning styling (got {cls})")

    print("\nthe notes that DEMAND an action are still warnings:")
    # These are the counterexamples: each one means a number on screen is wrong until the reader
    # goes and does something. Demoting these would be the same mistake in the other direction.
    for label, phrase in [
        ("no system set", r"job install fees are left out"),
        ("loss-making order", r"costs more to produce than the client is paying"),
        ("unreadable structure market", r"falls back to Jita"),
    ]:
        cls = _nearest_classes(js, phrase)
        check(bool(cls), f"the {label} warning is still shown")
        check("settings-note" in cls,
              f"...and {label} is still a warning, not a footnote (got {cls})")

    print("\nan account with nothing to footnote gets no empty box:")
    fn = re.search(r"function _rxFootnote\(parts\)\s*\{(.*?)\n\}", js, re.S)
    check(bool(fn), "_rxFootnote body is readable")
    if fn:
        body = fn.group(1)
        check("filter(" in body, "empty parts are filtered out")
        check(re.search(r"body\s*\?", body), "...and an all-empty list renders '' rather than a div")

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
