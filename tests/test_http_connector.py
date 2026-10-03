import json
import threading
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from txn_agent.connectors.base import CheckoutError
from txn_agent.connectors.http import HttpConnector
from txn_agent.intent import parse_request
from txn_agent.models import Offer, ReturnPolicy, Vertical

OFFER = {
    "offer_id": "off_8fa2",
    "title": "Dishwasher lower rack wheel kit",
    "unit_price": "24.99",
    "in_stock": True,
    "quantity": 1,
    "shipping": "4.99",
    "lead_time_days": 2,
    "return_policy": {"returnable": True, "window_days": 30,
                      "refund_type": "full", "restocking_fee_pct": "0"},
}

ORDERS: dict[str, dict] = {}


class MerchantHandler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # silence test output
        pass

    def _json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if not self.headers.get("Authorization", "").startswith("Bearer "):
            return self._json(401, {"error": "unauthorized"})
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        path = self.path.split("?")[0]
        if path.endswith("/offers/search"):
            return self._json(200, {"offers": [OFFER | {"quantity": body["quantity"]}]})
        if path.endswith("/orders"):
            key = body["idempotency_key"]
            if key in ORDERS:  # merchant MUST dedupe on the key
                return self._json(200, ORDERS[key])
            if not body["payment_token"].startswith("tok_"):
                return self._json(402, {"error": "bad payment token"})
            receipt = {"order_id": "ord_5501", "status": "confirmed", "total": "54.97",
                       "items": [[line["offer_id"], line["quantity"]] for line in body["lines"]]}
            ORDERS[key] = receipt
            return self._json(201, receipt)
        return self._json(404, {"error": "not found"})

    def do_GET(self):
        path = self.path.split("?")[0]
        if path.endswith("/offers/off_8fa2"):
            return self._json(200, OFFER)
        return self._json(404, {"error": "not found"})


@pytest.fixture(scope="module")
def server_url():
    srv = HTTPServer(("127.0.0.1", 0), MerchantHandler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()


def make(base) -> HttpConnector:
    return HttpConnector(Vertical.PARTS, base, api_token="merchant-key", merchant="PartsDirect")


def test_search_parses_offers(server_url):
    offers = make(server_url).search(parse_request("dishwasher wheel kit", 2))
    assert len(offers) == 1
    o = offers[0]
    assert o.offer_id == "off_8fa2"
    assert o.unit_price == D("24.99")
    assert o.quantity == 2
    assert o.return_policy.window_days == 30
    assert o.total == D("24.99") * 2 + D("4.99")


def test_revalidate_returns_live_offer(server_url):
    offer = Offer("off_8fa2", Vertical.PARTS, "PartsDirect", "kit", D("24.99"), True,
                  ReturnPolicy(True, 30), quantity=3)
    live = make(server_url).revalidate(offer)
    assert live.unit_price == D("24.99") and live.in_stock
    assert live.quantity == 3  # cart quantity preserved


def test_revalidate_404_fails_closed(server_url):
    offer = Offer("off_gone", Vertical.PARTS, "PartsDirect", "kit", D("24.99"), True,
                  ReturnPolicy(True, 30))
    live = make(server_url).revalidate(offer)
    assert live.in_stock is False  # unknown offer can never be bought


def test_checkout_returns_receipt_and_is_idempotent(server_url):
    conn = make(server_url)
    offer = Offer("off_8fa2", Vertical.PARTS, "PartsDirect", "kit", D("24.99"), True,
                  ReturnPolicy(True, 30))
    r1 = conn.checkout(offer, "tok_x", "key123", lines=[("off_8fa2", 2)])
    r2 = conn.checkout(offer, "tok_x", "key123", lines=[("off_8fa2", 2)])
    assert r1.order_id == "ord_5501" and r2.order_id == r1.order_id
    assert r1.total == D("54.97")
    assert r1.items == (("off_8fa2", 2),)


def test_checkout_payment_error_raises_checkout_error(server_url):
    conn = make(server_url)
    offer = Offer("off_8fa2", Vertical.PARTS, "PartsDirect", "kit", D("24.99"), True,
                  ReturnPolicy(True, 30))
    with pytest.raises(CheckoutError):
        conn.checkout(offer, "bad-token", "key402", lines=[("off_8fa2", 1)])


def test_network_down_fails_closed():
    # Nothing listens here -> URLError -> checkout raises, revalidate fails closed
    dead = "http://127.0.0.1:9"  # discard port
    conn = make(dead)
    offer = Offer("off_8fa2", Vertical.PARTS, "PartsDirect", "kit", D("24.99"), True,
                  ReturnPolicy(True, 30))
    live = conn.revalidate(offer)
    assert live.in_stock is False
    with pytest.raises(CheckoutError):
        conn.checkout(offer, "tok_x", "keydead")
