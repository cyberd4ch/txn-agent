import threading
import time
from decimal import Decimal as D

from fastapi.testclient import TestClient

from txn_agent import TransactionalAgent
from txn_agent.connectors import default_connectors
from txn_agent.payments import DemoVault
from txn_agent.service import ServiceApprover, Tenant, TenantRegistry, create_app, tenants_from_env
from txn_agent.storage import SQLiteStore

HEADERS = {"X-API-Key": "test-key"}


def make_tenant(store: SQLiteStore | None = None) -> Tenant:
    approver = ServiceApprover(timeout_s=5.0, store=store, tenant="t")
    agent = TransactionalAgent(default_connectors(), DemoVault(), approver=approver)
    return Tenant(name="t", agent=agent, budget_ceiling=D("500"), store=store, approver=approver)


def test_approval_resolves_across_workers(tmp_path):
    """Worker A parks an approval; worker B resolves it via the shared store."""
    store = SQLiteStore(tmp_path / "shared.db")
    worker_a = ServiceApprover(timeout_s=5.0, store=store, tenant="t")
    worker_b = ServiceApprover(timeout_s=5.0, store=store, tenant="t")

    results: dict[str, bool] = {}

    def park_and_wait():
        from txn_agent.policy import Decision, GateResult

        class FakeCart:
            pass

        # request() needs a Cart-shaped object only to build the payload
        results["x"] = worker_a.request(_fake_cart(), GateResult(Decision.CONFIRM, ("cap",)))

    thread = threading.Thread(target=park_and_wait)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not worker_b.snapshot():
        time.sleep(0.05)
    pending = worker_b.snapshot()
    assert pending, "worker B should see the pending approval via the store"
    (approval_id, _), = pending
    assert worker_b.resolve(approval_id, approved=True)
    thread.join(timeout=5)
    assert results["x"] is True
    row = store.load_approval("t", approval_id)
    assert row["decision"] == "approved"


def test_approval_denial_cross_worker(tmp_path):
    store = SQLiteStore(tmp_path / "shared2.db")
    worker_a = ServiceApprover(timeout_s=5.0, store=store, tenant="t")
    worker_b = ServiceApprover(timeout_s=5.0, store=store, tenant="t")
    results: dict[str, bool] = {}

    def park_and_wait():
        results["x"] = worker_a.request(_fake_cart(), _confirm_gate())

    thread = threading.Thread(target=park_and_wait)
    thread.start()
    deadline = time.time() + 5
    while time.time() < deadline and not worker_b.snapshot():
        time.sleep(0.05)
    (approval_id, _), = worker_b.snapshot()
    worker_b.resolve(approval_id, approved=False)
    thread.join(timeout=5)
    assert results["x"] is False  # fail closed even cross-worker


def _fake_cart():
    from txn_agent.models import Cart, CartItem, Offer, ReturnPolicy, Vertical

    offer = Offer("off_1", Vertical.PARTS, "M", "thing", D("10"), True, ReturnPolicy(True, 30))
    return Cart(items=(CartItem(offer=offer),))


def _confirm_gate():
    from txn_agent.policy import Decision, GateResult

    return GateResult(Decision.CONFIRM, ("cap",))


def test_run_liveness_across_workers(tmp_path):
    store = SQLiteStore(tmp_path / "svc.db")
    app1 = create_app(TenantRegistry({"test-key": make_tenant(store)}))
    c1 = TestClient(app1)

    # a run parked on this worker: another worker sees it as processing
    s = c1.post("/v1/search", json={"vertical": "parts", "query": "drain pump",
                                    "max_total": 300}, headers=HEADERS).json()
    offer = next(o for o in s["offers"] if o["in_stock"])
    cart_id = c1.post("/v1/carts", json={"lines": [{"intent_id": s["intent_id"],
                                                    "offer_id": offer["offer_id"],
                                                    "quantity": 3}]}, headers=HEADERS).json()["cart_id"]
    run_id = c1.post(f"/v1/carts/{cart_id}/checkout", headers=HEADERS).json()["run_id"]
    deadline = time.time() + 5
    while time.time() < deadline:  # park on approval (over cap), never decide
        if c1.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()["status"] == "pending":
            break
        time.sleep(0.05)

    # worker 2 (fresh process memory, same store): owner pid alive -> processing
    c2 = TestClient(create_app(TenantRegistry({"test-key": make_tenant(store)})))
    assert c2.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()["status"] == "processing"

    # simulate the owning worker dying: mark row with a dead pid
    store.save_run("t", run_id, "submitted", owner_pid=99999999)
    assert c2.get(f"/v1/purchases/{run_id}", headers=HEADERS).json()["status"] == "interrupted"


def test_tenants_from_env(monkeypatch, tmp_path):
    monkeypatch.setenv("TXN_TENANTS", __import__("json").dumps({
        "acme": {"api_key": "ak_acme", "budget_ceiling": "250",
                 "store_path": str(tmp_path / "acme.db")},
        "globex": {"api_key": "ak_globex", "budget_ceiling": "1000"},
    }))
    reg = tenants_from_env()
    assert reg is not None
    acme = reg.resolve("ak_acme")
    assert acme.name == "acme" and acme.budget_ceiling == D("250")
    assert acme.store is not None
    assert reg.resolve("ak_globex").store is None
    with __import__("pytest").raises(Exception):
        reg.resolve("nope")


def test_tenants_from_env_stripe(monkeypatch, tmp_path):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_123")
    monkeypatch.setenv("TXN_TENANTS", __import__("json").dumps({
        "shop": {"api_key": "ak_shop", "budget_ceiling": "500",
                 "store_path": str(tmp_path / "shop.db"),
                 "stripe_payment_methods": {"shop": "pm_card_visa"}}}))
    reg = tenants_from_env()
    assert isinstance(reg.resolve("ak_shop").agent.vault, __import__("txn_agent.payments",
                                                                        fromlist=["StripeVault"]).StripeVault)


def test_tenants_from_env_invalid(monkeypatch):
    monkeypatch.setenv("TXN_TENANTS", "not-json")
    assert tenants_from_env() is None
    monkeypatch.setenv("TXN_TENANTS", '{"x": {}}')  # missing api_key/ceiling
    assert tenants_from_env() is None
    monkeypatch.delenv("TXN_TENANTS")
    assert tenants_from_env() is None
