#!/usr/bin/env python3
"""The logged-out sidebar shows only what a stranger can actually use.

A logged-out visitor used to be shown "Find Buildables" and "Contribute" (both dead ends without
an account), landed on Find Buildables by default, and got a bare "Industry" group heading with
every item under it hidden. This pins all three down.

Be honest about what this is: a STRUCTURAL test, not a rendering one. It asserts that the nav
gating rules are present in the markup, the stylesheet and the redirect list — it does not run a
browser, so it cannot prove the computed layout. There is no browser-test harness in this repo
(see TODO.md); this catches the realistic regression, which is someone re-marking a tab `active`
or dropping a selector while editing the nav, not a subtle cascade bug.

    python test_nav_gating.py                        # read the repo's static/ files
    python test_nav_gating.py --url https://eveindustry.net   # check what is actually served
"""
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))

failures = []


def check(cond, msg):
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        failures.append(msg)


def _load(name, url):
    if url:
        req = urllib.request.Request(url.rstrip("/") + "/" + name.lstrip("/"),
                                     headers={"User-Agent": "test_nav_gating"})
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.read().decode("utf-8", "replace")
    path = os.path.join(HERE, "static", name.lstrip("/"))
    with open(path, encoding="utf-8") as f:
        return f.read()


def _rule_targets(css, selector_needle, decl_needle):
    """Every selector in a rule whose body contains decl_needle, for rules mentioning the needle."""
    out = []
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
        if decl_needle in body.replace(" ", ""):
            out.append(" ".join(sels.split()))
    return [s for s in out if selector_needle in s]


def main():
    url = None
    argv = sys.argv[1:]
    if "--url" in argv:
        url = argv[argv.index("--url") + 1]
    html = _load("index.html", url)
    css = _load("style-layout-admin.css", url)
    js = _load("planetary.js", url)
    print(f"source: {'live ' + url if url else 'repo static/'}\n")

    print("logged-out visitors are not shown account-only tabs:")
    hidden = " ".join(_rule_targets(css, "", "display:none"))
    shown_li = " ".join(_rule_targets(css, "html.nav-li", "display:flex"))
    for sel, label in [('.tab[data-pimode="build"]', "Find Buildables"),
                       ('.tab[data-tab="contribute"]', "Contribute")]:
        check(sel in hidden, f"{label} is hidden by default")
        check(sel in shown_li, f"{label} is restored only for html.nav-li (logged in)")

    print("the Industry group does not render as an empty heading:")
    check("#industryNavGroup" in hidden, "#industryNavGroup is hidden by default")
    shown_ind = [s for s in _rule_targets(css, "#industryNavGroup", "display:flex")]
    check(bool(shown_ind), "#industryNavGroup has a rule that can show it")
    check(all("html.nav-li" in s for s in shown_ind),
          "every rule showing #industryNavGroup also requires html.nav-li")
    check(any("nav-feat" in s for s in shown_ind),
          "showing #industryNavGroup also requires a feature class, so it never appears bare")
    check(".nav-group-empty" in css, "the runtime empty-group class has a hiding rule")
    check("_hideEmptyNavGroups" in js, "the runtime empty-group sweep exists")

    print("How it works is the default landing page, not Find Buildables:")
    fb = re.search(r'<button[^>]*data-pimode="build"[^>]*>', html)
    hiw = re.search(r'<button[^>]*data-tab="howitworks"[^>]*>', html)
    check(bool(fb) and "active" not in fb.group(0), "the Find Buildables nav button is not active")
    check(bool(hiw) and "active" in hiw.group(0), "the How it works nav button is active")
    planner_panel = re.search(r'<div id="tab-planner"[^>]*>', html)
    hiw_panel = re.search(r'<div id="tab-howitworks"[^>]*>', html)
    check(bool(planner_panel) and "display:none" in planner_panel.group(0),
          "the Find Buildables panel starts hidden")
    check(bool(hiw_panel) and "display:none" not in hiw_panel.group(0),
          "the How it works panel starts visible")

    print("a logged-out visitor on a gated tab is bounced to How it works:")
    m = re.search(r"const AUTH_TABS = \[(.*?)\]", js, re.S)
    check(bool(m), "the logged-out redirect list is present")
    if m:
        tabs = set(re.findall(r"'([a-z]+)'", m.group(1)))
        for t in ("planner", "contribute", "analyze", "planetary", "characters", "planetdb"):
            check(t in tabs, f"'{t}' bounces a logged-out visitor")
        # Factory Layout is shown on its feature flag alone, with no html.nav-li in the selector —
        # it is meant to work logged out, so bouncing off it would break a public page.
        check("layout" not in tabs, "'layout' does NOT bounce — it is public by design")
        check(bool(re.search(r"html\.nav-feat-layout\s+\.tab\[data-tab=\"layout\"\]", css)),
              "the layout nav rule still has no html.nav-li requirement")
        check("switchTab('howitworks')" in js, "the bounce target is How it works")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
