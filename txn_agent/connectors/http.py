"""HTTP implementation of the Connector protocol (see docs/connector-contract.md).

Talks to real merchant APIs over JSON/HTTP with stdlib urllib only, per the project's
no-runtime-dependencies rule. Fail-closed: unknown offer -> out of stock, HTTP errors
during revalidation -> out of stock, checkout errors surface as CheckoutError.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import replace
from decimal import Decimal
from typing import Any

from ..models import Intent, Offer, Receipt, ReturnPolicy, Vertical
from .base import CheckoutError

_DEFAULT_TIMEOUT = 10.0


def _request_json(url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                  token: str | None = None, timeout: float = _DEFAULT_TIMEOUT) -> tuple[int, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            payload = resp.read().decode()
            return resp.status, json.loads(payload) if payload else {}
    except urllib.error.HTTPError as e:
        return e.code, {}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
        raise CheckoutError(f"merchant API unreachable: {e}") from e


def _offer_from_json(data: dict[str, Any], vertical: Vertical, merchant: str) -> Offer:
    rp = data.get("return_policy") or {}
    return Offer(
        offer_id=str(data["offer_id"]),
        vertical=vertical,
        merchant=str(data.get("merchant") or merchant),
        title=str(data.get("title", "")),
        unit_price=Decimal(str(data["unit_price"])),
        in_stock=bool(data.get("in_stock", False)),
        return_policy=ReturnPolicy(
            returnable=bool(rp.get("returnable", False)),
            window_days=int(rp.get("window_days", 0)),
            refund_type=str(rp.get("refund_type", "none")),
            restocking_fee_pct=Decimal(str(rp.get("restocking_fee_pct", "0"))),
        ),
        quantity=int(data.get("quantity", 1)),
        shipping=Decimal(str(data.get("shipping", "0"))),
        currency=str(data.get("currency", "USD")),
        lead_time_days=int(data.get("lead_time_days", 0)),
    )


def _items_from(data: dict[str, Any], fallback: list[tuple[str, int]]) -> tuple[tuple[str, int], ...]:
    """Accept [["off", 2]] pairs or [{"offer_id", "quantity"}] objects from merchants."""
    raw = data.get("items") or fallback
    out: list[tuple[str, int]] = []
    for entry in raw:
        if isinstance(entry, dict):
            out.append((str(entry["offer_id"]), int(entry.get("quantity", 1))))
        else:
            out.append((str(entry[0]), int(entry[1])))
    return tuple(out)


class HttpConnector:
    """`Connector` over the merchant HTTP contract. One instance per merchant."""

    def __init__(self, vertical: Vertical, base_url: str, api_token: str,
                 merchant: str = "", timeout_s: float = _DEFAULT_TIMEOUT):
        self.vertical = vertical
        self.base_url = base_url.rstrip("/")
        self.api_token = api_token
        self.merchant = merchant or base_url
        self.timeout_s = timeout_s

    def search(self, intent: Intent) -> list[Offer]:
        status, data = _request_json(
            f"{self.base_url}/offers/search", method="POST",
            body={"query": intent.query, "quantity": intent.quantity, "currency": "USD"},
            token=self.api_token, timeout=self.timeout_s)
        if status != 200:
            return []  # search failure = no candidates; the run loop just finds nothing
        return [_offer_from_json(o, self.vertical, self.merchant) for o in data.get("offers", [])]

    def revalidate(self, offer: Offer) -> Offer:
        try:
            status, data = _request_json(
                f"{self.base_url}/offers/{offer.offer_id}",
                token=self.api_token, timeout=self.timeout_s)
        except CheckoutError:  # network down: can't confirm live state -> fail closed
            return replace(offer, in_stock=False)
        if status != 200:  # 404 or any other failure: can't confirm live state -> fail closed
            return replace(offer, in_stock=False)
        live = _offer_from_json(data, self.vertical, self.merchant)
        return replace(live, quantity=offer.quantity)  # keep the cart's requested quantity

    def checkout(self, offer: Offer, payment_token: str, idempotency_key: str,
                 lines: list[tuple[str, int]] | None = None) -> Receipt:
        payload_lines = lines or [(offer.offer_id, offer.quantity)]
        status, data = _request_json(
            f"{self.base_url}/orders", method="POST",
            body={"idempotency_key": idempotency_key, "payment_token": payment_token,
                  "lines": [{"offer_id": oid, "quantity": qty} for oid, qty in payload_lines]},
            token=self.api_token, timeout=self.timeout_s)
        if status not in (200, 201):
            raise CheckoutError(f"checkout failed (HTTP {status}): {data.get('error', 'unknown')}")
        return Receipt(
            order_id=str(data["order_id"]),
            offer_id=offer.offer_id,
            merchant=offer.merchant,
            total=Decimal(str(data.get("total", offer.total))),
            idempotency_key=idempotency_key,
            status=str(data.get("status", "confirmed")),
            items=_items_from(data, payload_lines),
        )
