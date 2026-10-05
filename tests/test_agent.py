from decimal import Decimal as D

from txn_agent import Status, TransactionalAgent, Vertical
from txn_agent.agent import idempotency_key
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request
from txn_agent.payments import DemoVault
from txn_agent.tools import ToolRouter


def make(confirm=None):
    conns = default_connectors()
    return TransactionalAgent(conns, DemoVault(), confirm=confirm), conns


def test_parse_request():
    i = parse_request("book a flight SFO to JFK under $400")
    assert i.vertical is Vertical.FLIGHTS and i.max_total == D("400")
    assert parse_request("restock whole milk").vertical is Vertical.GROCERY
    assert parse_request("dishwasher wheel kit").vertical is Vertical.PARTS


def test_parts_skips_out_of_stock_and_weak_returns_then_auto_buys():
    agent, _ = make()
    out = agent.run(parse_request("dishwasher lower rack wheel kit under $50"))
    assert out.status is Status.PURCHASED
    assert out.receipt.merchant == "AppliancePartsCo"  # cheapest (parts-1) is out of stock


def test_over_budget_finds_nothing():
    agent, _ = make()
    out = agent.run(parse_request("dishwasher lower rack wheel kit under $5"))
    assert out.status is Status.NO_VALID_OFFER


def test_flights_always_need_approval_and_fail_closed():
    agent, _ = make()  # no approver wired -> deny
    assert agent.run(parse_request("flight SFO to JFK under $400")).status is Status.DECLINED
    agent, _ = make(confirm=lambda o, g: True)
    out = agent.run(parse_request("flight SFO to JFK under $400"))
    assert out.status is Status.PURCHASED and out.receipt.merchant == "BudgetAir"


def test_price_drift_forces_confirmation():
    agent, conns = make()
    conns[Vertical.PARTS].price_overrides["parts-2"] = D("29.99")  # +~17%
    out = agent.run(parse_request("dishwasher lower rack wheel kit under $50"))
    assert out.status is Status.DECLINED
    assert any("price moved" in r for r in out.reasons)


def test_grocery_auto_buys_cheapest_total():
    agent, _ = make()
    out = agent.run(parse_request("restock whole milk under $20"))
    assert out.status is Status.PURCHASED and out.receipt.merchant == "FreshMart"


def test_checkout_is_idempotent():
    agent, conns = make()
    intent = parse_request("restock whole milk under $20")
    offer = agent.search(intent)[0]
    key = idempotency_key(intent, offer)
    conn = conns[Vertical.GROCERY]
    r1 = conn.checkout(offer, "tok_x", key)
    r2 = conn.checkout(offer, "tok_x", key)
    assert r1.order_id == r2.order_id and len(conn.orders) == 1


def test_router_caps_model_supplied_budget():
    agent, _ = make()
    router = ToolRouter(agent, budget_ceiling=D("5"))
    res = router.call("search_offers", {"vertical": "parts", "query": "dishwasher wheel kit",
                                        "max_total": 10000})
    out = router.call("purchase_offer", {"intent_id": res["intent_id"], "offer_id": "parts-2"})
    assert out["status"] == "rejected" and out["receipt"] is None


def test_router_accepts_currency_formatted_budget():
    """Local models send "$200"-style budgets; parse then cap at the ceiling."""
    agent, _ = make()
    router = ToolRouter(agent, budget_ceiling=D("100"))
    out = router.call("search_offers", {"vertical": "parts", "query": "drain pump",
                                        "max_total": "$200"})
    assert "error" not in out and out["offers"]  # parsed and capped to 100


def test_router_fails_soft_on_unparseable_budget():
    agent, _ = make()
    router = ToolRouter(agent, budget_ceiling=D("100"))
    out = router.call("search_offers", {"vertical": "parts", "query": "drain pump",
                                        "max_total": "cheap"})
    assert "error" in out and "unparseable budget" in out["error"]
