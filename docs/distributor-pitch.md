# For parts distributors: become purchasable by software agents

Your best B2B customers — repair shops, property managers, fleets — are starting to
ask for something new: **let our purchasing agent order routine parts from you,
without a human filling in your web form every time.** Today that traffic either
doesn't reach you, or it arrives as scraping against your website. Both are bad.

txn-agent is an open-source purchasing-agent safety layer that your customers deploy
on their side. It turns "an AI that clicks buy" into something a shop owner will
actually sign off on: every purchase is revalidated against your live prices, gated
by the buyer's own spend policy, approved by a human when it's over budget, charged
through their payment provider, and written to an append-only audit log.

All we need from you is three boring REST endpoints. That's the whole integration.

## What you expose (plain REST, one afternoon)

Spec: [connector-contract.md](connector-contract.md) · Reference merchant
implementation: [`examples/distributor_service.py`](../examples/distributor_service.py)

1. **`POST /v1/offers/search`** — query + quantity → your catalog hits.
2. **`GET /v1/offers/{offer_id}?quantity=N`** — live price/stock/policy for one offer
   (buyers call this seconds before ordering; you never get stale-quote disputes).
3. **`POST /v1/orders`** — the order, with two rules: dedupe on the `idempotency_key`
   (retries can never create a second order) and accept money as decimal **strings**.

Bearer-token auth, JSON in/out. A FastAPI adapter you can point at your catalog or
ERP ships in the repo — most teams wire it to their inventory in a day.

## What you get

- **Pre-authorized orders.** The agent only buys inside spend caps the buyer's
  business sets, and anything unusual (big ticket, new merchant, out-of-policy lead
  time) parks until a human approves. By the time an order hits your API, the buyer's
  side has already said yes.
- **No card data, ever.** The agent handles opaque payment references only. You settle
  with the buyer exactly as you do today — their card via their PSP, or your existing
  net-terms account. No marketplace in the middle, no rev-share.
- **No double orders.** Every checkout carries an idempotency key and you dedupe on
  it; flaky networks and retries can't duplicate a shipment.
- **A contract, not a platform.** It's your API, your customers, your margin. Any
  buyer running txn-agent (or anything that speaks the contract) can purchase.

## Onboarding, end to end

1. Run the reference adapter locally against a fake catalog:
   `MERCHANT_API_KEY=dist-key uvicorn examples.distributor_service:app --port 9000`
2. Swap its three handlers for your catalog/ERP lookups; keep the contract shapes.
3. Exchange bearer tokens with your agent-enabled buyers. Done — you're in their
   search results, gated and audited like every other merchant.

## Why open source

Buyers adopt the agent only if they trust it, and they trust it because the safety
core is inspectable and the invariants are under test: revalidate before checkout,
ceilings from the host never the model, fail-closed approvals, token-only payments,
append-only audit. The more shops run it, the more agent traffic flows to the
distributors that speak the contract — early adopters get that demand first.

---

*txn-agent is MIT-licensed at [github.com/cyberd4ch/txn-agent](https://github.com/cyberd4ch/txn-agent).
Start from [connector-contract.md](connector-contract.md) — three endpoints is the whole ask.*
