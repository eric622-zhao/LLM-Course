# -*- coding: utf-8 -*-
"""
core.report · 给用户看的清单
============================

`/tools` 和 `/skills` 这类命令的输出，CLI 与 TUI 共用同一份实现。

**为什么不让模型来数**：早期 `/tools` 在 CLI 里没被接住，文本落到 `agent.ask()`，
模型看着工具 schema 自己编了一张表 —— 42 个工具数成了 35 个。
清单类信息必须从注册表现场取，不能经过模型。

分组规则：
    原生工具  按 ToolSpec.category 分（时间 / 文件 / 网络 / 技能 …）
    MCP 工具  按 server 分。判定依据是名字前缀 `mcp__`，不是 category ——
              `mcpStatus` 这个原生工具的 category 也叫 "MCP"，
              按 category 分会把它混进 MCP 分组里。
"""

from __future__ import annotations

from .registry import TOOLS

MCP_PREFIX = "mcp__"
DESC_LIMIT = 52


def _first_sentence(text: str, limit: int = DESC_LIMIT) -> str:
    """取描述的第一句，超长按字符截断。

    工具描述常写成「查档案。支持按名字模糊匹配，返回 ……」，
    清单里只需要第一句；完整描述留给模型看。
    """
    text = " ".join((text or "").split())
    if not text:
        return ""
    for mark in ("。", "；"):
        idx = text.find(mark)
        if 0 < idx < limit:
            return text[:idx]
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _is_remote(name: str, spec) -> bool:
    return name.startswith(MCP_PREFIX)


def _split_remote(name: str) -> tuple[str, str]:
    """`mcp__server__tool` → (server, tool)。"""
    parts = name.split("__")
    if len(parts) >= 3:
        return parts[1], "__".join(parts[2:])
    return "MCP", name


def _strip_source_tag(description: str, server: str) -> str:
    """去掉描述开头的 `[MCP · server] `——分组标题里已经有来源了。"""
    tag = f"[MCP · {server}]"
    text = (description or "").strip()
    return text[len(tag):].strip() if text.startswith(tag) else text


def tools_report() -> str:
    """按来源分组列出全部已注册工具。"""
    native: dict[str, list] = {}
    remote: dict[str, list] = {}

    for name, spec in TOOLS.items():
        if _is_remote(name, spec):
            server, short = _split_remote(name)
            remote.setdefault(server, []).append((short, spec, server))
        else:
            native.setdefault(getattr(spec, "category", "通用") or "通用", []).append(
                (name, spec, "")
            )

    total = len(TOOLS)
    n_native = sum(len(v) for v in native.values())
    n_remote = total - n_native

    if total == 0:
        return "还没有注册任何工具。"

    lines = [f"共 {total} 个工具（原生 {n_native} · MCP {n_remote}）"]

    def block(title: str, items: list) -> None:
        if not items:
            return
        lines.append("")
        lines.append(f"{title}（{len(items)}）")
        width = max(len(n) for n, _, _ in items)
        for name, spec, server in items:
            desc = _strip_source_tag(getattr(spec, "description", ""), server)
            desc = _first_sentence(desc)
            lines.append(f"  {name.ljust(width)}  {desc}" if desc else f"  {name}")

    for category, items in native.items():
        block(category, items)
    for server, items in remote.items():
        block(f"MCP · {server}", items)

    if n_remote:
        lines.append("")
        lines.append("MCP 工具在调用时用全名 `mcp__<server>__<tool>`，模型按名字区分来源。")

    return "\n".join(lines)


def skills_report(skills_info: list) -> str:
    """列出已发现的技能。skills_info 是 [(name, description), ...]。"""
    if not skills_info:
        from . import skills as _skills  # 延迟导入，避免循环

        root_hint = getattr(_skills, "SKILLS_DIRNAME", ".agents/skills").replace("\\", "/")
        return (
            "当前没有 Skill。\n"
            f"把技能放到 <项目根>/{root_hint}/<名称>/SKILL.md 后重启，"
            "或直接让模型用 createSkill 创建一个。"
        )

    lines = [f"已加载 {len(skills_info)} 个 Skill："]
    width = max(len(name) for name, _ in skills_info)
    for name, desc in skills_info:
        desc = _first_sentence(desc)
        lines.append(f"  {name.ljust(width)}  {desc}" if desc else f"  {name}")
    lines.append("")
    lines.append("正文不在这里：模型认为需要时才调 loadSkill 读取，避免一开始就把上下文塞满。")
    return "\n".join(lines)
