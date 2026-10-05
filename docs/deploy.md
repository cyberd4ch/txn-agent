# Production deployment guide

How to run txn-agent as a service for multiple tenants, with real payments and
durable state. Read [security.md](security.md) first — deployment inherits every
assumption there.

## 1. Configure tenants

Tenants come from the `TXN_TENANTS` environment variable (JSON). Each tenant gets its
own API key, budget ceiling, optional SQLite state file, and optional Stripe vault:

```bash
export TXN_TENANTS='{
  "acme-parts": {
    "api_key": "ak_live_change_me_1",
    "budget_ceiling": "250",
    "approval_timeout_s": 300,
    "store_path": "/var/lib/txn-agent/acme.db",
    "stripe_payment_methods": {"acme-user": "pm_1ABCdefGHIjklMNOp"}
  },
  "globex-fleet": {
    "api_key": "ak_live_change_me_2",
    "budget_ceiling": "1000",
    "store_path": "/var/lib/txn-agent/globex.db"
  }
}'
```

Rules baked into `tenants_from_env`:

- A tenant with `stripe_payment_methods` **requires** `STRIPE_SECRET_KEY` in the
  environment; otherwise startup falls back to the demo tenant (fail closed).
- `budget_ceiling` is a hard cap on any budget a model or request may pass — the
  gate still applies `auto_buy_cap` on top of it.
- Without `store_path` the tenant runs on in-memory state (fine for dev, not prod).

Generate keys with `python -c "import secrets; print(secrets.token_urlsafe(24))"`.
Store them in your secret manager; rotate by adding a new key and removing the old.

## 2. Run the server

```bash
pip install "txn-agent[service]"   # or: pip install -e ".[service]"
uvicorn txn_agent.service:app --host 0.0.0.0 --port 8080 --workers 4
```

**Worker count vs. the approval flow:** approvals resolve across workers through the
shared SQLite store (each waiter also polls the DB row, not just its in-process
event), so `--workers > 1` works — but all workers must share the same `store_path`
per tenant, on the same host (SQLite + WAL is local-disk only). If you run without
`store_path`, use `--workers 1` or in-flight approvals will strand.

Run-state semantics across workers (implemented and tested):

- a run in flight on another worker polls as `processing` while its owner PID is alive
- if the owning worker died, it surfaces as `interrupted` — never silently resumed
- completed runs are readable from any worker, and re-checkouts of the same cart are
  idempotent (same receipt) regardless of which worker takes the retry

Put a real reverse proxy in front for TLS:

```
# Caddy (automatic HTTPS)
txn.example.com {
    reverse_proxy 127.0.0.1:8080
}
```

## 3. Payments (Stripe test mode first)

1. Create/get a Stripe account; take the **test-mode** secret key
   (`sk_test_...`) from <https://dashboard.stripe.com/apikeys>.
2. `export STRIPE_SECRET_KEY=sk_test_...`
3. List each user's saved payment method in `TXN_TENANTS` under
   `stripe_payment_methods` (test mode: `pm_card_visa` works out of the box).
4. Run a charge end-to-end: `python examples/stripe_charge.py` — it purchases from
   the demo catalog, then re-reads the PaymentIntent from Stripe and asserts the
   amount/status before printing a summary.

What the agent guarantees: money moves only **after** the deterministic gate passes
and (when required) approval lands; the charge is for the revalidated cart total, to
the cent; if the merchant order fails afterwards, the charge is voided automatically
(audit event `void_failed` is written if even that fails).

## 4. Approvals in operations

- `GET /ops` — self-contained console: pending approvals with full payloads,
  run statuses, audit tail. Authenticate with the tenant key (header, never URL).
- `POST /v1/approvals/{id}` — what the buttons call.
- For machine approvals point `WebhookApprover` at your ops service; it HMAC-signs
  requests with `APPROVAL_WEBHOOK_SECRET`.

## 5. State, backups, audit

- SQLite WAL: back up with `sqlite3 file.db ".backup '/backup/x.db'"` (online, safe)
  or ship the WAL to cold storage on your existing schedule.
- The audit log is append-only and mirrors into the store via `StoreAuditLog`;
  ship rows to your log pipeline (it's plain JSON) for retention compliance.
- Upgrade path: swap `SQLiteStore` for a Postgres-backed class with the same method
  surface — nothing else in the service changes.

## 6. systemd unit

```ini
# /etc/systemd/system/txn-agent.service
[Unit]
Description=txn-agent purchasing API
After=network-online.target

[Service]
User=txn-agent
EnvironmentFile=/etc/txn-agent.env        # TXN_TENANTS, STRIPE_SECRET_KEY, ...
ExecStart=/opt/txn-agent/.venv/bin/uvicorn txn_agent.service:app \
    --host 0.0.0.0 --port 8080 --workers 4
Restart=always
RestartSec=2
NoNewPrivileges=yes
ProtectSystem=strict
ReadWritePaths=/var/lib/txn-agent

[Install]
WantedBy=multi-user.target
```

## 7. Smoke check after every deploy

```bash
python scripts/service_smoke.py   # TXN_SMOKE_URL / TXN_SMOKE_KEY override
```

It walks healthz → search → cart → checkout → purchased → ops console and exits
non-zero on any failure (CI runs it on every push).

## 8. Publishing a release to PyPI

`.github/workflows/publish.yml` builds the sdist/wheel on every `v*` tag and uploads
via PyPI trusted publishing (OIDC) — no API token is stored in the repo.

One-time setup:

1. pypi.org → Account settings → **Publishing** → "Add a new pending publisher":
   PyPI project name `txn-agent`, Owner `cyberd4ch`, Repository `txn-agent`, Workflow
   `publish.yml`, Environment `pypi`. (The name was unclaimed as of 2026-10-05.)
2. Bump `version` in `pyproject.toml`, commit, tag `vX.Y.Z`, and `git push origin vX.Y.Z`.
3. The first run fails at the upload step if the pending publisher wasn't configured
   yet — fix step 1 and hit **Re-run jobs**; the tag doesn't need to change.
4. Verify in a fresh venv: `pip install txn-agent` and `txn-agent --help`.

The tag must match the version in `pyproject.toml` — the wheel filename and PyPI
version come from pyproject, not the tag.
