# -*- coding: utf-8 -*-
"""
tui.app · Textual 界面
======================

界面结构：

    ┌ 顶栏 ────────────────────────────────────────────┐
    │ ◆ 流明 LUMEN              model · 工具数 · 版本   │
    ├─────────────────────────────┬────────────────────┤
    │ 对话区（滚动）               │ 侧栏：工具 / 状态   │
    ├─────────────────────────────┴────────────────────┤
    │ » 输入框                                          │
    ├───────────────────────────────────────────────────┤
    │ Enter 发送 · Ctrl+L 清空 · Ctrl+Q 退出            │
    └───────────────────────────────────────────────────┘

关键实现点
----------
* LLM 请求跑在**工作线程**里（run_worker(thread=True)），否则会卡住界面。
* 线程里不能直接改 UI，必须用 self.call_from_thread(...) 回到 UI 线程。
* 流式增量不是每个 token 都重绘 Markdown —— 那样太贵，
  这里按 ~80ms 合并一次，肉眼几乎无感，CPU 占用低一个量级。
"""

from __future__ import annotations

import json
import time
import unicodedata

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.widgets import Input, Markdown, Static

from core import __version__, config, report
from core.agent import Agent
from core.registry import TOOLS

FLUSH_INTERVAL = 0.08  # 流式刷新的最小间隔（秒）


# ---------------------------------------------------------------------------
# 小组件
# ---------------------------------------------------------------------------
class UserMessage(Static):
    """用户提问气泡。markup=False 防止文本里的方括号被当成标签。"""

    def __init__(self, text: str) -> None:
        super().__init__(text, classes="msg user", markup=False)


class AgentMessage(Vertical):
    """助手回答气泡，内部是一个可增量更新的 Markdown。"""

    def __init__(self) -> None:
        super().__init__(classes="msg agent")
        self._plain = ""

    def compose(self) -> ComposeResult:
        yield Markdown("", classes="agent-body")

    def set_text(self, text: str) -> None:
        self._plain = text
        try:
            self.query_one(Markdown).update(text)
        except Exception:  # noqa: BLE001 - 组件还没挂载完时忽略
            pass

    @property
    def plain(self) -> str:
        return self._plain


class ToolCallLine(Static):
    def __init__(self, name: str, arguments: str) -> None:
        super().__init__(f"» {name}({_short_args(arguments)})", classes="tool-call", markup=False)


class ToolResultLine(Static):
    def __init__(self, name: str, result: str) -> None:
        summary = _first_line(result)
        super().__init__(f"└ {name} · {summary}", classes="tool-result", markup=False)


class ErrorLine(Static):
    def __init__(self, text: str) -> None:
        super().__init__(text, classes="error", markup=False)


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _short_args(raw: str, limit: int = 72) -> str:
    try:
        parsed = json.loads(raw) if raw and raw.strip() else {}
        text = json.dumps(parsed, ensure_ascii=False) if parsed else ""
    except (json.JSONDecodeError, TypeError):
        text = raw or ""
    text = text.strip()
    if text in ("{}", ""):
        return ""
    return text if len(text) <= limit else text[:limit] + "…"


def _first_line(text: str, limit: int = 96) -> str:
    for line in (text or "").splitlines():
        stripped = line.strip()
        if stripped:
            return stripped if len(stripped) <= limit else stripped[:limit] + "…"
    return "（无输出）"


def _char_width(ch: str) -> int:
    """终端列宽：东亚全角字符占 2 列，其余占 1 列。"""
    return 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1


def _fit(text: str, columns: int) -> str:
    """按**显示列数**截断（不是字符数）。

    中文一个字符顶两列，用 len() 截断会算错宽度：40 个汉字 = 80 列，
    在 40 列的侧栏里必然折行，而且续行没有缩进，看起来就错位了。
    """
    if columns <= 1:
        return ""
    used = 0
    out: list[str] = []
    for ch in text:
        width = _char_width(ch)
        if used + width > columns - 1:
            return "".join(out) + "…"
        out.append(ch)
        used += width
    return text


# ---------------------------------------------------------------------------
# 主应用
# ---------------------------------------------------------------------------
class LumenApp(App):
    CSS_PATH = "app.tcss"
    TITLE = "流明 Lumen"

    BINDINGS = [
        Binding("ctrl+q", "quit", "退出", show=True),
        Binding("ctrl+l", "clear", "清空", show=True),
        Binding("f1", "help", "帮助", show=True),
    ]

    def __init__(
        self,
        project_root: str,
        extra_system: str = "",
        skills: list[tuple[str, str]] | None = None,
        warning: str = "",
        system_provider=None,
        rules=None,
        mcp=None,
        store=None,
        session_id: str = "",
    ) -> None:
        super().__init__()
        self.project_root = project_root
        self.extra_system = extra_system
        # [(name, description)] —— 只用于侧栏展示，正文由 loadSkill 按需读取
        self.skills = skills or []
        self.skill_names = [name for name, _ in self.skills]
        # 配置类问题（缺 api_key 等）：界面照常打开，只在状态区提示
        self.warning = warning

        # 以下由 main.py 注入，缺省时界面仍可独立启动（测试会这么用）
        self.system_provider = system_provider
        self.rules = rules
        self.mcp = mcp
        self.store = store
        self.session_id = session_id

        self.agent: Agent | None = None
        self._init_error = ""
        self._agent_msg: AgentMessage | None = None
        self._acc = ""
        self._last_flush = 0.0
        self._busy = False
        self._turns = 0

    # ------------------------------------------------------------------ 布局
    def compose(self) -> ComposeResult:
        model = config.get("llm", "model") or "-"
        meta = f"{model}  ·  {len(TOOLS)} 个工具"
        if self.skill_names:
            meta += f"  ·  {len(self.skill_names)} 个 Skill"
        meta += f"  ·  v{__version__}"

        with Horizontal(id="topbar"):
            yield Static("◆ 流明 LUMEN", id="brand")
            yield Static(meta, id="meta")

        with Horizontal(id="body"):
            with VerticalScroll(id="log"):
                yield Static(
                    "你好，我是流明。可以查时间、算闰年、读写项目文件，也能执行本地命令、"
                    "抓取网页、联网搜索，并按需加载 Skill。\n"
                    "直接提问即可，/skills 看当前技能，F1 查看快捷键。",
                    classes="welcome",
                    markup=False,
                )
            with VerticalScroll(id="sidebar"):
                yield from self._sidebar()

        with Horizontal(id="prompt-row"):
            yield Static("»", id="prompt-mark")
            yield Input(placeholder="问点什么…（Enter 发送）", id="prompt")

        yield Static("Enter 发送  ·  Ctrl+L 清空上下文  ·  Ctrl+Q 退出", id="hint")

    def _sidebar(self):
        """按类别列出所有工具，再显示状态区。"""
        yield Static("可用工具", classes="side-title")
        grouped: dict[str, list[str]] = {}
        for spec in TOOLS.values():
            grouped.setdefault(spec.category, []).append(f"{spec.title}  ·  {spec.name}")
        for category, items in grouped.items():
            yield Static(f"[{category}]", classes="side-dim")
            for item in items:
                yield Static(f"  {item}", classes="side-item")

        if self.skills:
            yield Static("Skills", classes="side-title")
            for name, description in self.skills:
                yield Static(f"  {_fit(name, 40)}", classes="side-item")
                yield Static(f"    {_fit(description, 38)}", classes="side-dim")

        if self.rules is not None:
            yield Static("项目规则", classes="side-title")
            yield Static(f"  {_fit(self.rules.filename, 38)}", classes="side-item")
            yield Static(
                f"    {self.rules.total_chars} 字符"
                + ("（已截断注入）" if self.rules.truncated else ""),
                classes="side-dim",
            )

        if self.mcp is not None and getattr(self.mcp, "states", None):
            yield Static("MCP", classes="side-title")
            for name, status, detail in self.mcp.status_rows():
                mark = {"ready": "●", "failed": "○"}.get(status, "…")
                css = "side-item" if status == "ready" else "side-dim"
                yield Static(f"  {mark} {_fit(name, 18)}", classes=css)
                yield Static(f"    {_fit(detail, 36)}", classes="side-dim")

        yield Static("状态", classes="side-title")
        yield Static("就绪", classes="side-item", id="status")
        yield Static("轮次 0  ·  tokens 0", classes="side-dim", id="stats")
        if self.session_id:
            yield Static(f"  会话 {self.session_id}", classes="side-dim", id="session")
        if self.warning:
            yield Static("注意", classes="side-title")
            yield Static(f"  {self.warning}", classes="side-warn", markup=False)

    def on_mount(self) -> None:
        try:
            self.agent = Agent(
                extra_system=self.extra_system,
                system_provider=self.system_provider,
                session_store=self.store,
                session_id=self.session_id,
            )
            self.session_id = self.agent.session_id
        except Exception as exc:  # noqa: BLE001 - 依赖缺失/配置错误不该让界面起不来
            self._init_error = f"{type(exc).__name__}: {exc}"
            self._set_status("初始化失败")
            self._show_error(self._init_error)
        else:
            if self.agent.config_problem:
                self._set_status("未配置")
        self.query_one("#prompt", Input).focus()
        self._refresh_stats()

    # ------------------------------------------------------------------ 交互
    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        self.query_one("#prompt", Input).value = ""
        if not text:
            return

        lowered = text.lower()
        if lowered in ("/exit", "/quit"):
            self.exit()
            return
        if lowered == "/clear":
            self.action_clear()
            return
        if lowered in ("/help", "/?"):
            self.action_help()
            return
        if lowered == "/tools":
            await self._system_note(report.tools_report())
            return
        if lowered == "/skills":
            from core import skills as skills_mod

            registry = skills_mod.get_registry()
            info = registry.describe() if registry is not None else self.skills
            await self._system_note(report.skills_report(info))
            return
        if lowered == "/mcp":
            await self._show_mcp()
            return
        if lowered == "/rules":
            await self._show_rules()
            return
        if lowered == "/save":
            await self._save_session()
            return
        if lowered == "/sessions":
            await self._list_sessions()
            return
        if lowered.startswith("/load"):
            await self._load_session(text[5:].strip())
            return
        if lowered == "/new":
            self.action_clear()
            if self.agent:
                self.agent.session_id = ""
            self.session_id = ""
            await self._system_note("已开始新会话，下一轮会存成新文件。")
            return
        if lowered == "/trace":
            await self._show_trace()
            return
        if self._busy:
            await self._system_note("上一条还在处理中，请稍候。")
            return

        await self._ask(text)

    async def _ask(self, text: str) -> None:
        if self.agent is None:
            await self._system_note(
                f"无法发起请求：{self._init_error or 'Agent 未初始化'}\n"
                f"请检查 config.ini 的 api_key / base_url / model，以及依赖是否装齐（pip install -r requirements.txt）。"
            )
            return

        if self.agent.config_problem:
            await self._system_note(
                f"还没配置好，发不出去：{self.agent.config_problem}。\n"
                f"打开 {config.CONFIG_PATH} 填好再重开：\n"
                f"  api_key  —— 你的密钥（占位符里的中文会被当成请求头，必然失败）\n"
                f"  base_url —— 用 DeepSeek 就填 https://api.deepseek.com/v1\n"
                f"  model    —— 要和平台对上，比如 deepseek-chat"
            )
            return

        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))

        self._agent_msg = AgentMessage()
        await log.mount(self._agent_msg)
        log.scroll_end(animate=False)

        self._acc = ""
        self._last_flush = 0.0
        self._busy = True
        self._set_status("思考中…")

        self.run_worker(lambda: self._run_agent(text), thread=True, exclusive=True)

    # ------------------------------------------------------------------ 工作线程
    def _run_agent(self, text: str) -> None:
        agent = self.agent
        if agent is None:
            self.call_from_thread(self._finish_turn)
            return
        try:
            agent.ask(
                text,
                on_delta=self._on_delta,
                on_tool_call=self._on_tool_call,
                on_tool_result=self._on_tool_result,
            )
        except Exception as exc:  # noqa: BLE001 - 单轮失败不应终止整个程序
            message = f"{type(exc).__name__}: {exc}"
            self.call_from_thread(self._show_error, message)
        finally:
            self.call_from_thread(self._finish_turn)

    def _on_delta(self, chunk: str) -> None:
        """工作线程：累积增量，按时间片合并后推给 UI 线程。"""
        self._acc += chunk
        now = time.monotonic()
        if now - self._last_flush >= FLUSH_INTERVAL:
            self._last_flush = now
            snapshot = self._acc
            self.call_from_thread(self._set_agent_text, snapshot)

    def _on_tool_call(self, name: str, arguments: str) -> None:
        snapshot = self._acc
        self.call_from_thread(self._before_tool, name, arguments, snapshot)

    def _on_tool_result(self, name: str, result: str, size: int) -> None:
        self.call_from_thread(self._after_tool, name, result)

    # ------------------------------------------------------------------ UI 线程更新
    def _set_agent_text(self, text: str) -> None:
        if self._agent_msg is not None:
            self._agent_msg.set_text(text)
            self.query_one("#log", VerticalScroll).scroll_end(animate=False)

    def _before_tool(self, name: str, arguments: str, snapshot: str) -> None:
        log = self.query_one("#log", VerticalScroll)
        if self._agent_msg is not None:
            self._agent_msg.set_text(snapshot)
        log.mount(ToolCallLine(name, arguments))
        self._set_status(f"执行 {name} …")
        log.scroll_end(animate=False)

    def _after_tool(self, name: str, result: str) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(ToolResultLine(name, result))
        log.scroll_end(animate=False)
        self._set_status("思考中…")

    def _show_error(self, message: str) -> None:
        log = self.query_one("#log", VerticalScroll)
        log.mount(ErrorLine(f"请求失败：{message}\n请检查网络，以及 config.ini 中的 base_url / model / api_key。"))
        log.scroll_end(animate=False)
        self._set_status("出错")

    def _finish_turn(self) -> None:
        if self._agent_msg is not None and self._acc and self._agent_msg.plain != self._acc:
            self._agent_msg.set_text(self._acc)
        self._busy = False
        self._turns += 1
        self._agent_msg = None
        self._acc = ""
        self._set_status("就绪")
        self._refresh_stats()
        self.query_one("#prompt", Input).focus()

    def _set_status(self, text: str) -> None:
        try:
            self.query_one("#status", Static).update(text)
        except Exception:  # noqa: BLE001
            pass

    def _refresh_stats(self) -> None:
        tokens = self.agent.total_tokens if self.agent else 0
        try:
            self.query_one("#stats", Static).update(f"轮次 {self._turns}  ·  tokens {tokens}")
        except Exception:  # noqa: BLE001
            pass
        try:
            sid = (self.agent.session_id if self.agent else "") or self.session_id
            if sid:
                self.query_one("#session", Static).update(f"  会话 {_fit(sid, 36)}")
        except Exception:  # noqa: BLE001 - 会话标签可能还没挂载
            pass

    async def _system_note(self, text: str) -> None:
        log = self.query_one("#log", VerticalScroll)
        await log.mount(Static(text, classes="tool-result", markup=False))
        log.scroll_end(animate=False)

    # ------------------------------------------------------------------ 新命令
    async def _show_mcp(self) -> None:
        if self.mcp is None:
            await self._system_note(
                f"本次运行没有启动 MCP。\n"
                f"在 {self.project_root} 下创建 mcp.json 后重启即可（格式见 mcp.example.json）。"
            )
            return

        lines = [f"MCP：{self.mcp.summary()}"]
        for warning in getattr(self.mcp, "warnings", None) or []:
            lines.append(f"  · 告警：{warning}")
        for name, status, detail in self.mcp.status_rows():
            label = {"ready": "已连接", "failed": "失败", "pending": "连接中"}.get(status, status)
            lines.append(f"  {name}  [{label}]  {detail}")
        if not self.mcp.states:
            lines.append("  （没有配置任何 server）")
        await self._system_note("\n".join(lines))

    async def _show_rules(self) -> None:
        if self.rules is None:
            await self._system_note(
                f"未找到 AGENT.md。\n在 {self.project_root} 下新建 AGENT.md，"
                f"内容会作为长期规则注入 System Prompt（下次启动生效）。"
            )
            return
        await self._system_note(
            f"项目规则 {self.rules.summary()}\n{'─' * 40}\n{self.rules.text}"
        )

    async def _save_session(self) -> None:
        if self.store is None or self.agent is None:
            await self._system_note("会话存储不可用。")
            return
        info = self.store.save(self.agent.messages, self.agent.session_id, model=self.agent.model)
        if info is None:
            await self._system_note("保存失败（目录不可写？）")
            return
        self.agent.session_id = info.id
        self.session_id = info.id
        await self._system_note(f"已保存会话 {info.id}（{info.turns} 轮）。")

    async def _list_sessions(self) -> None:
        if self.store is None:
            await self._system_note("会话存储不可用。")
            return
        items = self.store.list()
        if not items:
            await self._system_note("还没有保存过的会话。每轮对话结束会自动保存。")
            return
        lines = [f"共 {len(items)} 个会话（最新在前）："]
        for info in items:
            current = self.agent is not None and info.id == self.agent.session_id
            lines.append(f"  {info.display()}{'  ← 当前' if current else ''}")
        lines.append("恢复：/load <id> 或 /load last")
        await self._system_note("\n".join(lines))

    async def _load_session(self, target: str) -> None:
        if self.store is None or self.agent is None:
            await self._system_note("会话存储不可用。")
            return
        if not target:
            await self._system_note("用法：/load <会话 id>，或 /load last")
            return
        if target == "last":
            latest = self.store.latest()
            if latest is None:
                await self._system_note("没有可恢复的会话。")
                return
            target = latest.id

        data = self.store.load(target)
        if data is None:
            await self._system_note(f"找不到会话 {target}。用 /sessions 看看有哪些。")
            return

        self.agent.messages = data["messages"]
        self.agent.session_id = data["id"]
        self.agent.turns = int(data.get("turns") or 0)
        self.agent.refresh_system()
        self.session_id = data["id"]
        self._turns = self.agent.turns
        self._refresh_stats()

        log = self.query_one("#log", VerticalScroll)
        await log.mount(
            Static(
                f"已恢复会话 {data['id']}（{self.agent.turns} 轮）：{data.get('title', '')}",
                classes="tool-result",
                markup=False,
            )
        )
        log.scroll_end(animate=False)

    async def _show_trace(self) -> None:
        from core import tracing

        tracer = tracing.get()
        if tracer is None:
            await self._system_note("追踪未启用（config.ini 的 [trace] enabled）。")
            return
        text = tracer.summary()
        if tracer.path:
            text += f"\n日志：{tracer.path}"
        await self._system_note(text)

    # ------------------------------------------------------------------ actions
    def action_clear(self) -> None:
        if self.agent:
            self.agent.reset()
        log = self.query_one("#log", VerticalScroll)
        log.remove_children()
        log.mount(Static("上下文已清空。", classes="welcome", markup=False))
        self._turns = 0
        self._refresh_stats()
        self._set_status("就绪")

    def action_help(self) -> None:
        self.notify(
            "Enter 发送 · Ctrl+L 清空上下文 · Ctrl+Q 退出\n"
            "/tools 工具 · /skills 技能 · /mcp MCP 状态 · /rules 项目规则\n"
            "/sessions 会话列表 · /load <id|last> 恢复 · /save 保存 · /new 新会话\n"
            "/trace 调用统计 · /clear 清空 · /help 帮助",
            title="快捷键",
            timeout=8,
        )
