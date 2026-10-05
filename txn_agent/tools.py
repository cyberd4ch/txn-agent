"""Claude tool-use surface. The model can search, build carts, and REQUEST purchases;
it cannot bypass the gate, raise the budget past the host-set ceiling, or touch payment data."""
from __future__ import annotations

from decimal import Decimal as D
from typing import TYPE_CHECKING, Any

from .agent import TransactionalAgent
from .models import Cart, CartItem, Intent, Offer, Vertical, to_dict

if TYPE_CHECKING:
    from .storage import SQLiteStore

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
    def __init__(self, agent: TransactionalAgent, budget_ceiling: D,
                 store: SQLiteStore | None = None, tenant: str = "default"):
        self.agent = agent
        self.budget_ceiling = budget_ceiling  # set by the user/host, never by the model
        self.store = store  # optional durable state (survives restarts)
        self.tenant = tenant
        self._intents: dict[str, Intent] = {}
        self._offers: dict[tuple[str, str], Offer] = {}
        self._carts: dict[str, Cart] = {}

    def _intent(self, intent_id: str) -> Intent | None:
        intent = self._intents.get(intent_id)
        if intent is None and self.store is not None:
            intent = self.store.load_intent(self.tenant, intent_id)
            if intent is not None:
                self._intents[intent_id] = intent
        return intent

    def _offer(self, intent_id: str, offer_id: str) -> Offer | None:
        offer = self._offers.get((intent_id, offer_id))
        if offer is None and self.store is not None:
            offer = self.store.load_offer(self.tenant, intent_id, offer_id)
            if offer is not None:
                self._offers[(intent_id, offer_id)] = offer
        return offer

    def _cart(self, cart_id: str) -> Cart | None:
        cart = self._carts.get(cart_id)
        if cart is None and self.store is not None:
            cart = self.store.load_cart(self.tenant, cart_id)
            if cart is not None:
                self._carts[cart_id] = cart
        return cart

    def _budget(self, requested: Any) -> D:
        """Clamp the model-requested budget to the host ceiling (never trust the model).
        Local models send things like "$200"; unparseable forms fail soft via the
        ValueError raised here (caught by call())."""
        text = str(requested).replace("$", "").replace(",", "").strip()
        try:
            return min(D(text), self.budget_ceiling)
        except ArithmeticError as e:  # decimal.InvalidOperation is an ArithmeticError
            raise ValueError(f"unparseable budget {requested!r}") from e

    def call(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        try:
            return self._dispatch(name, args)
        except (KeyError, TypeError, ValueError) as e:
            # Local/weak models send malformed arguments; fail soft so the loop can
            # report the problem back to the model instead of crashing the run.
            return {"error": f"invalid arguments for {name}: {e}"}

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        if name == "search_offers":
            intent = Intent(vertical=Vertical(args["vertical"]), query=args["query"],
                            max_total=self._budget(args["max_total"]),
                            quantity=int(args.get("quantity", 1)))
            self._intents[intent.intent_id] = intent
            offers = self.agent.search(intent)
            for o in offers:
                self._offers[(intent.intent_id, o.offer_id)] = o
            if self.store is not None:
                self.store.save_intent(self.tenant, intent)
                self.store.save_offers(self.tenant, intent.intent_id, offers)
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
            if self.store is not None:
                self.store.save_cart(self.tenant, cart)
            return {"cart_id": cart.cart_id, "merchant": merchant, "total": str(cart.total),
                    "lines": [to_dict(i.offer) | {"quantity": i.quantity} for i in items]}

        if name == "purchase_offer":
            pur_intent = self._intent(args["intent_id"])
            pur_quote = self._offer(args["intent_id"], args["offer_id"])
            if pur_intent is None or pur_quote is None:
                return {"error": "unknown intent_id/offer_id; search first"}
            out = self.agent.purchase(pur_intent, pur_quote)
            return {"status": out.status.value, "reasons": list(out.reasons),
                    "receipt": to_dict(out.receipt) if out.receipt else None}

        if name == "checkout_cart":
            pur_cart = self._cart(args["cart_id"])
            if pur_cart is None:
                return {"error": "unknown cart_id; build_cart first"}
            out = self.agent.checkout_cart(pur_cart)
            return {"status": out.status.value, "reasons": list(out.reasons),
                    "receipt": to_dict(out.receipt) if out.receipt else None}

        return {"error": f"unknown tool {name}"}
