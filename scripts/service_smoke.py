#!/usr/bin/env python3
"""End-to-end smoke test against a RUNNING txn-agent service (stdlib only).

Usage:
    uvicorn txn_agent.service:app --port 8080 &
    python scripts/service_smoke.py            # TXN_SMOKE_URL / TXN_SMOKE_KEY override

Exercises: healthz, search, cart build, checkout, poll-to-purchased, ops console.
Exits non-zero on any failure, printing what went wrong.
"""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request

BASE = os.environ.get("TXN_SMOKE_URL", "http://127.0.0.1:8080").rstrip("/")
KEY = os.environ.get("TXN_SMOKE_KEY", "demo-key")
STEPS: list[str] = []


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict | str]:
    req = urllib.request.Request(
        f"{BASE}{path}", method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"X-API-Key": KEY, "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode()
            return resp.status, (json.loads(raw) if raw and raw.startswith("{") else raw)
    except urllib.error.HTTPError as e:
        return e.code, {}


def step(msg: str) -> None:
    STEPS.append(msg)
    print(f"  ok: {msg}")


def fail(msg: str) -> None:
    print(f"FAIL after {len(STEPS)} ok steps: {msg}", file=sys.stderr)
    sys.exit(1)


def main() -> int:
    status, health = call("GET", "/healthz")
    if status != 200 or health != {"status": "ok"}:
        fail(f"healthz: {status} {health}")
    step("healthz")

    status, res = call("POST", "/v1/search",
                       {"vertical": "parts", "query": "wheel kit", "max_total": 50})
    if status != 200 or not res.get("offers"):
        fail(f"search: {status} {res}")
    intent_id = res["intent_id"]
    offer = next(o for o in res["offers"] if o["in_stock"])
    step(f"search -> {len(res['offers'])} offers, best in-stock {offer['offer_id']}")

    status, cart = call("POST", "/v1/carts",
                        {"lines": [{"intent_id": intent_id,
                                    "offer_id": offer["offer_id"], "quantity": 1}]})
    if status != 200 or "cart_id" not in cart:
        fail(f"build cart: {status} {cart}")
    step(f"cart {cart['cart_id']} total ${cart['total']} at {cart['merchant']}")

    status, run = call("POST", f"/v1/carts/{cart['cart_id']}/checkout")
    if status != 200 or run.get("status") != "submitted":
        fail(f"checkout: {status} {run}")
    step(f"checkout submitted (run {run['run_id'][:12]}...)")

    deadline = time.time() + 15
    while time.time() < deadline:
        status, poll = call("GET", f"/v1/purchases/{run['run_id']}")
        if status != 200:
            fail(f"poll: {status}")
        if poll["status"] == "final":
            break
        if poll["status"] == "interrupted":
            fail("run was marked interrupted on a live server")
        time.sleep(0.2)
    else:
        fail("purchase never reached a final state")
    outcome = poll["outcome"]
    if outcome["status"] != "purchased" or not outcome["receipt"]:
        fail(f"expected purchased, got: {outcome}")
    step(f"purchased: order {outcome['receipt']['order_id']} "
         f"at {outcome['receipt']['merchant']} for ${outcome['receipt']['total']}")

    status, page = call("GET", "/ops")
    if status != 200 or "txn-agent ops console" not in str(page):
        fail(f"ops console: {status}")
    step("ops console served")

    print(f"SMOKE PASS: {len(STEPS)} steps")
    return 0


if __name__ == "__main__":
    sys.exit(main())
