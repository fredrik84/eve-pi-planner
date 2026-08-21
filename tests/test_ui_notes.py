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

The same rule is then checked across the other pages that state an action: Setup Analysis told the
reader to rescan from the Characters tab when `rescanAll()` runs from anywhere, and Refill named the
exact plan to switch to without switching it.

    docker compose cp tests/test_ui_notes.py web:/srv/app/tests/ && \
      docker compose exec web python3 tests/test_ui_notes.py
"""
import os
import re
import sys

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
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

    print("\na note that names an action offers the action:")
    # Telling a reader to "set the price on the order" from a page that does not contain orders
    # leaves them to find the collapsed card themselves. Every note below names something to do, so
    # each must carry the route to it.
    m = re.search(r"priced at <b>market</b>.{0,300}", js, re.S)
    check(bool(m and "_rxGoToUnpricedOrder()" in m.group(0)),
          "the unpriced-order footnote links to the order")
    check(bool(m and "stat-footnote-link" in m.group(0)),
          "...styled as a link inside the footnote, not a stray button")
    check("function _rxGoToUnpricedOrder(" in js, "_rxGoToUnpricedOrder exists")
    check("function _rxIsUnpriced(" in js, "_rxIsUnpriced exists")
    m = re.search(r"falls back to Jita</b>.{0,300}", js, re.S)
    check(bool(m and "connectReactionsMarket()" in m.group(0)),
          "the unreadable-market warning offers the connect it asks for")
    check(bool(re.search(r"\.stat-footnote-link\s*\{", css)), ".stat-footnote-link is styled")

    print("\nthe destination says which order was meant:")
    check("rx-order-unpriced" in js and "no price set" in js,
          "unpriced rows are marked in the orders list")
    check(bool(re.search(r"\.rx-order-noprice\s*\{", css)), "...and the marker has a rule")
    # THE coupling that fails silently: `_rxIsUnpriced` reads `o.client_price`, which only exists
    # if the list endpoint selects it. Drop it from the query and every open order renders "no
    # price set" — a page-wide false alarm with nothing in the JS to blame.
    orders_py = open(os.path.join(HERE, "app", "reactions", "orders.py"), encoding="utf-8").read()
    sel = re.search(r"SELECT id, type_id, name, target_qty.*?FROM pp_reaction_orders", orders_py, re.S)
    check(bool(sel), "the order-list query is readable")
    if sel:
        check("client_price" in sel.group(0),
              "the list endpoint returns client_price — without it EVERY order reads as unpriced")
    # Scoped to the function, not the file: `o.status === 'open'` appears in the list renderer too,
    # so a file-wide search passes even with the status check torn out of _rxIsUnpriced.
    unp = re.search(r"function _rxIsUnpriced\(o\)\s*\{(.*?)\n\}", js, re.S)
    check(bool(unp), "_rxIsUnpriced body is readable")
    if unp:
        check("status" in unp.group(1),
              "only OPEN orders are nagged about — a delivered one is history, and editing a "
              f"closed record is not the ask (got {unp.group(1).strip()})")

    print("\nthe same rule on the other pages that name an action:")
    ana = _strip_comments(
        open(os.path.join(HERE, "static", "analysis.js"), encoding="utf-8").read())
    ref = _strip_comments(
        open(os.path.join(HERE, "static", "refill.js"), encoding="utf-8").read())

    # "Rescan colonies in the Characters tab" — `rescanAll()` is tab-independent and re-renders this
    # page when it finishes, so the trip was never needed. The note that raises the problem should
    # be the thing that clears it.
    m = re.search(r"Rescan for true numbers.{0,700}", ana, re.S)
    check(bool(m), "the stale-supply note still exists")
    if m:
        check("rescanAll()" in m.group(0), "...and rescans from where it is raised")
        check("in the Characters tab" not in m.group(0),
              "...and no longer sends the reader to another tab to press a button")

    # The empty state named two ways out and offered neither.
    m = re.search(r"Nothing to compare against yet.{0,700}", ana, re.S)
    check(bool(m), "the Setup Analysis empty state still exists")
    if m:
        check("rescanAll()" in m.group(0), "...and offers the rescan")
        check("switchTab('planner')" in m.group(0), "...and links to the planner")

    # Refill already knew WHICH plan was the right one.
    m = re.search(r"factories don't match your deployed setup.{0,700}", ref, re.S)
    check(bool(m), "the refill plan-mismatch warning still exists")
    if m:
        check("onPlanDistSelect(" in m.group(0),
              "...and switches to the plan it names, rather than naming it and stopping")

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
