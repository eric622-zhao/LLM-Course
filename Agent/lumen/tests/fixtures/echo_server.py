# -*- coding: utf-8 -*-
"""
tests/fixtures/echo_server.py · 最小 MCP server（测试用）
========================================================

不依赖任何外部服务，只为验证客户端的完整链路：
    拉起进程 → 握手 → tools/list → 调用 → 返回结果

也可以当 mcp.json 的示例：把 command 指到本文件即可跑通。

手动验证：
    py tests/fixtures/echo_server.py        # 从 stdin 读 JSON-RPC，会一直等输入
"""

from __future__ import annotations

import sys

try:
    from mcp.server.mcpserver import MCPServer  # mcp >= 2.0
except ImportError:  # pragma: no cover - 老版本 SDK 的包名
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]


server = MCPServer("echo-server")


@server.tool()
def echo(text: str) -> str:
    """把传入的文本原样回显，用于验证连通性。"""
    return f"echo: {text}"


@server.tool()
def add(a: int, b: int) -> int:
    """计算两个整数之和。"""
    return a + b


@server.tool()
def fail_always() -> str:
    """总是抛异常，用于验证错误是否能被客户端正确呈现。"""
    raise RuntimeError("这是故意触发的错误")


if __name__ == "__main__":
    server.run(transport="stdio")
    sys.exit(0)
