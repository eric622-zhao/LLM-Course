# -*- coding: utf-8 -*-
"""
core.tools.load_skill · Skill 动态加载工具（第七周 · 任务四）
============================================================

System Prompt 里只有 Skill 的 name + description（"菜单"），
本体要靠模型自己调这个工具取 —— 这就是「动态加载」：

    模型看到菜单 → 判断这个任务该用哪个 Skill → loadSkill(name) → 全文进上下文

两个参数
--------
name      必需。Skill 名称（Frontmatter 里的 name）。
filename  可选。需要加载 Skill 内的附件时传。
          * 只传 name            → 读 SKILL.md 全文
          * name + filename      → 只读附件，不重复读 SKILL.md（省 token）
          * filename 支持 Skill 相对路径、项目相对路径、绝对路径；
            只给文件名时会在该 Skill 目录内递归查找，
            命中多个会返回候选路径交给模型自己选。
"""

from __future__ import annotations

from core import skills
from ..registry import tool


@tool(
    name="loadSkill",
    description=(
        "加载某个 Skill 的完整说明或它的附件文件。"
        "System Prompt 里只列出了每个 Skill 的名称和一句话描述；当用户的任务与某个 Skill 的描述相符时，"
        "必须先调用本工具读取该 Skill 的 SKILL.md 全文，再按其中步骤执行。"
        "如果 SKILL.md 里提到某个参考文件/脚本，可再次调用本工具并传 filename 读取该附件。"
        "只传 name 时返回 SKILL.md；同时传 filename 时只返回附件（不重复返回 SKILL.md）。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill 名称，必须与 System Prompt 中列出的名称完全一致",
            },
            "filename": {
                "type": "string",
                "description": (
                    "可选。要读取的附件文件：可传相对于 Skill 目录的路径（如 reference.md）、"
                    "项目相对路径、绝对路径，或只给文件名（会在 Skill 目录内递归查找）"
                ),
            },
        },
        "required": ["name"],
    },
    title="加载技能",
    category="技能",
)
def load_skill(args: dict) -> str:
    registry = skills.get_registry()
    if registry is None:
        return (
            "错误：Skill 机制尚未初始化。\n"
            "这通常意味着程序没有通过 main.py 启动，或项目根目录未被设置。\n"
            "请告知用户检查启动方式，不要重复调用本工具。"
        )

    name = str(args.get("name") or "").strip()
    if not name:
        available = "、".join(registry.names()) or "（当前没有任何 Skill）"
        return f"错误：name 不能为空。可用 Skill：{available}"

    filename = args.get("filename")
    return registry.load(name, filename)
