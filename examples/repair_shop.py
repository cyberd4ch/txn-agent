"""Repair-shop demo: one weekly restock order, gated and audited.

Run:  PYTHONPATH=. python3 examples/repair_shop.py

The shop's parts manager (or an LLM with TOOL_SCHEMAS) decides WHAT to reorder.
This script plays that role with hardcoded needs, then hands the cart to the
TransactionalAgent. Note what the code does NOT do: it cannot choose the payment
method (tokens only), bypass the gate, or spend past the policy caps without an
approval going to the shop owner.
"""
from decimal import Decimal as D

from txn_agent import PolicyConfig, TransactionalAgent, Vertical
from txn_agent.approval import Approver, approval_payload
from txn_agent.audit import AuditLog
from txn_agent.connectors import default_connectors
from txn_agent.intent import parse_request
from txn_agent.models import Cart
from txn_agent.payments import DemoVault
from txn_agent.policy import GateResult

# -- Shop policy: the owner sets this once; no model can change it -------------
POLICY = PolicyConfig(
    auto_buy_cap={Vertical.PARTS: D("100")},           # orders over $100 need an owner OK
    min_return_days={Vertical.PARTS: 30},              # don't auto-buy non-returnable parts
    max_lead_time_days={Vertical.PARTS: 7},            # service SLAs: parts within a week
    approved_merchants={Vertical.PARTS: frozenset()},  # empty = any merchant (over cap -> review)
)

# -- What the shop needs this week (in real life: LLM/tool output or inventory) -
WEEKLY_NEEDS = [("drain pump", 1), ("door gasket", 2)]


class OwnerApproval(Approver):
    """In production: WebhookApprover posting to Slack/Teams. Here we simulate the
    owner seeing the exact payload a webhook would carry, then tapping Approve."""

    def request(self, cart: Cart, gate: GateResult) -> bool:
        payload = approval_payload(cart, gate)
        print("\n--- approval request -> shop owner -------------------------")
        for k, v in payload.items():
            print(f"{k:>12}: {v}")
        print("-------------------------------------------------------------")
        return True


def build_cart(agent: TransactionalAgent) -> Cart:
    items = []
    for query, qty in WEEKLY_NEEDS:
        hits = agent.search(parse_request(query))
        best = next(o for o in hits if o.in_stock)  # ranking already prefers cheap+returnable
        items.append(best.cart_item(qty))
    merchants = {i.offer.merchant for i in items}
    if len(merchants) > 1:  # one purchase order per merchant
        raise SystemExit(f"split needed: offers span {sorted(merchants)}")
    return Cart(items=tuple(items))


def main() -> int:
    agent = TransactionalAgent(default_connectors(), DemoVault(),
                               policy=POLICY, audit=AuditLog("repair_shop_audit.jsonl"),
                               approver=OwnerApproval())
    cart = build_cart(agent)
    out = agent.checkout_cart(cart)
    print(f"\nrestock cart: {out.cart.merchant}")
    for i in out.cart.items:
        print(f"  {i.offer.title:<35} x{i.quantity} @ ${i.offer.unit_price} (lead {i.offer.lead_time_days}d)")
    print(f"  total: ${out.cart.total}")
    print(f"-> {out.status.value}", *(f"\n   {r}" for r in out.reasons))
    if out.receipt:
        print(f"   order {out.receipt.order_id}: ${out.receipt.total}")
    return 0 if out.status.value == "purchased" else 1


if __name__ == "__main__":
    raise SystemExit(main())
