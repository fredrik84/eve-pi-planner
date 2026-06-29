import html as _html
import json as _json
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.sde import load_pi_data
from app.pi import calculate_production
from app.optimizer import optimize_production
from app.market import fetch_prices
from app.shares import save_share, load_share
from app.planetary import router as planetary_router
from app.esi import router as esi_router
from app.planner import router as planner_router
from app.fuelblock_planner import router as fuelblock_router
from app.bugs import router as bugs_router
from app.admin import router as admin_router
from app.features import router as features_router
from app.notifications import router as notifications_router, make_scheduler

app = FastAPI(title="EVE PI Planner")
app.include_router(planetary_router)
app.include_router(esi_router)
app.include_router(planner_router)
app.include_router(fuelblock_router)
app.include_router(bugs_router)
app.include_router(admin_router)
app.include_router(features_router)
app.include_router(notifications_router)

_scheduler = None


@app.on_event("startup")
async def _startup():
    global _scheduler
    _scheduler = make_scheduler()
    _scheduler.start()


@app.on_event("shutdown")
async def _shutdown():
    if _scheduler and _scheduler.running:
        _scheduler.shutdown(wait=False)


class AnalyzeRequest(BaseModel):
    inventory: str


class OptimizeRequest(BaseModel):
    inventory: str
    order: Optional[str] = ""


@app.post("/api/analyze")
def analyze(req: AnalyzeRequest):
    try:
        pi_data = load_pi_data()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"SDE not ready: {e}")

    data = calculate_production(req.inventory, pi_data)

    all_type_ids = [
        item["type_id"]
        for tier_items in data["results"].values()
        for item in tier_items
    ]
    prices = fetch_prices(all_type_ids)

    for tier_items in data["results"].values():
        for item in tier_items:
            sell_price = prices.get(item["type_id"], 0.0)
            item["sell_price"] = round(sell_price, 2)
            item["total_isk"] = round(sell_price * item["max_output"], 2)
        tier_items.sort(key=lambda x: -x["total_isk"])

    return data


@app.post("/api/optimize")
def optimize(req: OptimizeRequest):
    try:
        pi_data = load_pi_data()
    except Exception as e:
        raise HTTPException(status_code=503, detail=f"SDE not ready: {e}")

    result = optimize_production(req.inventory, req.order or "", pi_data)

    # Enrich plan with live prices
    type_ids = [item["type_id"] for item in result["plan"]]
    prices = fetch_prices(type_ids)

    total_isk = 0.0
    for item in result["plan"]:
        price = prices.get(item["type_id"], 0.0)
        item["sell_price"] = round(price, 2)
        item["total_isk"] = round(price * item["quantity"], 2)
        total_isk += item["total_isk"]

    result["total_isk"] = round(total_isk, 2)
    return result


@app.post("/api/share")
def create_share(req: AnalyzeRequest):
    share_id = save_share(req.inventory)
    return {"id": share_id}


@app.get("/api/share/{share_id}")
def get_share(share_id: str):
    inventory = load_share(share_id)
    if inventory is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"inventory": inventory}


def _fmt_isk(n: float) -> str:
    n = float(n)
    for unit, div in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if abs(n) >= div:
            return f"{n / div:.1f}{unit}"
    return f"{n:.0f}"


def _share_meta(share_id: str):
    """Return (title, description) for a stored plan share, or generic defaults."""
    from app.planner import get_connection, ensure_share_table

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


@app.get("/s/{share_id}")
def share_preview(share_id: str, request: Request):
    """Serve the SPA with plan-specific Open Graph tags so link unfurlers
    (Discord/Messenger/Slack/Twitter) show a rich preview. The fragment-based
    `#s=` link is invisible to crawlers; this path-based link is not."""
    try:
        with open("static/index.html", encoding="utf-8") as f:
            doc = f.read()
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Not found")

    title, desc = _share_meta(share_id)
    t = _html.escape(title, quote=True)
    d = _html.escape(desc, quote=True)
    base = str(request.base_url).rstrip("/")
    url = f"{base}/s/{share_id}"
    img = f"{base}/og-image.png?v=6"
    og = (
        f'<meta property="og:type" content="website">\n'
        f'  <meta property="og:site_name" content="EVE PI Planner">\n'
        f'  <meta property="og:title" content="{t}">\n'
        f'  <meta property="og:description" content="{d}">\n'
        f'  <meta property="og:url" content="{_html.escape(url, quote=True)}">\n'
        f'  <meta property="og:image" content="{_html.escape(img, quote=True)}">\n'
        f'  <meta name="twitter:card" content="summary_large_image">\n'
        f'  <meta name="twitter:title" content="{t}">\n'
        f'  <meta name="twitter:description" content="{d}">\n'
        f'  <meta name="twitter:image" content="{_html.escape(img, quote=True)}">\n'
        f'  <meta name="description" content="{d}">\n'
        f'  <script>window.__SHARE_ID__={_json.dumps(share_id)};</script>\n'
    )
    # Inject right after <head> so the per-share og:title/description take
    # precedence over the generic homepage defaults in index.html.
    doc = doc.replace("<head>", "<head>\n  " + og, 1)
    return HTMLResponse(doc)


app.mount("/", StaticFiles(directory="static", html=True), name="static")
