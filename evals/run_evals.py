"""Golden-set eval harness for txn-agent. Trace-based, offline, CI-gated.

Every case runs the real deterministic pipeline (ToolRouter -> TransactionalAgent ->
policy gate -> mock connector checkout) and checks:
  1. the final tool-call outcome against the expected status / error text, and
  2. the decision the audit trace recorded for the gate (trace-based scoring).

Run it standalone (report + exit code) or via `pytest tests/test_eval_golden.py`:
  python -m evals.run_evals                  # report; exit 1 on regression
  python -m evals.run_evals --update-baseline

Regression gate: the run fails if the pass rate falls below the recorded baseline
in evals/baseline.json, so no policy, harness, or tool change ships blind.
"""
from __future__ import annotations

import argparse
import json
import sys
from decimal import Decimal as D
from pathlib import Path

from txn_agent.agent import TransactionalAgent
from txn_agent.approval import AutoApprover
from txn_agent.connectors import default_connectors
from txn_agent.models import Vertical
from txn_agent.payments import DemoVault
from txn_agent.policy import PolicyConfig
from txn_agent.tools import ToolRouter

EVALS_DIR = Path(__file__).parent
GOLDEN = EVALS_DIR / "golden_set.json"
BASELINE = EVALS_DIR / "baseline.json"


def _cfg(case: dict) -> tuple[TransactionalAgent, ToolRouter]:
    """Build an isolated agent + router per case from its declared setup."""
    conns = default_connectors()
    override_map = case.get("price_overrides") or {}
    for oid, price in override_map.items():
        conns[Vertical("parts")].price_overrides[oid] = D(str(price))
    for oid, in_stock in (case.get("stock_overrides") or {}).items():
        conns[Vertical("parts")].stock_overrides[oid] = bool(in_stock)

    p = case.get("policy") or {}

    def dec(x):  # JSON has no decimals
        return D(str(x)) if x is not None else None

    policy = PolicyConfig(
        auto_buy_cap={v: dec(p.get("auto_buy_cap", {}).get(v.value))
                      if p.get("auto_buy_cap", {}).get(v.value) is not None else d
                      for v, d in {
                          Vertical.PARTS: dec(150), Vertical.FLIGHTS: dec(0),
                          Vertical.GROCERY: dec(120)}.items()},
        min_return_days={v: p.get("min_return_days", {}).get(v.value, d)
                         for v, d in {Vertical.PARTS: 30, Vertical.FLIGHTS: 0,
                                      Vertical.GROCERY: 0}.items()},
        approved_merchants={v: frozenset(merchants)
                            for v, merchants in (p.get("approved_merchants") or {}).items()},
        max_lead_time_days={v: p.get("max_lead_time_days", {}).get(v.value, d)
                            for v, d in {Vertical.PARTS: 14, Vertical.FLIGHTS: 0,
                                         Vertical.GROCERY: 0}.items()},
    )

    approver = case.get("approver")
    confirm = None
    auto = None
    if approver == "auto":
        auto = AutoApprover()
    elif approver == "allow_confirm":
        confirm = lambda offer, gate: True  # noqa: E731 - explicit test policy

    agent = TransactionalAgent(conns, DemoVault(), policy=policy,
                               approver=auto, confirm=confirm)
    return agent, ToolRouter(agent, budget_ceiling=D(str(case["budget_ceiling"])))


def _resolve(step_args: dict, outputs: list[dict]) -> dict:
    """Interpolate $intent.N / $cart.N / $offer.N.first_in_stock / $offer.N.<idx>
    anywhere in the step args (top level or nested in lists/dicts)."""
    out: dict = json.loads(json.dumps(step_args))  # deep copy

    def resolve(node: object) -> object:
        if isinstance(node, dict):
            return {k: resolve(v) for k, v in node.items()}
        if isinstance(node, list):
            return [resolve(v) for v in node]
        if isinstance(node, str) and node.startswith("$"):
            parts = node[1:].split(".")
            kind, idx = parts[0], int(parts[1])
            step = outputs[idx]
            if kind == "intent":
                return step["intent_id"]
            if kind == "cart":
                return step["cart_id"]
            if kind == "offer":
                selector = parts[2]
                offers = step["offers"]
                if selector == "first_in_stock":
                    return next(o["offer_id"] for o in offers if o["in_stock"])
                return offers[int(selector)]["offer_id"]
        return node

    return {k: resolve(v) for k, v in out.items()}  # type: ignore[arg-type]


def _last_gate_decision(events: list[dict]) -> str | None:
    for e in reversed(events):
        if e.get("event") == "gate":
            d = e["decision"]
            return getattr(d, "value", str(d))  # enum or str, normalized
    return None


def run_case(case: dict) -> tuple[bool, dict]:
    """Execute one golden case. Returns (passed, details)."""
    agent, router = _cfg(case)
    outputs: list[dict] = []
    detail: dict = {"id": case["id"], "desc": case["desc"], "error": None}
    try:
        for i, step in enumerate(case["steps"]):
            result = router.call(step["tool"], _resolve(step["args"], outputs))
            outputs.append(result)
            if "error" in result and not case["expect"].get("tool_error"):
                detail["error"] = f"step {i} ({step['tool']}): {result['error']}"
                return False, detail
        final = outputs[case["expect"]["final"]]
        exp = case["expect"]

        if exp.get("tool_error"):
            if "error" not in final:
                detail["error"] = f"expected a tool error, got {final}"
                return False, detail
            detail["error"] = None if exp["reason_has"][0] in final["error"] else \
                f"error text mismatch: {final['error']!r}"
            return detail["error"] is None, detail

        status_ok = final.get("status") == exp["status"]
        reasons = final.get("reasons") or []
        reasons_ok = all(any(h in r for r in reasons) for h in exp.get("reason_has", []))
        trace = _last_gate_decision(agent.audit.events)
        decision_ok = (exp.get("decision") is None and trace is None) or \
                      (exp.get("decision") is not None and trace == exp["decision"])

        detail.update(status=final.get("status"), trace_decision=trace,
                      reasons=reasons)
        if not status_ok:
            detail["error"] = f"status: expected {exp['status']}, got {final.get('status')}"
        elif not decision_ok:
            detail["error"] = f"trace decision: expected {exp['decision']}, got {trace}"
        elif not reasons_ok:
            detail["error"] = f"reasons: {reasons} missing {exp['reason_has']}"
        return detail["error"] is None, detail
    except Exception as e:  # noqa: BLE001 - a crash is a failed eval, not a stopped suite
        detail["error"] = f"{type(e).__name__}: {e}"
        return False, detail


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--update-baseline", action="store_true")
    args = ap.parse_args(argv)

    golden = json.loads(GOLDEN.read_text())
    results = [run_case(c) for c in golden["cases"]]
    passed = sum(1 for ok, _ in results if ok)
    total = len(results)
    rate = passed / total if total else 0.0

    print(f"\ntxn-agent golden-set evals: {passed}/{total} passed ({rate:.1%})")
    for ok, d in results:
        mark = "PASS" if ok else "FAIL"
        line = f"  [{mark}] {d['id']}"
        if not ok:
            line += f"  —  {d['error']}"
            if d.get("status") or d.get("trace_decision"):
                line += f"  (status={d.get('status')}, trace={d.get('trace_decision')})"
        print(line)

    if args.update_baseline:
        BASELINE.write_text(json.dumps(
            {"passed": passed, "total": total, "pass_rate": rate}, indent=2) + "\n")
        print(f"baseline updated: {BASELINE.name} -> {passed}/{total}")
        return 0 if passed == total else 1

    if BASELINE.exists():
        base = json.loads(BASELINE.read_text())
        if rate < base["pass_rate"]:
            print(f"\nREGRESSION: pass rate {rate:.1%} < baseline {base['pass_rate']:.1%} "
                  f"({base['passed']}/{base['total']})")
            return 1
        print(f"baseline check: no regression (baseline {base['passed']}/{base['total']})")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
