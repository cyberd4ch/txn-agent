# LinkedIn draft

> **Where:** personal profile post (not a company page — early project, personal
> credibility travels further). First line is the hook; keep the link in the body,
> not a preview stub. Best posted Tue–Thu morning.

---

Most "AI agent" demos stop at the moment that matters: the click that spends money.

We built the layer for everything after that click. **txn-agent** is an open-source
safety layer for purchasing agents, designed for B2B parts and procurement — repair
shops, property managers, fleets — and usable for any routine purchasing.

The idea in one sentence: the AI proposes, a deterministic policy gate disposes.

- Routine restock inside the shop's spend cap? Auto-buys — with the gate's reasons
  logged, not just the purchase.
- Big ticket, new merchant, out-of-policy lead time? Parks until a human approves —
  from a Slack webhook or an ops console that survives server restarts.
- Anything that fails revalidation against live stock and prices? Never bought.

For the shops: your agent buys within ceilings you set, only from merchants you
approve, and every decision lands in an append-only audit log you can hand to an
auditor or insurer.

For the distributors: three REST endpoints turn you into agent-purchasable
inventory. Orders arrive pre-authorized (the buyer's side approved them), idempotent
(retries can never duplicate a shipment), and card-data-free — you keep your
customer, your terms, your margin. No marketplace, no rev-share. The contract and a
runnable reference adapter are in the repo.

MIT-licensed, stdlib-only core, 62 tests and 87% coverage enforced in CI.
Deploy guide included: multi-worker, TLS, backups.

GitHub: https://github.com/cyberd4ch/txn-agent

If you run a parts distribution business or a service fleet, I'd genuinely like
15 minutes of your skepticism — what would stop you from letting an agent order
from your own stack?
