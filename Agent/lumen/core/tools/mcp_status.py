# -*- coding: utf-8 -*-
"""
core.tools.mcp_status · 让模型自己看见 MCP 连接状态
===================================================

MCP server 连不上时，模型看到的是「工具不存在」——它不知道该重试、换方案还是叫用户修。
这个工具把状态摊开：哪个 server 连上了、各有多少工具、失败的原因是什么。

对应评分标准里的「连接失败可降级、不阻塞主流程、状态可见」。
"""

from __future__ import annotations

from ..mcp_client import MCP_AVAILABLE, get_manager
from ..registry import tool


@tool(
    name="mcpStatus",
    description=(
        "查看已配置的 MCP server 连接状态：是否连上、各提供多少工具、失败原因。"
        "当你不确定某个 mcp__ 开头的工具为什么用不了，或用户问「MCP 连上了吗」时调用。"
    ),
    parameters={"type": "object", "properties": {}, "required": []},
    title="MCP 状态",
    category="MCP 管理",
)
def mcp_status(_args: dict) -> str:
    if not MCP_AVAILABLE:
        return (
            "MCP 功能不可用：未安装 mcp 库。\n"
            "请在项目目录执行 pip install mcp 后重启。"
        )

    manager = get_manager()
    if manager is None:
        return "MCP 未启动（本次运行没有初始化 MCP 客户端）。"

    if not manager.states:
        return (
            "MCP 未配置任何 server。\n"
            "如果确实需要，请在项目根目录创建 mcp.json（格式见 mcp.example.json），"
            "配置好后重启生效。"
        )

    lines = [f"MCP 概况：{manager.summary()}"]
    if manager.warnings:
        lines.append("")
        lines.append("配置告警：")
        lines.extend(f"  · {w}" for w in manager.warnings)

    lines.append("")
    lines.append("各 server 状态：")
    for name, status, detail in manager.status_rows():
        label = {"ready": "已连接", "failed": "失败", "pending": "连接中", "disabled": "已禁用"}.get(status, status)
        lines.append(f"  · {name}  [{label}]  {detail}")
        state = manager.states.get(name)
        if state and state.status == "ready" and state.tools:
            names = ", ".join(str(getattr(t, "name", "?")) for t in state.tools[:8])
            more = f" 等 {len(state.tools)} 个" if len(state.tools) > 8 else ""
            lines.append(f"      工具：{names}{more}")

    failed = [n for n, s, _ in manager.status_rows() if s == "failed"]
    if failed:
        lines.append("")
        lines.append(
            f"注意：{('、'.join(failed))} 连接失败。这些 server 的工具当前不可用，"
            f"请不要反复重试调用，如实告知用户并改用本地工具。"
        )

    return "\n".join(lines)
