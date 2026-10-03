# Merchant HTTP contract

`HttpConnector` speaks a small JSON-over-HTTP contract. Any merchant or aggregator API
that can expose (or adapter into) these three endpoints works with txn-agent unchanged.

All examples use base URL `https://parts.example.com/v1`.

## Authentication

Every request carries a bearer token (API key or OAuth token issued per agent/delegate):

```
Authorization: Bearer <merchant_api_token>
```

## 1. Search — `POST /offers/search`

Request:

```json
{
  "query": "dishwasher lower rack wheel kit",
  "quantity": 2,
  "currency": "USD"
}
```

Response `200`:

```json
{
  "offers": [
    {
      "offer_id": "off_8fa2",
      "title": "Dishwasher lower rack wheel kit",
      "unit_price": "24.99",
      "currency": "USD",
      "in_stock": true,
      "quantity": 2,
      "shipping": "4.99",
      "lead_time_days": 2,
      "expires_at": "2026-10-03T15:30:00Z",
      "return_policy": {
        "returnable": true,
        "window_days": 30,
        "refund_type": "full",
        "restocking_fee_pct": "0"
      }
    }
  ]
}
```

Money and percentages are decimal **strings** — never floats.

## 2. Revalidate — `GET /offers/{offer_id}?quantity=N`

Returns the same offer shape with the *live* price/stock/policy. Called immediately
before checkout; the gate re-evaluates on this data. `404` is treated as
`in_stock: false`.

## 3. Checkout — `POST /orders`

Request:

```json
{
  "idempotency_key": "9f1c... (32 hex chars)",
  "payment_token": "tok_...",
  "lines": [
    {"offer_id": "off_8fa2", "quantity": 2}
  ]
}
```

The merchant **MUST** dedupe on `idempotency_key`: a repeated request with the same key
returns the original order without creating a second one.

Response `201` (or `200` on idempotent replay):

```json
{
  "order_id": "ord_5501",
  "status": "confirmed",
  "total": "54.97",
  "items": [["off_8fa2", 2]]
}
```

Errors: `402`/`422` with `{"error": "..."}` surface as `CheckoutError`.
