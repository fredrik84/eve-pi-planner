import html as _html
import json as _json
import logging as _logging
import re as _re
import time as _time

import os as _os
GIT_COMMIT = _os.environ.get("GIT_COMMIT", "unknown")

# Cache-busting token for every static asset. index.html ships `?v=dev` on each script/stylesheet
# and this replaces it at serve time, so one deploy invalidates exactly the assets that changed.
# It replaces 18 hand-maintained `?v=N` numbers: bumping those was a manual step on every single
# frontend change, and forgetting one served a browser stale JS against a new API — a failure that
# looks like anything except a caching problem.
#
# Locally GIT_COMMIT is "unknown" (docker compose passes no build arg), so fall back to this
# process's start time: every `docker compose up` then serves fresh assets, which is what local
# iteration needs and what a fixed literal could never give.
ASSET_VERSION = GIT_COMMIT if GIT_COMMIT != "unknown" else str(int(_time.time()))

# Root logger defaults to WARNING with no handler, so every app.*.log.info(...) call
# (charlist cache hit/miss, the plan-timing instrumentation, etc.) was silently dropped —
# discovered while verifying new timing logs never appeared. INFO here + no handler means
# stdout via the default StreamHandler, which is what `docker logs`/`kubectl logs` capture.
_logging.basicConfig(level=_logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from app.analyzer import router as analyzer_router
from app.planetary import router as planetary_router
from app.esi import router as esi_router
from app.esi_data import router as esi_data_router
from app.planner import router as planner_router
from app.planner_advisor import router as planner_advisor_router
from app.planner_dashboard import router as planner_dashboard_router
from app.planner_store import router as planner_store_router
from app.fuelblock_planner import router as fuelblock_router
from app.bugs import router as bugs_router
from app.admin import router as admin_router
from app.features import router as features_router
from app.notifications import router as notifications_router, make_scheduler
from app.alert_settings import router as alert_settings_router
from app.internal import router as internal_router
from app.moon_goo import router as moon_goo_router
from app.reactions import router as reactions_router
from app.industry import router as industry_router, public_router as industry_public_router
from app.groups import router as groups_router
from app.markets import router as markets_router

app = FastAPI(title="EVE PI Planner")


@app.middleware("http")
async def _reactions_request_memo(request, call_next):
    """Open a per-request memo scope for the expensive evidence layers.

    That layer (owned blueprints, the print floor, enabled stock, the pasted library) is expensive
    on a real account and identical within one request, and a single customer-order report asked
    for it five times over. The scope is opened HERE rather than inside the package so that a
    direct call — every test, and any background job — gets no memoisation and therefore always
    sees its own writes. See `app.cache.request_memo`.
    """
    from app.cache import begin_request_memo
    begin_request_memo()
    return await call_next(request)
app.include_router(analyzer_router)          # the original Find-Buildables analyzer
app.include_router(planetary_router)
app.include_router(esi_router)
app.include_router(esi_data_router)
app.include_router(planner_router)
app.include_router(planner_advisor_router)
app.include_router(planner_dashboard_router)
app.include_router(planner_store_router)
app.include_router(fuelblock_router)
app.include_router(bugs_router)
app.include_router(admin_router)
app.include_router(features_router)
app.include_router(notifications_router)
app.include_router(alert_settings_router)
app.include_router(internal_router)
app.include_router(moon_goo_router)
app.include_router(reactions_router)
app.include_router(industry_router)
app.include_router(industry_public_router)   # ungated: the customer build-status link
app.include_router(groups_router)
app.include_router(markets_router)

_scheduler = None


@app.get("/healthz", include_in_schema=False)
def healthz():
    from app.db import get_connection
    try:
        con = get_connection()
        con.execute("SELECT 1")
        con.close()
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "error", "detail": str(e)})
    return {"status": "ok"}


@app.on_event("startup")
async def _startup():
    # Force the Postgres pool to open all its connections now, during pod boot (before the
    # readiness probe — and therefore real traffic — can reach this process), instead of lazily
    # under the first real burst of concurrent requests. See app.db._pg_pool()'s docstring.
    from app.db import get_connection
    con = get_connection()
    con.execute("SELECT 1")
    con.close()

    _ensure_all_tables()

    global _scheduler
    _scheduler = make_scheduler()
    _scheduler.start()


def _ensure_all_tables():
    """Create every pp_* table at boot instead of lazily on first use.

    Each `ensure_*_table` is `@ensure_once` and idempotent, so this costs one pass at startup and
    nothing afterwards. It exists because the lazy scheme has a real failure mode: a table is only
    created when someone hits the endpoint that owns it, but OTHER code queries those tables
    directly. On a database where nobody had yet saved a plan share, `pp_shares` did not exist, and
    Admin → System Stats and DB Cleanup both hard-500'd on `SELECT ... FROM pp_shares`. Found on
    dev 2026-07-29; the same trap applies to any fresh install.

    Failures are logged and skipped rather than raised — one bad migration must not stop the app
    from booting, which would take the whole site down instead of one admin panel.
    """
    from app import (alert_settings, admin, bugs, features, groups, jobs, markets, moon_goo,
                     notifications, planetary, planner_store, shares, yield_stats)
    from app.esi import ensure_char_tables, ensure_admin_table
    from app.industry import blueprints as ind_bp, bpc, jobs as ind_jobs, orders as ind_orders, \
        settings as ind_settings, shares as ind_shares, assets as ind_assets
    from app.reactions import jobs as rx_jobs, settings as rx_settings

    for fn in (
        ensure_char_tables, ensure_admin_table,
        planner_store.ensure_plan_tables, planner_store.ensure_share_table,
        planner_store.ensure_profile_tables, planner_store.ensure_plan_snapshot_table,
        planner_store.ensure_colony_flags_table,
        planetary.ensure_tables, admin.ensure_basket_tables, bugs.ensure_bugs_table,
        features.ensure_features_table, groups.ensure_group_tables, jobs.ensure_job_tables,
        markets.ensure_markets_table, markets.ensure_market_config_table,
        moon_goo.ensure_moon_goo_table, notifications.ensure_notification_tables,
        shares.ensure_inventory_shares_table,
        alert_settings.ensure_alert_settings_table, yield_stats.ensure_yield_avg_table,
        ind_bp.ensure_char_blueprints_table, ind_bp.ensure_formula_job_prints_table,
        ind_bp.ensure_manual_blueprints_table,
        bpc.ensure_bpc_tables,
        ind_jobs.ensure_manufacturing_jobs_table, ind_jobs.ensure_manufacturing_completions_table,
        ind_orders.ensure_industry_orders_table, ind_settings.ensure_industry_settings_table,
        ind_shares.ensure_industry_shares_table, ind_assets.ensure_asset_tables,
        rx_jobs.ensure_industry_jobs_table, rx_jobs.ensure_reaction_assignments_table,
        rx_jobs.ensure_reaction_orders_table, rx_jobs.ensure_reaction_completions_table,
        rx_settings.ensure_reaction_settings_table,
        rx_settings.ensure_account_reaction_settings_table,
    ):
        try:
            fn()
        except Exception as e:
            _logging.getLogger(__name__).warning("table ensure failed for %s: %s",
                                                 getattr(fn, "__name__", fn), e)

    # Runs AFTER the ensures above, so the tables it widens exist. Postgres-only, idempotent, and a
    # no-op on every boot after the first — see app.db.widen_epoch_columns for why epochs stored as
    # `REAL` were losing ~64 seconds each on Postgres.
    try:
        from app.db import widen_epoch_columns
        widened = widen_epoch_columns()
        if widened:
            _logging.getLogger(__name__).info(
                "widened %d epoch column(s) from float4 to double precision: %s",
                len(widened), ", ".join(widened))
    except Exception as e:
        _logging.getLogger(__name__).warning("epoch column widening failed: %s", e)


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


def _fmt_isk(n: float) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def _share_meta(share_id: str):
    """Return (title, description) for a stored plan share, or generic defaults."""
    from app.planner_store import get_connection, ensure_share_table

    title = "EVE PI Planner"
    desc = "Plan your EVE Online Planetary Industry production chains."
    try:
        ensure_share_table()
        con = get_connection()
        row = con.execute("SELECT payload FROM pp_shares WHERE id=?", (share_id,)).fetchone()
        if not row:
            con.close()
            return title, desc
        try:
            con.execute(
                "UPDATE pp_shares SET last_accessed=datetime('now') WHERE id=?",
                (share_id,),
            )
            con.commit()
        except Exception:
            pass
        con.close()
        payload = _json.loads(row[0])
    except Exception:
        return title, desc

    pn = payload.get("pn") or "PI plan"
    stats = (payload.get("plan") or {}).get("stats") or {}
    parts = []
    # When the plan is supply-limited the plan view shows the EFFECTIVE (supply-capped) numbers, not
    # the nominal "if fully fed" ones — the OG preview must match, or a shared SHPC plan reads 192/day
    # in the unfurl but 81/day on the page. Fall back to nominal for old shares / non-limited plans.
    supply_limited = stats.get("supply_limited")
    ppd = (stats.get("effective_products_per_day") if supply_limited else None) or stats.get("products_per_day")
    if ppd:
        parts.append(f"{round(ppd):,}/day")
    ipd = (stats.get("effective_isk_per_day") if supply_limited else None) or stats.get("isk_per_day")
    if ipd:
        parts.append(f"{_fmt_isk(ipd)} ISK/day")
    fac = stats.get("factories")
    if fac:
        parts.append(f"{fac} factor{'ies' if fac != 1 else 'y'}")
    syss = payload.get("cs") or []
    if syss:
        parts.append(f"{len(syss)} system{'s' if len(syss) != 1 else ''}")
    title = f"{pn} — EVE PI plan"
    if parts:
        desc = " · ".join(parts)
    return title, desc


_ASSET_V = _re.compile(r'(\?v=)[A-Za-z0-9._-]*')


def _page(filename: str) -> str:
    """Read a static HTML document and stamp the running build onto its asset URLs."""
    try:
        with open(f"static/{filename}", encoding="utf-8") as f:
            doc = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")
    return _ASSET_V.sub(lambda m: m.group(1) + ASSET_VERSION, doc)


def _with_og(doc: str, *, request: Request, path: str, title: str, desc: str,
             script: str, twitter_image: bool = True) -> str:
    """Inject Open Graph + Twitter meta right after <head>.

    Position matters: crawlers take the FIRST title/description they see, so these have to precede
    the generic ones index.html carries. `script` hands the share id to the page's own JS.
    """
    t = _html.escape(title, quote=True)
    d = _html.escape(desc, quote=True)
    base = str(request.base_url).rstrip("/")
    url = _html.escape(f"{base}{path}", quote=True)
    img = _html.escape(f"{base}/og-image.png?v={ASSET_VERSION}", quote=True)
    og = (
        f'<meta property="og:type" content="website">\n'
        f'  <meta property="og:site_name" content="EVE PI Planner">\n'
        f'  <meta property="og:title" content="{t}">\n'
        f'  <meta property="og:description" content="{d}">\n'
        f'  <meta property="og:url" content="{url}">\n'
        f'  <meta property="og:image" content="{img}">\n'
        f'  <meta name="twitter:card" content="summary_large_image">\n'
        f'  <meta name="twitter:title" content="{t}">\n'
        f'  <meta name="twitter:description" content="{d}">\n'
        + (f'  <meta name="twitter:image" content="{img}">\n' if twitter_image else '')
        + f'  <meta name="description" content="{d}">\n'
        f'  {script}\n'
    )
    return doc.replace("<head>", "<head>\n  " + og, 1)


# Explicit index route, registered before the StaticFiles mount so the SPA's asset URLs get
# stamped. Without it the mount would serve index.html verbatim, `?v=dev` and all.
@app.get("/", include_in_schema=False)
def index():
    return HTMLResponse(_page("index.html"))


# The StaticFiles mount would happily serve /index.html straight off disk — unstamped, so a
# browser landing there would cache every asset under the literal key `dev` and never see an
# update. Send it to the canonical path instead.
@app.get("/index.html", include_in_schema=False)
def index_html():
    return RedirectResponse("/", status_code=301)


@app.get("/s/{share_id}")
def share_preview(share_id: str, request: Request):
    """Serve the SPA with plan-specific Open Graph tags so link unfurlers
    (Discord/Messenger/Slack/Twitter) show a rich preview. The fragment-based
    `#s=` link is invisible to crawlers; this path-based link is not."""
    title, desc = _share_meta(share_id)
    return HTMLResponse(_with_og(
        _page("index.html"), request=request, path=f"/s/{share_id}", title=title, desc=desc,
        script=f"<script>window.__SHARE_ID__={_json.dumps(share_id)};</script>",
    ))


@app.get("/b/{share_id}")
def build_status_page(share_id: str, request: Request):
    """The customer-facing build status page for a shared order.

    Served from its own minimal document, NOT the SPA: this page is opened by people with no account
    and it must be incapable of showing account data even by accident. Open Graph tags are injected
    the same way the plan share does it, so pasting the link into Discord unfurls with the product
    and how far along it is — which is most of what the customer wanted to ask.
    """
    doc = _page("build.html")
    title, desc = "Build status", "Follow this build's progress."
    try:
        from app.industry.shares import build_status
        d = build_status(share_id)
        title = f"{d['quantity']}× {d['product']} — {round(d['pct'])}% built"
        desc = ("Finished — ready for handover." if d["status"] == "complete"
                else f"{round(d['pct'])}% complete · stage {d['current_stage'] or 1} of "
                     f"{len(d['stages']) or 1} · ~{round(d['eta_hours'] or 0)}h to go")
    except Exception:
        pass                       # a dead or unknown link still serves the page, which explains itself

    return HTMLResponse(_with_og(
        doc, request=request, path=f"/b/{share_id}", title=title, desc=desc,
        script=f"<script>window.__BUILD_ID__={_json.dumps(share_id)};</script>",
        twitter_image=False,       # this page never had one; keeping the unfurl shape unchanged
    ))


# Unmatched /api/* must 404, not fall through to the static mount below.
#
# StaticFiles is mounted at "/", so anything no API route matched lands there — and StaticFiles only
# serves GET/HEAD, so a POST to a missing endpoint came back "405 Method Not Allowed". That reads
# like the endpoint exists but rejects your verb, which is exactly the wrong hint: the real cause is
# usually a pod that hasn't rolled yet, mid-deploy, still missing the route. This catch-all is
# registered BEFORE the mount so unmatched API paths say what actually happened.
@app.api_route("/api/{rest:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
               include_in_schema=False)
def _api_not_found(rest: str):
    raise HTTPException(
        status_code=404,
        detail=f"No such API endpoint: /api/{rest}. If this worked a moment ago, a deploy may still "
               f"be rolling out — retry shortly.",
    )


app.mount("/", StaticFiles(directory="static", html=True), name="static")
