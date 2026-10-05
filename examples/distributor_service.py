"""Reference implementation of the MERCHANT side of docs/connector-contract.md.

A distributor runs something like this in front of their catalog/inventory system and
instantly becomes purchasable by any txn-agent deployment. It is deliberately small:
three endpoints, bearer auth, idempotent orders, decimal-string money.

Run it:
    pip install ".[service]"
    MERCHANT_API_KEY=dist-key uvicorn examples.distributor_service:app --port 9000

Then point an agent at it (see tests/test_distributor_service.py for the full loop):
    HttpConnector(Vertical.PARTS, "http://127.0.0.1:9000/v1", api_token="dist-key",
                  merchant="ExampleParts")
"""
from __future__ import annotations

import os
import uuid
from decimal import Decimal
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from pydantic import BaseModel

API_KEY = os.environ.get("MERCHANT_API_KEY", "dist-key")

# In production this is your catalog/ERP. Here: a small appliance-parts warehouse.
CATALOG: dict[str, dict[str, Any]] = {
    "wh-1001": {"title": "Dishwasher lower rack wheel kit", "unit_price": "21.50",
                "shipping": "4.99", "in_stock": True, "lead_time_days": 2,
                "return_policy": {"returnable": True, "window_days": 30,
                                  "refund_type": "full", "restocking_fee_pct": "0"}},
    "wh-1002": {"title": "Refrigerator door gasket", "unit_price": "39.00",
                "shipping": "6.50", "in_stock": True, "lead_time_days": 3,
                "return_policy": {"returnable": True, "window_days": 30,
                                  "refund_type": "full", "restocking_fee_pct": "0"}},
    "wh-1003": {"title": "Drain pump assembly", "unit_price": "52.25",
                "shipping": "5.99", "in_stock": True, "lead_time_days": 4,
                "return_policy": {"returnable": True, "window_days": 30,
                                  "refund_type": "full", "restocking_fee_pct": "0"}},
}
STOCK: dict[str, int] = {"wh-1001": 40, "wh-1002": 15, "wh-1003": 8}
ORDERS: dict[str, dict[str, Any]] = {}  # idempotency_key -> receipt


def _wire(oid: str, item: dict[str, Any], quantity: int) -> dict[str, Any]:
    return {"offer_id": oid, "title": item["title"], "unit_price": item["unit_price"],
            "in_stock": item["in_stock"] and STOCK[oid] >= quantity, "quantity": quantity,
            "shipping": item["shipping"], "lead_time_days": item["lead_time_days"],
            "return_policy": item["return_policy"]}


class SearchBody(BaseModel):
    query: str
    quantity: int = 1


class OrderLine(BaseModel):
    offer_id: str
    quantity: int = 1


class OrderBody(BaseModel):
    idempotency_key: str
    payment_token: str
    lines: list[OrderLine]


app = FastAPI(title="ExampleParts distributor adapter", version="1.0.0")


@app.middleware("http")
async def require_bearer(request: Request, call_next):
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or auth.removeprefix("Bearer ") != API_KEY:
        raise HTTPException(status_code=401, detail="invalid distributor API token")
    return await call_next(request)


@app.post("/v1/offers/search")
def search(body: SearchBody) -> dict[str, Any]:
    words = set(body.query.lower().split())
    hits = []
    for oid, item in CATALOG.items():
        if words & set(item["title"].lower().split()):
            hits.append(_wire(oid, item, max(1, body.quantity)))
    return {"offers": hits}


@app.get("/v1/offers/{offer_id}")
def revalidate(offer_id: str, quantity: int = 1) -> dict[str, Any]:
    item = CATALOG.get(offer_id)
    if item is None:
        raise HTTPException(status_code=404, detail="unknown offer")
    return _wire(offer_id, item, max(1, quantity))


@app.post("/v1/orders")
def place_order(body: OrderBody) -> dict[str, Any]:
    if not body.payment_token.startswith(("tok_", "pi_")):
        raise HTTPException(status_code=402, detail="payment reference required")
    if body.idempotency_key in ORDERS:  # MUST dedupe on the key
        return ORDERS[body.idempotency_key]
    for line in body.lines:
        item = CATALOG.get(line.offer_id)
        if item is None or STOCK[line.offer_id] < line.quantity:
            raise HTTPException(status_code=422,
                                detail=f"out of stock: {line.offer_id}")
    total = sum((Decimal(CATALOG[ln.offer_id]["unit_price"]) * ln.quantity
                 + Decimal(CATALOG[ln.offer_id]["shipping"]) for ln in body.lines),
                Decimal("0"))
    for line in body.lines:  # commit after all lines validate (all-or-nothing)
        STOCK[line.offer_id] -= line.quantity
    receipt = {"order_id": f"ord-{uuid.uuid4().hex[:10]}", "status": "confirmed",
               "total": str(total),
               "items": [[ln.offer_id, ln.quantity] for ln in body.lines]}
    ORDERS[body.idempotency_key] = receipt
    return receipt
