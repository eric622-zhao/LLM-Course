# -*- coding: utf-8 -*-
"""
core.agent · Function Calling 主循环
====================================

一次 ask() 的完整流程：

    用户消息 → LLM（流式）
                 ↓ 返回 tool_calls
               执行工具（dispatch），结果以 role=tool 回填
                 ↓
               LLM 再读一遍 → 继续调工具 or 给出最终回答

这个「模型 → 工具 → 模型」的往返就是 Agent 的心跳。
每一轮都把**完整历史**发回去，模型才能记得上一轮查到了什么。

为什么流式还要处理工具调用？
    因为 chat.completions 的流式响应里，tool_calls 是以 delta 的形式
    **分片**到达的（名字一片、参数一片、id 一片）。必须按 index 归并起来，
    才能还原出一个完整的调用请求。这是本节最容易踩的坑。
"""

from __future__ import annotations

from typing import Callable

from core import config
from core.registry import dispatch, schemas

# 导入工具包即完成注册。放在这里而不是只在 main.py 里做，
# 是为了让任何入口（TUI、脚本、测试）一用到 Agent 就一定有完整工具集。
import core.tools  # noqa: F401,E402

BASE_SYSTEM_PROMPT = """你是「流明」（Lumen），一个可以调用工具的 AI 助手，运行在用户本机的命令行环境中。

工作原则：
1. 需要事实、数据、时间、文件内容时，先调用工具查证，不要凭记忆编造。
2. 用户问时间相关的问题，用 getLocalTime；问闰年，用 isLeapYear。
3. 涉及文件和目录时，用对应的专用工具，不要臆测文件内容。
4. 工具返回「错误：」开头的内容时，先读懂错误原因，再决定换参数、换方案，还是如实告诉用户；不要重复同样的失败调用。
5. 删除类操作不可恢复，执行前先向用户确认。
6. 回答使用简体中文，条理清晰、直击要点；不确定的地方要明确说出来。
"""


class Agent:
    """带工具、支持流式的对话 Agent。"""

    def __init__(
        self,
        system_prompt: str | None = None,
        extra_system: str = "",
        on_delta: Callable[[str], None] | None = None,
        on_tool_call: Callable[[str, str], None] | None = None,
        on_tool_result: Callable[[str, str, int], None] | None = None,
        on_reasoning: Callable[[str], None] | None = None,
    ) -> None:
        from openai import OpenAI

        api_key = config.get("llm", "api_key") or ""
        base_url = config.get("llm", "base_url") or None
        self.model = config.get("llm", "model") or "gpt-4o-mini"
        self.temperature = config.get_float("llm", "temperature", 0.7)
        self.max_rounds = config.get_int("agent", "max_tool_rounds", 12)

        self.client = OpenAI(api_key=api_key, base_url=base_url)

        self.on_delta = on_delta
        self.on_tool_call = on_tool_call
        self.on_tool_result = on_tool_result
        self.on_reasoning = on_reasoning

        system = (system_prompt or BASE_SYSTEM_PROMPT).strip()
        if extra_system.strip():
            system = f"{system}\n\n{extra_system.strip()}"
        self.system_prompt = system

        self.messages: list[dict] = [{"role": "system", "content": self.system_prompt}]
        self.total_tokens = 0

    # ------------------------------------------------------------------

    def reset(self) -> None:
        """清空上下文，保留 System Prompt。"""
        self.messages = [{"role": "system", "content": self.system_prompt}]

    def _callbacks(self) -> dict:
        return {
            "on_delta": self.on_delta,
            "on_tool_call": self.on_tool_call,
            "on_tool_result": self.on_tool_result,
        }

    def ask(self, user_input: str, **overrides) -> str:
        """处理一轮用户输入，返回最终回答文本。

        overrides 可以临时替换 on_delta / on_tool_call / on_tool_result 回调
        （TUI 里每个气泡各自绑自己的回调时会用到）。
        """
        on_delta = overrides.get("on_delta", self.on_delta)
        on_tool_call = overrides.get("on_tool_call", self.on_tool_call)
        on_tool_result = overrides.get("on_tool_result", self.on_tool_result)

        self.messages.append({"role": "user", "content": user_input})

        for _round in range(1, self.max_rounds + 1):
            collected = self._stream_once(on_delta)

            if not collected["tool_calls"]:
                self.messages.append({"role": "assistant", "content": collected["text"]})
                return collected["text"]

            # 模型要调工具：先把「它想调什么」写回历史
            self.messages.append(
                {
                    "role": "assistant",
                    "content": collected["text"],
                    "tool_calls": [
                        {
                            "id": call["id"],
                            "type": "function",
                            "function": {"name": call["name"], "arguments": call["arguments"]},
                        }
                        for call in collected["tool_calls"]
                    ],
                }
            )

            for call in collected["tool_calls"]:
                if on_tool_call:
                    on_tool_call(call["name"], call["arguments"])
                result = dispatch(call["name"], call["arguments"])
                if on_tool_result:
                    on_tool_result(call["name"], result, len(result))
                self.messages.append(
                    {"role": "tool", "tool_call_id": call["id"], "content": result}
                )

        return (
            f"已达到单轮最多 {self.max_rounds} 次工具调用的上限，为避免死循环先停下。\n"
            f"上面的操作可能只完成了一部分，请缩小问题范围后重试。"
        )

    # ------------------------------------------------------------------

    def _stream_once(self, on_delta: Callable[[str], None] | None) -> dict:
        """发一次流式请求，把正文和 tool_calls 分片都收齐。"""
        stream = self.client.chat.completions.create(
            model=self.model,
            messages=self.messages,
            tools=schemas(),
            tool_choice="auto",
            temperature=self.temperature,
            stream=True,
        )

        text_parts: list[str] = []
        # index -> {"id":..., "name":..., "arguments":...}
        call_slots: dict[int, dict] = {}

        for chunk in stream:
            usage = getattr(chunk, "usage", None)
            if usage is not None and getattr(usage, "total_tokens", None):
                self.total_tokens += usage.total_tokens

            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta is None:
                continue

            # 部分模型（如 DeepSeek-R1）会先输出思维链，单独勾出来
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning and self.on_reasoning:
                self.on_reasoning(reasoning)

            if delta.content:
                text_parts.append(delta.content)
                if on_delta:
                    on_delta(delta.content)

            for piece in (delta.tool_calls or []):
                index = piece.index if piece.index is not None else 0
                slot = call_slots.setdefault(index, {"id": "", "name": "", "arguments": ""})
                if piece.id:
                    slot["id"] = piece.id
                function = getattr(piece, "function", None)
                if function is not None:
                    if function.name:
                        slot["name"] += function.name
                    if function.arguments:
                        slot["arguments"] += function.arguments

        calls = []
        for index in sorted(call_slots):
            slot = call_slots[index]
            if not slot["name"]:
                continue
            calls.append(
                {
                    "id": slot["id"] or f"call_{index}",
                    "name": slot["name"],
                    "arguments": slot["arguments"] or "{}",
                }
            )

        return {"text": "".join(text_parts), "tool_calls": calls}
