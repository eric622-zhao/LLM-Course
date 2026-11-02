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
