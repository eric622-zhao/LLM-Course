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
from pathlib import Path
from typing import Any

import httpx

try:  # mcp >= 2.0：FastMCP 改名为 MCPServer
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # 兼容 mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]

    class ToolError(Exception):  # type: ignore[no-redef]
        """1.x 没有专门的工具错误类型。"""


# 工具里抛 ToolError，框架才会把可读信息透传给模型；
# 抛普通异常只会在客户端看到一句 "Error executing tool xxx"。


DEFAULT_API = os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001").rstrip("/")
API_KEY = os.environ.get("ANYTHINGLLM_API_KEY", "").strip()
TIMEOUT = float(os.environ.get("ANYTHINGLLM_TIMEOUT", "15"))

mcp = MCPServer(
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
        raise ToolError(
            f"连接 AnythingLLM 失败（{_base}）。请先打开 AnythingLLM 桌面端再重试。"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolError(f"请求超时：GET {path}") from exc

    if resp.status_code == 401:
        raise ToolError("AnythingLLM 返回 401：该接口需要鉴权，请设置 ANYTHINGLLM_API_KEY。")
    if resp.status_code >= 400:
        raise ToolError(f"AnythingLLM 返回 {resp.status_code}：{resp.text[:300]}")

    try:
        return resp.json()
    except ValueError:
        return resp.text


def v1(method: str, path: str, **kwargs: Any) -> Any:
    """调用 AnythingLLM Developer API（`/api/v1/*`）。

    与上面 `call()` 的分工：
      - `/api/*`      前端自用路由，本地通常免鉴权，但字段随版本漂移，
                      只用来做「读」——列工作区、看文档。
      - `/api/v1/*`   官方文档承诺的开发者接口，需要 API Key，结构稳定。
                      所有「写」操作（上传、向量化、对话）都走这里。

    Key 未配置时直接给出获取步骤，而不是让请求以 401 失败 ——
    模型看到「去哪拿 Key」才知道下一步该做什么。
    """
    if not API_KEY:
        raise ToolError(
            "该操作需要 AnythingLLM 的 API Key。\n"
            "获取方式：打开 AnythingLLM → 左下角设置（齿轮）→ API Keys → 新建，"
            "然后设置环境变量 ANYTHINGLLM_API_KEY=<你的 key> 并重启本 server。\n"
            "（只读工具如 list_workspaces 不需要 Key，可以先试用那些。）"
        )

    try:
        resp = client().request(method, path, **kwargs)
    except httpx.ConnectError as exc:
        raise ToolError(
            f"连接 AnythingLLM 失败（{_base}）。请先打开 AnythingLLM 桌面端再重试。"
        ) from exc
    except httpx.TimeoutException as exc:
        raise ToolError(f"请求超时：{method} {path}（当前上限 {TIMEOUT:.0f}s）") from exc

    if resp.status_code == 401:
        raise ToolError(
            "AnythingLLM 返回 401：API Key 无效或已失效。"
            "请到设置 → API Keys 重新生成，并更新环境变量 ANYTHINGLLM_API_KEY。"
        )
    if resp.status_code == 403:
        raise ToolError("AnythingLLM 返回 403：当前 API Key 没有该操作的权限。")

    try:
        body = resp.json()
    except ValueError:
        if resp.status_code >= 400:
            raise ToolError(
                f"AnythingLLM 返回 {resp.status_code}，且响应不是 JSON：{resp.text[:200]}"
            ) from None
        return resp.text

    if resp.status_code >= 400:
        # 把上游的 error 字段提出来，别把整个 JSON 甩给模型
        detail = body.get("error") or body.get("message") if isinstance(body, dict) else None
        raise ToolError(
            f"AnythingLLM 返回 {resp.status_code}：{detail or str(body)[:200]}"
        )
    return body


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
        raise ToolError("没有读到任何工作区，请确认 AnythingLLM 已打开且已创建工作区。")
    return items[0]


# --------------------------------------------------------------------------- #
# 文档上传与向量化（Developer API，需要 API Key）
# --------------------------------------------------------------------------- #
MAX_UPLOAD_MB = float(os.environ.get("ANYTHINGLLM_MAX_UPLOAD_MB", "50"))


@mcp.tool()
def upload_document(path: str, workspace: str = "") -> Any:
    """把本地文件上传到 AnythingLLM 的文档库，可选直接向量化进某个工作区。

    上传成功后 AnythingLLM 会给文件加一段 hash 后缀重命名（例如
    `report.pdf-3f2a....json`），返回里的 location 就是后续向量化要用的标识。

    Args:
        path: 本地文件路径，绝对路径或相对于当前工作目录的路径都行。
            支持 pdf / docx / txt / md / xlsx 等，具体见 accepted_file_types。
        workspace: 目标工作区 slug。填了就上传完立刻向量化进该工作区；
            留空则只进文档库，需要时再调 embed_documents。

    注意：本工具只接受本地文件，不抓取网址 —— 要导入网页请先自行下载。
    """
    target = Path(path).expanduser()
    if not target.is_file():
        raise ToolError(
            f"文件不存在或不是普通文件：{path}\n"
            "请确认路径拼写，或先用 listDirectory 类工具看看目录里有什么。"
        )

    size_mb = target.stat().st_size / (1024 * 1024)
    if size_mb > MAX_UPLOAD_MB:
        raise ToolError(
            f"文件过大：{size_mb:.1f} MB，超过上限 {MAX_UPLOAD_MB:.0f} MB。"
            "可设置环境变量 ANYTHINGLLM_MAX_UPLOAD_MB 调整。"
        )

    with target.open("rb") as fh:
        payload = v1(
            "POST",
            "/api/v1/document/upload",
            files={"file": (target.name, fh, "application/octet-stream")},
        )

    # 统一取出上传结果的各项标识，字段名在 1.6/1.7 之间略有差异
    doc = payload.get("document") if isinstance(payload, dict) else None
    if not isinstance(doc, dict):
        doc = {}
    location = doc.get("location")
    if not location and isinstance(payload, dict):
        documents = payload.get("documents")
        if isinstance(documents, list) and documents and isinstance(documents[0], dict):
            location = documents[0].get("location")
    result: dict[str, Any] = {
        "uploaded": target.name,
        "sizeMB": round(size_mb, 2),
        "location": location,
        "raw": payload,
    }

    if workspace and location:
        result["embedded"] = v1(
            "POST",
            f"/api/v1/workspace/{workspace}/update-embeddings",
            json={"adds": [location], "deletes": []},
        )
        result["embeddedInto"] = workspace

    if not location:
        result["hint"] = "上游未返回 location，无法自动向量化；请从 raw 字段找到文件标识。"
    return result


@mcp.tool()
def embed_documents(slug: str, adds: list[str], deletes: list[str] | None = None) -> Any:
    """触发向量化：把文档库里已有的文件加入工作区，或从工作区移除。

    模型能读到 ≠ 模型能用上。文件必须向量化进工作区后，才能在该工作区的对话里被检索到。

    Args:
        slug: 工作区标识，先用 list_workspaces 取。
        adds: 要加入的文档 location 列表，形如
            `custom-documents/report.pdf-3f2a....json`（由 upload_document 返回）。
        deletes: 要从工作区移除的文档 location 列表，可省略。
    """
    if not adds and not deletes:
        raise ToolError("adds 和 deletes 都是空的，没有可执行的操作。")

    return v1(
        "POST",
        f"/api/v1/workspace/{slug}/update-embeddings",
        json={"adds": adds or [], "deletes": deletes or []},
    )


# --------------------------------------------------------------------------- #
# 检索问答
# --------------------------------------------------------------------------- #
@mcp.tool()
def chat(slug: str, message: str, mode: str = "query", session_id: str = "") -> Any:
    """在某工作区里提问，由 AnythingLLM 检索该工作区的文档后作答。

    Args:
        slug: 工作区标识。
        message: 用户的问题。
        mode: 两种模式，语义不同，别混用 ——
            "query"（默认）只检索文档、让模型基于文档回答，并返回引用来源，
                适合「资料里怎么写的」这类问题，答案不易编造。
            "chat" 允许模型结合自身知识自由回答，适合闲聊或文档里没有的追问。
        session_id: 会话标识。留空则是一次性问答（无上下文）；
            想多轮追问时，把上一次返回里的 sessionId 再传进来。

    返回里的 textResponse 是回答正文，sources 是命中的文档片段，
    id 字段即后续可复用的 sessionId。
    """
    mode = mode.lower().strip()
    if mode not in ("query", "chat"):
        raise ToolError(f'mode 只能是 "query" 或 "chat"，收到的是 {mode!r}。')

    body: dict[str, Any] = {"message": message, "mode": mode}
    if session_id:
        body["sessionId"] = session_id

    payload = v1("POST", f"/api/v1/workspace/{slug}/chat", json=body)

    # 只把模型真正要用的字段挑出来，附上完整原文备查
    if isinstance(payload, dict):
        return {
            "answer": payload.get("textResponse"),
            "sources": [
                {"title": s.get("title"), "score": s.get("score"), "snippet": s.get("text")}
                for s in (payload.get("sources") or [])
                if isinstance(s, dict)
            ],
            "sessionId": payload.get("id") or session_id,
            "mode": mode,
            "raw": payload,
        }
    return payload


# --------------------------------------------------------------------------- #
# 文档库浏览（分页）
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_all_documents(limit: int = 20, offset: int = 0) -> Any:
    """分页浏览 AnythingLLM 文档库里的全部文件（不只是某个工作区的）。

    list_documents 看的是「某个工作区里已向量化的」，本工具看的是「库里的全部」，
    两者的差集就是「上传了但还没向量化」的文件 —— 排查「为什么模型读不到」时很有用。

    Args:
        limit: 每页条数，1~100，默认 20。
        offset: 跳过前面多少条，用于翻页。
    """
    if not 1 <= limit <= 100:
        raise ToolError(f"limit 需在 1~100 之间，收到 {limit}。")
    if offset < 0:
        raise ToolError(f"offset 不能为负，收到 {offset}。")

    payload = v1(
        "GET",
        "/api/v1/documents",
        params={"limit": limit, "offset": offset},
    )

    if isinstance(payload, dict):
        items = payload.get("localFiles") or payload.get("documents") or []
        return {
            "total": len(items),
            "limit": limit,
            "offset": offset,
            "items": items,
            "hint": (
                "还有更多时把 offset 增加 limit 再查。"
                "要向量化进工作区，用 embed_documents 并传入条目的 location。"
            ),
        }
    return payload


@mcp.tool()
def accepted_file_types() -> Any:
    """列出 AnythingLLM 当前接受的上传文件类型（扩展名）。

    上传前先查一次，比事后收到「不支持的格式」更好排查。
    """
    return v1("GET", "/api/v1/document/accepted-file-types")


def main() -> None:
    parser = argparse.ArgumentParser(description="AnythingLLM MCP 服务器")
    parser.add_argument("--stdio", action="store_true", help="以 stdio 方式运行")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8091, help="HTTP 监听端口，默认 8091")
    args = parser.parse_args()

    if args.stdio:
        mcp.run(transport="stdio")
        return

    print(f"[anythingllm-mcp] Streamable HTTP -> http://{args.host}:{args.port}/mcp", flush=True)
    print(f"[anythingllm-mcp] 后端 AnythingLLM -> {_base}", flush=True)
    mcp.run(transport="streamable-http", host=args.host, port=args.port)


if __name__ == "__main__":
    main()
