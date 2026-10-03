# txn-agent

**A safety-first transaction layer for purchasing agents.** Search → rank → revalidate →
policy gate → human approval → idempotent checkout — with a deterministic gate the LLM
can never bypass, and an audit trail of every decision.

Built for **B2B parts & procurement** (repair shops, property management, fleets) and
equally usable for flights or grocery restocking.

```python
from decimal import Decimal as D
from txn_agent import PolicyConfig, TransactionalAgent, Vertical
from txn_agent.approval import WebhookApprover
from txn_agent.connectors import default_connectors   # swap for real merchants
from txn_agent.payments import DemoVault              # swap for your PSP's tokens

agent = TransactionalAgent(
    default_connectors(), DemoVault(),
    policy=PolicyConfig(auto_buy_cap={Vertical.PARTS: D("100")}),
    approver=WebhookApprover("https://slack-bot.internal/approve"),
)
outcome = agent.checkout_cart(cart)   # one gate decision, one approval, one order
```

## Why

Letting an LLM click "buy" is a trust problem, not a prompting problem. txn-agent makes
the *money-moving decision* a deterministic, testable, auditable function of the offer
and a policy you own:

- **The model proposes, the gate disposes.** An LLM (or a human, or your inventory
  system) builds carts through tools; `policy.evaluate_cart` decides whether the
  purchase auto-runs, needs approval, or is rejected.
- **Fail closed everywhere.** No approver wired → decline. Webhook down → decline.
  Stock/price changed at revalidation → re-gate or reject.
- **Only payment tokens cross the boundary.** Card data never enters this process.

## Invariants (kept under test)

1. Revalidate live stock/price/policy right before checkout.
2. Budget ceilings come from the user/host, never the model (`ToolRouter` caps them).
3. Verticals flagged `always_confirm` (flights) always need approval; no approver wired = decline.
4. Checkout is idempotent on a deterministic key (same cart → same key → safe retries).
5. Only payment tokens cross the agent boundary.
6. Every search, gate decision, approval, and order is recorded in an append-only audit log.

## Features

- **Carts as purchase orders** — multi-line, single-merchant, gated as a unit with one
  idempotency key (the B2B path; single offers are just one-line carts).
- **B2B policy knobs** — `approved_merchants` (routes unknown merchants to review),
  `max_lead_time_days`, per-vertical `auto_buy_cap`, `min_return_days`, restocking-fee
  awareness, and a price-drift check against the quoted total.
- **Pluggable approvals** — `WebhookApprover` (HMAC-signed POST, expects
  `{"approved": true|false}`, fails closed) or bring your own `Approver`.
- **LLM tool surface** — `TOOL_SCHEMAS` + `ToolRouter` for a Claude tool-use loop:
  `search_offers`, `build_cart`, `purchase_offer`, `checkout_cart`.
- **Stdlib-only core** — no runtime dependencies; mocks for all three verticals.

## Install

```bash
pip install -e ".[dev]"     # with test tools
pytest
```

## Try it

```bash
python -m txn_agent "dishwasher lower rack wheel kit under $50"
python -m txn_agent "flight SFO to JFK under $400"
python -m txn_agent "restock whole milk under $20"

# multi-line B2B purchase order (auto-picks in-stock offers, single merchant)
python -m txn_agent --cart "drain pump" 1 --cart "door gasket" 2

# route approvals to a webhook instead of the terminal
python -m txn_agent --cart "drain pump" 1 --approval-webhook https://your.app/approve
```

A complete B2B walkthrough (shop policy, weekly restock cart, owner approval, audit
file): [`examples/repair_shop.py`](examples/repair_shop.py) —
`PYTHONPATH=. python3 examples/repair_shop.py`.

## Architecture

```
Intent ──▶ search (Connector) ──▶ rank ──▶ revalidate (live) ──▶ policy gate ──▶ approve? ──▶ checkout
                                              │                    │                │             │
                                              └─ stale = never bought  │     Approver (webhook)  Receipt
                                                                       │                         │
                                                                    audit.log ◀──────────────────┘
```

See [docs/architecture.md](docs/architecture.md) for the module map and
[docs/security.md](docs/security.md) for the threat model and deployment checklist.

## Layout

- `agent.py` — orchestrator: `search`, `purchase`, `checkout_cart`, `run`
- `policy.py` — deterministic gate: `AUTO_BUY / CONFIRM / B2B_REVIEW / REJECT`
- `approval.py` — approval channels: webhook (HMAC-signed, fail-closed), auto (demo only)
- `connectors/` — `Connector` protocol (search, revalidate, checkout) + mock merchants
- `tools.py` — Claude tool schemas + router (the model can request, never force, a buy)
- `intent.py` — naive request parser (swap for LLM extraction; keep budget user-controlled)
- `payments.py` — token-only vault protocol; `audit.py` — JSONL decision log

## Roadmap

- Real connectors (distributor APIs with quote/stock endpoints are a natural fit for `Connector`).
- Wire `TOOL_SCHEMAS` into a Claude tool-use loop with `ToolRouter.call` as the executor.
- Grocery baskets with perishable constraints; multi-merchant order splitting.
- Real payment tokenization (PSP delegated payments) and a signed approval UI.

## License

MIT — see [LICENSE](LICENSE).
