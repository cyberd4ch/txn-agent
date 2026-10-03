"""Deterministic purchase gate. The LLM never decides whether money moves; this does.

Works on whole Carts (a merchant purchase order) as well as single offers: one
gate decision, one approval, one checkout, one idempotency key.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal as D
from enum import Enum
from typing import Protocol

from .models import Cart, CartItem, Intent, Offer, Vertical


class Decision(str, Enum):
    AUTO_BUY = "auto_buy"
    CONFIRM = "confirm"
    B2B_REVIEW = "b2b_review"
    REJECT = "reject"


@dataclass(frozen=True)
class GateResult:
    decision: Decision
    reasons: tuple[str, ...] = ()
    offer: Offer | None = None  # set when the gate was called on a single offer


@dataclass
class PolicyConfig:
    # Totals at or below the cap may auto-buy; above it needs human approval.
    auto_buy_cap: dict[Vertical, D] = field(default_factory=lambda: {
        Vertical.PARTS: D("150"), Vertical.FLIGHTS: D("0"), Vertical.GROCERY: D("120"),
    })
    # Minimum acceptable return window (days) for auto-buy; 0 = don't care.
    min_return_days: dict[Vertical, int] = field(default_factory=lambda: {
        Vertical.PARTS: 30, Vertical.FLIGHTS: 0, Vertical.GROCERY: 0,
    })
    # Verticals that ALWAYS need explicit approval (flights are rarely refundable).
    always_confirm: frozenset[Vertical] = frozenset({Vertical.FLIGHTS})
    # Max tolerated drift between the quoted total and the live total at checkout.
    max_price_drift_pct: D = D("2")
    # B2B: only buy from merchants on this list; empty = any merchant (with approval if over cap).
    approved_merchants: dict[Vertical, frozenset[str]] = field(default_factory=lambda: {
        Vertical.PARTS: frozenset(),
    })
    # B2B: max acceptable delivery lead time per vertical; 0 = don't care.
    max_lead_time_days: dict[Vertical, int] = field(default_factory=lambda: {
        Vertical.PARTS: 14, Vertical.FLIGHTS: 0, Vertical.GROCERY: 0,
    })


class _NeedsConfirm(Protocol):
    def total(self) -> D: ...
    @property
    def merchant(self) -> str: ...


def evaluate(offer: Offer, intent: Intent, cfg: PolicyConfig, quoted_total: D) -> GateResult:
    """Back-compat single-offer gate: evaluates the offer as a one-line cart."""
    return evaluate_cart(Cart(items=(CartItem(offer=offer),)), intent, cfg,
                         quoted_total=quoted_total)


def evaluate_cart(cart: Cart, intent: Intent | None, cfg: PolicyConfig,
                  quoted_total: D | None = None) -> GateResult:
    """Gate a whole cart. Fail closed: unknowns become CONFIRM (or REJECT for hard blocks)."""
    reject: list[str] = []
    confirm: list[str] = []

    if not cart.items:
        return GateResult(Decision.REJECT, ("empty cart",))
    vertical = cart.vertical
    if any(i.offer.vertical is not vertical for i in cart.items):
        return GateResult(Decision.REJECT,
                          ("cart mixes verticals; split into one cart per merchant/vertical",))
    if any(i.offer.merchant != cart.merchant for i in cart.items):
        return GateResult(Decision.REJECT, ("cart mixes merchants; split into one cart per merchant",))

    quoted = quoted_total if quoted_total is not None else cart.total

    for i in cart.items:
        o = i.offer
        if i.quantity < 1:
            reject.append(f"{o.offer_id}: quantity must be >= 1")
        if not o.in_stock:
            reject.append(f"{o.offer_id} ({o.title}): out of stock")
        if o.expires_at and o.expires_at <= datetime.now(timezone.utc):
            reject.append(f"{o.offer_id} ({o.title}): offer expired")

    if intent is not None and cart.total > intent.max_total:
        reject.append(f"total {cart.total} exceeds budget {intent.max_total}")
    if cart.max_total is not None and cart.total > cart.max_total:
        reject.append(f"total {cart.total} exceeds cart budget {cart.max_total}")

    if reject:
        return GateResult(Decision.REJECT, tuple(reject))

    if quoted > 0:
        drift = abs(cart.total - quoted) / quoted * 100
        if drift > cfg.max_price_drift_pct:
            confirm.append(f"price moved {drift:.1f}% since quote ({quoted} -> {cart.total})")

    for i in cart.items:
        o = i.offer
        rp = o.return_policy
        need = cfg.min_return_days.get(vertical, 0)
        if need and (not rp.returnable or rp.window_days < need):
            confirm.append(f"{o.offer_id} ({o.title}): return policy weaker than {need}-day minimum")
        if rp.restocking_fee_pct > 0:
            confirm.append(f"{o.offer_id} ({o.title}): restocking fee {rp.restocking_fee_pct}%")
        lead = cfg.max_lead_time_days.get(vertical, 0)
        if lead and o.lead_time_days > lead:
            confirm.append(f"{o.offer_id} ({o.title}): lead time {o.lead_time_days}d exceeds {lead}d cap")
        allowed = cfg.approved_merchants.get(vertical)
        if allowed and o.merchant not in allowed:
            return GateResult(Decision.B2B_REVIEW,
                              (f"merchant {o.merchant} not in approved list -> B2B review",))

    if vertical in cfg.always_confirm:
        confirm.append(f"{vertical.value} purchases always need approval")
    if cart.total > cfg.auto_buy_cap.get(vertical, D("0")):
        confirm.append(f"total {cart.total} above auto-buy cap")

    if confirm:
        return GateResult(Decision.CONFIRM, tuple(confirm))
    return GateResult(Decision.AUTO_BUY, ("within all auto-buy limits",))
