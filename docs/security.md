# Security model & deployment checklist

## Threat model

| Threat | Mitigation |
|---|---|
| LLM spends beyond its mandate | Budget ceiling set by host (`ToolRouter.budget_ceiling`); per-vertical `auto_buy_cap`; totals above cap require human approval |
| LLM bypasses the gate | Checkout paths only exist on `TransactionalAgent`; tools can only *request*. The gate is deterministic code, not a prompt |
| Prompt-injected "approved=yes" | Approvals never come from model output. `confirm`/`Approver` results must originate outside the LLM context; webhook approvals are HMAC-signed (`X-Txn-Agent-Signature`) and must be verified by the receiver |
| Stale price/stock bait | Mandatory `revalidate` immediately before checkout; >2% drift from quote forces confirmation |
| Stuck/duplicate orders | Content-derived idempotency keys; safe retries return the original receipt |
| Card data leakage | Only opaque payment tokens (`tok_...`) cross the boundary; the vault protocol keeps PSP integration outside the agent |
| Silent failures | Append-only audit log records every search, gate decision, approval, and order |
| Unvetted merchants | `approved_merchants` routes unknown merchants to `B2B_REVIEW` instead of buying |
| Broken approval channel | All approvers fail closed — network errors, malformed replies, or timeouts count as denial |

## What this library intentionally does NOT do

- **No real payments.** `DemoVault` is a stub; integrate your PSP's tokenization or
  delegated-payments flow before production. Never pass raw card data to a connector.
- **No authentication/authorization.** The host app owns user identity, sessions, and
  who may set `PolicyConfig`.
- **No encryption at rest.** Audit files are plaintext JSONL — ship them to storage your
  compliance team approves, or extend `AuditLog` with your own sink.
- **No anti-fraud.** The gate encodes *your* purchasing policy; velocity checks and
  fraud scoring belong in your `Approver` or PSP.

## Deployment checklist

- [ ] Replace `DemoVault` with a PSP token vault; assert tokens match your PSP's format.
- [ ] Replace `MockConnector`s with real merchant APIs; keep `revalidate` cheap (<2s).
- [ ] Set explicit `PolicyConfig` values per vertical — do not ship defaults.
- [ ] Configure `approved_merchants` for B2B verticals.
- [ ] Wire `WebhookApprover` with `APPROVAL_WEBHOOK_SECRET`; verify the HMAC header on
      the receiving service before acting on `{"approved": true}`.
- [ ] Point `AuditLog` at durable storage; alert on gate `REJECT`/`FAILED` spikes.
- [ ] Keep the LLM's tool set limited to `TOOL_SCHEMAS`; never expose a tool that can
      mutate `PolicyConfig` or the budget ceiling at runtime.
- [ ] Set `always_confirm` for any vertical where returns are hard (flights, custom parts).
- [ ] Review `max_price_drift_pct` — for volatile markets, lower it or tighten the cap.

## Reporting

Please report vulnerabilities privately via SECURITY.md contact (or open a draft
security advisory on GitHub) rather than a public issue.
