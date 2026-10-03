"""The agent only ever handles opaque tokens, never card numbers."""
from __future__ import annotations

from typing import Protocol


class PaymentVault(Protocol):
    def token_for(self, user_id: str) -> str: ...


class DemoVault:
    """Replace with your PSP's tokenization / delegated-payment flow."""

    def token_for(self, user_id: str) -> str:
        return f"tok_demo_{user_id}"
