# -*- coding: utf-8 -*-
"""
PetHospitalMCP · 联调自检脚本
=============================

不需要 AnythingLLM / Claude 之类的客户端，自己起一个 MCP 客户端连上来，
走一遍真实链路：拉起子进程 → 握手 → tools/list → tools/call。

用途：
    * 改完代码后确认「协议层还是通的」
    * 后端没启动时，确认模型侧拿到的是可读中文提示，而不是一句
      "Error executing tool health"

用法：
    py check_stdio.py                 # 默认调 health
    py check_stdio.py list_pets       # 调指定工具
    py check_stdio.py get_pet PET-000001

退出码：0 表示链路正常（工具报错也算正常，只要错误信息可读）；1 表示异常。
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

SERVER = Path(__file__).with_name("server.py")

# 缩短超时与重试，让「后端没启动」的场景几秒内返回，而不是干等 30 秒
CHILD_ENV = {
    **os.environ,
    "PET_HOSPITAL_TIMEOUT": "3",
    "PET_HOSPITAL_RETRIES": "1",
    "PYTHONIOENCODING": "utf-8",
}


def _parse_args(argv: list[str]) -> tuple[str, dict]:
    """把 `check_stdio.py 工具名 key=value ...` 解析成 (工具名, 参数)。"""
    if len(argv) <= 1:
        return "health", {}
    name, rest = argv[1], argv[2:]
    arguments: dict = {}
    for item in rest:
        if "=" not in item:
            print(f"[跳过] 参数 {item!r} 不是 key=value 形式", file=sys.stderr)
            continue
        key, value = item.split("=", 1)
        arguments[key] = value
    return name, arguments


def _as_text(result) -> str:
    """CallToolResult → 纯文本，兼容 2.x(is_error) 与 1.x(isError)。"""
    parts = [c.text for c in result.content if getattr(c, "text", None)]
    return "\n".join(parts) if parts else "(无文本内容)"


async def run(name: str, arguments: dict) -> int:
    # stderr 走临时文件：MCP 的日志不能混进 stdout，否则会污染 JSON-RPC
    with tempfile.NamedTemporaryFile("w+", suffix=".log", delete=False,
                                     encoding="utf-8") as log_file:
        log_path = log_file.name
        params = StdioServerParameters(
            command=sys.executable, args=[str(SERVER)], env=CHILD_ENV,
        )
        async with stdio_client(params, errlog=log_file) as (read, write):
            async with ClientSession(read, write) as session:
                await session.initialize()

                tools = await session.list_tools()
                print(f"[1] 握手成功，工具 {len(tools.tools)} 个")
                print("    " + "、".join(t.name for t in tools.tools[:6])
                      + ("…" if len(tools.tools) > 6 else ""))

                print(f"[2] 调用 {name}({json.dumps(arguments, ensure_ascii=False)})")
                result = await session.call_tool(name, arguments)
                is_error = getattr(result, "is_error", getattr(result, "isError", None))
                text = _as_text(result)
                print(f"    isError = {is_error}")
                for line in text.strip().splitlines()[:8]:
                    print("    " + line[:160])

    # 子进程日志：确认调用确实被记录，而不是只 print 给用户
    log = Path(log_path).read_text(encoding="utf-8", errors="replace")
    logged = name in log
    print(f"[3] 子进程日志 {len(log)} 字符，记录了本次调用：{'是' if logged else '否'}")
    if log.strip():
        print("    " + log.strip().splitlines()[0][:140])
    os.unlink(log_path)

    # 后端没启动时 is_error=True 是预期结果，只要信息可读就算通过
    readable = (not is_error) or ("Error executing tool" in text and "：" in text)
    print(f"[4] 结论：{'链路正常' if readable else '错误信息不可读，需检查 ToolError 是否生效'}")
    return 0 if readable else 1


def main() -> None:
    name, arguments = _parse_args(sys.argv)
    try:
        code = asyncio.run(run(name, arguments))
    except KeyboardInterrupt:
        code = 130
    sys.exit(code)


if __name__ == "__main__":
    main()
