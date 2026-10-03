import json
from dataclasses import replace
from decimal import Decimal as D

from txn_agent import Status, TransactionalAgent, Vertical
from txn_agent.agent import cart_idempotency_key
from txn_agent.approval import AutoApprover, approval_payload
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request
from txn_agent.models import Cart, CartItem


def make(confirm=None, approver=None, policy=None):
    conns = default_connectors()
    return (TransactionalAgent(conns, DemoVault(), confirm=confirm,
                               approver=approver, policy=policy), conns)


from txn_agent.payments import DemoVault  # noqa: E402


def cart_of(agent, *spec):
    """Build a single-merchant cart from (query, qty) specs against the mock catalog."""
    items = []
    for q, qty in spec:
        hits = agent.search(parse_request(q))
        assert hits, q
        live = next(o for o in hits if o.in_stock)  # a host would only cart in-stock offers
        items.append(CartItem(offer=live, quantity=qty))
    merchants = {i.offer.merchant for i in items}
    assert len(merchants) == 1, f"test cart must be single-merchant, got {merchants}"
    return Cart(items=tuple(items))


def test_cart_checkout_multi_line_b2b_order():
    agent, conns = make(approver=AutoApprover())
    cart = cart_of(agent, ("drain pump", 1), ("door gasket", 2))  # both PartsDirect
    out = agent.checkout_cart(cart)
    assert out.status is Status.PURCHASED
    assert out.receipt.merchant == "PartsDirect"
    assert dict(out.receipt.items) == {"parts-6": 1, "parts-5": 2}
    # shipping is charged once per line, matching CartItem.line_total
    assert out.receipt.total == D("58.75") + D("5.99") + 2 * D("44.00") + D("7.99")


def test_cart_gate_rejects_mixed_merchants():
    agent, _ = make(approver=AutoApprover())
    cart = Cart(items=(
        CartItem(agent.search(parse_request("drain pump"))[0], 1),
        CartItem(agent.search(parse_request("whole milk"))[0], 1),
    ))
    out = agent.checkout_cart(cart)
    assert out.status is Status.REJECTED
    assert any("mixes" in r for r in out.reasons)


def test_cart_price_drift_forces_approval():
    agent, conns = make(approver=None, confirm=lambda o, g: False)
    cart = cart_of(agent, ("drain pump", 1))
    quoted = cart.total
    conns[Vertical.PARTS].price_overrides["parts-6"] = D("75.00")  # +28%
    out = agent.checkout_cart(cart, quoted_total=quoted)
    assert out.status is Status.DECLINED
    assert any("price moved" in r for r in out.reasons)


def test_approved_merchants_routes_to_b2b_review():
    from txn_agent.policy import PolicyConfig
    cfg = PolicyConfig(approved_merchants={Vertical.PARTS: frozenset({"PartsDirect"})})
    agent, _ = make(approver=AutoApprover(), policy=cfg)
    out = agent.checkout_cart(cart_of(agent, ("dishwasher lower rack wheel kit", 1)))
    # cheapest in-stock (parts-2) is AppliancePartsCo -> B2B review -> auto-approved
    assert out.status is Status.PURCHASED and out.receipt.merchant == "AppliancePartsCo"
    assert any("not in approved list" in r for r in out.reasons)


def test_max_lead_time_flags_slow_merchants():
    from txn_agent.policy import PolicyConfig
    cfg = PolicyConfig(max_lead_time_days={Vertical.PARTS: 3})
    agent, _ = make(approver=AutoApprover(), policy=cfg)
    out = agent.checkout_cart(cart_of(agent, ("door gasket", 1)))  # parts-5, 6-day lead
    assert out.status is Status.PURCHASED
    assert any("lead time" in r for r in out.reasons)


def test_webhook_approval_signs_and_fails_closed():
    from txn_agent.approval import WebhookApprover
    from txn_agent.policy import Decision, GateResult

    agent, _ = make()
    cart = cart_of(agent, ("door gasket", 1))
    gate = GateResult(Decision.CONFIRM, ("needs approval",))

    captured = {}

    class Handler:
        def __init__(self, code=200, body=b'{"approved": true}'):
            self.code, self.body = code, body

        def __call__(self, req, timeout=15):
            # urllib normalizes header names to 'X-txn-agent-signature'
            captured["sig"] = req.headers.get("X-txn-agent-signature")
            captured["payload"] = json.loads(req.data)
            return self._resp()

        def _resp(self):
            import io
            return io.BytesIO(self.body)

    good = Handler()
    ap = WebhookApprover(url="http://approve.test/hook", secret="s3cr3t")
    import urllib.request
    orig = urllib.request.urlopen
    urllib.request.urlopen = good
    try:
        assert ap.request(cart, gate) is True
    finally:
        urllib.request.urlopen = orig
    assert captured["sig"].startswith("sha256=")
    assert captured["payload"]["total"] == str(cart.total)
    assert captured["payload"]["lines"][0]["offer_id"] == "parts-5"

    # Any channel failure denies (fail closed)
    import urllib.error
    ap_bad = WebhookApprover(url="http://approve.test/hook")
    assert ap_bad.request(cart, gate) is False  # no secret set -> no sig, but URL ok?
    # Actually with a reachable-but-rejecting endpoint:
    class Rejecting:
        def __call__(self, req, timeout=15):
            raise urllib.error.URLError("down")
    urllib.request.urlopen = Rejecting()
    try:
        ap2 = WebhookApprover(url="http://approve.test/hook")
        assert ap2.request(cart, gate) is False
    finally:
        urllib.request.urlopen = orig


def test_payload_shape_is_human_readable():
    agent, _ = make()
    cart = cart_of(agent, ("drain pump", 2))
    from txn_agent.policy import Decision, GateResult
    payload = approval_payload(cart, GateResult(Decision.CONFIRM, ("cap exceeded",)))
    assert payload["merchant"] == "PartsDirect"
    assert payload["lines"] == [{"offer_id": "parts-6", "title": "Drain pump assembly",
                                 "qty": 2, "unit_price": "58.75"}]
    assert payload["reasons"] == ["cap exceeded"]


def test_cart_idempotency_key_stable_and_content_sensitive():
    agent, _ = make()
    c1 = cart_of(agent, ("drain pump", 1), ("door gasket", 2))
    c2 = replace(c1)  # same cart_id + contents -> same key (safe retry)
    assert cart_idempotency_key(c1) == cart_idempotency_key(c2)
    assert cart_idempotency_key(c1) != cart_idempotency_key(replace(c1, cart_id="other"))
    assert cart_idempotency_key(c1) != cart_idempotency_key(replace(c1, items=c1.items[:1]))


def test_cart_checkout_is_idempotent():
    agent, conns = make(approver=AutoApprover())
    cart = cart_of(agent, ("drain pump", 1), ("door gasket", 1))
    out1 = agent.checkout_cart(cart)
    out2 = agent.checkout_cart(cart)
    assert out1.status is Status.PURCHASED and out2.status is Status.PURCHASED
    assert out1.receipt.order_id == out2.receipt.order_id
    assert len(conns[Vertical.PARTS].orders) == 1


def test_cart_out_of_stock_line_rejects_whole_order():
    agent, conns = make(approver=AutoApprover())
    cart = cart_of(agent, ("drain pump", 1), ("door gasket", 1))
    conns[Vertical.PARTS].stock_overrides["parts-5"] = False  # live stock drop
    out = agent.checkout_cart(cart)
    assert out.status is Status.REJECTED
    assert any("out of stock" in r for r in out.reasons)
