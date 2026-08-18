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


def _selectors_for(css, needle, decl):
    """INDIVIDUAL selectors mentioning `needle` from rules declaring `decl`.

    Not `_rule_targets`: that joins a rule's whole comma-separated selector list into one string
    and keeps the preceding comment with it. Both matter here — the html-class prefix has to be
    read off the one selector that targets this element, and a leading `/* … */` would otherwise
    make an `html.nav-li`-gated rule parse as unconditional (it did, on the first draft).
    """
    plain = re.sub(r"/\*.*?\*/", " ", css, flags=re.S)
    # This flat parse cannot see an `@media` wrapper — it would match the inner rule and drop the
    # condition, modelling a screen-width-scoped hide as unconditional. No nav rule lives in one
    # today; if that changes, say so loudly instead of quietly answering the wrong question.
    for block in re.findall(r"@media[^{]*\{(.*?)\n\}", plain, re.S):
        if needle in block:
            raise AssertionError(
                f"{needle} is styled inside an @media block — _visible() models the cascade "
                f"flatly and would ignore the media condition. Teach it, or stop using it here.")
    out = []
    for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", plain):
        if decl not in body.replace(" ", ""):
            continue
        out += [" ".join(s.split()) for s in sels.split(",") if needle in s]
    return out


def _required_classes(selector):
    """The `html.` classes a selector demands, e.g. `html.nav-li.nav-feat-industry .tab[…]`."""
    m = re.match(r"html((?:\.[\w-]+)*)", selector.strip())
    return {c for c in m.group(1).split(".") if c} if m else set()


def _visible(css, needle, active):
    """Would an element matching `needle` be visible with exactly `active` set on <html>?

    The stylesheet uses one shape throughout — hide unconditionally, then re-show under an
    `html.<class>` prefix — so this is decidable without a browser: visible unless something hides
    it, and visible again if any show-rule's required classes are all present.
    """
    # Only an UNCONDITIONAL hide counts as the baseline; a conditional one would need cascade
    # ordering to resolve, which this model deliberately does not attempt.
    if not any(not _required_classes(s) for s in _selectors_for(css, needle, "display:none")):
        return True
    return any(_required_classes(s) <= active
               for s in _selectors_for(css, needle, "display:flex"))


def _check_group_never_bare(html, css, group_id):
    """No combination of states shows the group heading with every item under it hidden.

    This replaces an assertion that the group's show-rule "also requires a feature class" — a proxy
    for the real invariant that stopped being true when Reactions went from feature-gated to
    login-gated. The group was still never bare (Reactions and the group's own How it works both
    show for any logged-in user), but the proxy said otherwise, so it failed on correct code.
    Assert the property itself instead: it survives an item changing which flag it sits behind.
    """
    m = re.search(r'id="%s"(.*?)</div>\s*<div class="nav-group' % group_id, html, re.S)
    items = re.findall(r'data-tab="([\w-]+)"', m.group(1)) if m else []
    check(len(items) >= 2, f"the {group_id} items were found in the markup ({items})")
    if not items:
        return
    # Every class the <head> pre-paint script can set — the whole state space the nav has.
    from itertools import combinations
    flags = ["nav-li", "nav-adm", "nav-grpmgr", "nav-feat-layout", "nav-feat-industry"]
    bare = []
    for n in range(len(flags) + 1):
        for combo in combinations(flags, n):
            active = set(combo)
            if not _visible(css, "#" + group_id, active):
                continue
            if not any(_visible(css, '.tab[data-tab="%s"]' % t, active) for t in items):
                bare.append(sorted(active) or ["(no classes)"])
    check(not bare,
          f"the {group_id} heading is never drawn over an empty group "
          f"(bare in these states: {bare[:3]})")


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
    for sel, label in [('.tab[data-tab="planner"][data-submode="build"]', "Find Buildables"),
                       ('.tab[data-tab="contribute"]', "Contribute")]:
        check(sel in hidden, f"{label} is hidden by default")
        check(sel in shown_li, f"{label} is restored only for html.nav-li (logged in)")

    print("the Industry group does not render as an empty heading:")
    check("#industryNavGroup" in hidden, "#industryNavGroup is hidden by default")
    shown_ind = [s for s in _rule_targets(css, "#industryNavGroup", "display:flex")]
    check(bool(shown_ind), "#industryNavGroup has a rule that can show it")
    check(all("html.nav-li" in s for s in shown_ind),
          "every rule showing #industryNavGroup also requires html.nav-li")
    _check_group_never_bare(html, css, "industryNavGroup")
    check(".nav-group-empty" in css, "the runtime empty-group class has a hiding rule")
    check("_hideEmptyNavGroups" in js, "the runtime empty-group sweep exists")

    print("How it works is the default landing page, not Find Buildables:")
    fb = re.search(r'<button[^>]*data-tab="planner"[^>]*data-submode="build"[^>]*>', html)
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
        # `corrected: true` since 2026-08-15: pages have URLs, so a bounce must REPLACE the address
        # rather than push, or Back sends the visitor straight back into the login wall.
        check("switchTab('howitworks', { corrected: true })" in js,
              "the bounce target is How it works, and it corrects the URL")

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        return 1
    print("all assertions passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
