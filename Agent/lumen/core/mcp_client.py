# -*- coding: utf-8 -*-
"""
core.mcp_client · 作为 MCP 客户端接入外部 server
================================================

MCP（Model Context Protocol）让「工具的实现」和「工具的调用方」解耦：
别人写好的 server，本 Agent 连上去就能用，不需要把代码搬进来。

这一节实现的是**客户端**那一半：

    读配置 → 拉起/连接 server → tools/list 拿到工具清单
           → 把 MCP 的 inputSchema 转成 OpenAI function schema
           → 并入本地可调用集合（名字带 server 前缀，标明来源）
           → 模型调用时，转发给对应 server 执行

两种传输
--------
- **stdio**：本地进程，通过标准输入输出说 JSON-RPC。
  适合「本机有个 server.py」的情况，不用开端口、不用管防火墙。
- **streamable HTTP / SSE**：连远程或已启动的 HTTP server。
  适合 server 跑在别的机器、或需要多客户端共享。

为什么要在后台线程里跑一个事件循环
----------------------------------
mcp SDK 是全异步的，而本项目的 Agent 主循环是同步的（TUI 里还要跑在工作线程）。
最简单的桥接是：**一个常驻线程 + 一个常驻事件循环**，
主线程用 `run_coroutine_threadsafe` 把协程丢过去执行。
这样 session 能长期存活（MCP 是有状态的），也避免了「每次调用重建连接」。

失败必须降级
------------
server 连不上、超时、进程起不来 —— 都只记录状态，**绝不阻塞启动**。
侧栏能看到每个 server 的状态，模型也能通过 mcpStatus 工具查。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from core import config
from .registry import TOOLS, ToolSpec

# MCP 是可选依赖：没装不应该让整个程序起不来
try:  # pragma: no cover - 取决于环境
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    try:
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:  # 老版本 SDK
        streamable_http_client = None  # type: ignore[assignment]
    try:
        from mcp.client.sse import sse_client
    except ImportError:
        sse_client = None  # type: ignore[assignment]

    MCP_AVAILABLE = True
except ImportError:  # pragma: no cover
    MCP_AVAILABLE = False
    ClientSession = None  # type: ignore[assignment]
    StdioServerParameters = None  # type: ignore[assignment]
    stdio_client = None  # type: ignore[assignment]
    streamable_http_client = None  # type: ignore[assignment]
    sse_client = None  # type: ignore[assignment]


TOOL_PREFIX = "mcp__"
MAX_TOOL_NAME = 64          # OpenAI function name 上限
DEFAULT_CONNECT_TIMEOUT = 20.0
DEFAULT_CALL_TIMEOUT = 60.0

_ENV_REF = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class ServerConfig:
    name: str
    transport: str = "stdio"
    command: str = ""
    args: list[str] = field(default_factory=list)
    env: dict[str, str] = field(default_factory=dict)
    cwd: str | None = None
    url: str = ""
    headers: dict[str, str] = field(default_factory=dict)
    enabled: bool = True
    connect_timeout: float = DEFAULT_CONNECT_TIMEOUT
    call_timeout: float = DEFAULT_CALL_TIMEOUT

    def describe(self) -> str:
        if self.transport == "stdio":
            return f"stdio · {self.command} {' '.join(self.args)}".strip()
        return f"{self.transport} · {self.url}"


@dataclass
class ServerState:
    config: ServerConfig
    status: str = "pending"          # pending / ready / failed / disabled
    error: str = ""
    tools: list[Any] = field(default_factory=list)
    session: Any = None
    connected_at: float = 0.0
    stop_event: Any = None

    @property
    def name(self) -> str:
        return self.config.name


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------
def _expand(value: str) -> str:
    """把 ${ENV_VAR} 展开成环境变量，避免把密钥写进配置文件后提交上去。"""
    if not isinstance(value, str) or "${" not in value:
        return value

    def repl(match: re.Match[str]) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return _ENV_REF.sub(repl, value)


def _as_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in ("0", "false", "no", "off", "disabled")


def _parse_server(name: str, raw: dict, project_root: str) -> ServerConfig | None:
    if not isinstance(raw, dict):
        return None
    if not _as_bool(raw.get("enabled"), True) and not raw.get("disabled"):
        # enabled=true 正常；enabled=false 或 disabled=true 都算关闭
        return None
    if _as_bool(raw.get("disabled"), False):
        return None
    if raw.get("enabled") is not None and not _as_bool(raw.get("enabled"), True):
        return None

    url = _expand(str(raw.get("url") or "").strip())
    command = _expand(str(raw.get("command") or "").strip())

    transport = str(raw.get("transport") or raw.get("type") or "").strip().lower()
    if not transport:
        transport = "http" if url else "stdio"
    if transport in ("streamable_http", "streamable-http", "streamablehttp"):
        transport = "http"

    if transport == "stdio" and not command:
        return None
    if transport in ("http", "sse") and not url:
        return None

    args = [_expand(str(a)) for a in (raw.get("args") or [])]
    env = {str(k): _expand(str(v)) for k, v in (raw.get("env") or {}).items()}
    headers = {str(k): _expand(str(v)) for k, v in (raw.get("headers") or {}).items()}

    cwd = raw.get("cwd")
    if cwd:
        cwd = _expand(str(cwd))
        if not os.path.isabs(cwd):
            cwd = os.path.join(project_root, cwd)

    # 相对路径的 command（如 ./server.py）基于项目根目录，而不是进程 CWD 以外的地方
    if command and os.sep in command and not os.path.isabs(command):
        command = os.path.join(project_root, command)

    def _num(key: str, default: float) -> float:
        try:
            return float(raw.get(key, default))
        except (TypeError, ValueError):
            return default

    return ServerConfig(
        name=name,
        transport=transport,
        command=command,
        args=args,
        env=env,
        cwd=cwd,
        url=url,
        headers=headers,
        enabled=True,
        connect_timeout=_num("connect_timeout", DEFAULT_CONNECT_TIMEOUT),
        call_timeout=_num("call_timeout", DEFAULT_CALL_TIMEOUT),
    )


def config_path(project_root: str) -> str:
    """mcp.json 的位置，可在 config.ini 的 [mcp] config 里改。"""
    configured = (config.get("mcp", "config") or "").strip()
    if configured:
        return configured if os.path.isabs(configured) else os.path.join(project_root, configured)
    return os.path.join(project_root, "mcp.json")


def load_config(project_root: str) -> tuple[dict[str, ServerConfig], list[str]]:
    """读取 mcp.json，返回 (server 配置表, 告警列表)。文件不存在是正常情况。"""
    warnings: list[str] = []
    path = config_path(project_root)

    if not os.path.isfile(path):
        return {}, warnings

    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, json.JSONDecodeError) as exc:
        return {}, [f"读取 {os.path.basename(path)} 失败：{exc}"]

    if not isinstance(data, dict):
        return {}, [f"{os.path.basename(path)} 的顶层必须是对象"]

    # 同时认 {"mcpServers": {...}}（Claude Desktop 习惯）和 {"servers": {...}}
    raw_servers = data.get("mcpServers") or data.get("servers") or data.get("mcp_servers") or {}
    if not isinstance(raw_servers, dict):
        return {}, [f"{os.path.basename(path)} 里的 servers 必须是对象"]

    servers: dict[str, ServerConfig] = {}
    for name, raw in raw_servers.items():
        # 以 _ 开头的键是文档说明（JSON 没有注释，只能这么写），跳过
        if str(name).startswith("_"):
            continue
        parsed = _parse_server(str(name), raw, project_root)
        if parsed is None:
            if isinstance(raw, dict) and not _as_bool(raw.get("enabled"), True):
                warnings.append(f"[{name}] 已禁用，跳过")
            else:
                warnings.append(f"[{name}] 配置不完整（stdio 需要 command，http 需要 url），跳过")
            continue
        servers[parsed.name] = parsed

    return servers, warnings


# ---------------------------------------------------------------------------
# schema 转换
# ---------------------------------------------------------------------------
def _slug(text: str, limit: int) -> str:
    """把任意名字压成 OpenAI function name 允许的字符集。"""
    out = re.sub(r"[^A-Za-z0-9_-]+", "_", str(text or "")).strip("_")
    if not out:
        out = "x"
    if out[0].isdigit():
        out = f"t_{out}"
    return out[:limit]


def qualified_name(server: str, tool: str) -> str:
    """带来源前缀的工具名：mcp__<server>__<tool>。前缀让模型能分清工具出处。"""
    return f"{TOOL_PREFIX}{_slug(server, 16)}__{_slug(tool, MAX_TOOL_NAME - len(TOOL_PREFIX) - 18)}"


def _normalize_schema(tool: Any) -> dict:
    """MCP 的 inputSchema → OpenAI function 的 parameters。"""
    schema = getattr(tool, "inputSchema", None) or getattr(tool, "input_schema", None) or {}
    if not isinstance(schema, dict) or not schema:
        return {"type": "object", "properties": {}, "required": []}

    params: dict[str, Any] = dict(schema)
    # 少数 server 会漏掉 type，OpenAI 侧要求必须有 object
    if params.get("type") != "object":
        params = {
            "type": "object",
            "properties": params.get("properties", {}) if isinstance(params.get("properties"), dict) else {},
            "required": params.get("required", []),
        }
    params.setdefault("properties", {})
    params.setdefault("required", [])
    return params


def _tool_description(server: str, tool: Any) -> str:
    """在描述里标明来源，便于模型与用户判断这个工具是谁提供的。"""
    raw = str(getattr(tool, "description", "") or "").strip()
    head = f"[MCP · {server}]"
    return f"{head} {raw}" if raw else f"{head} 由 MCP server「{server}」提供的工具。"


def _content_to_text(result: Any) -> str:
    """把 CallToolResult 的 content 列表拼成人话。

    MCP SDK 的字段命名在 1.x 是 camelCase、2.x 改成了 snake_case，
    这里两种都试一遍，免得升一次库就解析不出来。
    """
    blocks = getattr(result, "content", None) or []
    parts: list[str] = []

    for block in blocks:
        kind = getattr(block, "type", "") or ""
        if kind == "text" or hasattr(block, "text"):
            text = getattr(block, "text", None)
            if text:
                parts.append(str(text))
        elif kind == "image":
            mime = getattr(block, "mimeType", None) or getattr(block, "mime_type", "image")
            parts.append(f"（图片内容：{mime}，当前环境无法直接查看）")
        elif kind == "resource":
            resource = getattr(block, "resource", None)
            text = _attr(resource, "text") if resource else None
            parts.append(str(text) if text else "（资源内容）")
        else:
            text = getattr(block, "text", None)
            parts.append(str(text) if text else f"（{kind or '未知'} 类型内容）")

    out = "\n".join(p for p in parts if p).strip()
    if not out:
        # 有些 server 只返回结构化结果
        structured = _attr(result, "structured_content", "structuredContent")
        if structured:
            out = json.dumps(structured, ensure_ascii=False, indent=2)
    if not out:
        out = "（server 返回了空结果）"

    if _attr(result, "is_error", "isError", default=False):
        return f"MCP server 报告执行出错：\n{out}"
    return out


def _attr(obj: Any, *names: str, default: Any = None) -> Any:
    """按顺序取第一个存在的属性，兼容 camelCase / snake_case。"""
    for name in names:
        value = getattr(obj, name, None)
        if value is not None:
            return value
    return default


# ---------------------------------------------------------------------------
# 管理器
# ---------------------------------------------------------------------------
class MCPManager:
    """持有所有 server 的连接、工具清单与状态。"""

    def __init__(self, project_root: str) -> None:
        self.root = os.path.realpath(project_root)
        self.servers: dict[str, ServerConfig] = {}
        self.states: dict[str, ServerState] = {}
        self.warnings: list[str] = []

        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._started = threading.Event()
        self._stopped = False
        # qualified name -> (server, 原始 tool 名)
        self._call_map: dict[str, tuple[str, str]] = {}

    # -- 生命周期 -----------------------------------------------------------
    def start(self, block: bool = True) -> "MCPManager":
        """读取配置并在后台线程连接所有 server。

        block=False 时不等待连接完成，用于「启动界面优先」的场景。
        """
        if not MCP_AVAILABLE:
            self.warnings.append("未安装 mcp 库（pip install mcp），MCP 功能不可用")
            self._started.set()
            return self

        if not _enabled():
            self._started.set()
            return self

        self.servers, self.warnings = load_config(self.root)
        if not self.servers:
            self._started.set()
            return self

        for name, cfg in self.servers.items():
            self.states[name] = ServerState(config=cfg)

        self._thread = threading.Thread(target=self._thread_main, name="lumen-mcp", daemon=True)
        self._thread.start()

        if block:
            self._started.wait(timeout=max(c.connect_timeout for c in self.servers.values()) + 10)
        return self

    def _thread_main(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        try:
            self._loop.run_until_complete(self._bootstrap())
        except Exception as exc:  # noqa: BLE001 - 后台线程兜底，不能让主线程崩
            self.warnings.append(f"MCP 后台线程异常：{exc}")
        finally:
            self._started.set()
        if self._stopped:
            return
        try:
            self._loop.run_forever()
        finally:
            try:
                self._loop.close()
            except Exception:  # noqa: BLE001
                pass

    async def _bootstrap(self) -> None:
        """为每个 server 起一个常驻协程，并等待它们各自就绪。"""
        tasks = []
        for name, cfg in self.servers.items():
            state = self.states[name]
            state.stop_event = asyncio.Event()
            tasks.append(asyncio.create_task(self._serve(state), name=f"mcp-{name}"))

        # 各自等待就绪（带超时），互不阻塞
        await asyncio.gather(
            *(self._await_ready(state) for state in self.states.values()),
            return_exceptions=True,
        )

    async def _await_ready(self, state: ServerState) -> None:
        deadline = time.monotonic() + state.config.connect_timeout
        while state.status == "pending" and time.monotonic() < deadline:
            await asyncio.sleep(0.05)
        if state.status == "pending":
            state.status = "failed"
            state.error = f"连接超时（{state.config.connect_timeout:.0f} 秒）"

    async def _serve(self, state: ServerState) -> None:
        """一个 server 的完整生命周期：连接 → 初始化 → 列工具 → 挂起 → 关闭。"""
        cfg = state.config
        try:
            if cfg.transport == "stdio":
                await self._serve_stdio(state)
            elif cfg.transport == "sse":
                await self._serve_sse(state)
            else:
                await self._serve_http(state)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 单个 server 失败不影响其它
            state.status = "failed"
            state.error = _friendly_error(exc)
        finally:
            state.session = None
            if state.status == "pending":
                state.status = "failed"
                state.error = state.error or "未能建立连接"

    async def _serve_stdio(self, state: ServerState) -> None:
        cfg = state.config
        env = dict(cfg.env)
        # SDK 只继承白名单变量，这里补两个 Windows 上常被漏掉的
        for key in ("COMSPEC", "WINDIR", "NUMBER_OF_PROCESSORS"):
            if key in os.environ and key not in env:
                env.setdefault(key, os.environ[key])

        params = StdioServerParameters(
            command=cfg.command,
            args=cfg.args,
            env=env or None,
            cwd=cfg.cwd,
        )

        # server 的 stderr 必须导走：直接继承父进程的话，
        # 它抛异常时的整段 traceback 会糊到 TUI 上，把界面冲花。
        errlog, owns = self._server_errlog(state)
        try:
            async with stdio_client(params, errlog=errlog) as streams:
                await self._run_session(state, streams)
        finally:
            if owns:
                try:
                    errlog.close()
                except Exception:  # noqa: BLE001
                    pass

    def _server_errlog(self, state: ServerState) -> tuple[Any, bool]:
        """把 server 的 stderr 落到 .lumen/mcp/<name>.log，排障时能翻。"""
        try:
            directory = os.path.join(self.root, ".lumen", "mcp")
            os.makedirs(directory, exist_ok=True)
            path = os.path.join(directory, f"{_slug(state.name, 32)}.log")
            return open(path, "a", encoding="utf-8", errors="replace"), True
        except OSError:
            try:
                return open(os.devnull, "w", encoding="utf-8"), True
            except OSError:
                return None, False

    async def _serve_http(self, state: ServerState) -> None:
        cfg = state.config
        if streamable_http_client is None:
            raise RuntimeError("当前 mcp 版本不支持 streamable HTTP 传输，请升级 mcp 库")

        client = None
        if cfg.headers:
            try:
                import httpx

                client = httpx.AsyncClient(headers=cfg.headers, timeout=cfg.call_timeout)
            except ImportError:
                pass
        try:
            async with streamable_http_client(cfg.url, http_client=client) as streams:
                await self._run_session(state, streams)
        finally:
            if client is not None:
                await client.aclose()

    async def _serve_sse(self, state: ServerState) -> None:
        cfg = state.config
        if sse_client is None:
            raise RuntimeError("当前 mcp 版本不支持 SSE 传输")
        async with sse_client(cfg.url, headers=cfg.headers or None) as streams:
            await self._run_session(state, streams)

    async def _run_session(self, state: ServerState, streams: Any) -> None:
        read, write = streams[0], streams[1]
        async with ClientSession(read, write) as session:
            await session.initialize()
            listing = await session.list_tools()

            state.tools = list(getattr(listing, "tools", []) or [])
            state.session = session
            state.status = "ready"
            state.connected_at = time.time()
            state.error = ""

            self._register_tools(state)

            # 挂起，直到 stop() 触发。session 必须在这个 async with 内部保持存活。
            await state.stop_event.wait()

    # -- 工具注册 -----------------------------------------------------------
    def _register_tools(self, state: ServerState) -> None:
        """把 MCP 工具并入全局注册表，名字带来源前缀。"""
        for tool in state.tools:
            original = str(getattr(tool, "name", "") or "").strip()
            if not original:
                continue
            qname = qualified_name(state.name, original)
            if qname in TOOLS:
                # 极端情况：两个 server 有同名工具且 slug 后撞车
                suffix = 2
                while f"{qname}_{suffix}" in TOOLS:
                    suffix += 1
                qname = f"{qname}_{suffix}"

            TOOLS[qname] = ToolSpec(
                name=qname,
                description=_tool_description(state.name, tool),
                parameters=_normalize_schema(tool),
                func=self._make_caller(qname),
                title=f"{state.name}/{original}",
                category="MCP",
            )
            self._call_map[qname] = (state.name, original)

    def _make_caller(self, qname: str):
        def _call(args: dict) -> str:
            return self.call(qname, args)

        return _call

    # -- 调用 ---------------------------------------------------------------
    def call(self, qname: str, arguments: dict) -> str:
        """同步调用一个 MCP 工具（主线程用）。"""
        mapping = self._call_map.get(qname)
        if mapping is None:
            return f"错误：未知的 MCP 工具 {qname}。"

        server_name, tool_name = mapping
        state = self.states.get(server_name)
        if state is None or state.status != "ready" or state.session is None:
            detail = state.error if state and state.error else "未连接"
            return (
                f"错误：MCP server「{server_name}」当前不可用（{detail}）。\n"
                f"建议：告知用户该 server 未连上，改用本地工具或其它方案，不要重复重试。"
            )
        if self._loop is None or self._loop.is_closed():
            return f"错误：MCP 事件循环不可用，无法调用「{server_name}」。"

        timeout = state.config.call_timeout
        try:
            future = asyncio.run_coroutine_threadsafe(
                state.session.call_tool(tool_name, arguments or {}), self._loop
            )
        except RuntimeError as exc:
            return f"错误：提交 MCP 调用失败 — {exc}"

        try:
            result = future.result(timeout=timeout)
        except TimeoutError:
            future.cancel()
            return (
                f"错误：调用 MCP 工具 {tool_name}（server「{server_name}」）超时（{timeout:.0f} 秒）。\n"
                f"建议：可能该操作本身很慢，或 server 卡住了。可以先把情况告诉用户，不要立刻重试。"
            )
        except Exception as exc:  # noqa: BLE001
            return f"错误：MCP 调用失败 — {type(exc).__name__}: {exc}"

        return _content_to_text(result)

    # -- 状态 ---------------------------------------------------------------
    def status_rows(self) -> list[tuple[str, str, str]]:
        """[(server, 状态, 说明)]，给 TUI 侧栏与 CLI 启动信息用。"""
        rows: list[tuple[str, str, str]] = []
        for name, state in self.states.items():
            count = len(state.tools)
            if state.status == "ready":
                detail = f"{count} 个工具 · {state.config.transport}"
            elif state.status == "failed":
                detail = state.error or "连接失败"
            else:
                detail = "连接中…"
            rows.append((name, state.status, detail))
        return rows

    def summary(self) -> str:
        if not MCP_AVAILABLE:
            return "未安装 mcp 库"
        if not _enabled():
            return "已在配置中关闭"
        if not self.states:
            return "未配置 server"
        ready = sum(1 for s in self.states.values() if s.status == "ready")
        total = len(self.states)
        tools = sum(len(s.tools) for s in self.states.values())
        return f"{ready}/{total} 个 server 已连接 · {tools} 个工具"

    def stop(self) -> None:
        """关闭所有连接并结束后台线程。"""
        self._stopped = True
        if self._loop is None or self._loop.is_closed():
            return
        for state in self.states.values():
            if state.stop_event is not None:
                try:
                    self._loop.call_soon_threadsafe(state.stop_event.set)
                except RuntimeError:
                    pass
        try:
            self._loop.call_soon_threadsafe(self._loop.stop)
        except RuntimeError:
            pass
        if self._thread is not None:
            self._thread.join(timeout=5)


def _friendly_error(exc: BaseException) -> str:
    """把 SDK 内部异常翻译成用户能看懂的一句话。"""
    # anyio 的 TaskGroup 会把真实异常包在 ExceptionGroup 里，
    # 直接 str() 只能看到 "unhandled errors in a TaskGroup" —— 往里挖一层。
    if isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        inner = _friendly_error(exc.exceptions[0])
        if len(exc.exceptions) == 1:
            return inner
        return f"{inner}（另有 {len(exc.exceptions) - 1} 个并发错误）"

    text = str(exc).strip() or type(exc).__name__
    low = text.lower()
    if isinstance(exc, FileNotFoundError) or "no such file" in low or "cannot find" in low:
        return f"启动命令不存在：{text}"
    if "connection refused" in low or "connect error" in low or "all connection attempts failed" in low:
        return f"连接被拒绝（服务未启动或端口不对）：{text[:200]}"
    if "timeout" in low or "timed out" in low:
        return f"连接超时：{text[:200]}"
    if "permission" in low or "access is denied" in low:
        return f"没有权限：{text[:200]}"
    if "401" in low or "unauthorized" in low:
        return f"鉴权失败（检查 headers/token）：{text[:200]}"
    return f"{type(exc).__name__}: {text[:300]}"


def _enabled() -> bool:
    raw = config.get("mcp", "enabled")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


# ---------------------------------------------------------------------------
# 全局实例
# ---------------------------------------------------------------------------
_manager: MCPManager | None = None


def start(project_root: str, block: bool = True) -> MCPManager:
    global _manager
    if _manager is not None:
        return _manager
    _manager = MCPManager(project_root).start(block=block)
    return _manager


def get_manager() -> MCPManager | None:
    return _manager


def stop() -> None:
    global _manager
    if _manager is not None:
        _manager.stop()
        _manager = None
