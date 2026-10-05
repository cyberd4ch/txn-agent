# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: SemVer.

## [0.4.1] - 2026-10-05

### Added
- **Cross-worker approvals**: approvals and runs persist to the SQLite store, so a
  decision made by one worker (or the `/ops` console) resolves a run parked on
  another; `snapshot()` merges in-memory and stored state. Runs record their owning
  worker (`owner_pid`, with in-place `_migrate()` for existing databases); a restart
  that kills the owner surfaces the run as `interrupted` — never silently resumed
  (fail closed).
- **Multi-tenant env config**: `tenants_from_env()` builds the tenant registry from
  `TXN_TENANTS` JSON (per-tenant api key, budget ceiling, store path, Stripe payment
  methods, approval timeout) for multi-worker deploys behind one service.
- **Deploy guide** ([docs/deploy.md](docs/deploy.md)): uvicorn workers, `TXN_TENANTS`,
  TLS via Caddy, Stripe test/live setup, backups, a systemd unit, and a post-deploy
  smoke check.
- **Merchant reference adapter** (`examples/distributor_service.py`): the merchant
  side of the connector contract as a runnable FastAPI app (bearer auth, idempotent
  orders, decimal-string money) so a distributor becomes agent-purchasable in an
  afternoon; tested end-to-end through `HttpConnector`.
- **Distributor one-pager** ([docs/distributor-pitch.md](docs/distributor-pitch.md)).
- **Stripe charge example** (`examples/stripe_charge.py`): first live test-mode charge
  through the full pipeline — gate → PaymentIntent for the exact total → merchant
  order → read the intent back and verify amount/status. Refuses non-test keys.
- `StripeVault.retrieve(payment_ref)` for verification and reconciliation; the fake
  Stripe test server now serves GETs, which caught voids being sent as GET (see Fixed).
- **Coverage in CI**: pytest-cov with `--cov-fail-under=85`, Codecov upload, and CI/
  coverage badges.

### Fixed
- `StripeVault._request` sent bodyless POSTs (empty form, e.g. intent cancel) as GET —
  voids never reached the cancel endpoint against a conforming server.

### Changed
- Package version 0.4.1.

## [0.4.0] - 2026-10-03

### Added
- **Real payments**: `payments.py` gains `ChargingVault` (authorize/void, idempotent)
  and `StripeVault` — off-session Stripe PaymentIntents for the exact cart total via
  stdlib urllib (no SDK), voided automatically if the merchant order fails. The agent
  charges only after the gate passes; declined payments fail the purchase closed.
- **Durable state**: `storage.py` with `SQLiteStore` (stdlib sqlite3, WAL, tenant-
  namespaced intents/offers/carts/runs/approvals/audit) and `StoreAuditLog`.
  `ToolRouter` persists and reloads state, so carts can be checked out after a
  restart with identical receipts (idempotency across restarts). Runs interrupted by
  a restart surface as `interrupted` — never silently resumed (fail closed).
- **Ops console**: `GET /ops` (self-contained HTML/JS) lists pending approvals with
  Approve/Deny buttons, run statuses, and the audit tail, backed by `GET /v1/ops/state`.
- **CI service smoke**: `scripts/service_smoke.py` (stdlib only) exercises a live
  uvicorn server end-to-end (healthz → search → cart → checkout → purchased → ops);
  GitHub Actions gains a `service-smoke` job.

### Changed
- Mock connectors accept `pi_...` payment references (Stripe PaymentIntent ids) in
  addition to `tok_...`.
- Package version 0.4.0.

## [0.3.0] - 2026-10-03

### Added
- **Claude tool-use loop** (`txn_agent/llm.py`): `run_llm_request` wires `TOOL_SCHEMAS`
  into a real model run with `ToolRouter` as executor — host budget ceilings enforced,
  every tool call audited, turn budget guard, injectable client for tests.
  CLI: `python -m txn_agent --llm "..."`.
- **HTTP merchant connector** (`txn_agent/connectors/http.py`): `HttpConnector`
  implements the `Connector` protocol over the documented merchant REST contract
  (`docs/connector-contract.md`) — bearer auth, fail-closed revalidation (unknown
  offer / network error = out of stock), idempotent checkout, decimal-string money.
  Tested against a live in-process HTTP server.
- **REST service** (`txn_agent/service.py`): FastAPI app with per-tenant API keys and
  budget ceilings, async background purchase runs, HTTP approval flow (over-cap
  purchases park on `POST /v1/approvals/{id}`; timeout or denial = declined, fail
  closed), tenant-scoped audit endpoint. Extra: `pip install ".[service]"`.

### Changed
- Package version 0.3.0; `dev` extra now includes `httpx`; new `service` extra
  (`fastapi`, `uvicorn`).

## [0.2.0] - 2026-10-03

### Added
- **Carts**: multi-line, single-merchant purchase orders. `Cart`/`CartItem` models,
  `TransactionalAgent.checkout_cart` (revalidate all lines → gate whole cart → one
  approval → one idempotent checkout), `cart_idempotency_key`.
- **B2B policy knobs**: `approved_merchants` (unknown merchant → `B2B_REVIEW` decision),
  `max_lead_time_days`, plus `Offer.lead_time_days`.
- **Approval channels**: `approval.py` with `Approver` protocol, `WebhookApprover`
  (HMAC-signed POST, fails closed), `AutoApprover` (demo only), and a human-readable
  `approval_payload` for approval UIs.
- **Cart tools for LLM loops**: `build_cart` and `checkout_cart` schemas on
  `ToolRouter` with single-merchant validation and host budget capping.
- CLI `--cart` mode for multi-line orders and `--approval-webhook` for external approvals.
- B2B demo: `examples/repair_shop.py` (shop policy → restock cart → owner approval → audit).
- Docs: `docs/architecture.md`, `docs/security.md`; CONTRIBUTING, CHANGELOG, MIT LICENSE.
- Mock connector: cart checkout (per-line stock checks, per-line shipping), stock
  overrides for testing live stock drops.

### Changed
- Gate reasons are now returned with successful purchases (visible approval context).
- `evaluate(offer, ...)` now delegates to `evaluate_cart`; behavior preserved.
- README rewritten for the B2B parts/procurement positioning.

### Fixed
- Mock cart receipts now include per-line shipping, matching `Cart.total`.
