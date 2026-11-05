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

联网相关：
7. 涉及新闻、时事、价格、版本、人物近况等「训练数据之后可能已变化」的信息，用 searchWeb 搜索；不要凭记忆回答。
8. 用户给出网址、或需要读某个具体页面的完整内容时，用 fetchWeb 获取正文；它是 HTML 网页专用工具，PDF/视频不行。
9. searchWeb 返回的只是摘要，遇到关键结论建议再用 fetchWeb 打开原始来源核对，并在回答中标注来源。

本地命令：
10. 需要只有命令行才能拿到的信息（git 状态、python 版本、pip 列表等）时用 RunCli。
    参数必须拆成「程序名 + 参数列表」，不能把整条命令写进 command，也不能使用管道、重定向、&& 等 shell 语法。
11. RunCli 执行的是用户本机上的真实程序，有副作用（可能修改文件）。执行前先确认这是用户真正想要的；
    对破坏性命令（删除、格式化、强制推送等）必须先向用户确认。

技能：
12. System Prompt 末尾可能会列出可用 Skill（只有名称和一句话描述）。当用户的任务与某个 Skill 的描述相符时，
    必须先调 loadSkill 读取该 Skill 的完整说明，再严格按照说明执行；不要凭猜测使用技能。
13. 如果 Skill 说明里提到参考文件或脚本，用 loadSkill 带上 filename 读取它，不要假设其内容。
"""


def _first_non_ascii(text: str) -> int:
    """返回第一个非 ASCII 字符的下标；全是 ASCII 则返回 -1。

    HTTP 头的值只能是 ASCII。配置里混入中文（最常见的是没改的占位符
    「sk-替换成你的key」）会在发请求时炸成 UnicodeEncodeError，
    报错信息只说 position N，完全看不出是配置没填 —— 所以提前查出来。
    """
    for index, char in enumerate(text):
        if ord(char) > 127:
            return index
    return -1


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

        api_key = (config.get("llm", "api_key") or "").strip()
        base_url = (config.get("llm", "base_url") or "").strip() or None
        self.model = (config.get("llm", "model") or "").strip() or "gpt-4o-mini"
        self.temperature = config.get_float("llm", "temperature", 0.7)
        self.max_rounds = config.get_int("agent", "max_tool_rounds", 12)

        # 配置体检：有问题就记下来，ask() 时一次性说清楚，而不是让 SDK 抛出
        # 一句「ascii codec can't encode ...」让用户去猜。
        problems: list[str] = []
        if config.missing_or_placeholder(api_key):
            problems.append("config.ini 的 [llm] api_key 还没填写")
            api_key = ""  # 占位符里的中文绝不能进 Authorization 头
        elif _first_non_ascii(api_key) >= 0:
            problems.append("[llm] api_key 含非 ASCII 字符（像是没替换干净的占位符）")

        for label, value in (("base_url", base_url or ""), ("model", self.model)):
            position = _first_non_ascii(value)
            if position >= 0:
                problems.append(
                    f"[llm] {label} 含非 ASCII 字符（第 {position} 位起：{value[position:position + 8]!r}）"
                )

        self.config_problem = "；".join(problems)

        # 传一个 ASCII 占位值：空字符串在不同 openai 版本里行为不一致，
        # 而真正的拦截在 ask() 里，不会走到网络。
        self.client = OpenAI(api_key=api_key or "not-configured", base_url=base_url)

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
        if self.config_problem:
            raise RuntimeError(
                f"{self.config_problem}。\n"
                f"配置文件：{config.CONFIG_PATH}\n"
                f"请填好 [llm] 的 api_key / base_url / model 后重启。"
            )

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
