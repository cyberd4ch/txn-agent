# Show HN draft

> **Title:** Show HN: txn-agent – a deterministic gate between LLM purchasing agents and money
>
> **URL:** https://github.com/cyberd4ch/txn-agent

---

Hi HN — I built txn-agent because "please don't spend more than $200" is a prompt,
and prompts are suggestions. If an agent can click "buy," the spend limit needs to
live in code between the model and the checkout button.

txn-agent is a Python library (MIT, stdlib-only core) that sits exactly there. The
model (or a human, or your inventory system) builds a cart through tools; then a
deterministic function — not the model — decides: auto-buy, ask a human, or reject.

The invariants, all under test:

1. Revalidate live price/stock immediately before checkout; stale quotes are never
   bought, and the gate re-evaluates on the live numbers.
2. Budget ceilings come from the host, never from the model — the model literally
   cannot raise them.
3. Fail closed: no approver wired = decline; webhook down = decline; run
   interrupted by a restart = surfaced as interrupted, never silently resumed.
4. Checkout is idempotent on a content-derived key — retries are safe, mutations
   change the key.
5. Only payment tokens cross the agent boundary.
6. Every search, gate decision, approval, and order goes to an append-only audit log.

What's wired: a Claude tool-use loop (search_offers / build_cart / checkout_cart with
a ToolRouter executor), Stripe off-session charges for exact cart totals that void
automatically if the merchant order fails, a FastAPI service with per-tenant keys,
background runs, approvals that survive across workers, and an ops console for the
humans.

Honest state: merchant connectors are in-memory mocks plus a tested HTTP client
(docs/connector-contract.md is the integration path for real distributors). The
Stripe and Claude paths are verified against conforming fakes in CI; the two live
smokes are one command each and documented.

The part I think is most interesting is the merchant side: a distributor becomes
agent-purchasable with three REST endpoints and a mandatory idempotency dedupe —
no marketplace, no rev-share, and the buyers arrive pre-approved and audited.

Repo: https://github.com/cyberd4ch/txn-agent
B2B walkthrough: examples/repair_shop.py · Deploy guide: docs/deploy.md

Happy to answer questions about the gate design, the approval model, or why the
money-moving decision is a pure function of the offer.
