# Reddit drafts

> **Where:** r/AI_Agents (primary), r/LocalLLaMA (lead with the model-agnostic
> angle), r/SideProject (lead with the demo). One post per sub, edited to each
> sub's self-promo rules.

---

**r/AI_Agents version:**

I got tired of "agent safety" threads where the answer is better prompts, so I built
the boring thing: a library that puts a deterministic gate between a purchasing
agent and the checkout button.

**txn-agent** (MIT, Python, stdlib-only core): the LLM builds a cart through tools,
then a plain function decides auto-buy / ask-a-human / reject. The model never sees
payment data, can never raise its own budget, and every decision is written to an
append-only audit log.

What it does in practice:
- Revalidates live price/stock seconds before checkout — stale quotes are never bought
- Fail-closed everywhere: no approver wired = decline; webhook down = decline
- Stripe charges the exact cart total off-session and voids automatically if the
  merchant order fails
- A FastAPI service with per-tenant keys and an ops console where a human approves
  over-cap purchases — approvals persist in SQLite, so any worker picks them up

Real transcripts (from the launch post, actual output):

```
$ python -m txn_agent "drain pump under $80"
-> purchased, order parts-0001 at PartsDirect, $64.74

$ python -m txn_agent --cart "drain pump" 1 --cart "door gasket" 2
Approval needed ... total 160.73 above auto-buy cap
(no interactive approver available -> denied)
-> declined
```

Repo: https://github.com/cyberd4ch/txn-agent — 62 tests, 87% coverage, mypy strict.
Merchants are mocked (contract doc + reference adapter for real ones). Roast the
gate design, that's what I want the feedback on.

---

**r/LocalLLaMA version:**

The interesting bit for this sub: **the safety layer doesn't care which model does
the proposing.** The Claude loop is one frontend; the actual executor is
`ToolRouter` — plain Python tools (`search_offers`, `build_cart`, `checkout_cart`)
with a deterministic policy gate between the tools and any checkout. Point any
tool-calling model — local or API — at the same router and the invariants hold,
because they live in the executor and the gate, not in the system prompt:

- budgets capped by the host, never the model's claim
- revalidate-before-checkout, idempotent content-derived checkout keys
- fail-closed approvals, append-only audit

txn-agent (MIT, stdlib-only core): https://github.com/cyberd4ch/txn-agent

Would love to see someone wire a local tool-calling model up to ToolRouter — the
loop in `txn_agent/llm.py` is ~80 lines and the SDK client is injectable, so an
OpenAI-compatible shim is small. Issues open if you want to build that adapter.

---

**r/SideProject version:**

I built the checkout button's bodyguard. txn-agent is an open-source Python library
that lets an AI agent do real purchasing — but only through a deterministic policy
gate: under your cap and in policy = auto-buys; anything unusual = parks until a
human approves; wrong merchant/lead time/price drift = rejected.

Built for B2B parts procurement (repair shops restocking pumps and gaskets), works
the same for flights and groceries. Stripe test-mode charges, audit log, ops
console for approvals, deploy guide included.

Demo transcripts and the honest "what's mock" list in the README:
https://github.com/cyberd4ch/txn-agent
