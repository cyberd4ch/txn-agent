"""Run the same purchasing loop on any OpenAI-compatible endpoint — local models included.

`OpenAICompatClient` speaks the OpenAI chat-completions wire format (stdlib urllib,
no SDK) and presents the Anthropic-Messages shape that `run_llm_request` expects.
The safety story is unchanged and model-independent: the model proposes tool calls,
`ToolRouter` executes them, the deterministic gate disposes.

Works with Ollama, llama.cpp server, vLLM, LM Studio, OpenRouter, or any gateway
that implements POST /v1/chat/completions with function tools:

    ollama serve
    python -m txn_agent --llm "restock 2 door gaskets, under $150" \
        --llm-endpoint http://127.0.0.1:11434/v1 --model qwen2.5:7b-instruct
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any

DEFAULT_OPENAI_TIMEOUT_S = 120.0


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict[str, Any]
    type: str = "tool_use"


@dataclass
class LLMResponse:
    """Anthropic-Messages-shaped response consumed by run_llm_request."""

    role: str = "assistant"
    content: list[Any] = field(default_factory=list)
    stop_reason: str = "end_turn"


class _MessagesNamespace:
    """SDK-style surface: client.messages.create(...) like the anthropic SDK."""

    def __init__(self, client: OpenAICompatClient):
        self._client = client

    def create(self, **kwargs: Any) -> LLMResponse:
        return self._client._create(**kwargs)


class OpenAICompatClient:
    """Anthropic `.messages.create(...)` over an OpenAI-compatible chat endpoint."""

    def __init__(self, base_url: str, api_key: str = "local", *,
                 timeout_s: float = DEFAULT_OPENAI_TIMEOUT_S):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_s = timeout_s
        self.messages = _MessagesNamespace(self)

    # ------------------------------------------------------------------ wire

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        req = urllib.request.Request(
            f"{self.base_url}{path}",
            data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {self.api_key}"},
            method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                return dict(json.loads(resp.read().decode()))
        except urllib.error.HTTPError as e:
            detail = ""
            try:
                detail = e.read().decode()[:200]
            except (OSError, ValueError):
                pass
            raise RuntimeError(f"openai-compatible endpoint error (HTTP {e.code}): "
                               f"{detail or e.reason}") from e
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as e:
            raise RuntimeError(f"openai-compatible endpoint unreachable: {e}") from e

    # ------------------------------------------------------------ translation

    @staticmethod
    def _translate_tool(tool: dict[str, Any]) -> dict[str, Any]:
        """Anthropic {name, description, input_schema} -> OpenAI function tool."""
        return {"type": "function",
                "function": {"name": tool["name"],
                             "description": tool.get("description", ""),
                             "parameters": tool.get("input_schema", {"type": "object"})}}

    @classmethod
    def _translate_message(cls, message: dict[str, Any]) -> list[dict[str, Any]]:
        """One Anthropic-shaped message -> one or more OpenAI messages.

        Tool results arrive as a user message whose content is a list of
        {type: tool_result, tool_use_id, content}; OpenAI wants one
        role=tool message per result, right after the assistant turn.
        """
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, str) or content is None:
            return [{"role": role, "content": content or ""}]
        out: list[dict[str, Any]] = []
        tool_calls: list[dict[str, Any]] = []
        text_parts: list[str] = []
        for block in content:
            btype = getattr(block, "type", None) or (
                block.get("type") if isinstance(block, dict) else None)
            if btype == "tool_result":
                tool_use_id, result = cls._tool_result_parts(block)
                out.append({"role": "tool", "tool_call_id": tool_use_id,
                            "content": result})
            elif btype == "tool_use":
                tool_calls.append(cls._tool_call(block))
            else:
                text_parts.append(str(getattr(block, "text", block)))
        if tool_calls:  # assistant turn: tool_calls first, results may ride along
            out.insert(0, {"role": "assistant", "content": " ".join(text_parts) or None,
                           "tool_calls": tool_calls})
            return out
        if out:  # pure tool-result turn(s)
            return out
        return [{"role": role, "content": " ".join(text_parts)}]

    @staticmethod
    def _tool_result_parts(block: Any) -> tuple[str, str]:
        if isinstance(block, dict):
            return str(block.get("tool_use_id", "")), str(block.get("content", ""))
        return str(getattr(block, "tool_use_id", "")), str(getattr(block, "content", ""))

    @staticmethod
    def _tool_call(block: Any) -> dict[str, Any]:
        if isinstance(block, dict):
            block_id, name, args = (block.get("id", ""), block.get("name", ""),
                                    block.get("input", {}))
        else:
            block_id, name, args = block.id, block.name, block.input
        return {"id": str(block_id) or f"call_{abs(hash(name)) % 10**8}",
                "type": "function",
                "function": {"name": str(name),
                             "arguments": json.dumps(args, default=str)}}

    # ------------------------------------------------------------------ api

    def _create(self, *, model: str, max_tokens: int, system: str,
                        messages: list[dict[str, Any]],
                        tools: list[dict[str, Any]]) -> LLMResponse:
        body: dict[str, Any] = {
            "model": model,
            "max_tokens": max_tokens,  # widest-compat name across local servers
            "messages": [{"role": "system", "content": system}]
            + [m for msg in messages for m in self._translate_message(msg)],
            "tools": [self._translate_tool(t) for t in tools],
        }
        resp = self._post("/chat/completions", body)
        choice = resp["choices"][0]
        msg = choice.get("message", {})
        content: list[Any] = []
        if msg.get("content"):
            content.append(TextBlock(text=str(msg["content"])))
        for i, tc in enumerate(msg.get("tool_calls") or []):
            fn = tc.get("function", {})
            raw_args = fn.get("arguments")
            if isinstance(raw_args, str):
                try:
                    args: dict[str, Any] = json.loads(raw_args or "{}")
                except json.JSONDecodeError:
                    args = {}
            else:
                args = dict(raw_args or {})
            content.append(ToolUseBlock(
                id=str(tc.get("id") or f"call_{i}"),
                name=str(fn.get("name", "")),
                input=args))
        stop = "tool_use" if msg.get("tool_calls") else "end_turn"
        return LLMResponse(role="assistant", content=content, stop_reason=stop)
