from __future__ import annotations

from typing import Protocol

from ..models import Intent, Offer, Receipt, Vertical


class CheckoutError(Exception):
    pass


class Connector(Protocol):
    """One per merchant/aggregator. Implement these three calls for a real integration."""

    vertical: Vertical

    def search(self, intent: Intent) -> list[Offer]:
        """Candidate offers with price, stock and return policy attached."""

    def revalidate(self, offer: Offer) -> Offer:
        """Live re-read of stock/price/policy immediately before checkout."""

    def checkout(self, offer: Offer, payment_token: str, idempotency_key: str,
                 lines: list[tuple[str, int]] | None = None) -> Receipt:
        """Place the order. MUST be idempotent on idempotency_key.

        `lines` is [(offer_id, quantity), ...] for a multi-line cart; None means a
        single-line purchase of `offer` itself.
        """
