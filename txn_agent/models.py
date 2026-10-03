from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import Any, cast


class Vertical(str, Enum):
    PARTS = "parts"
    FLIGHTS = "flights"
    GROCERY = "grocery"


@dataclass(frozen=True)
class Intent:
    """What the user wants. max_total is a HARD ceiling set by the user/host app."""
    vertical: Vertical
    query: str
    max_total: Decimal
    quantity: int = 1  # units, or passengers for flights
    constraints: dict[str, Any] = field(default_factory=dict)
    intent_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])


@dataclass(frozen=True)
class ReturnPolicy:
    returnable: bool
    window_days: int = 0
    refund_type: str = "full"  # full | credit | none
    restocking_fee_pct: Decimal = Decimal("0")


@dataclass(frozen=True)
class Offer:
    offer_id: str
    vertical: Vertical
    merchant: str
    title: str
    unit_price: Decimal
    in_stock: bool
    return_policy: ReturnPolicy
    quantity: int = 1
    shipping: Decimal = Decimal("0")
    currency: str = "USD"
    expires_at: datetime | None = None
    lead_time_days: int = 0  # days until delivery; B2B buyers usually cap this

    @property
    def total(self) -> Decimal:
        return self.unit_price * self.quantity + self.shipping

    def cart_item(self, quantity: int = 1) -> CartItem:
        """Wrap this offer as a cart line (ergonomics for host apps)."""
        return CartItem(offer=self, quantity=quantity)


@dataclass(frozen=True)
class CartItem:
    """One line in a cart: an offer plus how many units to buy."""
    offer: Offer
    quantity: int = 1

    @property
    def line_total(self) -> Decimal:
        return self.offer.unit_price * self.quantity + self.offer.shipping


@dataclass(frozen=True)
class Cart:
    """A multi-line purchase from ONE merchant in ONE vertical (a purchase order).
    Built by the host app or the model from prior search results, then gated as a unit:
    one gate decision, one approval, one checkout, one idempotency key."""
    items: tuple[CartItem, ...] = ()
    max_total: Decimal | None = None  # HARD ceiling set by the user/host, never the model
    cart_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])

    @property
    def total(self) -> Decimal:
        return sum((i.line_total for i in self.items), Decimal("0"))

    @property
    def vertical(self) -> Vertical:
        return self.items[0].offer.vertical

    @property
    def merchant(self) -> str:
        return self.items[0].offer.merchant

    @property
    def title(self) -> str:
        return "; ".join(f"{i.offer.title} x{i.quantity}" for i in self.items)


@dataclass(frozen=True)
class Receipt:
    order_id: str
    offer_id: str
    merchant: str
    total: Decimal
    idempotency_key: str
    status: str = "confirmed"
    items: tuple[tuple[str, int], ...] = ()  # (offer_id, quantity) per line


def to_dict(obj: Any) -> dict[str, Any]:
    """JSON-safe dict (Decimal/datetime/Enum -> str) for tool results and audit logs."""
    return cast(dict[str, Any], json.loads(json.dumps(asdict(obj), default=str)))
