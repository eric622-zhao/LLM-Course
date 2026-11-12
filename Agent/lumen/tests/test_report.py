# -*- coding: utf-8 -*-
"""
tests.test_report · 清单输出的离线测试
======================================

`/tools` 与 `/skills` 的输出有个前科：CLI 里没接住这两条命令，
文本落到 agent.ask()，模型照着 schema 自己编了一张工具表 ——
42 个工具数成了 35 个。所以这里钉住三件事：

    1. 数字必须等于注册表实际长度，不能由模型估
    2. 原生工具与 MCP 工具分开列，`mcpStatus` 这种 category 名为 MCP
       的原生工具不能混进 MCP server 分组
    3. MCP 工具的描述里已经带了 `[MCP · server]`，清单里要去掉，否则重复

跑法：
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import report  # noqa: E402
from core.registry import TOOLS, ToolSpec  # noqa: E402


def _spec(name: str, desc: str, category: str = "通用", title: str = "") -> ToolSpec:
    return ToolSpec(
        name=name,
        description=desc,
        parameters={"type": "object", "properties": {}, "required": []},
        func=lambda args: "",
        title=title or name,
        category=category,
    )


class ToolsReportTest(unittest.TestCase):
    """替换全局注册表跑断言，跑完原样还原。"""

    def setUp(self) -> None:
        self._backup = dict(TOOLS)
        TOOLS.clear()
        TOOLS["getLocalTime"] = _spec(
            "getLocalTime", "获取本机当前的本地日期和时间。额外说明在这里被截掉。", "系统"
        )
        TOOLS["readFile"] = _spec("readFile", "读取指定纯文本文件的内容", "文件")
        TOOLS["mcpStatus"] = _spec(
            "mcpStatus", "查看已配置的 MCP server 连接状态", "MCP 管理"
        )
        TOOLS["mcp__pet-hospital__health"] = _spec(
            "mcp__pet-hospital__health",
            "[MCP · pet-hospital] 健康检查：确认服务是否已启动",
            "MCP",
            "pet-hospital/health",
        )
        TOOLS["mcp__anythingllm__chat"] = _spec(
            "mcp__anythingllm__chat",
            "[MCP · anythingllm] 在工作区里提问，由 AnythingLLM 检索文档后回答",
            "MCP",
            "anythingllm/chat",
        )

    def tearDown(self) -> None:
        TOOLS.clear()
        TOOLS.update(self._backup)

    # -- 数字 ------------------------------------------------------------- #
    def test_total_matches_registry(self) -> None:
        text = report.tools_report()
        self.assertIn("共 5 个工具（原生 3 · MCP 2）", text)

    def test_empty_registry(self) -> None:
        TOOLS.clear()
        self.assertIn("还没有注册任何工具", report.tools_report())

    # -- 分组 ------------------------------------------------------------- #
    def test_native_mcp_category_not_mixed_into_remote(self) -> None:
        """mcpStatus 的 category 是 MCP，但它是原生工具，不能算进 MCP 分组。"""
        text = report.tools_report()
        remote_block = text.split("MCP · pet-hospital")[0]
        self.assertIn("mcpStatus", remote_block)
        # MCP 分组的工具数只算真正带前缀的两个
        self.assertIn("MCP · pet-hospital（1）", text)
        self.assertIn("MCP · anythingllm（1）", text)

    def test_remote_tools_listed_under_server_with_short_name(self) -> None:
        text = report.tools_report()
        self.assertIn("MCP · pet-hospital（1）", text)
        self.assertIn("  health ", text)
        # 全名太长，分组标题已经说明 server，清单里用短名
        self.assertNotIn("mcp__pet-hospital__health", text)

    # -- 描述 ------------------------------------------------------------- #
    def test_source_tag_stripped(self) -> None:
        """描述开头的 [MCP · server] 与分组标题重复，要去掉。"""
        text = report.tools_report()
        self.assertNotIn("[MCP · pet-hospital]", text)
        self.assertIn("健康检查", text)

    def test_first_sentence_only(self) -> None:
        text = report.tools_report()
        self.assertIn("获取本机当前的本地日期和时间", text)
        self.assertNotIn("额外说明在这里被截掉", text)

    def test_long_description_truncated(self) -> None:
        TOOLS["verbose"] = _spec("verbose", "很长" * 80, "通用")
        line = [ln for ln in report.tools_report().splitlines() if "verbose" in ln][0]
        self.assertLessEqual(len(line), report.DESC_LIMIT + 30)
        self.assertIn("…", line)


class SkillsReportTest(unittest.TestCase):
    def test_lists_name_and_description(self) -> None:
        text = report.skills_report([("weekly-report", "把零散记录整理成周报。含附件说明")])
        self.assertIn("已加载 1 个 Skill", text)
        self.assertIn("weekly-report", text)
        self.assertIn("把零散记录整理成周报", text)
        # 只显示第一句
        self.assertNotIn("附件说明", text)

    def test_empty_gives_actionable_hint(self) -> None:
        text = report.skills_report([])
        self.assertIn("当前没有 Skill", text)
        # 要告诉用户放哪儿，否则这条提示没有用
        self.assertIn(".agents/skills", text)


class CommandParityTest(unittest.TestCase):
    """CLI 与 TUI 的命令集合不能漂移。

    真实事故：TUI 支持 `/tools`，CLI 没接住，这条文本落进了 agent.ask()，
    模型看着 schema 自己编了一张工具表，42 个工具数成了 35 个 ——
    用户看到的不是「命令无效」，而是一段自信的错误答案。
    """

    ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    PATTERN = re.compile(
        r'lowered\s+(?:==\s*"([^"]+)"|in\s*\(([^)]*)\)|startswith\(\s*"([^"]+)")'
    )

    def _commands(self, relative: str) -> set[str]:
        with open(os.path.join(self.ROOT, relative), encoding="utf-8") as fh:
            source = fh.read()
        found: set[str] = set()
        for single, group, prefix in self.PATTERN.findall(source):
            if single:
                found.add(single)
            if group:
                found.update(re.findall(r'"([^"]+)"', group))
            if prefix:
                found.add(prefix)
        return found

    def test_cli_covers_every_tui_command(self) -> None:
        cli = self._commands("main.py")
        tui = self._commands(os.path.join("tui", "app.py"))

        self.assertTrue(cli, "没能从 main.py 里解析出任何命令，正则可能失效了")
        missing = tui - cli
        # `/?` 是 TUI 的额外简写，CLI 不提供不算缺陷
        self.assertEqual(missing, {"/?"}, f"CLI 少实现了这些命令：{sorted(missing)}")

    def test_help_text_mentions_every_command(self) -> None:
        """帮助里没写的命令，用户就不会发现它存在。"""
        with open(os.path.join(self.ROOT, "main.py"), encoding="utf-8") as fh:
            source = fh.read()
        help_block = source.split('if lowered == "/help":')[1].split("continue")[0]
        for cmd in self._commands("main.py"):
            with self.subTest(cmd=cmd):
                self.assertIn(cmd, help_block, f"/help 里漏了 {cmd}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
