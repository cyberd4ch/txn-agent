from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from decimal import Decimal as D
from enum import Enum
from typing import Any

from . import telemetry
from .approval import Approver
from .audit import AuditLog
from .connectors.base import CheckoutError, Connector
from .models import Cart, CartItem, Intent, Offer, Receipt, Vertical, to_dict
from .payments import PaymentError, PaymentVault
from .policy import Decision, GateResult, PolicyConfig, evaluate, evaluate_cart

ConfirmFn = Callable[[Offer, GateResult], bool]


class Status(str, Enum):
    PURCHASED = "purchased"
    DECLINED = "declined"          # human said no (or no approver wired up)
    REJECTED = "rejected"          # gate refused this offer
    NO_VALID_OFFER = "no_valid_offer"
    FAILED = "failed"              # checkout error


@dataclass
class Outcome:
    status: Status
    intent_id: str
    offer: Offer | None = None
    receipt: Receipt | None = None
    reasons: tuple[str, ...] = ()
    cart: Cart | None = None


def idempotency_key(intent: Intent, offer: Offer) -> str:
    raw = f"{intent.intent_id}|{offer.offer_id}|{offer.total}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def cart_idempotency_key(cart: Cart) -> str:
    """Same cart contents -> same key -> safe checkout retries."""
    lines = "|".join(f"{i.offer.offer_id}x{i.quantity}"
                     for i in sorted(cart.items, key=lambda i: i.offer.offer_id))
    raw = f"{cart.cart_id}|{cart.merchant}|{lines}|{cart.total}"
    return hashlib.sha256(raw.encode()).hexdigest()[:32]


def single_line_cart(offer: Offer, intent_id: str) -> Cart:
    """Adapt a single-offer purchase into a one-line cart (stable cart_id from the intent)."""
    return Cart(items=(CartItem(offer=offer),), cart_id=intent_id)


class TransactionalAgent:
    """search -> rank -> revalidate -> gate -> (confirm) -> checkout."""

    def __init__(self, connectors: Mapping[Vertical, Connector], vault: PaymentVault,
                 policy: PolicyConfig | None = None, audit: AuditLog | None = None,
                 confirm: ConfirmFn | None = None, user_id: str = "user",
                 approver: Approver | None = None):
        self.connectors = connectors
        self.vault = vault
        self.policy = policy or PolicyConfig()
        self.audit = audit or AuditLog()
        self.confirm: ConfirmFn = confirm or (lambda offer, gate: False)  # fail closed
        self.approver = approver  # structured approval channel (e.g. webhook); wins over confirm
        self.user_id = user_id

    def _approve(self, cart: Cart, gate: GateResult) -> bool:
        if self.approver is not None:
            return bool(self.approver.request(cart, gate))
        first = cart.items[0].offer
        return bool(self.confirm(first, GateResult(gate.decision, gate.reasons)))

    def _pay(self, cart: Cart, key: str, context_id: str) -> tuple[str, bool]:
        """Authorize the exact total if the vault supports it. Returns (payment_ref, charged).
        ChargingVaults move money here — after the gate, before the merchant order."""
        authorize = getattr(self.vault, "authorize", None)
        if authorize is None:
            return self.vault.token_for(self.user_id), False
        currency = cart.items[0].offer.currency
        try:
            return str(authorize(self.user_id, cart.total, currency, key)), True
        except PaymentError as e:
            self.audit.record(context_id, "payment_failed", error=str(e))
            raise

    def _void(self, payment_ref: str, context_id: str) -> None:
        """Best-effort cancel of an authorization after a failed merchant order."""
        void = getattr(self.vault, "void", None)
        if void is None:
            return
        try:
            void(payment_ref)
        except Exception as e:  # noqa: BLE001 - never mask the original checkout error
            self.audit.record(context_id, "void_failed", payment_ref=payment_ref, error=str(e))

    def checkout_cart(self, cart: Cart, quoted_total: D | None = None) -> Outcome:
        """Revalidate every line -> gate the whole cart -> (approve) -> one idempotent checkout."""
        with telemetry.span(telemetry.SPAN_CHECKOUT, {
            telemetry.ATTR_CART_ID: cart.cart_id,
            telemetry.ATTR_MERCHANT: cart.merchant,
            telemetry.ATTR_VERTICAL: cart.vertical.value,
            telemetry.ATTR_CART_TOTAL: cart.total,
            telemetry.ATTR_CART_LINES: len(cart.items),
        }) as active:
            return self._checkout_cart(cart, quoted_total, active)

    def _checkout_cart(self, cart: Cart, quoted_total: D | None, active: Any) -> Outcome:
        """The checkout pipeline, with the span passed in so every exit is recorded."""

        def done(outcome: Outcome) -> Outcome:
            telemetry.set_attribute(active, telemetry.ATTR_OUTCOME, outcome.status.value)
            if outcome.reasons:
                telemetry.add_event(active, "txn_agent.outcome", {"reasons": list(outcome.reasons)})
            return outcome

        conn = self.connectors[cart.vertical]
        live_items = tuple(CartItem(offer=conn.revalidate(i.offer), quantity=i.quantity)
                           for i in cart.items)  # never buy on stale quotes
        live = replace(cart, items=live_items)
        gate = evaluate_cart(live, None, self.policy,
                             quoted_total=quoted_total if quoted_total is not None else live.total)
        self.audit.record(live.cart_id, "gate", cart=to_dict(live),
                          decision=gate.decision, reasons=list(gate.reasons))
        # The decision is the fact worth querying later: which gates fire, on what,
        # and how often. Reasons are a short tuple, not free text, so they stay
        # safe as an attribute (and the gate is what produces them).
        telemetry.set_attributes(active, {
            telemetry.ATTR_GATE_DECISION: gate.decision.value,
            telemetry.ATTR_GATE_REASONS: list(gate.reasons),
        })

        if gate.decision is Decision.REJECT:
            return done(Outcome(Status.REJECTED, live.cart_id, cart=live,
                                reasons=tuple(f"{live.merchant}: {r}" for r in gate.reasons)))
        if gate.decision in (Decision.CONFIRM, Decision.B2B_REVIEW):
            with telemetry.span(telemetry.SPAN_APPROVAL, {
                telemetry.ATTR_CART_ID: live.cart_id,
                telemetry.ATTR_CART_TOTAL: live.total,
            }) as approval_span:
                approved = self._approve(live, gate)
                telemetry.set_attribute(approval_span, telemetry.ATTR_APPROVED, approved)
            telemetry.set_attribute(active, telemetry.ATTR_APPROVAL_REQUIRED, True)
            self.audit.record(live.cart_id, "confirmation", approved=approved)
            if not approved:
                return done(Outcome(Status.DECLINED, live.cart_id, cart=live, reasons=gate.reasons))

        key = cart_idempotency_key(live)
        telemetry.set_attribute(active, telemetry.ATTR_IDEMPOTENCY_KEY, key)
        first = live.items[0].offer
        lines = [(i.offer.offer_id, i.quantity) for i in live.items]
        try:
            payment_ref, charged = self._pay(live, key, live.cart_id)
        except PaymentError as e:
            telemetry.add_event(active, "txn_agent.payment_failed", {"error": str(e)})
            return done(Outcome(Status.FAILED, live.cart_id, cart=live, reasons=(f"payment: {e}",)))
        try:
            receipt = conn.checkout(first, payment_ref, key, lines=lines)
        except CheckoutError as e:
            telemetry.record_exception(active, e)
            if charged:
                self._void(payment_ref, live.cart_id)
            self.audit.record(live.cart_id, "checkout_failed", error=str(e))
            return done(Outcome(Status.FAILED, live.cart_id, cart=live, reasons=(str(e),)))
        self.audit.record(live.cart_id, "purchased", receipt=to_dict(receipt))
        return done(Outcome(Status.PURCHASED, live.cart_id, cart=live, receipt=receipt,
                            reasons=gate.reasons))

    def _rank_cost(self, o: Offer) -> D:
        cost = o.total * (1 + o.return_policy.restocking_fee_pct / 100)
        need = self.policy.min_return_days.get(o.vertical, 0)
        rp = o.return_policy
        if need and (not rp.returnable or rp.window_days < need):
            cost *= D("1.10")  # weak return policy costs ~10% in ranking
        return cost

    def search(self, intent: Intent) -> list[Offer]:
        offers = self.connectors[intent.vertical].search(intent)
        ranked = sorted(offers, key=self._rank_cost)
        self.audit.record(intent.intent_id, "search", query=intent.query,
                          vertical=intent.vertical, results=[o.offer_id for o in ranked])
        return ranked

    def purchase(self, intent: Intent, quote: Offer) -> Outcome:
        with telemetry.span(telemetry.SPAN_PURCHASE, {
            telemetry.ATTR_MERCHANT: quote.merchant,
            telemetry.ATTR_VERTICAL: quote.vertical.value,
            telemetry.ATTR_CART_TOTAL: quote.total,
        }) as active:
            return self._purchase(intent, quote, active)

    def _purchase(self, intent: Intent, quote: Offer, active: Any) -> Outcome:
        """Single-offer path (run/CLI), with the span passed in for every exit."""

        def done(outcome: Outcome) -> Outcome:
            telemetry.set_attribute(active, telemetry.ATTR_OUTCOME, outcome.status.value)
            return outcome

        conn = self.connectors[intent.vertical]
        live = conn.revalidate(quote)  # never buy on a stale quote
        gate = evaluate(live, intent, self.policy, quote.total)
        self.audit.record(intent.intent_id, "gate", offer=to_dict(live),
                          decision=gate.decision, reasons=gate.reasons)
        telemetry.set_attributes(active, {
            telemetry.ATTR_GATE_DECISION: gate.decision.value,
            telemetry.ATTR_GATE_REASONS: list(gate.reasons),
        })

        if gate.decision is Decision.REJECT:
            return done(Outcome(Status.REJECTED, intent.intent_id, live,
                                reasons=tuple(f"{live.merchant}: {r}" for r in gate.reasons)))
        if gate.decision in (Decision.CONFIRM, Decision.B2B_REVIEW):
            with telemetry.span(telemetry.SPAN_APPROVAL, {
                telemetry.ATTR_CART_TOTAL: live.total,
                telemetry.ATTR_VERTICAL: live.vertical.value,
            }) as approval_span:
                approved = self._approve(single_line_cart(live, intent.intent_id), gate)
                telemetry.set_attribute(approval_span, telemetry.ATTR_APPROVED, approved)
            telemetry.set_attribute(active, telemetry.ATTR_APPROVAL_REQUIRED, True)
            self.audit.record(intent.intent_id, "confirmation", approved=approved)
            if not approved:
                return done(Outcome(Status.DECLINED, intent.intent_id, live, reasons=gate.reasons))

        key = idempotency_key(intent, live)
        telemetry.set_attribute(active, telemetry.ATTR_IDEMPOTENCY_KEY, key)
        try:
            payment_ref, charged = self._pay(single_line_cart(live, intent.intent_id), key, intent.intent_id)
        except PaymentError as e:
            telemetry.add_event(active, "txn_agent.payment_failed", {"error": str(e)})
            return done(Outcome(Status.FAILED, intent.intent_id, live, reasons=(f"payment: {e}",)))
        try:
            receipt = conn.checkout(live, payment_ref, key)
        except CheckoutError as e:
            telemetry.record_exception(active, e)
            if charged:
                self._void(payment_ref, intent.intent_id)
            self.audit.record(intent.intent_id, "checkout_failed", error=str(e))
            return done(Outcome(Status.FAILED, intent.intent_id, live, reasons=(str(e),)))
        self.audit.record(intent.intent_id, "purchased", receipt=to_dict(receipt))
        return done(Outcome(Status.PURCHASED, intent.intent_id, live, receipt,
                            reasons=gate.reasons))

    def run(self, intent: Intent) -> Outcome:
        rejections: list[str] = []
        for quote in self.search(intent):
            out = self.purchase(intent, quote)
            if out.status is not Status.REJECTED:
                return out
            rejections.extend(out.reasons)
        return Outcome(Status.NO_VALID_OFFER, intent.intent_id, reasons=tuple(rejections))
