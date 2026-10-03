"""Claude tool-use surface. The model can search, build carts, and REQUEST purchases;
it cannot bypass the gate, raise the budget past the host-set ceiling, or touch payment data."""
from __future__ import annotations

from decimal import Decimal as D
from typing import Any

from .agent import TransactionalAgent
from .models import Cart, CartItem, Intent, Offer, Vertical, to_dict

TOOL_SCHEMAS = [
    {
        "name": "search_offers",
        "description": "Search merchants for parts, flights, or groceries. Returns ranked offers "
                       "with price, stock and return policy. Does not buy anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "vertical": {"type": "string", "enum": [v.value for v in Vertical]},
                "query": {"type": "string"},
                "max_total": {"type": "number", "description": "Budget in USD (capped by the host app)"},
                "quantity": {"type": "integer", "minimum": 1, "default": 1},
            },
            "required": ["vertical", "query", "max_total"],
        },
    },
    {
        "name": "build_cart",
        "description": "Build a purchase-order cart from offers returned by search_offers. "
                       "All lines must be from ONE merchant. Does not buy anything.",
        "input_schema": {
            "type": "object",
            "properties": {
                "lines": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "intent_id": {"type": "string"},
                            "offer_id": {"type": "string"},
                            "quantity": {"type": "integer", "minimum": 1, "default": 1},
                        },
                        "required": ["intent_id", "offer_id"],
                    },
                },
                "max_total": {"type": "number",
                              "description": "Optional cart budget in USD (capped by the host app)"},
            },
            "required": ["lines"],
        },
    },
    {
        "name": "purchase_offer",
        "description": "Request purchase of a single offer from a prior search. Goes through the "
                       "policy gate; may require user approval or be refused.",
        "input_schema": {
            "type": "object",
            "properties": {"intent_id": {"type": "string"}, "offer_id": {"type": "string"}},
            "required": ["intent_id", "offer_id"],
        },
    },
    {
        "name": "checkout_cart",
        "description": "Request purchase of a whole cart built with build_cart. One gate decision, "
                       "one approval, one order. May require user approval or be refused.",
        "input_schema": {
            "type": "object",
            "properties": {"cart_id": {"type": "string"}},
            "required": ["cart_id"],
        },
    },
]


class ToolRouter:
    def __init__(self, agent: TransactionalAgent, budget_ceiling: D):
        self.agent = agent
        self.budget_ceiling = budget_ceiling  # set by the user/host, never by the model
        self._intents: dict[str, Intent] = {}
        self._offers: dict[tuple[str, str], Offer] = {}
        self._carts: dict[str, Cart] = {}

    def _budget(self, requested: Any) -> D:
        return min(D(str(requested)), self.budget_ceiling)

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_offers":
            intent = Intent(vertical=Vertical(args["vertical"]), query=args["query"],
                            max_total=self._budget(args["max_total"]),
                            quantity=int(args.get("quantity", 1)))
            self._intents[intent.intent_id] = intent
            offers = self.agent.search(intent)
            for o in offers:
                self._offers[(intent.intent_id, o.offer_id)] = o
            return {"intent_id": intent.intent_id,
                    "offers": [to_dict(o) | {"total": str(o.total)} for o in offers]}

        if name == "build_cart":
            items: list[CartItem] = []
            for line in args["lines"]:
                hit = self._offers.get((line["intent_id"], line["offer_id"]))
                if hit is None:
                    return {"error": f"unknown intent_id/offer_id: {line}; search first"}
                items.append(CartItem(offer=hit, quantity=int(line.get("quantity", 1))))
            if not items:
                return {"error": "empty cart"}
            merchant = items[0].offer.merchant
            if any(i.offer.merchant != merchant for i in items):
                return {"error": "cart must contain lines from a single merchant"}
            cart = Cart(items=tuple(items),
                        max_total=self._budget(args["max_total"]) if "max_total" in args else None)
            self._carts[cart.cart_id] = cart
            return {"cart_id": cart.cart_id, "merchant": merchant, "total": str(cart.total),
                    "lines": [to_dict(i.offer) | {"quantity": i.quantity} for i in items]}

        if name == "purchase_offer":
            pur_intent = self._intents.get(args["intent_id"])
            pur_quote = self._offers.get((args["intent_id"], args["offer_id"]))
            if pur_intent is None or pur_quote is None:
                return {"error": "unknown intent_id/offer_id; search first"}
            out = self.agent.purchase(pur_intent, pur_quote)
            return {"status": out.status.value, "reasons": list(out.reasons),
                    "receipt": to_dict(out.receipt) if out.receipt else None}

        if name == "checkout_cart":
            pur_cart = self._carts.get(args["cart_id"])
            if pur_cart is None:
                return {"error": "unknown cart_id; build_cart first"}
            out = self.agent.checkout_cart(pur_cart)
            return {"status": out.status.value, "reasons": list(out.reasons),
                    "receipt": to_dict(out.receipt) if out.receipt else None}

        return {"error": f"unknown tool {name}"}
