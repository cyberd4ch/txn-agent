"""In-memory merchants for all three verticals. Replace with real API clients."""
from __future__ import annotations

import re
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal as D

from ..models import Intent, Offer, Receipt, ReturnPolicy, Vertical
from .base import CheckoutError

_STOP = {"a", "an", "the", "for", "to", "of", "on", "in", "my", "me", "i", "need",
         "want", "buy", "get", "find", "replacement", "restock", "flight", "under"}


def _tokens(s: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", s.lower())) - _STOP


class MockConnector:
    def __init__(self, vertical: Vertical, catalog: list[Offer]):
        self.vertical = vertical
        self._catalog = {o.offer_id: o for o in catalog}
        self.price_overrides: dict[str, D] = {}  # simulate live price changes in tests
        self.stock_overrides: dict[str, bool] = {}  # simulate live stock changes in tests
        self.orders: dict[str, Receipt] = {}

    def search(self, intent: Intent) -> list[Offer]:
        want = _tokens(intent.query)
        return [replace(o, quantity=intent.quantity)
                for o in self._catalog.values() if want & _tokens(o.title)]

    def revalidate(self, offer: Offer) -> Offer:
        live = self._catalog.get(offer.offer_id)
        if live is None:
            return replace(offer, in_stock=False)
        live = replace(live, quantity=offer.quantity)
        if offer.offer_id in self.price_overrides:
            live = replace(live, unit_price=self.price_overrides[offer.offer_id])
        if offer.offer_id in self.stock_overrides:
            live = replace(live, in_stock=self.stock_overrides[offer.offer_id])
        return live

    def checkout(self, offer: Offer, payment_token: str, idempotency_key: str,
                 lines: list[tuple[str, int]] | None = None) -> Receipt:
        if idempotency_key in self.orders:  # safe retry: same key -> same order
            return self.orders[idempotency_key]
        if not payment_token.startswith("tok_"):
            raise CheckoutError("tokenized payment required")
        if lines:  # multi-line cart: every line must be in stock
            for oid, _qty in lines:
                if oid not in self._catalog or not self._catalog[oid].in_stock:
                    raise CheckoutError(f"out of stock at checkout: {oid}")
            total = sum((self._catalog[oid].unit_price * qty + self._catalog[oid].shipping
                         for oid, qty in lines), D("0"))
            items = tuple(lines)
        else:
            if not offer.in_stock:
                raise CheckoutError("out of stock at checkout")
            total = offer.total
            items = ((offer.offer_id, offer.quantity),)
        receipt = Receipt(order_id=f"{self.vertical.value}-{len(self.orders) + 1:04d}",
                          offer_id=offer.offer_id, merchant=offer.merchant,
                          total=total, idempotency_key=idempotency_key, items=items)
        self.orders[idempotency_key] = receipt
        return receipt


def default_connectors() -> dict[Vertical, MockConnector]:
    soon = datetime.now(timezone.utc) + timedelta(minutes=30)
    P, F, G = Vertical.PARTS, Vertical.FLIGHTS, Vertical.GROCERY
    return {
        P: MockConnector(P, [
            # Same part, several merchants: differ on stock, returns, lead time, fee.
            Offer("parts-1", P, "PartsDirect", "Dishwasher lower rack wheel kit",
                  D("14.99"), False, ReturnPolicy(True, 30), shipping=D("4.99"), lead_time_days=3),
            Offer("parts-2", P, "AppliancePartsCo", "Dishwasher lower rack wheel kit",
                  D("24.99"), True, ReturnPolicy(True, 30), shipping=D("4.99"), lead_time_days=2),
            Offer("parts-3", P, "BudgetParts", "Dishwasher lower rack wheel kit",
                  D("22.99"), True, ReturnPolicy(False, 0, "none"), shipping=D("6.99"), lead_time_days=9),
            Offer("parts-4", P, "OEM Supply", "Dishwasher lower spray arm",
                  D("31.50"), True, ReturnPolicy(True, 30), shipping=D("0"), lead_time_days=4),
            Offer("parts-5", P, "PartsDirect", "Refrigerator door gasket",
                  D("44.00"), True, ReturnPolicy(True, 30), shipping=D("7.99"), lead_time_days=6),
            Offer("parts-6", P, "PartsDirect", "Drain pump assembly",
                  D("58.75"), True, ReturnPolicy(True, 30), shipping=D("5.99"), lead_time_days=5),
        ]),
        F: MockConnector(F, [
            Offer("fl-1", F, "SkyFare", "SFO to JFK nonstop 2026-11-02",
                  D("312"), True, ReturnPolicy(True, 1, "full"), expires_at=soon),
            Offer("fl-2", F, "BudgetAir", "SFO to JFK 1 stop 2026-11-02",
                  D("228"), True, ReturnPolicy(False, 0, "none"), expires_at=soon),
        ]),
        G: MockConnector(G, [
            Offer("gr-1", G, "FreshMart", "Whole milk 1 gal",
                  D("4.29"), True, ReturnPolicy(False, 0, "none")),
            Offer("gr-2", G, "ValueGrocer", "Whole milk 1 gal",
                  D("3.89"), True, ReturnPolicy(False, 0, "none"), shipping=D("2.99")),
            Offer("gr-3", G, "FreshMart", "Large eggs dozen",
                  D("3.99"), True, ReturnPolicy(False, 0, "none")),
        ]),
    }
