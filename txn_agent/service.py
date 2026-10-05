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

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import HTMLResponse
from pydantic import BaseModel

from .agent import Outcome, TransactionalAgent
from .approval import Approver, approval_payload
from .audit import AuditLog
from .connectors import default_connectors
from .models import Cart, to_dict
from .payments import DemoVault, StripeVault
from .policy import GateResult
from .storage import SQLiteStore, StoreAuditLog
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

    def __init__(self, timeout_s: float, store: SQLiteStore | None = None,
                 tenant: str = "default"):
        self.timeout_s = timeout_s
        self.store = store
        self.tenant = tenant
        self._lock = threading.Lock()
        self.pending: dict[str, dict[str, Any]] = {}   # approval_id -> payload for humans
        self._events: dict[str, threading.Event] = {}
        self._results: dict[str, bool] = {}

    def request(self, cart: Cart, gate: GateResult) -> bool:
        approval_id = uuid.uuid4().hex
        payload = approval_payload(cart, gate)
        with self._lock:
            self.pending[approval_id] = payload
            event = threading.Event()
            self._events[approval_id] = event
        if self.store is not None:
            self.store.record_approval(self.tenant, approval_id, payload)
        approved = self._wait_decision(approval_id, event)
        with self._lock:
            self.pending.pop(approval_id, None)
            self._events.pop(approval_id, None)
            self._results.pop(approval_id, None)
        return approved

    def _wait_decision(self, approval_id: str, event: threading.Event) -> bool:
        """Wait in-process first; with a store, also watch the DB so an operator can
        resolve from a different worker process."""
        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False  # timeout -> deny (fail closed)
            if event.wait(timeout=min(0.25, remaining)):
                return bool(self._results.get(approval_id, False))
            if self.store is not None:
                row = self.store.load_approval(self.tenant, approval_id)
                if row is not None and row["decision"] is not None:
                    return bool(row["decision"] == "approved")

    def resolve(self, approval_id: str, approved: bool) -> bool:
        """Resolve locally if the waiter lives here; with a store, also record the
        decision so other workers' waiters observe it."""
        with self._lock:
            event = self._events.get(approval_id)
            if event is not None:
                self._results[approval_id] = approved
                event.set()
        if self.store is not None:
            return self.store.resolve_approval(self.tenant, approval_id, approved)
        return event is not None

    def snapshot(self) -> list[tuple[str, dict[str, Any]]]:
        """All pending approvals: this worker's, plus unresolved rows in the store."""
        with self._lock:
            items = list(self.pending.items())
        if self.store is not None:
            seen = {aid for aid, _ in items}
            for aid, payload in self.store.pending_approvals(self.tenant):
                if aid not in seen:
                    items.append((aid, payload))
        return items


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
    store: SQLiteStore | None = None
    router: ToolRouter | None = None
    approver: ServiceApprover | None = None
    runs: dict[str, RunState] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.approver is None:
            self.approver = ServiceApprover(self.approval_timeout_s,
                                            store=self.store, tenant=self.name)
        if self.router is None:
            self.router = ToolRouter(self.agent, budget_ceiling=self.budget_ceiling,
                                     store=self.store, tenant=self.name)


def _default_tenant(store: SQLiteStore | None = None) -> Tenant:
    approver = ServiceApprover(timeout_s=300.0, store=store, tenant="demo")
    audit = StoreAuditLog(store, "demo") if store else AuditLog(None)
    agent = TransactionalAgent(default_connectors(), DemoVault(), approver=approver,
                               audit=audit, confirm=lambda offer, gate: False)
    return Tenant(name="demo", agent=agent, budget_ceiling=D("500"),
                  approver=approver, store=store)


class TenantRegistry:
    """API key -> tenant. Swap the storage for a real identity provider in production."""

    def __init__(self, keys: dict[str, Tenant]):
        self.keys = keys

    def resolve(self, api_key: str) -> Tenant:
        tenant = self.keys.get(api_key)
        if tenant is None:
            raise HTTPException(status_code=401, detail="invalid API key")
        return tenant


DEFAULT_REGISTRY = TenantRegistry({"demo-key": _default_tenant()})  # noqa: E501


def _outcome_dict(outcome: Outcome) -> dict[str, Any]:
    return {"status": outcome.status.value, "reasons": list(outcome.reasons),
            "receipt": to_dict(outcome.receipt) if outcome.receipt else None,
            "cart": to_dict(outcome.cart) if outcome.cart else None}


def _pid_alive(pid: int) -> bool:
    """POSIX liveness probe (same-host workers). EPERM means alive-but-foreign."""
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def tenants_from_env(env: str = "TXN_TENANTS") -> TenantRegistry | None:
    """Build the tenant registry from configuration, e.g.

        TXN_TENANTS='{"acme": {"api_key": "ak_live_...", "budget_ceiling": "500",
                     "store_path": "/var/lib/txn-agent/acme.db",
                     "stripe_payment_methods": {"acme-user": "pm_card_visa"}}}'

    Vault selection: if "stripe_payment_methods" is present, STRIPE_SECRET_KEY must be
    set and a StripeVault is used; otherwise DemoVault. Missing/invalid config -> None
    (callers fall back to the demo registry)."""
    raw = os.environ.get(env)
    if not raw:
        return None
    try:
        cfg: dict[str, dict[str, Any]] = json.loads(raw)
    except json.JSONDecodeError:
        return None
    keys: dict[str, Tenant] = {}
    for name, t in cfg.items():
        if "api_key" not in t or "budget_ceiling" not in t:
            return None
        store = SQLiteStore(t["store_path"]) if t.get("store_path") else None
        audit = StoreAuditLog(store, name) if store else AuditLog(None)
        vault: DemoVault | StripeVault
        if t.get("stripe_payment_methods"):
            secret = os.environ.get("STRIPE_SECRET_KEY", "")
            if not secret:
                return None
            vault = StripeVault(secret, t["stripe_payment_methods"])
        else:
            vault = DemoVault()
        agent = TransactionalAgent(default_connectors(), vault, audit=audit)
        keys[t["api_key"]] = Tenant(
            name=name, agent=agent, budget_ceiling=D(str(t["budget_ceiling"])),
            approval_timeout_s=float(t.get("approval_timeout_s", 300)), store=store)
    return TenantRegistry(keys)


def create_app(registry: TenantRegistry | None = None,
               approval_timeout_s: float | None = None,
               store: SQLiteStore | None = None) -> FastAPI:
    app = FastAPI(title="txn-agent", version="0.4.0",
                  description="Safety-first transaction layer for purchasing agents")
    reg = registry or TenantRegistry({"demo-key": _default_tenant(store=store)})

    def tenant_for(x_api_key: str = Header(alias="X-API-Key")) -> Tenant:
        tenant = reg.resolve(x_api_key)
        if approval_timeout_s is not None and tenant.approver is not None:
            tenant.approver.timeout_s = approval_timeout_s
        return tenant

    def _start_run(tenant: Tenant, run_id: str, cart: Cart) -> None:
        state = RunState()
        tenant.runs[run_id] = state
        if tenant.store is not None:
            tenant.store.save_run(tenant.name, run_id, "submitted")

        def work() -> None:
            outcome = tenant.agent.checkout_cart(cart)
            state.result = _outcome_dict(outcome)
            if tenant.store is not None:
                tenant.store.save_run(tenant.name, run_id, "final", state.result)
            state.done.set()

        if tenant.store is not None:
            tenant.store.save_run(tenant.name, run_id, "submitted", owner_pid=os.getpid())

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
            saved = tenant.store.load_run(tenant.name, run_id) if tenant.store else None
            if saved is None:
                raise HTTPException(status_code=404, detail="unknown run_id")
            if saved["status"] == "final":
                return {"status": "final", "outcome": saved["outcome"]}
            # another worker owns this run: alive pid -> still processing, dead -> interrupted
            pid = saved.get("owner_pid")
            if pid and _pid_alive(int(pid)):
                return {"status": "processing"}
            return {"status": "interrupted"}  # owning worker is gone, fail closed
        if state.result is not None:
            return {"status": "final", "outcome": state.result}
        approver = tenant.approver
        assert approver is not None
        pendings = approver.snapshot()
        if pendings:
            approval_id, payload = pendings[0]
            return {"status": "pending", "approval_id": approval_id, "approval": payload}
        return {"status": "processing"}

    @app.post("/v1/approvals/{approval_id}")
    def decide(approval_id: str, body: ApprovalDecision,
               tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.approver is not None
        if not tenant.approver.resolve(approval_id, body.approved):
            raise HTTPException(status_code=404, detail="unknown or already-resolved approval")
        return {"approval_id": approval_id, "approved": body.approved}

    # ------------------------------------------------------------------ ops UI

    @app.get("/v1/ops/state")
    def ops_state(tenant: Tenant = Depends(tenant_for)) -> dict[str, Any]:
        assert tenant.approver is not None
        approvals = [{"approval_id": aid, "payload": payload}
                     for aid, payload in tenant.approver.snapshot()]
        runs = {run_id: (s.result["status"] if s.result else "in progress")
                for run_id, s in list(tenant.runs.items())[-25:]}
        return {"approvals": approvals, "runs": runs,
                "audit": tenant.agent.audit.events[-50:]}

    @app.get("/ops", response_class=HTMLResponse)
    def ops_console() -> str:
        return OPS_HTML

    return app


OPS_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>txn-agent ops console</title>
<style>
  body { font-family: ui-monospace, Menlo, monospace; background: #111; color: #ddd;
         margin: 2rem; }
  h1 { font-size: 1.1rem; } h2 { font-size: 0.9rem; color: #9ad; margin-top: 2rem; }
  .card { background: #1b1b1b; border: 1px solid #333; border-radius: 8px;
          padding: 0.8rem 1rem; margin: 0.5rem 0; max-width: 46rem; }
  .reason { color: #fa6; } .ok { color: #7c7; } .bad { color: #d66; }
  button { background: #2a4; color: #06130a; border: 0; border-radius: 4px;
           padding: 0.3rem 0.9rem; font-weight: 700; cursor: pointer; margin-right: 6px; }
  button.deny { background: #a33; color: #fff; }
  input { background: #222; color: #ddd; border: 1px solid #444; padding: 0.3rem; }
  pre { white-space: pre-wrap; margin: 0.3rem 0; }
  .muted { color: #777; }
</style>
</head>
<body>
<h1>txn-agent ops console</h1>
<p class="muted">Approvals park here when a purchase exceeds policy caps.
The checkout run resumes the moment you decide; silence = denial.</p>
<div class="card">API key: <input id="key" type="password" size="32">
<button onclick="saveKey()">save</button> <span id="conn" class="muted"></span></div>
<h2>pending approvals</h2><div id="approvals" class="muted">(none)</div>
<h2>runs</h2><div id="runs" class="muted">(none)</div>
<h2>audit tail</h2><div id="audit" class="muted">(empty)</div>
<script>
const keyInput = document.getElementById('key');
keyInput.value = localStorage.getItem('txn_ops_key') || '';
function saveKey() { localStorage.setItem('txn_ops_key', keyInput.value); refresh(); }
function hdrs() { return {'X-API-Key': localStorage.getItem('txn_ops_key') || '',
                          'Content-Type': 'application/json'}; }
async function decide(id, approved) {
  await fetch('/v1/approvals/' + id, {method: 'POST', headers: hdrs(),
                                      body: JSON.stringify({approved})});
  refresh();
}
function esc(s) { const d = document.createElement('div'); d.textContent = String(s); return d.innerHTML; }
async function refresh() {
  try {
    const r = await fetch('/v1/ops/state', {headers: hdrs()});
    if (r.status === 401) { document.getElementById('conn').textContent = 'bad key'; return; }
    document.getElementById('conn').textContent = 'connected';
    const s = await r.json();
    document.getElementById('approvals').innerHTML = s.approvals.length ?
      s.approvals.map(a => `<div class="card"><pre>` +
        esc(JSON.stringify(a.payload, null, 2)) + `</pre>` +
        `<button onclick="decide('${a.approval_id}', true)">APPROVE</button>` +
        `<button class="deny" onclick="decide('${a.approval_id}', false)">DENY</button></div>`).join('')
      : '(none)';
    document.getElementById('runs').innerHTML = Object.keys(s.runs).length ?
      Object.entries(s.runs).map(([id, st]) =>
        `<div class="card"><span class="${String(st).includes('purchased') ? 'ok' : 'muted'}">` +
        esc(id.slice(0, 12)) + ' &rarr; ' + esc(st) + '</span></div>').join('') : '(none)';
    document.getElementById('audit').innerHTML = s.audit.slice(-12).map(e =>
      `<div class="card"><span class="muted">${esc(e.ts || '')}</span> ` +
      `<b>${esc(e.event)}</b> ${esc(e.intent_id || '')}` +
      (e.decision ? ` <span class="${e.decision === 'auto_buy' ? 'ok' : 'bad'}">` +
        esc(e.decision) + `</span>` : '') + `</div>`).join('') || '(empty)';
  } catch (e) { document.getElementById('conn').textContent = 'offline'; }
}
refresh(); setInterval(refresh, 2000);
</script>
</body>
</html>"""

app = create_app(tenants_from_env())  # TXN_TENANTS config, else demo tenant
