import json
import threading
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs

import pytest

from txn_agent import Status, TransactionalAgent, Vertical
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request
from txn_agent.models import Cart
from txn_agent.payments import PaymentError, StripeVault

INTENTS: dict[str, dict] = {}  # Idempotency-Key -> intent dict
CANCELED: set[str] = set()


class FakeStripe(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path.startswith("/v1/payment_intents/"):
            pi_id = self.path.rsplit("/", 1)[-1]
            for intent in INTENTS.values():
                if intent["id"] == pi_id:
                    return self._json(200, intent)
        return self._json(404, {"error": {"message": "not found"}})

    def do_POST(self):
        form = {k: v[0] for k, v in parse_qs(self.rfile.read(
            int(self.headers["Content-Length"])).decode()).items()}
        idem = self.headers.get("Idempotency-Key")
        if self.path == "/v1/payment_intents":
            assert idem, "Stripe requires Idempotency-Key for agent charges"
            if idem in INTENTS:  # Stripe dedupes on the key
                return self._json(200, INTENTS[idem])
            if form.get("payment_method") == "pm_card_declined":
                return self._json(402, {"error": {"type": "card_error",
                                                  "message": "Your card was declined."}})
            intent = {"id": f"pi_{idem[:12]}", "status": "succeeded",
                      "amount": int(form["amount"]), "currency": form["currency"]}
            INTENTS[idem] = intent
            return self._json(200, intent)
        if self.path.endswith("/cancel"):
            pi = self.path.rsplit("/", 2)[-2]
            CANCELED.add(pi)
            return self._json(200, {"id": pi, "status": "canceled"})
        return self._json(404, {"error": {"message": "not found"}})


@pytest.fixture(scope="module")
def stripe_url():
    srv = HTTPServer(("127.0.0.1", 0), FakeStripe)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()


def make_vault(base: str) -> StripeVault:
    return StripeVault("sk_test_123", {"shop-acct": "pm_card_visa"}, base_url=base)


def test_authorize_success_and_idempotency(stripe_url):
    vault = make_vault(stripe_url)
    ref1 = vault.authorize("shop-acct", D("160.73"), "USD", "key-1")
    ref2 = vault.authorize("shop-acct", D("160.73"), "USD", "key-1")
    assert ref1.startswith("pi_") and ref1 == ref2  # same key -> same intent
    # exact amount in cents, no float drift
    assert INTENTS["key-1"]["amount"] == 16073


def test_declined_card_raises_payment_error(stripe_url):
    vault = make_vault(stripe_url)
    vault.payment_methods["bad"] = "pm_card_declined"
    with pytest.raises(PaymentError, match="declined"):
        vault.authorize("bad", D("10.00"), "USD", "key-decline")


def test_missing_payment_method_fails_closed(stripe_url):
    vault = make_vault(stripe_url)
    with pytest.raises(PaymentError, match="no payment method"):
        vault.authorize("nobody", D("10.00"), "USD", "key-none")


def test_void_cancels_intent(stripe_url):
    vault = make_vault(stripe_url)
    ref = vault.authorize("shop-acct", D("5.00"), "USD", "key-void")
    vault.void(ref)
    assert ref in CANCELED


def test_retrieve_returns_intent_by_ref(stripe_url):
    vault = make_vault(stripe_url)
    ref = vault.authorize("shop-acct", D("12.34"), "USD", "key-retrieve")
    pi = vault.retrieve(ref)
    assert pi["id"] == ref and pi["status"] == "succeeded" and pi["amount"] == 1234


def test_example_stripe_charge_runs_green(stripe_url):
    """examples/stripe_charge.py must verify end-to-end against the fake Stripe."""
    import os
    import subprocess
    import sys

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = dict(os.environ, STRIPE_SECRET_KEY="sk_test_123", STRIPE_API_BASE=stripe_url)
    proc = subprocess.run(
        [sys.executable, os.path.join(root, "examples", "stripe_charge.py")],
        capture_output=True, text=True, env=env, timeout=60)
    assert proc.returncode == 0, f"stdout:\n{proc.stdout}\nstderr:\n{proc.stderr}"
    assert "purchasing:" in proc.stdout and "verified:  YES" in proc.stdout


def test_secret_key_validation():
    with pytest.raises(ValueError, match="secret key"):
        StripeVault("pk_live_x", {})


def test_agent_charges_exact_cart_total_then_checks_out(stripe_url):
    from txn_agent.approval import AutoApprover

    agent = TransactionalAgent(default_connectors(), make_vault(stripe_url),
                               approver=AutoApprover(), user_id="shop-acct")
    cart = Cart(items=(
        agent.search(parse_request("drain pump"))[0].cart_item(1),
        agent.search(parse_request("door gasket"))[0].cart_item(2),
    ))
    out = agent.checkout_cart(cart)
    assert out.status is Status.PURCHASED
    pi = INTENTS[out.receipt.idempotency_key]
    assert pi["amount"] == 16073  # cart total charged, to the cent


def test_agent_voids_charge_when_merchant_order_fails(stripe_url):
    from txn_agent.connectors.base import CheckoutError

    conns = default_connectors()

    def boom(offer, payment_token, idempotency_key, lines=None):
        raise CheckoutError("merchant exploded")

    conns[Vertical.GROCERY].checkout = boom  # type: ignore[method-assign]
    agent = TransactionalAgent(conns, make_vault(stripe_url), user_id="shop-acct")
    cart = Cart(items=(agent.search(parse_request("whole milk"))[0].cart_item(1),))
    out = agent.checkout_cart(cart)
    assert out.status is Status.FAILED
    assert any("merchant exploded" in r for r in out.reasons)
    assert CANCELED  # the PaymentIntent was voided after the checkout error
