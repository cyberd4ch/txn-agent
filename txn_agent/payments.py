"""The agent only ever handles opaque tokens, never card numbers.

`PaymentVault` = identity-only (token_for). `ChargingVault` adds off-session money
movement: authorize before checkout, void if the merchant order fails. StripeVault is
the reference implementation against Stripe test mode (stdlib urllib only — no SDK).
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal
from typing import Any, Protocol

DEFAULT_STRIPE_TIMEOUT_S = 15.0


class PaymentError(Exception):
    """Raised when money movement fails (declined, no method on file, API error)."""


class PaymentVault(Protocol):
    def token_for(self, user_id: str) -> str: ...


class ChargingVault(PaymentVault, Protocol):
    """A vault that can move money for the exact purchase amount.

    `authorize` MUST be idempotent on idempotency_key (same key -> same charge).
    `void` cancels an authorization; failures surface so the caller can audit them.
    """

    def authorize(self, user_id: str, amount: Decimal, currency: str,
                  idempotency_key: str) -> str: ...

    def void(self, payment_ref: str) -> None: ...


class DemoVault:
    """Replace with your PSP's tokenization / delegated-payment flow."""

    def token_for(self, user_id: str) -> str:
        return f"tok_demo_{user_id}"


class StripeVault:
    """ChargingVault over Stripe PaymentIntents (test mode friendly).

    Off-session charge flow, which is what an autonomous agent needs:
      authorize()  -> confirm a PaymentIntent for the exact cart total (idempotent)
      void()       -> cancel it if the merchant order fails

    No Stripe SDK: talks form-encoded REST with stdlib urllib, so the core stays
    dependency-free and tests can point `base_url` at a fake server.

    Config: STRIPE_SECRET_KEY secret key (sk_test_...), and per-user saved payment
    methods (test mode cards like `pm_card_visa`). The agent process never sees card
    data — only PSP ids.
    """

    def __init__(self, api_key: str, payment_methods: dict[str, str], *,
                 base_url: str = "https://api.stripe.com", timeout_s: float = DEFAULT_STRIPE_TIMEOUT_S):
        if not api_key.startswith("sk_"):
            raise ValueError("StripeVault needs a secret key (sk_test_... / sk_live_...)")
        self.api_key = api_key
        self.payment_methods = dict(payment_methods)  # user_id -> pm_...
        self.base_url = base_url.rstrip("/")
        self.timeout_s = timeout_s

    # ------------------------------------------------------------------ helpers

    def _request(self, path: str, form: dict[str, Any] | None = None,
                 idempotency_key: str | None = None) -> dict[str, Any]:
        data = urllib.parse.urlencode(form, doseq=True).encode() if form is not None else None
        headers = {"Authorization": f"Bearer {self.api_key}"}
        if form is not None:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        if idempotency_key:
            headers["Idempotency-Key"] = idempotency_key
        req = urllib.request.Request(f"{self.base_url}{path}", data=data,
                                     headers=headers,
                                     method="POST" if data is not None else "GET")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return dict(json.loads(resp.read().decode()))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("error", {}).get("message", "")
            except (ValueError, AttributeError):
                pass
            raise PaymentError(f"stripe error (HTTP {e.code}): {detail or e.reason}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise PaymentError(f"stripe unreachable: {e}") from e

    @staticmethod
    def _cents(amount: Decimal) -> int:
        return int((amount * 100).quantize(Decimal("1")))

    # ------------------------------------------------------------------ protocol

    def token_for(self, user_id: str) -> str:
        """Identity-only token (no charge) for connectors that charge merchant-side."""
        return f"tok_stripe_{user_id}"

    def retrieve(self, payment_ref: str) -> dict[str, Any]:
        """Read a PaymentIntent back (verification, reconciliation, receipts)."""
        return self._request(f"/v1/payment_intents/{payment_ref}")

    def authorize(self, user_id: str, amount: Decimal, currency: str,
                  idempotency_key: str) -> str:
        pm = self.payment_methods.get(user_id)
        if pm is None:
            raise PaymentError(f"no payment method on file for user {user_id!r}")
        intent = self._request("/v1/payment_intents", {
            "amount": self._cents(amount),
            "currency": currency.lower(),
            "payment_method": pm,
            "confirm": "true",
            "off_session": "true",
            "payment_method_types[]": "card",
            "description": "txn-agent purchase",
        }, idempotency_key=idempotency_key)
        if intent.get("status") != "succeeded":
            raise PaymentError(f"payment not succeeded: status={intent.get('status')!r} "
                               f"last_error={intent.get('last_payment_error')}")
        return str(intent["id"])

    def void(self, payment_ref: str) -> None:
        self._request(f"/v1/payment_intents/{payment_ref}/cancel", {})
