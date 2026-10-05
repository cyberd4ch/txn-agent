# Launch post: txn-agent v0.4.1

*(Draft for a technical launch — HN/Reddit/LinkedIn/blog. Tone: show the invariant,
show the code, admit what's mock. Terminal transcripts below are real captured output.)*

---

## Your purchasing agent doesn't need better prompts. It needs a gate.

If you let an LLM click "buy," you have a trust problem, not a prompting problem.
"Please don't spend more than $200" is a suggestion. A spend ceiling enforced in code
before money moves is a guarantee.

**txn-agent** (MIT, [github.com/cyberd4ch/txn-agent](https://github.com/cyberd4ch/txn-agent))
is a safety-first transaction layer for purchasing agents, built for B2B parts
procurement — repair shops, property management, fleets — and equally usable for
flights or grocery restocking.

The model proposes. The gate disposes.

```python
agent = TransactionalAgent(
    connectors, StripeVault(api_key, payment_methods),
    policy=PolicyConfig(auto_buy_cap={Vertical.PARTS: D("150")}),
    approver=WebhookApprover("https://slack-bot.internal/approve"),
)
outcome = agent.checkout_cart(cart)   # one gate decision, one approval, one order
```

What that looks like in a real terminal. Under the cap, the agent just buys
(the gate says so, in its own words):

```text
$ python -m txn_agent "drain pump under $80"
[parts] 'drain pump' budget $80
-> purchased
   within all auto-buy limits
   order parts-0001 at PartsDirect, $64.74
   line: parts-6 x1
```

Over the cap with no human wired in, it refuses — the same pipeline, failing closed
(note the gate re-checked the live total, $160.73, before asking):

```text
$ python -m txn_agent --cart "drain pump" 1 --cart "door gasket" 2

Approval needed: PartsDirect - Drain pump assembly x1 = $64.74
  - total 160.73 above auto-buy cap
Approve purchase? [y/N]   (no interactive approver available -> denied)
[cart] PartsDirect: Drain pump assembly x1; Refrigerator door gasket x2 = $160.73
-> declined
   total 160.73 above auto-buy cap
```

No prompt engineering in either run — the second one is the *absence* of an
approver, and the system treats that as a no.

Every purchase flows through one choke point: revalidate live prices/stock → evaluate
a deterministic policy gate (`AUTO_BUY / CONFIRM / B2B_REVIEW / REJECT`) → human
approval when the policy says so → one idempotent checkout. The LLM can build carts
and request purchases, but it structurally cannot bypass the gate — budgets come from
the host, never from the model, and there's no code path that moves money without
passing through `evaluate_cart`.

### Invariants, kept under test

1. Revalidate live state immediately before checkout — stale quotes are never bought.
2. Budget ceilings come from the host, never the model.
3. Fail closed everywhere: no approver wired = decline; webhook down = decline.
4. Checkout is idempotent on a content-derived key (safe retries, across restarts).
5. Only payment tokens cross the agent boundary — no card data in the process.
6. Every search, gate decision, approval, and order lands in an append-only audit log.

62 tests, 87% coverage, CI-enforced 85% floor, ruff + mypy strict, stdlib-only core.

### What's in the box

- **Carts as purchase orders** — multi-line, single-merchant, gated as a unit.
- **Wired Claude loop** — the model gets `search_offers`/`build_cart`/`checkout_cart`
  tools; the deterministic router executes them. Turn-budgeted, audited.
- **Real payments** — `StripeVault` charges exact cart totals off-session via
  PaymentIntents (stdlib urllib, no SDK) and voids automatically if the merchant
  order fails.
- **REST service + ops console** — per-tenant API keys, background purchase runs,
  approvals that persist across workers (approve from the web console, the worker
  picks it up), SQLite-backed state that survives restarts. Runs killed mid-purchase
  surface as `interrupted`, never silently resumed.
- **A merchant contract, not a platform** — distributors become agent-purchasable
  with three REST endpoints (`POST /offers/search`, `GET /offers/{id}`,
  `POST /orders` with mandatory idempotency dedupe). A runnable reference adapter
  ships in the repo; a one-pager for distributors explains the ask: pre-authorized
  orders, no double orders, no card data, no marketplace, no rev-share.

### Honest state

Merchant connectors are in-memory mocks plus a tested HTTP client — no live
distributor is integrated yet (that's the roadmap headline, and the contract doc is
the on-ramp). The Claude loop and Stripe path are verified against scripted/fake
servers, with the two live smokes documented as one-command manual steps. The buy
side of the house is the product; the catalog side is deliberately boring.

If you run a parts distribution business: three endpoints is the whole integration,
and the buyers arrive pre-approved and audited. If you build agents: the gate is a
library, not a philosophy — steal the invariants even if you don't take the code.

Repo: <https://github.com/cyberd4ch/txn-agent> ·
Deploy guide: `docs/deploy.md` · Merchant contract: `docs/connector-contract.md`
