"""Claude tool-use loop. Wires TOOL_SCHEMAS into a real model run with ToolRouter
as the executor.

Trust boundaries preserved inside the loop:
- The model sees offers and proposes tool calls; it never sees payment data.
- ToolRouter caps every budget the model supplies to the host-set ceiling.
- Purchases still pass the deterministic policy gate and human approvals.
- Every tool call the model makes is written to the audit log.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from decimal import Decimal as D
from typing import Any

from .agent import TransactionalAgent
from .tools import TOOL_SCHEMAS, ToolRouter

DEFAULT_MODEL = "claude-sonnet-5-5"  # any tool-use-capable Claude model

SYSTEM_PROMPT = """You are a purchasing assistant for parts, flights, and groceries.
Rules you must never break:
- Search before buying; only purchase offer_ids that a search_offers call returned.
- The host application caps every budget you pass. Never claim a budget the user did not give.
- Payments are handled entirely by the system. Never invent, request, or repeat payment details.
- After any purchase tool call, report the status and reasons exactly as returned.
- Multi-item orders: build a cart with build_cart (one merchant), then checkout_cart.
"""


@dataclass
class LLMResult:
    reply: str
    stopped_reason: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


def run_llm_request(agent: TransactionalAgent, request: str, *, budget_ceiling: D,
                    model: str = DEFAULT_MODEL, max_turns: int = 8,
                    api_key: str | None = None, client: Any = None,
                    router: ToolRouter | None = None) -> LLMResult:
    """Run one user request through a Claude tool-use loop.

    `client` is injectable for tests; production callers omit it and the official
    `anthropic` SDK client is constructed (uses ANTHROPIC_API_KEY by default).
    """
    if client is None:
        try:
            import anthropic  # type: ignore[import-not-found]
        except ImportError as e:  # pragma: no cover
            raise RuntimeError("install the llm extra: pip install 'txn-agent[llm]'") from e
        client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()

    router = router or ToolRouter(agent, budget_ceiling=budget_ceiling)
    messages: list[dict[str, Any]] = [{"role": "user", "content": request}]
    tool_calls: list[dict[str, Any]] = []
    reply, stop = "", "end_turn"

    for _ in range(max_turns):
        resp = client.messages.create(model=model, max_tokens=1024, system=SYSTEM_PROMPT,
                                      messages=messages, tools=TOOL_SCHEMAS)
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        if resp.stop_reason != "tool_use":
            stop = resp.stop_reason
            break
        messages.append({"role": resp.role, "content": resp.content})
        results: list[dict[str, Any]] = []
        for block in resp.content:
            if getattr(block, "type", "") != "tool_use":
                continue
            out = router.call(block.name, dict(block.input))
            tool_calls.append({"tool": block.name, "args": dict(block.input), "result": out})
            agent.audit.record("llm", "tool_call", tool=block.name, result=out)
            results.append({"type": "tool_result", "tool_use_id": block.id,
                            "content": json.dumps(out, default=str)})
        messages.append({"role": "user", "content": results})
    else:
        stop = "max_turns"  # model kept requesting tools past the turn budget

    return LLMResult(reply=reply, stopped_reason=stop, tool_calls=tool_calls)
