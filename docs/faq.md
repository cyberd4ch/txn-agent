# FAQ — the objections, answered

Questions we expect on every launch thread, answered from the code rather than
around them.

## "Can't I just prompt the model not to overspend?"

A prompt is a suggestion; `policy.evaluate_cart` is a function of the offer and your
`PolicyConfig`. The model never holds an unchecked path to money: budgets pass
through `ToolRouter._budget`, which clamps to the host-set ceiling before anything
else happens, and `checkout_cart` re-runs the gate on freshly revalidated offers.
The ceiling is a constructor argument, not a sentence in the system prompt.

## "An agent spending money autonomously — who is liable?"

You configured the caps and the merchant list; the agent can only act inside them,
and anything unusual parks for a human (`WebhookApprover`, the `/ops` console, or
your own `Approver`). Every search, gate decision, approval, and order is in an
append-only audit log. Charges are voided automatically if the merchant order fails.
The Stripe path is off-session against a payment method *you* registered — the same
liability shape as a saved card with a human clicking, plus a paper trail.

## "What stops the model from inventing offer IDs or merchants?"

Tool calls only work on state the router issued: `search_offers` returns offers and
records them under their `intent_id`; `purchase_offer`/`build_cart` can only spend
from that mapping — invented IDs fail with "search first" (a live `qwen2.5:3b` run
did exactly this and recovered). Unknown merchants route to `B2B_REVIEW` when you
set `approved_merchants`, and revalidation treats unknown/stale offers as out of
stock — fail closed.

## "Is this production-ready?"

Honest answer: the safety core is (65 tests, 86% coverage, CI-enforced 85% floor,
mypy strict, multi-worker approval resolution, restart-safe state). The merchant
side is mocked plus a tested HTTP contract — no live distributor yet, and the two
live smokes (Stripe test mode, a real local model) are documented one-command steps
in `docs/architecture.md`. See *Verification status* there for exactly what is and
is not verified.

## "Why stdlib-only, no SDKs?"

Auditability and dependency risk: the code that moves money is ~1,200 lines you can
read in an afternoon, and nothing in the supply chain changes under you. The Stripe
client is form-encoded REST; the local-model adapter is the same. The Claude SDK is
the only optional dependency, and it's injectable — the loop accepts any client.

## "How is this different from LangChain / agent frameworks?"

Frameworks orchestrate model calls; txn-agent governs the transaction boundary. It
is deliberately thin and boring at the model layer (one tool-use loop, ~150 lines)
so the guarantees live in the executor and the gate — which any framework, human,
or cron job can call. Bring your orchestrator; the choke point is
`TransactionalAgent`.

## "What about refunds and returns?"

Return policy is a first-class ranking input (window, refund type, restocking fee
feed the ranking cost and `min_return_days` policy). Post-purchase flows — returns,
disputes, partial refunds — are not built yet; the audit log is the paper trail
until then, and the roadmap lists the signed approval/refund UI.

## "Does the model ever see payment data?"

No. Card data never enters the process; connectors receive opaque `tok_*`/`pi_*`
references. Payment methods live in the vault configuration (e.g. `StripeVault`'s
per-user method map), which the model does not see.

## "Why is checkout idempotency content-derived?"

So retries are safe *across restarts* and across workers: the key hashes cart id,
merchant, sorted lines, and total — the same order request yields the same key
(the merchant or your own retry dedupes), and any mutation yields a new one. It is
also what makes `StripeVault` replays read back the same PaymentIntent instead of
double-charging.

## "Can it buy from multiple merchants in one order?"

No, by design: a cart is one merchant, one vertical, one gate decision, one
idempotency key — a purchase order, not a marketplace basket. Multi-merchant
requests split into separate carts (each gated on its own merits).
