# Changelog

All notable changes to this project are documented here. Format based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versioning: SemVer.

## [Unreleased]

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
