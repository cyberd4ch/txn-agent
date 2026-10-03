"""Human approval channels. Fail closed: no approver wired = no purchase.

The agent only asks; a human (or your own auto-approver) decides. Every channel
returns a plain bool and must never raise into the transaction path — a broken
channel is treated as a denial.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import urllib.error
import urllib.request
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import Any

from .models import Cart
from .policy import GateResult

DEFAULT_TIMEOUT_S = 15


def approval_payload(cart: Cart, gate: GateResult) -> dict[str, Any]:
    """The exact JSON a human sees: who/what/why, amounts, gate reasons, audit ids."""
    return {
        "type": "approval_request",
        "cart_id": cart.cart_id,
        "merchant": cart.merchant,
        "vertical": cart.vertical.value,
        "total": str(cart.total),
        "lines": [{"offer_id": i.offer.offer_id, "title": i.offer.title,
                   "qty": i.quantity, "unit_price": str(i.offer.unit_price)} for i in cart.items],
        "decision": gate.decision.value,
        "reasons": list(gate.reasons),
        "requested_at": datetime.now(timezone.utc).isoformat(),
    }


class Approver(ABC):
    """Implement `request` to deliver the payload and return the human's decision."""

    @abstractmethod
    def request(self, cart: Cart, gate: GateResult) -> bool: ...


class AutoApprover(Approver):
    """Approves everything the gate sends. For demos/tests ONLY — never production."""

    def request(self, cart: Cart, default_gate: GateResult) -> bool:
        return True


class WebhookApprover(Approver):
    """POST the approval payload to your Slack/Teams/internal service and expect
    `{"approved": true|false}` back. Network or payload errors DENY (fail closed).

    Includes an HMAC-SHA256 signature header so the receiving side can verify the
    request came from a holder of `secret`. Set APPROVAL_WEBHOOK_URL + (optionally)
    APPROVAL_WEBHOOK_SECRET, or pass them explicitly.
    """

    def __init__(self, url: str | None = None, secret: str | None = None,
                 timeout_s: int = DEFAULT_TIMEOUT_S):
        self.url = url or os.environ.get("APPROVAL_WEBHOOK_URL", "")
        self.secret = secret or os.environ.get("APPROVAL_WEBHOOK_SECRET", "")
        self.timeout_s = timeout_s
        if not self.url:
            raise ValueError("WebhookApprover requires a URL (env APPROVAL_WEBHOOK_URL)")

    def _sign(self, body: bytes) -> dict[str, str]:
        if not self.secret:
            return {}
        mac = hmac.new(self.secret.encode(), body, hashlib.sha256).hexdigest()
        return {"X-Txn-Agent-Signature": f"sha256={mac}"}

    def request(self, cart: Cart, gate: GateResult) -> bool:
        body = json.dumps(approval_payload(cart, gate), default=str).encode()
        try:
            req = urllib.request.Request(
                self.url, data=body, method="POST",
                headers={"Content-Type": "application/json", **self._sign(body)})
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode())
            return bool(data.get("approved") is True)
        except (urllib.error.URLError, TimeoutError, ValueError, KeyError):
            return False  # fail closed: any channel failure is a denial
