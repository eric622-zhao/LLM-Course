# -*- coding: utf-8 -*-
"""
core.agent_md · 项目级长期规则（AGENT.md）
==========================================

解决的问题：**有些话希望 Agent 每次都记住，但又不想每次提问都重复一遍。**

比如「这个项目用 4 空格缩进」「回答一律用中文」「改代码前先跑测试」——
这类约定和具体任务无关，适合放在仓库里跟着代码走，而不是塞进某一轮对话。

约定
----
启动时读取**项目根目录**（启动时的 CWD）下的 `AGENT.md`，
兼容 `AGENTS.md` / `agent.md` / `agents.md` 等大小写变体
（不同工具的命名习惯不同，等价命名都认）。

内容会拼进 System Prompt，位置在基础人设之后、技能清单之前 ——
因为它是"这个项目的规矩"，优先级高于通用技能说明。

三条工程约束
------------
1. **只注入一次**：在启动引导阶段读一次，之后随 System Prompt 常驻。
   reset() 清空对话时会保留（它是规则，不是对话内容）。
2. **长度截断**：文件可能被写成几百 KB，必须截断并明确告知模型
   「后面还有内容没给你」，否则它会以为自己看全了。
3. **缺失不报错**：没有这个文件是完全正常的，静默跳过。

开关与文件名都可以在 config.ini 里改：

    [agent]
    rules_enabled = true
    rules_file = AGENT.md
    rules_max_chars = 8000

`rules_file` 留空则按 DEFAULT_FILENAMES 的顺序找第一个存在的。
"""

from __future__ import annotations

import os
from dataclasses import dataclass

from core import config

# 等价命名，按优先级排列。Windows 上大小写不敏感，但别的系统敏感，
# 所以这里显式列出常见写法，避免「明明有文件却读不到」。
DEFAULT_FILENAMES = ("AGENT.md", "AGENTS.md", "agent.md", "agents.md", "Agent.md")

DEFAULT_MAX_CHARS = 8000


@dataclass
class RulesDoc:
    """一份被加载的项目规则。"""

    path: str          # 绝对路径
    filename: str      # 文件名（相对项目根目录）
    text: str          # 实际注入的内容（可能已截断）
    total_chars: int   # 原文长度
    truncated: bool

    def summary(self) -> str:
        """一行状态，给 TUI 侧栏和启动信息用。"""
        if self.truncated:
            return f"{self.filename}（{self.total_chars} 字符，已截断注入 {len(self.text)} 字符）"
        return f"{self.filename}（{len(self.text)} 字符）"


def _read_text(path: str) -> str:
    """按 UTF-8 → GB18030 读。Windows 下记事本存的文件常是 GBK。"""
    with open(path, "rb") as fh:
        raw = fh.read()
    for encoding in ("utf-8-sig", "utf-8", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _candidate_names() -> list[str]:
    """配置里指定的文件名优先；没指定就用默认候选列表。"""
    configured = (config.get("agent", "rules_file") or "").strip()
    if configured:
        # 允许写子路径，如 docs/AGENT.md
        return [configured]
    return list(DEFAULT_FILENAMES)


def _locate(project_root: str) -> tuple[str, str] | None:
    """找到规则文件，返回 (绝对路径, 相对项目根的展示名)。

    展示名直接取**候选名**，不用 `relpath(realpath(path), project_root)` 反推。
    Windows 上这条路会翻车：GitHub Actions 的 temp 目录是 8.3 短名
    （`C:\\Users\\RUNNER~1\\...`），`realpath()` 会把它展开成长名
    （`C:\\Users\\runneradmin\\...`），两者前缀不同，relpath 就算出
    `../../../../../runneradmin/AppData/Local/Temp/...` 这种东西。
    本地用户目录一般没有短名差异，所以这个坑只在 CI 上暴露。
    """
    for name in _candidate_names():
        candidate = os.path.join(project_root, name)
        if os.path.isfile(candidate):
            display = os.path.basename(name) if os.path.isabs(name) else name
            return os.path.realpath(candidate), display.replace(os.sep, "/")
    return None


def find(project_root: str) -> str | None:
    """在项目根目录下找规则文件，返回绝对路径；没有则 None。"""
    located = _locate(project_root)
    return located[0] if located else None


def load(project_root: str) -> RulesDoc | None:
    """读取项目规则。没有文件、被关掉、或读失败都返回 None，不抛异常。"""
    if not _enabled():
        return None

    located = _locate(project_root)
    if located is None:
        return None
    path, display = located

    try:
        text = _read_text(path).strip()
    except OSError:
        return None

    if not text:
        return None

    total = len(text)
    limit = max(1000, config.get_int("agent", "rules_max_chars", DEFAULT_MAX_CHARS))
    truncated = total > limit

    if truncated:
        text = (
            f"{text[:limit]}\n\n"
            f"…（规则文件共 {total} 字符，此处只注入前 {limit} 字符。"
            f"后续内容未提供给你；如确实需要，请让用户改用 readFile 读取剩余部分。）"
        )

    return RulesDoc(
        path=path,
        filename=display,
        text=text,
        total_chars=total,
        truncated=truncated,
    )


def _enabled() -> bool:
    raw = config.get("agent", "rules_enabled")
    if raw is None or not str(raw).strip():
        return True  # 默认开启：文件不存在时本来就什么都不做，无需用户先配置
    return str(raw).strip().lower() not in ("0", "false", "no", "off", "否")


def prompt_block(doc: RulesDoc | None) -> str:
    """把规则包装成 System Prompt 片段。没有规则时返回空串。"""
    if doc is None:
        return ""

    return (
        "## 项目规则（AGENT.md）\n"
        f"以下内容来自本项目的 `{doc.filename}`，是这个项目的长期约定，"
        "在本次会话的所有回复中都必须遵守；它们优先于通用风格偏好，"
        "但不得违反安全底线。若与用户当轮的明确指令冲突，以用户当轮指令为准，并说明冲突点。\n"
        f"\n<project_rules source=\"{doc.filename}\">\n"
        f"{doc.text}\n"
        "</project_rules>"
    )
