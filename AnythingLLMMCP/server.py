# -*- coding: utf-8 -*-
"""
AnythingLLM MCP 服务器
=======================

把本地 AnythingLLM 的工作区信息暴露成 MCP 工具，供 AI 客户端调用。

课堂上以 **Streamable HTTP** 方式常驻运行：
    端口 8091，端点 http://127.0.0.1:8091/mcp
后端调用 AnythingLLM 的 GET /api/workspaces（本地实测无需鉴权，直接返回 JSON）。

详细说明见同目录 README.md。

运行方式
--------
    python server.py                    # Streamable HTTP，默认 127.0.0.1:8091/mcp
    python server.py --port 8099        # 换端口
    python server.py --stdio            # 改为 stdio，由客户端拉起进程

环境变量
--------
    ANYTHINGLLM_API      后端地址，默认 http://127.0.0.1:3001
    ANYTHINGLLM_API_KEY  可选。填了就以 Bearer 方式带上，便于走开发者 API
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Any

import httpx

try:  # mcp >= 1.2
    from mcp.server.fastmcp import FastMCP
except Exception as exc:  # pragma: no cover
    print("未找到 mcp 依赖，请先执行： pip install -r requirements.txt", file=sys.stderr)
    raise SystemExit(1) from exc


DEFAULT_API = os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001").rstrip("/")
API_KEY = os.environ.get("ANYTHINGLLM_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("ANYTHINGLLM_TIMEOUT", "15"))

mcp = FastMCP(
    name="anythingllm",
    instructions=(
        "读取本地 AnythingLLM 的工作区与文档信息。"
        f"后端地址：{DEFAULT_API}（可用环境变量 ANYTHINGLLM_API 覆盖）。"
        "若连接失败，请确认 AnythingLLM 桌面端已经打开，且端口为 3001。"
    ),
)

_client: httpx.Client | None = None
_base = DEFAULT_API


def client() -> httpx.Client:
    global _client
    if _client is None:
        headers = {"Accept": "application/json"}
        if API_KEY:
            headers["Authorization"] = f"Bearer {API_KEY}"
        _client = httpx.Client(base_url=_base, timeout=TIMEOUT, headers=headers)
    return _client


def call(path: str, params: dict[str, Any] | None = None) -> Any:
    """GET 一个 AnythingLLM 接口，失败时抛出人类可读的错误。"""
    try:
        resp = client().get(path, params=params)
    except httpx.ConnectError as exc:
        raise RuntimeError(
            f"连接 AnythingLLM 失败（{_base}）。请先打开 AnythingLLM 桌面端再重试。"
        ) from exc
    except httpx.TimeoutException as exc:
        raise RuntimeError(f"请求超时：GET {path}") from exc

    if resp.status_code == 401:
        raise RuntimeError("AnythingLLM 返回 401：该接口需要鉴权，请设置 ANYTHINGLLM_API_KEY。")
    if resp.status_code >= 400:
        raise RuntimeError(f"AnythingLLM 返回 {resp.status_code}：{resp.text[:300]}")

    try:
        return resp.json()
    except ValueError:
        return resp.text


def _workspaces(payload: Any) -> list[dict[str, Any]]:
    """兼容多种返回结构，统一取出工作区数组。"""
    if isinstance(payload, dict):
        for key in ("workspaces", "data", "items"):
            value = payload.get(key)
            if isinstance(value, list):
                return value
        return []
    return payload if isinstance(payload, list) else []


# --------------------------------------------------------------------------- #
# 工具
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_workspaces() -> Any:
    """列出 AnythingLLM 中的所有工作区信息。

    返回每个工作区的 id、name、slug、创建时间等字段。
    后续凡是需要 workspace slug 的工具（如 list_documents），都先用本工具取值。
    """
    return _workspaces(call("/api/workspaces"))


@mcp.tool()
def get_workspace(slug: str) -> Any:
    """按 slug 查询单个工作区的详细信息（含已嵌入的文档列表、聊天设置等）。

    Args:
        slug: 工作区标识，可先用 list_workspaces 获取。课堂约定「第一个工作区」即列表第一项。
    """
    return call(f"/api/workspace/{slug}")


@mcp.tool()
def list_documents(slug: str) -> Any:
    """列出某个工作区里已经嵌入（可以参与问答）的文档。

    Args:
        slug: 工作区标识。
    """
    payload = call(f"/api/workspace/{slug}")
    if isinstance(payload, dict):
        docs = payload.get("documents") or payload.get("workspace", {}).get("documents")
        if docs is not None:
            return docs
    return payload


@mcp.tool()
def first_workspace() -> Any:
    """直接返回列表中的第一个工作区（课堂任务里常说的「AnythingLLM 的第一个工作区」）。"""
    items = _workspaces(call("/api/workspaces"))
    if not items:
        raise RuntimeError("没有读到任何工作区，请确认 AnythingLLM 已打开且已创建工作区。")
    return items[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="AnythingLLM MCP 服务器")
    parser.add_argument("--stdio", action="store_true", help="以 stdio 方式运行")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8091, help="HTTP 监听端口，默认 8091")
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
        return

    mcp.settings.host = args.host
    mcp.settings.port = args.port
    print(f"[anythingllm-mcp] Streamable HTTP -> http://{args.host}:{args.port}/mcp", flush=True)
    print(f"[anythingllm-mcp] 后端 AnythingLLM -> {_base}", flush=True)
    mcp.run(transport="streamable-http")


if __name__ == "__main__":
    main()
