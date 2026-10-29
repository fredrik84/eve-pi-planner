#!/usr/bin/env python3
"""Actionable warnings retain their warning styling and link to the action they name.

The old price-basis footnotes were removed from the product. These checks cover the remaining
warnings, order price markers, and Setup Analysis / Refill navigation.
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

    check(bool(re.search(r"\.settings-note::before\s*\{[^}]*content:\s*[\"']!", css)),
          "actionable warnings retain their ! marker")

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
    m = re.search(r"falls back to Jita</b>.{0,300}", js, re.S)
    check(bool(m and "connectReactionsMarket()" in m.group(0)),
          "the unreadable-market warning offers the connect it asks for")

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

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
