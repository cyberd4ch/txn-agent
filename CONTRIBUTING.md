# Contributing

Thanks for helping make purchasing agents safe to deploy.

## Ground rules

1. **The gate is sacred.** `policy.py` must stay deterministic, dependency-free, and
   fail closed. Any change that lets a purchase happen without an explicit policy
   decision will be rejected.
2. **Invariants are contracts.** The six invariants in the README must hold and stay
   under test. New features must add tests that exercise them.
3. **Stdlib-only core.** `txn_agent/` must not gain runtime dependencies. Dev tooling
   (pytest, ruff, mypy) lives in optional extras.
4. **No money-talk in the model layer.** `tools.py` may never expose anything that can
   mutate `PolicyConfig`, budgets, or payment tokens.

## Workflow

```bash
pip install -e ".[dev]"
pytest            # all tests must pass
ruff check .      # lint
ruff format .     # format
mypy txn_agent    # types
```

Before opening a PR: add/adjust tests, update the README/docs if behavior changed,
and add a line to CHANGELOG.md under "Unreleased".

## Adding a connector

Implement `Connector` (search / revalidate / checkout) for the merchant API and prove
idempotency: calling `checkout` twice with the same key must return the same receipt.
Include contract tests using the mocks as references.

## Adding an approval channel

Subclass `Approver`. Fail closed: any error, timeout, or ambiguous response must return
`False`, never raise into the transaction path.
