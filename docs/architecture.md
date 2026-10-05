# Architecture

```
                 ┌────────────────────────────────────────────────────────────┐
                 │ host app / LLM loop (tools.py TOOL_SCHEMAS + ToolRouter)   │
                 │   search_offers · build_cart · purchase_offer · checkout_cart
                 └───────────────┬────────────────────────────────────────────┘
                                 │ intents, carts (budgets capped by host)
                                 ▼
┌──────────────┐   ┌───────────────────────────┐   ┌──────────────────────────┐
│ Connector    │   │ TransactionalAgent        │   │ policy.evaluate_cart     │
│ (per merchant│──▶│ search → rank → revalidate│──▶│ AUTO_BUY / CONFIRM /     │
│  search,     │   │ → gate → approve → checkout│  │ B2B_REVIEW / REJECT      │
│  revalidate, │   └─────────────┬─────────────┘   └────────────┬─────────────┘
│  checkout)   │                 │ over cap / flagged           │ reject
└──────────────┘                 ▼                              ▼
                   ┌───────────────────────────┐   (no order; reasons returned)
                   │ Approver (webhook, HMAC)  │
                   │ fails closed              │
                   └─────────────┬─────────────┘
                                 │ approved
                                 ▼
                   ┌───────────────────────────┐    ┌───────────────────────┐
                   │ Connector.checkout        │───▶│ Receipt (idempotent)  │
                   │ payment token only        │    └───────────────────────┘
                   └───────────────────────────┘
                                 │ every step
                                 ▼
                   ┌───────────────────────────┐
                   │ AuditLog (append-only JSONL) │
                   └───────────────────────────┘
```

## Module map

| Module | Responsibility | Key types |
|---|---|---|
| `models.py` | Frozen dataclasses shared everywhere | `Intent`, `Offer`, `CartItem`, `Cart`, `Receipt` |
| `policy.py` | Deterministic purchase gate — the only code that decides whether money moves | `PolicyConfig`, `evaluate_cart`, `Decision` |
| `agent.py` | Orchestration: search → rank → revalidate → gate → approve → checkout | `TransactionalAgent`, `Outcome`, `cart_idempotency_key` |
| `approval.py` | Human-in-the-loop channels, all fail closed | `Approver`, `WebhookApprover`, `approval_payload` |
| `connectors/base.py` | The merchant integration surface (3 calls) | `Connector`, `CheckoutError` |
| `connectors/mock.py` | In-memory merchants for tests/demos | `MockConnector`, `default_connectors` |
| `tools.py` | LLM tool schemas + router; the model can request, never force | `TOOL_SCHEMAS`, `ToolRouter` |
| `intent.py` | NL request → `Intent` (replaceable; budget stays host-controlled) | `parse_request` |
| `payments.py` | Token-only payment vault protocol | `PaymentVault`, `DemoVault` |
| `audit.py` | Append-only decision log (memory + optional JSONL file) | `AuditLog` |

## Design rules

1. **Single choke point.** Every purchase flows through `TransactionalAgent` — there is
   no API that moves money without passing the gate.
2. **Carts are the unit of purchase.** A `Cart` is one merchant, one vertical, N lines.
   Gating, approval, idempotency, and checkout all operate on the whole cart, which maps
   1:1 onto a B2B purchase order.
3. **Revalidation is mandatory.** The agent always re-reads live state immediately before
   checkout and re-gates on the live numbers; quoted prices are only used for drift checks.
4. **Idempotency is content-derived.** `cart_idempotency_key` hashes cart id, merchant,
   sorted lines, and total — retries are safe, mutations produce a new key.
5. **Extending = implementing two protocols.** A new merchant implements `Connector`; a
   new approval channel implements `Approver`. Nothing else in the core changes.

## Verification status

- **Core invariants** (revalidate-before-checkout, host-only budget ceilings,
  fail-closed approvals, idempotent checkout, token-only payments, append-only
  audit): under test in `tests/` — 62 tests, 87% coverage, CI-enforced 85% floor.
- **LLM loop** (`llm.py`): verified end-to-end against scripted tool-use clients
  (`tests/test_llm_loop.py`) — search → build_cart → checkout_cart with gate and
  ceiling enforcement. Live-model smoke is a manual step:
  `ANTHROPIC_API_KEY=sk-ant-... python -m txn_agent --llm "restock 2 door gaskets, under $150"`.
  Without the key the CLI fails closed (`error: install the llm extra...`, exit 2).
- **Local models** (`llm_openai.py`, live smoke 2026-10-05, Ollama 0.20.5,
  `qwen2.5:3b` via `--llm-endpoint`): under-cap request completed a full
  search → build_cart → checkout_cart → **purchased** ($64.74, order parts-0001)
  on the first try. A multi-line restock saw the model hallucinate offer ids,
  receive the fail-soft error, recover, and assemble the $160.73 cart — then stop
  and ask for approval rather than force checkout of an over-cap cart. Two real
  fixes came out of this smoke: currency-formatted budgets ("$200") now parse and
  cap, and malformed tool arguments fail soft into the loop instead of crashing.
  Note: models without tool templates (gemma3:1b) are rejected by the server;
  the deepseek models on disk are coder/reasoner variants without tool support.
- **Stripe** (`StripeVault`): verified against a conforming fake Stripe server —
  exact-cents charges, idempotent replays, void-on-merchant-failure, retrieve
  verification. A live test-mode charge is a manual step:
  `STRIPE_SECRET_KEY=sk_test_... python examples/stripe_charge.py`.
