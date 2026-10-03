from __future__ import annotations

import argparse

from .agent import TransactionalAgent
from .approval import WebhookApprover
from .audit import AuditLog
from .connectors import default_connectors
from .intent import parse_request
from .models import Cart, CartItem, Offer
from .payments import DemoVault
from .policy import GateResult


def prompt_confirm(offer: Offer, gate: GateResult) -> bool:
    print(f"\nApproval needed: {offer.merchant} - {offer.title} x{offer.quantity} = ${offer.total}")
    for r in gate.reasons:
        print(f"  - {r}")
    try:
        return input("Approve purchase? [y/N] ").strip().lower() == "y"
    except EOFError:  # non-interactive shell: fail closed
        print("  (no interactive approver available -> denied)")
        return False


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="txn-agent")
    p.add_argument("request", nargs="?", default=None,
                   help='e.g. "dishwasher lower rack wheel kit under $50"')
    p.add_argument("--quantity", type=int, default=1)
    p.add_argument("--audit-file", default=None)
    p.add_argument("--cart", nargs=2, action="append", metavar=("QUERY", "QTY"),
                   help="repeatable: multi-line cart, e.g. --cart 'drain pump' 1 --cart 'door gasket' 2")
    p.add_argument("--approval-webhook", default=None,
                   help="POST approval requests here; the endpoint replies {\"approved\": true|false}")
    a = p.parse_args(argv)

    agent = TransactionalAgent(default_connectors(), DemoVault(),
                               audit=AuditLog(a.audit_file),
                               confirm=prompt_confirm,
                               approver=WebhookApprover(a.approval_webhook) if a.approval_webhook else None)

    if a.cart:  # multi-line B2B purchase order through the cart pipeline
        items = []
        for query, qty in a.cart:
            hits = agent.search(parse_request(query))
            if not hits:
                print(f"no offers for '{query}'")
                return 1
            items.append(CartItem(offer=hits[0], quantity=int(qty)))
        merchants = {i.offer.merchant for i in items}
        if len(merchants) > 1:
            print(f"cart mixes merchants {sorted(merchants)}; split into one order per merchant")
            return 1
        out = agent.checkout_cart(Cart(items=tuple(items)))
        ordered = out.cart or Cart(items=tuple(items))  # None only on early rejection
        print(f"[cart] {ordered.merchant}: "
              + "; ".join(f"{i.offer.title} x{i.quantity}" for i in ordered.items)
              + f" = ${ordered.total}")
    else:
        if not a.request:
            p.error("provide a request, or use --cart for multi-line orders")
        intent = parse_request(a.request, a.quantity)
        print(f"[{intent.vertical.value}] '{intent.query}' budget ${intent.max_total}")
        out = agent.run(intent)

    print(f"-> {out.status.value}", *(f"\n   {r}" for r in out.reasons))
    if out.receipt:
        print(f"   order {out.receipt.order_id} at {out.receipt.merchant}, ${out.receipt.total}"
              + ("".join(f"\n   line: {oid} x{qty}" for oid, qty in out.receipt.items)))
    return 0 if out.status.value == "purchased" else 1
