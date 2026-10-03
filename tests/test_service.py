import time
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from txn_agent import TransactionalAgent
from txn_agent.connectors import default_connectors
from txn_agent.payments import DemoVault
from txn_agent.service import ServiceApprover, Tenant, TenantRegistry, create_app

HEADERS = {"X-API-Key": "test-key"}


def make_tenant(approval_timeout_s: float = 5.0) -> Tenant:
    approver = ServiceApprover(timeout_s=approval_timeout_s)
    agent = TransactionalAgent(default_connectors(), DemoVault(), approver=approver)
    return Tenant(name="t", agent=agent, budget_ceiling=D("500"),
                  approval_timeout_s=approval_timeout_s, approver=approver)


@pytest.fixture()
def client():
    app = create_app(TenantRegistry({"test-key": make_tenant()}))
    return TestClient(app)


def poll_final(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()
        if body["status"] == "final":
            return body
        time.sleep(0.05)
    raise AssertionError("run did not reach a final state")


def poll_pending(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    """Poll past the 'processing' window until an approval is parked."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()
        if body["status"] != "processing":
            return body
        time.sleep(0.05)
    raise AssertionError("run never parked on an approval")


def over_cap_cart(client: TestClient) -> str:
    """Pump x1 + gasket x2 = $160.73, above the $150 parts auto-buy cap."""
    cart_lines = []
    for query, qty in (("drain pump", 1), ("door gasket", 2)):
        s = client.post("/v1/search", json={"vertical": "parts", "query": query,
                                            "max_total": 300}, headers=HEADERS).json()
        offer = next(o for o in s["offers"] if o["in_stock"])
        cart_lines.append({"intent_id": s["intent_id"], "offer_id": offer["offer_id"],
                           "quantity": qty})
    return client.post("/v1/carts", json={"lines": cart_lines},
                       headers=HEADERS).json()["cart_id"]


def test_healthz_no_auth(client):
    assert client.get("/healthz").json() == {"status": "ok"}


def test_search_requires_api_key(client):
    assert client.post("/v1/search", json={}).status_code in (401, 422)
    r = client.post("/v1/search", json={"vertical": "parts", "query": "kit", "max_total": 50},
                    headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


def test_search_returns_offers(client):
    r = client.post("/v1/search",
                    json={"vertical": "parts", "query": "dishwasher wheel kit", "max_total": 50},
                    headers=HEADERS)
    assert r.status_code == 200
    offers = r.json()["offers"]
    assert offers and any(o["in_stock"] for o in offers)


def test_auto_buy_purchase_flow(client):
    # grocery milk is far below the auto-buy cap: no approval needed
    s = client.post("/v1/search",
                    json={"vertical": "grocery", "query": "whole milk", "max_total": 20},
                    headers=HEADERS).json()
    offer = next(o for o in s["offers"] if o["in_stock"])
    cart = client.post("/v1/carts",
                       json={"lines": [{"intent_id": s["intent_id"],
                                        "offer_id": offer["offer_id"], "quantity": 1}]},
                       headers=HEADERS).json()
    r = client.post(f"/v1/carts/{cart['cart_id']}/checkout", headers=HEADERS)
    assert r.status_code == 200 and r.json()["status"] == "submitted"
    final = poll_final(client, r.json()["run_id"])
    assert final["outcome"]["status"] == "purchased"
    assert final["outcome"]["receipt"]["merchant"] in ("FreshMart", "ValueGrocer")


def test_over_cap_purchase_requires_approval_then_purchases(client):
    cart_id = over_cap_cart(client)
    run_id = client.post(f"/v1/carts/{cart_id}/checkout",
                         headers=HEADERS).json()["run_id"]

    body = poll_pending(client, run_id)
    assert body["status"] == "pending"
    assert body["approval"]["total"] == "160.73"
    assert any("auto-buy cap" in r for r in body["approval"]["reasons"])

    d = client.post(f"/v1/approvals/{body['approval_id']}", json={"approved": True},
                    headers=HEADERS)
    assert d.status_code == 200
    final = poll_final(client, run_id)
    assert final["outcome"]["status"] == "purchased"


def test_approval_denied_declines_purchase(client):
    cart_id = over_cap_cart(client)
    run_id = client.post(f"/v1/carts/{cart_id}/checkout",
                         headers=HEADERS).json()["run_id"]
    body = poll_pending(client, run_id)
    client.post(f"/v1/approvals/{body['approval_id']}", json={"approved": False},
                headers=HEADERS)
    final = poll_final(client, run_id)
    assert final["outcome"]["status"] == "declined"


def test_approval_timeout_fails_closed():
    app = create_app(TenantRegistry({"test-key": make_tenant(approval_timeout_s=0.3)}))
    client = TestClient(app)
    cart_id = over_cap_cart(client)
    run_id = client.post(f"/v1/carts/{cart_id}/checkout",
                         headers=HEADERS).json()["run_id"]
    body = poll_pending(client, run_id)
    assert body["status"] == "pending"
    time.sleep(0.5)  # let the timeout expire with no decision
    final = poll_final(client, run_id)
    assert final["outcome"]["status"] == "declined"  # fail closed


def test_unknown_cart_404(client):
    r = client.post("/v1/carts/nope/checkout", headers=HEADERS)
    assert r.status_code == 404
