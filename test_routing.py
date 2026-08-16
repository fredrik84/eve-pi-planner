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
    # Cut at the tuple's own closing paren in column 0 — a `)` inside one of the comments in that
    # block truncated the parse and silently reported half the routes as missing. Comment lines are
    # then dropped outright, since a quoted phrase in prose is not a route.
    spa_body = "\n".join(ln for ln in spa_block[:spa_block.index("\n)")].splitlines()
                         if not ln.lstrip().startswith("#"))
    spa = set(re.findall(r'"([^"]+)"', spa_body))

    print("\nevery page in the UI has a URL:")
    check(bool(slugs) and bool(panels) and bool(spa),
          f"all three lists were found ({len(slugs)} slugs, {len(panels)} panels, {len(spa)} routes)")
    missing = sorted(panels - set(slugs))
    check(not missing, f"no panel is unreachable by link (missing slugs: {missing})")
    extra = sorted(set(slugs) - panels)
    check(not extra, f"no slug points at a panel that does not exist (extra: {extra})")

    # Sections within a page (`/admin/bugs`, `/planner/refill`) — a second segment under a tab's own
    # slug. Parsed the same way and folded into the same set, because the server does not care which
    # of the two kinds a path is: it either has a route or it 404s.
    sub_block = js[js.index("const TAB_SUBPAGES = {"):]
    sub_block = sub_block[:sub_block.index("\n};")]
    sub_urls = set()
    for tab, body in re.findall(r"(\w+):\s*\{(.*?)\n  \},", sub_block, re.S):
        slug_part = body[body.index("slugs:"):] if "slugs:" in body else ""
        base = slugs.get(tab, "")
        for _key, sub in re.findall(r"(\w+):\s*'([^']+)'", slug_part):
            sub_urls.add(("" if base == "/" else base) + "/" + sub)
    check(bool(sub_urls), f"the sections were found ({len(sub_urls)})")
    check(all(u.count("/") >= 1 for u in sub_urls), "every section hangs off its page's own slug")

    print("\n...and the server serves exactly those URLs:")
    # `/` is the dashboard and is registered separately, so it is not in SPA_PAGES by design.
    want = {v.lstrip("/") for k, v in slugs.items() if v != "/"} | {u.lstrip("/") for u in sub_urls}
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

        print("\nlive: a typed trailing slash reaches the page instead of 404ing:")
        # Starlette's own `redirect_slashes` never fires here — StaticFiles is mounted at `/`, so
        # every path matches something and the router never reaches its no-match case. Without the
        # explicit redirect routes, `/admin/` 404s while `/admin` works.
        for path in ["/admin/", "/reactions/", "/planner/refill/", "/industry/how-it-works/"]:
            check(_status(path) in (200, 308), f"GET {path} -> {_status(path)} (want 308 or 200)")

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
    check("const routed = routeForPath(location.pathname);" in js
          and js.index("const routed = routeForPath") < js.index("else if (isMobile)"),
          "an explicit link is checked BEFORE the localStorage fallback")
    check("switchTab(routed.tab, { fromHistory: true, sub: routed.sub, record: routed })" in js,
          "...and the SECTION and RECORD in that link are honoured too, not just the page")

    _records(js, main_py, spa, slugs)
    return _finish(js)


def _records(js: str, main_py: str, spa: set, slugs: dict) -> None:
    """A page that can name a RECORD — `/manufacturing/order/123` — is a fourth list that has to
    agree with the other three: `TAB_RECORDS` in app.js and `SPA_RECORDS` in main.py.

    The id is the part of a URL that can disclose something (CLAUDE.md rule 8), so two things are
    asserted about it beyond the mapping: the server route must look NOTHING up — it serves the same
    document as every other page, so a recipient learns nothing from the fact that it answered — and
    a path that is not one of ours must still fall through rather than be swallowed."""
    print("\nthe pages that can name a record agree, front and back:")
    rec_block = js[js.index("const TAB_RECORDS = {"):]
    rec_block = rec_block[:rec_block.index("\n};")]
    # {tab: {kind, …}} — the kinds are the keys one level in, each followed by `open:`.
    js_records = set()
    for tab, body in re.findall(r"^  (\w+): \{(.*?)^  \},", rec_block + "\n  },", re.S | re.M):
        for kind in re.findall(r"^    (\w+): \{", body, re.M):
            js_records.add((tab, kind))

    srec_block = main_py[main_py.index("SPA_RECORDS = ("):]
    srec_body = "\n".join(ln for ln in srec_block[:srec_block.index("\n)")].splitlines()
                          if not ln.lstrip().startswith("#"))
    py_records = set(re.findall(r'\("([^"]+)",\s*"([^"]+)"\)', srec_body))

    check(bool(js_records) and bool(py_records),
          f"both lists were found ({len(js_records)} in app.js, {len(py_records)} in main.py)")
    # PAGE BY PAGE, not just the set of kind names. Comparing the names alone let `SPA_RECORDS` name
    # a different page than `TAB_RECORDS` does and still pass — `("reactions", "order")` instead of
    # `("manufacturing", "order")` — while `/manufacturing/order/123` 404s on refresh. The JS side is
    # keyed by TAB and the server side by that tab's SLUG, so the tabs are translated first, and a
    # server entry under one of the page's own SECTIONS counts as that page.
    slug_of = {t: v.lstrip("/") for t, v in slugs.items()}
    want = {(slug_of.get(tab, tab), kind) for tab, kind in js_records}
    got = {(page.split("/")[0] if page.split("/")[0] in set(slug_of.values()) else page, kind)
           for page, kind in py_records}
    check(want == got,
          f"every page offers the SAME record kinds on both sides "
          f"(only in JS: {sorted(want - got)}; only in main.py: {sorted(got - want)})")
    # Every page a record route hangs off must itself be a page, or the URL below it is unreachable.
    for page, _kind in sorted(py_records):
        check(page in spa, f"`{page}` is a real page, so `/{page}/…` can be reached")

    try:
        from app.main import app as _app
        paths = {getattr(r, "path", "") for r in _app.routes}
        for page, kind in sorted(py_records):
            check(f"/{page}/{kind}/{{record_id}}" in paths,
                  f"/{page}/{kind}/{{record_id}} is registered")
    except Exception as e:
        check(False, f"could not inspect the route table: {type(e).__name__}: {e}")

    src = main_py[main_py.index("SPA_RECORDS = ("):main_py.index('app.mount("/", StaticFiles')]
    body = src[src.index("def _spa_record"):]
    body = body[:body.index("app.add_api_route")]
    check("get_connection" not in body and "api(" not in body and "_order_row" not in body,
          "the record route looks nothing up — it cannot confirm an id exists (rule 8)")

    if _status("/") == 0:
        print("  SKIP live record checks — the app is not answering on 127.0.0.1:8000")
        return
    print("\nlive: a record URL serves the app, and says nothing about the record:")
    for page, kind in sorted(py_records):
        # A plainly nonexistent id. It must answer exactly as a real one would — the SERVER must not
        # be the thing that tells a recipient whether the row is there.
        check(_status(f"/{page}/{kind}/999999999") == 200,
              f"GET /{page}/{kind}/999999999 -> 200 (the same page a real id gets)")
    check(_status("/manufacturing/order/1/extra") != 200,
          "a fourth segment is not a route — the record grammar stops at the id")
    check(_status("/manufacturing/nonsense/1") != 200,
          "an unknown record kind is not a route")


def _finish(js: str) -> int:
    check("addEventListener('popstate'" in js, "back/forward is handled")
    check("fromHistory" in js, "...without pushing the entry the browser just moved to")
    check("corrected: true" in js,
          "a gating bounce REPLACES the URL, so the bar never names a page you cannot open")

    print("\n" + ("FAILED: " + "; ".join(_fails) if _fails else "all checks passed"))
    return 1 if _fails else 0


if __name__ == "__main__":
    sys.exit(main())
