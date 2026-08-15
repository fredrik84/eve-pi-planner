#!/usr/bin/env python3
"""A page is a URL, and three lists have to agree about which.

Pages used to live entirely at `/`: the current tab was a `localStorage` key, so a link could not be
shared, a refresh restored whichever page the browser had last rather than the one that was sent,
and back/forward did nothing.

Three lists now describe the same set and are written in three different places — the panels in
`static/index.html`, the `TAB_SLUGS` map in `static/app.js`, and `SPA_PAGES` in `app/main.py`. Any
one drifting is a silent 404 or a page nobody can link to, so they are checked rather than
remembered.

Also pinned, because it is the failure mode with the worst symptom: **a path that is not a page must
still fall through to the static mount.** Registering these as a `/{page}` wildcard would swallow
every unmatched asset request and return this HTML document instead, which a browser reports as a
syntax error inside the document rather than as the 404 it is.

In-process where it can be; the HTTP half needs the container serving.

    docker compose cp test_routing.py web:/srv/app/ && \
      docker compose exec web python3 test_routing.py
"""
import json
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, ".")

BASE = "http://127.0.0.1:8000"
_fails = []


def check(cond: bool, msg: str) -> None:
    print(("  ok   " if cond else "  FAIL ") + msg)
    if not cond:
        _fails.append(msg)


def _status(path: str) -> int:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:
        return 0


def _body(path: str) -> str:
    try:
        with urllib.request.urlopen(BASE + path, timeout=10) as r:
            return r.read().decode("utf-8", "replace")
    except Exception:
        return ""


def main() -> int:
    js = open("static/app.js", encoding="utf-8").read()
    html = open("static/index.html", encoding="utf-8").read()
    main_py = open("app/main.py", encoding="utf-8").read()

    # ── the three lists ────────────────────────────────────────────────────────────────────────
    block = js[js.index("const TAB_SLUGS = {"):]
    block = block[:block.index("};")]
    slugs = dict(re.findall(r"(\w+):\s*'([^']+)'", block))
    panels = set(re.findall(r'id="tab-([a-z]+)"', html))
    spa_block = main_py[main_py.index("SPA_PAGES = ("):]
    spa = set(re.findall(r'"([^"]+)"', spa_block[:spa_block.index(")")]))

    print("\nevery page in the UI has a URL:")
    check(bool(slugs) and bool(panels) and bool(spa),
          f"all three lists were found ({len(slugs)} slugs, {len(panels)} panels, {len(spa)} routes)")
    missing = sorted(panels - set(slugs))
    check(not missing, f"no panel is unreachable by link (missing slugs: {missing})")
    extra = sorted(set(slugs) - panels)
    check(not extra, f"no slug points at a panel that does not exist (extra: {extra})")

    print("\n...and the server serves exactly those URLs:")
    # `/` is the dashboard and is registered separately, so it is not in SPA_PAGES by design.
    want = {v.lstrip("/") for k, v in slugs.items() if v != "/"}
    check(want == spa,
          f"the frontend's slugs and the server's routes are the same set "
          f"(only in JS: {sorted(want - spa)}; only in main.py: {sorted(spa - want)})")
    check(slugs.get("dashboard") == "/",
          f"the dashboard keeps `/` rather than gaining a second address (got {slugs.get('dashboard')})")

    print("\nthe routes are explicit, not a wildcard — the thing that would break assets:")
    # Checked against the REAL route table, not the source text: the registration loop legitimately
    # contains an f-string that looks like a wildcard, and a grep-shaped assertion flagged it.
    try:
        from app.main import app as _app
        wild = [getattr(r, "path", "") for r in _app.routes
                if re.fullmatch(r"/\{[^/]+\}", getattr(r, "path", "") or "")]
        check(not wild, f"no single-segment catch-all page route exists (found: {wild})")
        paths = {getattr(r, "path", "") for r in _app.routes}
        check(all(("/" + p) in paths for p in spa),
              "every page in SPA_PAGES is really registered on the app")
    except Exception as e:
        check(False, f"could not inspect the route table: {type(e).__name__}: {e}")
    check(main_py.index("SPA_PAGES = (") < main_py.index('app.mount("/", StaticFiles'),
          "the page routes are registered BEFORE the static mount, or they would never match")

    print("\nlive: every page URL serves the app:")
    if _status("/") == 0:
        print("  SKIP http checks — the app is not answering on 127.0.0.1:8000")
    else:
        for path in ["/"] + sorted("/" + p for p in spa):
            check(_status(path) == 200, f"GET {path} -> 200")

        print("\nlive: a non-page path is NOT swallowed by the page routes:")
        check(_status("/nope.js") == 404,
              "a missing asset still 404s rather than returning the SPA document")
        body = _body("/nope.js")
        check("<!doctype" not in body.lower() and "<html" not in body.lower(),
              "...and its body is not HTML, which a browser would report as a syntax error")
        check(_status("/app.js") == 200, "a real asset still serves")
        check(_status("/api/features") == 200, "the API is untouched")
        check(_status("/api/nothing-here") == 404, "an unknown API path still 404s as an API error")
        check(_status("/healthz") == 200, "healthz still answers")

    print("\nthe router honours the URL over the remembered tab:")
    check("const routed = tabForPath(location.pathname);" in js
          and js.index("const routed = tabForPath") < js.index("else if (isMobile)"),
          "an explicit link is checked BEFORE the localStorage fallback")
    check("addEventListener('popstate'" in js, "back/forward is handled")
    check("fromHistory" in js, "...without pushing the entry the browser just moved to")
    check("corrected: true" in js,
          "a gating bounce REPLACES the URL, so the bar never names a page you cannot open")

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
