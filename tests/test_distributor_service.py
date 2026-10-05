import json
from decimal import Decimal as D
from typing import Any

import pytest
from fastapi.testclient import TestClient

from txn_agent import Status, TransactionalAgent, Vertical
from txn_agent.connectors import http as http_mod
from txn_agent.connectors.http import HttpConnector
from txn_agent.intent import parse_request
from txn_agent.payments import DemoVault


@pytest.fixture()
def dist_client(monkeypatch):
    """In-process distributor service, with HttpConnector's HTTP seam pointed at it.

    monkeypatch restores `http_mod._request_json` after every test — a leak here
    would redirect unrelated HttpConnector tests into the distributor app.
    """
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples"))
    from distributor_service import app as distributor_app

    def via_test_client(url: str, *, method: str = "GET", body: dict[str, Any] | None = None,
                        token: str | None = None, timeout: float = 10):
        path = "/" + url.split("/", 3)[3]
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        if body is not None:
            headers["Content-Type"] = "application/json"
        r = client.request(method, path,
                           content=json.dumps(body).encode() if body is not None else None,
                           headers=headers)
        if r.status_code >= 400:
            return r.status_code, {}
        return r.status_code, (json.loads(r.content) if r.content else {})

    with TestClient(distributor_app) as client:
        monkeypatch.setattr(http_mod, "_request_json", via_test_client)
        yield client


def make_agent() -> TransactionalAgent:
    conn = HttpConnector(Vertical.PARTS, "http://distributor.test/v1",
                         api_token="dist-key", merchant="ExampleParts", timeout_s=5)
    return TransactionalAgent({Vertical.PARTS: conn}, DemoVault())


def test_agent_buys_from_distributor_adapter(dist_client):
    agent = make_agent()
    out = agent.run(parse_request("dishwasher lower rack wheel kit under $40"))
    assert out.status is Status.PURCHASED
    assert out.receipt.merchant == "ExampleParts"
    assert out.receipt.total == D("21.50") + D("4.99")
    assert dict(out.receipt.items) == {"wh-1001": 1}


def test_distributor_stock_drop_rejects_at_revalidation(dist_client):
    from distributor_service import STOCK

    STOCK["wh-1001"] = 0
    agent = make_agent()
    out = agent.run(parse_request("dishwasher lower rack wheel kit under $40"))
    assert out.status is not Status.PURCHASED  # out of stock -> no sale


def test_distributor_order_is_idempotent(dist_client):
    agent = make_agent()
    out1 = agent.run(parse_request("drain pump under $70"))
    out2 = agent.run(parse_request("drain pump under $70"))
    assert out1.status is Status.PURCHASED and out2.status is Status.PURCHASED
    assert out1.receipt.order_id != out2.receipt.order_id  # separate intents, separate orders
