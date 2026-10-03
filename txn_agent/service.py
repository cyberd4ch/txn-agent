"""HTTP service wrapper: exposes the transactional agent over a REST API.

Trust model is unchanged and restated for the network boundary:
- API keys identify tenants; each tenant has its own agent, budget ceiling, and audit log.
- Requests can search, build carts, and REQUEST purchases. They cannot touch payment
  tokens, policy config, or budget ceilings.
- Purchases that need approval run in a background thread and park on an approval that
  must arrive through POST /v1/approvals/{approval_id}. If no decision arrives within
  `approval_timeout_s`, the purchase is denied (fail closed).
- Single-process demo topology: tenant state (intents, carts, runs) is in memory. Put a
  real backing store behind `TenantRegistry` before scaling out.

NOTE: request models live at module level on purpose. With `from __future__ import
annotations`, FastAPI cannot resolve string annotations for classes defined inside
`create_app` (get_type_hints only sees module globals), so keep them top-level.

Run the demo server:
    pip install ".[service]"
    uvicorn txn_agent.service:app --port 8080
"""
from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel

from .agent import Outcome, TransactionalAgent
from .approval import Approver, approval_payload
from .connectors import default_connectors
from .models import Cart, to_dict
from .payments import DemoVault
from .policy import GateResult
from .tools import ToolRouter

# ------------------------------------------------------------------ request models

class SearchRequest(BaseModel):
    vertical: str
    query: str
    max_total: float
    quantity: int = 1


class PurchaseRequest(BaseModel):
    intent_id: str
    offer_id: str


class CartLine(BaseModel):
    intent_id: str
    offer_id: str
    quantity: int = 1


class CartRequest(BaseModel):
    lines: list[CartLine]


class ApprovalDecision(BaseModel):
    approved: bool


# --------------------------------------------------------------------------- tenants

class ServiceApprover(Approver):
    """Approval gate for the service: registers a pending approval and blocks the
    purchase thread until an operator resolves it over the API (or timeout -> deny)."""

    def __init__(self, timeout_s: float):
        self.timeout_s = timeout_s
        self._lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}   # approval_id -> payload for humans
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, bool] = {}

    def request(self, cart: Cart, gate: GateResult) -> bool:
        approval_id = uuid.uuid4().hex
        with self._lock:
            self.pending[approval_id] = approval_payload(cart, gate)
            event = threading.Event()
            self._events[approval_id] = event
        approved = event.wait(timeout=self.timeout_s) and self._results.get(approval_id, False)
        with self._lock:
            self.pending.pop(approval_id, None)
            self._events.pop(approval_id, None)
            self._results.pop(approval_id, None)
        return approved

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """True if the id existed and was resolved."""
        with self._lock:
            event = self._events.get(approval_id)
            if event is None:
                return False
            self._results[approval_id] = approved
            event.set()
            return True


@dataclass
class RunState:
    """Lifecycle of one background purchase attempt."""
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None


@dataclass
class Tenant:
    name: str
    agent: TransactionalAgent
    budget_ceiling: D
    approval_timeout_s: float = 300.0
    router: ToolRouter | None = None
    approver: ServiceApprover | None = None
    runs: dict[str, RunState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approver is None:
            self.approver = ServiceApprover(self.approval_timeout_s)
        if self.router is None:
            self.router = ToolRouter(self.agent, budget_ceiling=self.budget_ceiling)


def _default_tenant() -> Tenant:
    approver = ServiceApprover(timeout_s=300.0)
    agent = TransactionalAgent(default_connectors(), DemoVault(), approver=approver,
                               confirm=lambda offer, gate: False)
    return Tenant(name="demo", agent=agent, budget_ceiling=D("500"), approver=approver)


class TenantRegistry:
    """API key -> tenant. Swap the storage for a real identity provider in production."""

    def __init__(self, keys: dict[str, Tenant]):
        self.keys = keys

    def resolve(self, api_key: str) -> Tenant:
        tenant = self.keys.get(api_key)
        if tenant is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        return tenant


DEFAULT_REGISTRY = TenantRegistry({"demo-key": _default_tenant()})


def _outcome_dict(outcome: Outcome) -> dict[str, Any]:
    return {"status": outcome.status.value, "reasons": list(outcome.reasons),
            "receipt": to_dict(outcome.receipt) if outcome.receipt else None,
            "cart": to_dict(outcome.cart) if outcome.cart else None}


def create_app(registry: TenantRegistry | None = None,
               approval_timeout_s: float | None = None) -> FastAPI:
    app = FastAPI(title="txn-agent", version="0.3.0",
                  description="Safety-first transaction layer for purchasing agents")
    reg = registry or DEFAULT_REGISTRY

    def tenant_for(x_api_key: str = Header(alias="X-API-Key")) -> Tenant:
        tenant = reg.resolve(x_api_key)
        if approval_timeout_s is not None and tenant.approver is not None:
            tenant.approver.timeout_s = approval_timeout_s
        return tenant

    def _start_run(tenant: Tenant, run_id: str, cart: Cart) -> None:
        state = RunState()
        tenant.runs[run_id] = state
        assert tenant.approver is not None

        def work() -> None:
            outcome = tenant.agent.checkout_cart(cart)
            state.result = _outcome_dict(outcome)
            state.done.set()

        threading.Thread(target=work, daemon=True).start()

    # ------------------------------------------------------------------ meta

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/audit")
    def audit(tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        return {"events": tenant.agent.audit.events[-200:]}

    # ------------------------------------------------------------------ shopping

    @app.post("/v1/search")
    def search(body: SearchRequest, tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.router is not None
        return tenant.router.call("search_offers", body.model_dump())

    @app.post("/v1/purchase")
    def purchase(body: PurchaseRequest,
                 tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.router is not None
        built = tenant.router.call("build_cart",
                                   {"lines": [body.model_dump() | {"quantity": 1}]})
        if "error" in built:
            raise HTTPException(status_code=404, detail=built["error"])
        cart = tenant.router._carts[built["cart_id"]]
        run_id = built["cart_id"]
        _start_run(tenant, run_id, cart)
        return {"run_id": run_id, "status": "submitted"}

    @app.post("/v1/carts")
    def build_cart(body: CartRequest, tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.router is not None
        out = tenant.router.call("build_cart", {"lines": [line.model_dump() for line in body.lines]})
        if "error" in out:
            raise HTTPException(status_code=422, detail=out["error"])
        return out

    @app.post("/v1/carts/{cart_id}/checkout")
    def checkout(cart_id: str, tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.router is not None
        cart = tenant.router._carts.get(cart_id)
        if cart is None:
            raise HTTPException(status_code=404, detail="unknown cart_id")
        _start_run(tenant, cart_id, cart)
        return {"run_id": cart_id, "status": "submitted"}

    @app.get("/v1/purchases/{run_id}")
    def poll(run_id: str, tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        state = tenant.runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="unknown run_id")
        if state.result is not None:
            return {"status": "final", "outcome": state.result}
        approver = tenant.approver
        assert approver is not None
        with approver._lock:
            pendings = dict(approver.pending)
        if pendings:
            approval_id, payload = next(iter(pendings.items()))
            return {"status": "pending", "approval_id": approval_id, "approval": payload}
        return {"status": "processing"}

    @app.post("/v1/approvals/{approval_id}")
    def decide(approval_id: str, body: ApprovalDecision,
               tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.approver is not None
        if not tenant.approver.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="unknown or already-resolved approval")
        return {"approval_id": approval_id, "approved": body.approved}

    return app


app = create_app()  # for `uvicorn txn_agent.service:app`
