# -*- coding: utf-8 -*-
"""
core.tools · 工具包的汇总入口
============================

导入本包会把下面所有模块里的 @tool 函数注册进 core.registry.TOOLS。
新增工具时在末尾加一行 import 即可。
"""

from __future__ import annotations

from ..registry import TOOLS, ToolSpec, dispatch, schemas, tool, truncate  # noqa: F401

# 第五、六周：系统类（外部命令 / 内部计算）
from . import system  # noqa: E402,F401

# 第五、六周：10 个文件与目录操作
from . import file_ops  # noqa: E402,F401

# 第七周：网页正文获取（curl + trafilatura）
from . import fetch_web  # noqa: E402,F401

# 第七周：联网搜索（百度千帆 AI 搜索）
from . import search_web  # noqa: E402,F401

# 第七周：本地命令行调用
# 注意：run_cli 依赖 file_ops 的 PROJECT_ROOT，必须排在 file_ops 之后导入
from . import run_cli  # noqa: E402,F401

# 第七周：Skill 动态加载
from . import load_skill  # noqa: E402,F401
