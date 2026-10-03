"""Naive request parser. Swap for an LLM extraction step, but keep the budget
ceiling user-controlled: the model may propose it, the host app must cap it."""
from __future__ import annotations

import re
from decimal import Decimal as D

from .models import Intent, Vertical

HINTS = {
    Vertical.FLIGHTS: ("flight", "fly ", "airfare", "airline"),
    Vertical.GROCERY: ("grocer", "restock", "milk", "eggs", "bread"),
}
DEFAULT_BUDGET = {Vertical.PARTS: D("100"), Vertical.FLIGHTS: D("500"), Vertical.GROCERY: D("75")}
_BUDGET = re.compile(r"(?:under|below|max|up to|<)\s*\$?(\d+(?:\.\d{1,2})?)|\$(\d+(?:\.\d{1,2})?)")


def parse_request(text: str, quantity: int = 1) -> Intent:
    lower = text.lower()
    vertical = Vertical.PARTS
    for v, hints in HINTS.items():
        if any(h in lower for h in hints):
            vertical = v
            break
    m = _BUDGET.search(lower)
    budget = D(m.group(1) or m.group(2)) if m else DEFAULT_BUDGET[vertical]
    query = (lower[:m.start()] + lower[m.end():]).strip() if m else lower
    return Intent(vertical=vertical, query=query, max_total=budget, quantity=quantity)
