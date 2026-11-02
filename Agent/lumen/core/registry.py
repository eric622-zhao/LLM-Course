# -*- coding: utf-8 -*-
"""
core.registry · 工具注册表
=========================

Function Calling 的两半：
    schemas()  → 交给 LLM 的工具清单（OpenAI function calling 格式）
    dispatch() → 按 LLM 返回的函数名与参数执行真正的工作

新增一个工具只要三行：
    @tool(name="xxx", description="...", parameters={...})
    def xxx(args: dict) -> str: ...
然后确保所在的模块被 core/tools/__init__.py 导入。
"""

from __future__ import annotations

import json
import traceback
from dataclasses import dataclass
from typing import Any, Callable


@dataclass
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    func: Callable[[dict[str, Any]], str]
    # 中文显示名，TUI 侧栏用；不影响 LLM
    title: str = ""
    category: str = "通用"


TOOLS: dict[str, ToolSpec] = {}


def tool(
    name: str,
    description: str,
    parameters: dict[str, Any],
    title: str = "",
    category: str = "通用",
):
    """把被装饰的函数注册成一个 Agent 工具。"""

    def decorator(func: Callable[[dict[str, Any]], str]) -> Callable[[dict[str, Any]], str]:
        if name in TOOLS:
            raise RuntimeError(f"工具名重复：{name}")
        TOOLS[name] = ToolSpec(
            name=name,
            description=description,
            parameters=parameters,
            func=func,
            title=title or name,
            category=category,
        )
        return func

    return decorator


def schemas() -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "function": {
                "name": spec.name,
                "description": spec.description,
                "parameters": spec.parameters,
            },
        }
        for spec in TOOLS.values()
    ]


def dispatch(name: str, raw_arguments: str) -> str:
    """执行工具调用。

    约定：**永远返回字符串，永远不抛异常**。
    任何失败都变成 LLM 能读懂的中文错误说明，模型才有机会换方案重试。
    """
    spec = TOOLS.get(name)
    if spec is None:
        available = "、".join(sorted(TOOLS)) or "（无）"
        return f"错误：不存在名为 {name} 的工具。当前可用工具：{available}"

    if not raw_arguments or not raw_arguments.strip():
        arguments: dict[str, Any] = {}
    else:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError as exc:
            return f"错误：工具 {name} 的参数不是合法 JSON（{exc}）。收到的内容：{raw_arguments[:300]}"
        if not isinstance(arguments, dict):
            return f"错误：工具 {name} 的参数必须是 JSON 对象，收到的是 {type(arguments).__name__}。"

    try:
        result = spec.func(arguments)
    except Exception as exc:  # noqa: BLE001 - 工具层兜底
        detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
        return f"错误：工具 {name} 执行失败 — {detail}"

    if result is None:
        return "（工具执行完成，但没有返回内容）"
    return result if isinstance(result, str) else str(result)


def truncate(text: str, limit: int, label: str = "内容") -> str:
    """按字符数截断，并明确告诉 LLM 截掉了多少。"""
    if limit <= 0 or len(text) <= limit:
        return text
    omitted = len(text) - limit
    return (
        f"{text[:limit]}\n\n…（{label}已截断：原文共 {len(text)} 字符，"
        f"此处保留前 {limit} 字符，省略 {omitted} 字符）"
    )
