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
- **Wired Claude loop** — `txn_agent.llm.run_llm_request` runs a real tool-use loop
  (`search_offers`, `build_cart`, `purchase_offer`, `checkout_cart`) with `ToolRouter`
  as the executor: the model proposes, the gate disposes, every tool call is audited.
- **Real merchant connectors** — `HttpConnector` speaks a small documented REST
  contract ([docs/connector-contract.md](docs/connector-contract.md)) so any parts
  distributor/aggregator can adopt it; tested against live HTTP.
- **REST service** — `txn_agent.service` ships a FastAPI app with per-tenant API keys,
  budget ceilings, background purchase runs, and an HTTP approval flow (over-cap
  purchases park on `POST /v1/approvals/{id}`; timeout = denied).
- **Stdlib-only core** — no runtime dependencies in the core; LLM/service extras are
  optional (`pip install ".[llm]"` / `".[service]"`).

## Install

```bash
pip install -e ".[dev]"        # with test tools
pip install -e ".[llm,service]" # Claude loop + REST service
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

# full Claude tool-use loop (needs ANTHROPIC_API_KEY)
export ANTHROPIC_API_KEY=sk-ant-...
python -m txn_agent --llm "restock 2 door gaskets and a drain pump, keep it under $200"

# REST service with per-tenant keys and HTTP approvals
uvicorn txn_agent.service:app --port 8080
curl -X POST localhost:8080/v1/search -H "X-API-Key: demo-key" \
     -H 'Content-Type: application/json' -d '{"vertical":"parts","query":"wheel kit","max_total":50}'
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
- `llm.py` — Claude tool-use loop: model proposes, `ToolRouter` executes, gate disposes
- `service.py` — FastAPI app: per-tenant keys, async purchase runs, HTTP approvals
- `connectors/` — `Connector` protocol; `mock.py` (in-memory merchants) + `http.py`
  (real APIs via [docs/connector-contract.md](docs/connector-contract.md))
- `tools.py` — Claude tool schemas + router (the model can request, never force, a buy)
- `intent.py` — naive request parser (swap for LLM extraction; keep budget user-controlled)
- `payments.py` — token-only vault protocol; `audit.py` — JSONL decision log

## Roadmap

- First live distributor on `HttpConnector` (the contract doc is the integration path).
- Real payment tokenization (PSP delegated payments) and a signed approval UI.
- Grocery baskets with perishable constraints; multi-merchant order splitting.
- Durable backing store for tenant state behind `TenantRegistry`; OpenAPI-first ops UI.

## License

MIT — see [LICENSE](LICENSE).
