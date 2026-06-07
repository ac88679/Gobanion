"""
LLM HTTP 客户端 — 支持双模型链

- public=True  → 公网模型（DeepSeek），用于主 Agent 规划
- public=False → 本地模型（Qwen3.5-9B），用于子 Agent 执行

所有 LLM 调用的输入输出都会记录到日志。
"""

import json
import re
from typing import Optional

import httpx

from config import get_settings
from services.logger import get_logger

log = get_logger("llm")


class LLMClient:
    """Thread-safe LLM client for chat completions with dual-model support."""

    def __init__(self, public: bool = False):
        settings = get_settings()
        if public:
            self.api_base = settings.llm.FALLBACK_BASE.rstrip("/")
            self.api_key = settings.llm.FALLBACK_KEY
            self.model = settings.llm.FALLBACK_MODEL
            self.max_tokens = settings.llm.FALLBACK_MAX_TOKENS
            self.model_type = "public"
        else:
            self.api_base = settings.llm.API_BASE.rstrip("/")
            self.api_key = settings.llm.API_KEY
            self.model = settings.llm.MODEL
            self.max_tokens = settings.llm.MAX_TOKENS
            self.model_type = "local"
        self.temperature = settings.llm.TEMPERATURE
        self.timeout = settings.llm.TIMEOUT

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    def chat(
        self,
        messages: list[dict],
        *,
        response_format: Optional[dict] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """
        Call chat completions.
        Returns the full response dict (contains choices[0].message.content).
        """
        body = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": False,
        }
        if response_format:
            body["response_format"] = response_format

        # ── Log input (full body JSON) ──
        log.info(f"LLM request [{self.model_type}] {self.model}",
                 body=json.dumps(body, ensure_ascii=False))

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.api_base}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            resp = r.json()

        # ── Log output (full body JSON) ──
        log.info(f"LLM response [{self.model_type}] {self.model}",
                 body=json.dumps(resp, ensure_ascii=False))

        return resp

    def chat_text(self, messages: list[dict]) -> str:
        """Convenience: return just the text content."""
        resp = self.chat(messages)
        return resp["choices"][0]["message"]["content"]

    def chat_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        *,
        tool_choice: str = "required",
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> list[dict]:
        """
        Call chat completions with tool calling (function calling).
        Returns list of tool_calls (each with name + parsed arguments).

        vLLM supports OpenAI-compatible tool calling by default.
        tool_choice='required' guarantees at least one tool call.
        """
        body = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "tool_choice": tool_choice,
            "temperature": temperature if temperature is not None else self.temperature,
            "max_tokens": max_tokens if max_tokens is not None else self.max_tokens,
            "stream": False,
        }

        # ── Log input (full body JSON) ──
        log.info(f"LLM tools request [{self.model_type}] {self.model}",
                 body=json.dumps(body, ensure_ascii=False))

        with httpx.Client(timeout=self.timeout) as client:
            r = client.post(
                f"{self.api_base}/chat/completions",
                headers=self._headers(),
                json=body,
            )
            r.raise_for_status()
            resp = r.json()

        # ── Parse tool calls ──
        choice = resp.get("choices", [{}])[0].get("message", {})
        tool_calls = choice.get("tool_calls", [])

        results = []
        for tc in tool_calls:
            name = tc.get("function", {}).get("name", "")
            args_raw = tc.get("function", {}).get("arguments", "{}")
            try:
                args = json.loads(args_raw)
            except json.JSONDecodeError:
                log.warning(f"Tool call arguments parse failed for {name}",
                            raw=args_raw)
                args = {"_raw": args_raw}
            results.append({"name": name, "arguments": args})

        # ── Log output (full body JSON) ──
        log.info(f"LLM tools response [{self.model_type}] {self.model}",
                 body=json.dumps(resp, ensure_ascii=False))

        return results

    def chat_json(self, messages: list[dict]) -> dict:
        """
        Convenience: request JSON mode and parse response.
        Falls back to JSON extraction if response_format not supported.
        """
        resp = self.chat(messages, response_format={"type": "json_object"})
        content = resp["choices"][0]["message"]["content"]
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            # Fallback: try to find JSON block
            match = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", content, re.DOTALL)
            if match:
                return json.loads(match.group(1))
            raise
