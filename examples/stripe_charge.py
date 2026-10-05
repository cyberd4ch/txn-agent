"""First live Stripe test-mode charge through the full agent pipeline.

    export STRIPE_SECRET_KEY=sk_test_...
    python3 examples/stripe_charge.py

What it does, in order:
 1. Builds an agent on the demo catalog with a StripeVault (test payment method).
 2. Runs a real purchase: gate -> PaymentIntent confirmed for the exact total
    -> (mock) merchant order -> receipt.
 3. Reads the PaymentIntent back from Stripe and verifies amount + status.
 4. Prints where to see it in the Stripe dashboard.

Optional: STRIPE_API_BASE overrides the API endpoint (tests point this at a fake).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from txn_agent import Cart, PaymentError, Status, StripeVault, TransactionalAgent
from txn_agent.approval import AutoApprover
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request


def main() -> int:
    secret = os.environ.get("STRIPE_SECRET_KEY", "")
    if not secret:
        print("set STRIPE_SECRET_KEY (test mode: sk_test_...)")
        return 2
    if not secret.startswith("sk_test_"):
        print("refusing: this example only runs with a TEST MODE key (sk_test_...)")
        return 2
    base = os.environ.get("STRIPE_API_BASE", "https://api.stripe.com")

    vault = StripeVault(secret, {"shop": "pm_card_visa"}, base_url=base)
    agent = TransactionalAgent(default_connectors(), vault, approver=AutoApprover(),
                               user_id="shop")
    offers = agent.search(parse_request("dishwasher lower rack wheel kit under $40"))
    best = next(o for o in offers if o.in_stock)
    cart = Cart(items=(best.cart_item(1),))

    print(f"purchasing: {best.title!r} from {best.merchant} (${best.total})")
    try:
        out = agent.checkout_cart(cart)
    except PaymentError as e:
        print(f"payment failed: {e}")
        return 1
    if out.status is not Status.PURCHASED:
        print(f"purchase did not complete: {out.status.value} {out.reasons}")
        return 1

    receipt = out.receipt
    assert receipt is not None
    # Replay the SAME idempotency key: the vault returns the original PaymentIntent
    # (never a second charge) and gives us its id back to verify against.
    try:
        pi_id = vault.authorize("shop", receipt.total, best.currency,
                                receipt.idempotency_key)
        pi = vault.retrieve(pi_id)
    except PaymentError as e:
        print(f"verification failed: {e}")
        return 1
    expected_cents = int(receipt.total * 100)
    ok = (pi.get("status") == "succeeded" and pi.get("amount") == expected_cents)
    print(f"receipt:   order {receipt.order_id} @ {receipt.merchant} for ${receipt.total}")
    print(f"stripe:    {pi.get('id')} status={pi.get('status')} "
          f"amount={pi.get('amount')} cents ({pi.get('currency', '').upper()})")
    print(f"verified:  {'YES' if ok else 'NO - AMOUNT/STATUS MISMATCH'}")
    print("dashboard: https://dashboard.stripe.com/test/payments")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
