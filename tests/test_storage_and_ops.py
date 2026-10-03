import time
from decimal import Decimal as D

import pytest
from fastapi.testclient import TestClient

from txn_agent import TransactionalAgent
from txn_agent.connectors import default_connectors
from txn_agent.payments import DemoVault
from txn_agent.service import ServiceApprover, Tenant, TenantRegistry, create_app
from txn_agent.storage import SQLiteStore, StoreAuditLog
from txn_agent.tools import ToolRouter

HEADERS = {"X-API-Key": "test-key"}


@pytest.fixture()
def client():
    app = create_app(TenantRegistry({"test-key": make_tenant()}))
    return TestClient(app)


def make_tenant(store: SQLiteStore | None = None, approval_timeout_s: float = 5.0) -> Tenant:
    approver = ServiceApprover(timeout_s=approval_timeout_s, store=store, tenant="t")
    audit = StoreAuditLog(store, "t") if store else None
    kwargs = {}
    if audit is not None:
        kwargs["audit"] = audit
    agent = TransactionalAgent(default_connectors(), DemoVault(), approver=approver, **kwargs)
    return Tenant(name="t", agent=agent, budget_ceiling=D("500"), store=store, approver=approver)


# ---------------------------------------------------------------- store roundtrips

def test_store_roundtrip_preserves_decimal_semantics(tmp_path):
    store = SQLiteStore(tmp_path / "t.db")
    r = ToolRouter(TransactionalAgent(default_connectors(), DemoVault()),
                   budget_ceiling=D("500"), store=store, tenant="t")
    res = r.call("search_offers", {"vertical": "parts", "query": "drain pump", "max_total": 300})
    intent_id = res["intent_id"]
    offer = next(o for o in res["offers"] if o["in_stock"])
    cart = r.call("build_cart", {"lines": [{"intent_id": intent_id,
                                            "offer_id": offer["offer_id"], "quantity": 2}]})

    loaded = store.load_cart("t", cart["cart_id"])
    assert loaded is not None
    assert loaded.total == D("58.75") * 2 + D("5.99")  # shipping once per line
    assert loaded.cart_id == cart["cart_id"]

    assert store.load_intent("t", intent_id).max_total == D("300")
    assert store.load_offer("t", intent_id, offer["offer_id"]).unit_price == D("58.75")


def test_toolrouter_survives_restart(tmp_path):
    store = SQLiteStore(tmp_path / "t.db")
    agent = TransactionalAgent(default_connectors(), DemoVault())
    r1 = ToolRouter(agent, budget_ceiling=D("500"), store=store, tenant="t")
    res = r1.call("search_offers", {"vertical": "parts", "query": "drain pump", "max_total": 300})
    offer = next(o for o in res["offers"] if o["in_stock"])
    cart = r1.call("build_cart", {"lines": [{"intent_id": res["intent_id"],
                                             "offer_id": offer["offer_id"]}]})

    # a "restarted" router: empty memory, same store
    r2 = ToolRouter(agent, budget_ceiling=D("500"), store=store, tenant="t")
    out = r2.call("checkout_cart", {"cart_id": cart["cart_id"]})
    assert out["status"] == "purchased"
    assert out["receipt"]["merchant"] == "PartsDirect"


# ---------------------------------------------------------------- service restarts

def _search_and_cart(client: TestClient, query: str, qty: int = 1) -> str:
    s = client.post("/v1/search", json={"vertical": "parts", "query": query,
                                        "max_total": 300}, headers=HEADERS).json()
    offer = next(o for o in s["offers"] if o["in_stock"])
    return client.post("/v1/carts",
                       json={"lines": [{"intent_id": s["intent_id"],
                                        "offer_id": offer["offer_id"], "quantity": qty}]},
                       headers=HEADERS).json()["cart_id"]


def poll_final(client: TestClient, run_id: str, timeout: float = 5.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()
        if body["status"] == "final":
            return body
        time.sleep(0.05)
    raise AssertionError("run did not reach a final state")


def test_service_state_survives_restart(tmp_path):
    store = SQLiteStore(tmp_path / "svc.db")
    app1 = create_app(TenantRegistry({"test-key": make_tenant(store)}))
    c1 = TestClient(app1)
    cart_id = _search_and_cart(c1, "drain pump")
    run_id = c1.post(f"/v1/carts/{cart_id}/checkout", headers=HEADERS).json()["run_id"]
    final1 = poll_final(c1, run_id)
    order_id = final1["outcome"]["receipt"]["order_id"]

    # "restart": fresh app, fresh in-memory state, same DB file
    app2 = create_app(TenantRegistry({"test-key": make_tenant(store)}))
    c2 = TestClient(app2)
    # the completed run is still queryable
    assert c2.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()["outcome"] \
        ["receipt"]["order_id"] == order_id
    # re-checkout of the same cart is idempotent across restarts
    c2.post(f"/v1/carts/{cart_id}/checkout", headers=HEADERS)
    final2 = poll_final(c2, run_id)
    assert final2["outcome"]["receipt"]["order_id"] == order_id
    # audit history survived
    assert any(e["event"] == "purchased" for e in c2.get("/v1/audit", headers=HEADERS).json()["events"])


def test_interrupted_run_after_restart_fails_closed(tmp_path):
    store = SQLiteStore(tmp_path / "svc2.db")
    c1 = TestClient(create_app(TenantRegistry({"test-key": make_tenant(store)})))
    cart_id = _search_and_cart(c1, "drain pump")
    # over the cap: pump x3 = 182.24 > 150 -> approval required, we never decide
    c1.post("/v1/search", json={"vertical": "parts", "query": "drain pump",
                                "max_total": 300}, headers=HEADERS)
    s = c1.post("/v1/search", json={"vertical": "parts", "query": "drain pump",
                                    "max_total": 300}, headers=HEADERS).json()
    offer = next(o for o in s["offers"] if o["in_stock"])
    cart_id = c1.post("/v1/carts", json={"lines": [{"intent_id": s["intent_id"],
                                                    "offer_id": offer["offer_id"],
                                                    "quantity": 3}]}, headers=HEADERS).json()["cart_id"]
    run_id = c1.post(f"/v1/carts/{cart_id}/checkout", headers=HEADERS).json()["run_id"]
    deadline = time.time() + 5
    while time.time() < deadline:  # wait until parked on approval
        if c1.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()["status"] == "pending":
            break
        time.sleep(0.05)

    # restart: the pending approval died with the old process
    c2 = TestClient(create_app(TenantRegistry({"test-key": make_tenant(store)})))
    body = c2.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()
    assert body["status"] == "interrupted"  # never silently resumed


# ---------------------------------------------------------------- ops console

def test_ops_console_and_state(client):
    html = client.get("/ops")
    assert html.status_code == 200 and "txn-agent ops console" in html.text
    assert client.get("/v1/ops/state").status_code in (401, 422)
    state = client.get("/v1/ops/state", headers=HEADERS).json()
    assert set(state) == {"approvals", "runs", "audit"}


def test_ops_state_shows_pending_approval_and_resolves(client):
    s = client.post("/v1/search", json={"vertical": "parts", "query": "drain pump",
                                        "max_total": 300}, headers=HEADERS).json()
    offer = next(o for o in s["offers"] if o["in_stock"])
    cart_id = client.post("/v1/carts", json={"lines": [{"intent_id": s["intent_id"],
                                                        "offer_id": offer["offer_id"],
                                                        "quantity": 3}]},
                          headers=HEADERS).json()["cart_id"]
    client.post(f"/v1/carts/{cart_id}/checkout", headers=HEADERS)
    deadline = time.time() + 5
    approvals = []
    while time.time() < deadline:
        approvals = client.get("/v1/ops/state", headers=HEADERS).json()["approvals"]
        if approvals:
            break
        time.sleep(0.05)
    assert approvals, "over-cap purchase should park an approval in ops state"
    assert approvals[0]["payload"]["total"] == "182.24"  # 3 x (58.75 + 5.99 shipping once)
    aid = approvals[0]["approval_id"]
    assert client.post(f"/v1/approvals/{aid}", json={"approved": True},
                       headers=HEADERS).status_code == 200
