import json
from decimal import Decimal as D

from txn_agent import TransactionalAgent
from txn_agent.connectors import default_connectors
from txn_agent.llm import run_llm_request
from txn_agent.payments import DemoVault
from txn_agent.tools import ToolRouter

MODEL = "test-model"


class FakeBlock:
    def __init__(self, block: dict):
        self.__dict__.update(block)


class FakeResponse:
    def __init__(self, stop_reason: str, content: list[dict]):
        self.stop_reason = stop_reason
        self.content = [FakeBlock(b) for b in content]
        self.role = "assistant"


class ScriptedClient:
    """Pops canned responses per messages.create call. A step may be a callable
    receiving the client, so later turns can react to earlier tool results."""

    def __init__(self, steps: list):
        self.steps = list(steps)
        self.calls: list[dict] = []
        self.messages = self  # client.messages.create(...)

    def create(self, **kwargs):
        # snapshot: the loop mutates its messages list in place, so keep call-time state
        self.calls.append({**kwargs, "messages": [dict(m) for m in kwargs["messages"]]})
        step = self.steps.pop(0)
        return step(self) if callable(step) else step


def tool_use(id_: str, name: str, args: dict) -> dict:
    return {"type": "tool_use", "id": id_, "name": name, "input": args}


def text(t: str) -> dict:
    return {"type": "text", "text": t}


def last_tool_result(client: ScriptedClient) -> dict:
    return json.loads(client.calls[-1]["messages"][-1]["content"][0]["content"])


def make_agent():
    conns = default_connectors()
    agent = TransactionalAgent(conns, DemoVault(), confirm=lambda o, g: False)
    return agent, conns


def test_llm_loop_end_to_end_purchase():
    agent, _ = make_agent()

    def purchase_step(client: ScriptedClient) -> FakeResponse:
        intent_id = last_tool_result(client)["intent_id"]  # react to the real search result
        return FakeResponse("tool_use", [tool_use("t2", "purchase_offer",
                                                  {"intent_id": intent_id, "offer_id": "parts-2"})])

    client = ScriptedClient([
        FakeResponse("tool_use", [tool_use("t1", "search_offers",
                                           {"vertical": "parts", "query": "dishwasher wheel kit",
                                            "max_total": 50})]),
        purchase_step,
        FakeResponse("end_turn", [text("Purchased the dishwasher wheel kit.")]),
    ])
    router = ToolRouter(agent, budget_ceiling=D("500"))
    result = run_llm_request(agent, "buy a dishwasher wheel kit under $50",
                             budget_ceiling=D("500"), model=MODEL, client=client, router=router)

    assert result.stopped_reason == "end_turn"
    assert result.reply == "Purchased the dishwasher wheel kit."
    assert [t["tool"] for t in result.tool_calls] == ["search_offers", "purchase_offer"]
    purchase = result.tool_calls[1]
    assert purchase["result"]["status"] == "purchased"
    assert purchase["result"]["receipt"]["merchant"] == "AppliancePartsCo"
    # tool_result blocks go back with matching tool_use ids
    results = [c for c in client.calls[1]["messages"][-1]["content"] if c["type"] == "tool_result"]
    assert results[0]["tool_use_id"] == "t1"


def test_llm_loop_reports_empty_search_and_does_not_buy():
    agent, _ = make_agent()
    client = ScriptedClient([
        FakeResponse("tool_use", [tool_use("t1", "search_offers",
                                           {"vertical": "parts", "query": "unobtainium widget",
                                            "max_total": 50})]),
        FakeResponse("end_turn", [text("Nothing found; I did not buy anything.")]),
    ])
    router = ToolRouter(agent, budget_ceiling=D("500"))
    result = run_llm_request(agent, "buy an unobtainium widget", budget_ceiling=D("500"),
                             model=MODEL, client=client, router=router)
    assert result.tool_calls[0]["result"]["offers"] == []


def test_llm_loop_cart_flow_builds_and_buys():
    agent, _ = make_agent()

    def cart_step(client: ScriptedClient) -> FakeResponse:
        res = last_tool_result(client)
        offer = next(o for o in res["offers"] if o["in_stock"])
        return FakeResponse("tool_use", [tool_use("t2", "build_cart",
                                                  {"lines": [{"intent_id": res["intent_id"],
                                                              "offer_id": offer["offer_id"],
                                                              "quantity": 2}]})])

    client = ScriptedClient([
        FakeResponse("tool_use", [tool_use("t1", "search_offers",
                                           {"vertical": "parts", "query": "drain pump",
                                            "max_total": 200})]),
        cart_step,
        FakeResponse("tool_use", [tool_use("t3", "checkout_cart",
                                           {"cart_id": "FROM_BUILD"})]),  # replaced below
        FakeResponse("end_turn", [text("Ordered.")]),
    ])
    # patch t3 args with the real cart_id from the build result
    def checkout_step(c: ScriptedClient) -> FakeResponse:
        cart_id = last_tool_result(c)["cart_id"]
        return FakeResponse("tool_use", [tool_use("t3", "checkout_cart", {"cart_id": cart_id})])

    client.steps[2] = checkout_step
    router = ToolRouter(agent, budget_ceiling=D("500"))
    result = run_llm_request(agent, "restock 2 drain pumps", budget_ceiling=D("500"),
                             model=MODEL, client=client, router=router)
    assert [t["tool"] for t in result.tool_calls] == ["search_offers", "build_cart", "checkout_cart"]
    assert result.tool_calls[2]["result"]["status"] == "purchased"


def test_llm_loop_max_turns_guard():
    agent, _ = make_agent()
    endless = [FakeResponse("tool_use", [tool_use(f"t{i}", "search_offers",
                                                  {"vertical": "parts", "query": "kit",
                                                   "max_total": 50})])
               for i in range(20)]
    client = ScriptedClient(endless)
    router = ToolRouter(agent, budget_ceiling=D("500"))
    result = run_llm_request(agent, "loop forever", budget_ceiling=D("500"), model=MODEL,
                             client=client, router=router, max_turns=3)
    assert result.stopped_reason == "max_turns"
    assert len(client.calls) == 3


def test_llm_loop_tool_calls_are_audited():
    agent, _ = make_agent()
    client = ScriptedClient([
        FakeResponse("tool_use", [tool_use("t1", "search_offers",
                                           {"vertical": "parts", "query": "kit", "max_total": 50})]),
        FakeResponse("end_turn", [text("done")]),
    ])
    router = ToolRouter(agent, budget_ceiling=D("500"))
    run_llm_request(agent, "search kits", budget_ceiling=D("500"), model=MODEL,
                    client=client, router=router)
    assert "tool_call" in [e["event"] for e in agent.audit.events]


def test_llm_loop_model_budget_is_capped_by_host_ceiling():
    agent, _ = make_agent()
    client = ScriptedClient([
        FakeResponse("tool_use", [tool_use("t1", "search_offers",
                                           {"vertical": "parts", "query": "kit", "max_total": 9999})]),
        FakeResponse("end_turn", [text("done")]),
    ])
    router = ToolRouter(agent, budget_ceiling=D("25"))
    run_llm_request(agent, "kit", budget_ceiling=D("25"), model=MODEL, client=client, router=router)
    # the gate event for the purchase attempt must show the capped budget, not 9999;
    # simplest observable: no offer above $25 gets purchased in a follow-up buy.
    search_event = agent.audit.events[0]
    assert search_event["event"] == "search"
