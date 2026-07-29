"""
The original "Find Buildables" analyzer — the app's first feature, kept as its own router.

Answers two questions about a pasted inventory: what P2/P3/P4 can I build right now
(`/api/analyze`, greedy, via `app.pi`), and how do I best fill a specific order
(`/api/optimize`, an LP over the production graph, via `app.optimizer`). `/api/share` +
`/api/share/{id}` persist a pasted inventory behind a short id — note this is the ORIGINAL
inventory-share mechanism (`app.shares`, its own tiny store) and has nothing to do with the
plan shares in `pp_shares` that `/s/{id}` serves; two different features, similar names.

These four lived directly on the FastAPI app in `main.py` while every other feature moved to a
router. Nothing here is new — this is the same code, relocated so `main.py` is composition
only. Worth knowing: `/api/optimize` is the ONLY caller of `highspy`+`numpy` (~55 MB of the
image), and `app.optimizer` imports them lazily inside the solve, so they cost nothing at
startup and nothing at all unless someone optimizes an order. If this feature is ever retired,
deleting this module plus `app/pi.py`, `app/optimizer.py`, `app/shares.py` and those two
requirements is the whole job.
"""
from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from app.market import fetch_prices
from app.optimizer import optimize_production
from app.pi import calculate_production
from app.sde import load_pi_data
from app.shares import load_share, save_share

router = APIRouter()


class AnalyzeRequest(BaseModel):
    inventory: str


class OptimizeRequest(BaseModel):
    inventory: str
    order: Optional[str] = ""


@router.post("/api/analyze")
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


@router.post("/api/optimize")
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


@router.post("/api/share")
def create_share(req: AnalyzeRequest):
    share_id = save_share(req.inventory)
    return {"id": share_id}


@router.get("/api/share/{share_id}")
def get_share(share_id: str):
    inventory = load_share(share_id)
    if inventory is None:
        raise HTTPException(status_code=404, detail="Share not found")
    return {"inventory": inventory}
