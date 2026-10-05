import json
import threading
from decimal import Decimal as D
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from txn_agent import TransactionalAgent
from txn_agent.approval import AutoApprover
from txn_agent.connectors import default_connectors
from txn_agent.llm import run_llm_request
from txn_agent.llm_openai import OpenAICompatClient
from txn_agent.payments import DemoVault

REQUESTS: list[dict] = []
SCRIPT: list = []  # message dicts, or callables(req_body) -> message dict


class FakeOpenAI(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def do_POST(self):
        assert self.path.endswith("/chat/completions")
        REQUESTS.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
        step = SCRIPT.pop(0)
        message = step(REQUESTS[-1]) if callable(step) else step
        body = json.dumps({"choices": [{"message": message, "finish_reason": "stop"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture()
def endpoint():
    srv = HTTPServer(("127.0.0.1", 0), FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{srv.server_address[1]}/v1"
    srv.shutdown()
    REQUESTS.clear()
    SCRIPT.clear()


def make_agent() -> TransactionalAgent:
    return TransactionalAgent(default_connectors(), DemoVault(),
                              approver=AutoApprover(), user_id="shop")


def _tool_call(call_id: str, name: str, args: dict) -> dict:
    return {"content": None, "tool_calls": [
        {"id": call_id, "type": "function",
         "function": {"name": name, "arguments": json.dumps(args)}}]}


def _purchase_step(req: dict) -> dict:
    """Read the real intent_id out of the search tool-result on the wire."""
    tool_msg = next(m for m in req["messages"] if m["role"] == "tool")
    intent_id = json.loads(tool_msg["content"])["intent_id"]
    return _tool_call("call_2", "purchase_offer",
                      {"intent_id": intent_id, "offer_id": "parts-6"})


def test_local_model_drives_full_purchase(endpoint):
    SCRIPT.extend([
        _tool_call("call_1", "search_offers",
                   {"vertical": "parts", "query": "drain pump", "max_total": 70}),
        _purchase_step,
        {"content": "Purchased the drain pump for $64.74. Receipt order parts-0001."},
    ])
    client = OpenAICompatClient(endpoint, api_key="local")
    result = run_llm_request(make_agent(), "buy a drain pump under $70",
                             budget_ceiling=D("80"),
                             model="qwen2.5:7b-instruct", client=client)

    assert result.stopped_reason == "end_turn"
    assert result.reply.startswith("Purchased the drain pump")
    statuses = [tc["result"].get("status") for tc in result.tool_calls]
    assert statuses == [None, "purchased"]  # search returns offers, purchase returns status

    # wire format: Anthropic tools arrived as OpenAI function tools
    first = REQUESTS[0]
    assert first["messages"][0]["role"] == "system"
    fn = first["tools"][0]["function"]
    assert fn["name"] == "search_offers" and "parameters" in fn
    # the tool result came back as role=tool with the matching call id
    tool_msgs = [m for m in REQUESTS[1]["messages"] if m["role"] == "tool"]
    assert tool_msgs[0]["tool_call_id"] == "call_1"
    assert "parts-6" in tool_msgs[0]["content"]


def test_malformed_arguments_fail_soft(endpoint):
    """Weak local models send broken args; the router must report, not crash."""
    SCRIPT.extend([
        _tool_call("call_1", "search_offers", {"vertical": "parts"}),  # missing fields
        {"content": "The search failed: I omitted the query and budget. Retrying would "
                    "require them."},
    ])
    client = OpenAICompatClient(endpoint)
    result = run_llm_request(make_agent(), "buy something", budget_ceiling=D("50"),
                             model="tiny-local-model", client=client)

    assert result.stopped_reason == "end_turn"
    assert "invalid arguments for search_offers" in result.tool_calls[0]["result"]["error"]
    # the error reached the model as a tool message it could read
    tool_msgs = [m for m in REQUESTS[1]["messages"] if m["role"] == "tool"]
    assert "invalid arguments" in tool_msgs[0]["content"]


def test_dead_endpoint_raises_runtime_error():
    client = OpenAICompatClient("http://127.0.0.1:9/v1", timeout_s=2)
    with pytest.raises(RuntimeError, match="unreachable"):
        client.messages.create(model="m", max_tokens=8, system="s",
                               messages=[{"role": "user", "content": "hi"}], tools=[])
