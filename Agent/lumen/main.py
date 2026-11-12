# -*- coding: utf-8 -*-
"""
main.py · 流明 Lumen 启动入口
=============================

启动时按顺序做四件事：

    1. 读配置（缺了就按模板生成）
    2. 读 AGENT.md —— 本项目的长期规则，注入 System Prompt
    3. 扫描 .agents/skills —— 只取名称与描述进 System Prompt
    4. 连接 mcp.json 里配置的 MCP server —— 失败只记状态，不阻塞

运行：

    cd <你的项目根目录>          # 这个目录会作为「项目根目录」传给文件类工具
    py <lumen路径>/main.py

可选参数：
    --cli        不用 TUI，走最朴素的命令行循环（终端不支持时用）
    --no-mcp     本次不连接 MCP server（排查启动慢时用）

如果启动时报「找不到配置文件」，先执行：
    copy config.example.ini config.ini
然后填好 [llm] api_key。
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field

# Windows 终端默认可能是 GBK，遇到不能编码的字符直接替换，不要崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import __version__, agent_md, config, mcp_client, skills, tracing  # noqa: E402
from core.agent import Agent  # noqa: E402
import core.tools  # noqa: E402,F401  —— 导入即注册所有工具
from core.registry import TOOLS  # noqa: E402
from core.session import SessionStore  # noqa: E402
from core.tools import file_ops  # noqa: E402

LINE = "─" * 62


@dataclass
class Boot:
    """启动阶段准备好的所有上下文。"""

    project_root: str
    rules: object | None = None            # agent_md.RulesDoc
    rules_block: str = ""                  # 已包装好的 System Prompt 片段
    skills_info: list = field(default_factory=list)
    warning: str = ""
    store: SessionStore | None = None
    session_id: str = ""
    mcp: object | None = None
    notes: list = field(default_factory=list)

    def system_provider(self) -> str:
        """返回 System Prompt 的「增量部分」（规则 + 技能清单）。

        每次调用都重新读技能注册表 —— createSkill 新建的技能因此能立刻进清单，
        而 AGENT.md 的规则在启动时读一次就固定下来（长期规则不该随对话漂移）。
        """
        parts: list[str] = []
        if self.rules_block:
            parts.append(self.rules_block)
        registry = skills.get_registry()
        if registry is not None:
            block = registry.prompt_block()
            if block:
                parts.append(block)
        return "\n\n".join(parts)


def _ensure_config() -> str | None:
    """确保 config.ini 存在；缺了就自动从模板复制一份。

    返回一行给用户看的提示（无需提示则返回 None）。
    """
    if os.path.exists(config.CONFIG_PATH):
        return None

    example = os.path.join(config.PACKAGE_ROOT, "config.example.ini")
    if not os.path.exists(example):
        return f"缺少配置文件：{config.CONFIG_PATH}"

    try:
        with open(example, "r", encoding="utf-8") as src:
            content = src.read()
        with open(config.CONFIG_PATH, "w", encoding="utf-8") as dst:
            dst.write(content)
    except OSError as exc:
        return f"无法创建配置文件：{exc}"

    return f"已根据模板生成 {config.CONFIG_PATH}，请填写其中的 [llm] api_key。"


def _bootstrap() -> Boot:
    """配置 → 规则 → 技能 → MCP → 会话 → 追踪。

    任何一步失败都不应让程序起不来：界面要能打开，问题以警告形式呈现，
    否则用户看到的只是一个闪退的窗口，无从判断哪里出错。
    """
    notes: list[str] = []

    ensured = _ensure_config()
    if ensured:
        notes.append(ensured)

    project_root = os.getcwd()
    file_ops.set_project_root(project_root)
    boot = Boot(project_root=project_root)

    try:
        config.load()
    except config.ConfigError as exc:
        print(f"[警告] {exc}")
        notes.append("未找到配置文件")
        # 没有配置也要尽量把能准备的准备好，这样界面至少是完整的
        boot.rules = None
        registry = skills.discover(project_root)
        boot.skills_info = registry.describe()
        boot.store = SessionStore(project_root)
        boot.warning = "；".join(notes)
        boot.notes = notes
        return boot

    api_key = config.get("llm", "api_key") or ""
    if config.missing_or_placeholder(api_key):
        notes.append("config.ini 里的 [llm] api_key 还没填写，提问会失败")

    # --- 1. 项目规则（AGENT.md）---
    boot.rules = agent_md.load(project_root)
    boot.rules_block = agent_md.prompt_block(boot.rules)
    if boot.rules is not None:
        notes.append(f"已注入项目规则 {boot.rules.summary()}")

    # --- 2. 技能发现 ---
    registry = skills.discover(project_root)
    for warning in registry.warnings:
        print(f"[Skill] {warning}")
    boot.skills_info = registry.describe()

    # --- 3. 追踪与会话 ---
    boot.store = SessionStore(project_root)
    tracing.init(project_root)

    # --- 4. MCP ---
    if "--no-mcp" in sys.argv:
        notes.append("已按 --no-mcp 跳过 MCP 连接")
    elif os.path.isfile(mcp_client.config_path(project_root)):
        print("[MCP] 正在连接 mcp.json 中配置的 server…")
        boot.mcp = mcp_client.start(project_root, block=True)
        for warning in getattr(boot.mcp, "warnings", []):
            print(f"[MCP] {warning}")
            notes.append(f"MCP：{warning}")
        if boot.mcp is not None and boot.mcp.states:
            notes.append(f"MCP {boot.mcp.summary()}")
    else:
        # 没有 mcp.json 是完全正常的，不打扰用户
        boot.mcp = mcp_client.start(project_root, block=True)

    boot.warning = "；".join(notes)
    boot.notes = notes
    return boot


# ---------------------------------------------------------------------------
# CLI 模式
# ---------------------------------------------------------------------------
def _print_sessions(boot: Boot) -> None:
    store = boot.store
    if store is None:
        print("会话存储不可用。")
        return
    items = store.list()
    if not items:
        print("还没有保存过的会话。")
        return
    print(f"共 {len(items)} 个会话（最新在前）：")
    for info in items:
        mark = " ← 当前" if info.id == boot.session_id else ""
        print(f"  {info.display()}{mark}")


def run_cli(boot: Boot) -> int:
    """最朴素的命令行循环，用于排查 TUI 或远程终端问题。"""
    agent = Agent(
        system_provider=boot.system_provider,
        session_store=boot.store,
        session_id=boot.session_id,
    )
    boot.session_id = agent.session_id

    if boot.warning:
        print(f"[提示] {boot.warning}")
    print(LINE)
    print(f" 流明 Lumen v{__version__} · CLI 模式")
    print(f" 模型：{config.get('llm', 'model')}")
    print(f" 项目根目录：{boot.project_root}")
    print(f" 工具 {len(TOOLS)} 个")
    if boot.rules is not None:
        print(f" 项目规则：{boot.rules.summary()}")
    if boot.skills_info:
        print(f" Skill {len(boot.skills_info)} 个：{'、'.join(name for name, _ in boot.skills_info)}")
    if boot.mcp is not None and boot.mcp.states:
        print(f" MCP：{boot.mcp.summary()}")
    print(LINE)
    print("输入 /exit 退出，/help 查看全部命令。")

    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            _shutdown(boot)
            return 0

        if not question:
            continue

        lowered = question.lower()
        if lowered in ("/exit", "/quit"):
            print("再见！")
            _shutdown(boot)
            return 0
        if lowered == "/clear":
            agent.reset()
            print("上下文已清空。")
            continue
        if lowered == "/help":
            print(
                "  /clear          清空上下文\n"
                "  /mcp            查看 MCP server 状态\n"
                "  /rules          查看已注入的 AGENT.md\n"
                "  /save           手动保存当前会话（每轮也会自动保存）\n"
                "  /sessions       列出历史会话\n"
                "  /load <id|last> 恢复某个会话\n"
                "  /new            开始新会话\n"
                "  /trace          本次运行的调用统计\n"
                "  /exit           退出"
            )
            continue
        if lowered == "/mcp":
            print("MCP：" + (boot.mcp.summary() if boot.mcp else "未启动"))
            if boot.mcp is not None:
                for name, status, detail in boot.mcp.status_rows():
                    print(f"  {name}  [{status}]  {detail}")
            continue
        if lowered == "/rules":
            if boot.rules is None:
                print(f"未找到 AGENT.md（放在 {boot.project_root} 下即可生效）。")
            else:
                print(f"{boot.rules.summary()}\n{boot.rules.text}")
            continue
        if lowered == "/save":
            info = boot.store.save(agent.messages, agent.session_id, model=agent.model)
            if info is None:
                print("保存失败（目录不可写？）")
            else:
                print(f"已保存：{info.id}")
            continue
        if lowered == "/sessions":
            _print_sessions(boot)
            continue
        if lowered.startswith("/load"):
            target = question[5:].strip()
            if not target:
                print("用法：/load <会话 id>，或 /load last")
                continue
            if target == "last":
                latest = boot.store.latest()
                if latest is None:
                    print("没有可恢复的会话。")
                    continue
                target = latest.id
            data = boot.store.load(target)
            if data is None:
                print(f"找不到会话 {target}。用 /sessions 看看有哪些。")
                continue
            agent.messages = data["messages"]
            agent.session_id = data["id"]
            agent.refresh_system()
            boot.session_id = data["id"]
            print(f"已恢复会话 {data['id']}（{data.get('turns', 0)} 轮）：{data.get('title', '')}")
            continue
        if lowered == "/new":
            agent.reset()
            agent.session_id = ""
            boot.session_id = ""
            print("已开始新会话，下一轮会存成新文件。")
            continue
        if lowered == "/trace":
            tracer = tracing.get()
            print(tracer.summary() if tracer else "追踪未启用")
            if tracer and tracer.path:
                print(f"日志：{tracer.path}")
            continue

        print("流明：", end="", flush=True)
        try:
            agent.ask(
                question,
                on_delta=lambda chunk: print(chunk, end="", flush=True),
                on_tool_call=lambda name, args: print(f"\n  [调用] {name} {args[:80]}", flush=True),
                on_tool_result=lambda name, result, size: print(f"  [返回] {name}（{size} 字符）", flush=True),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] {type(exc).__name__}: {exc}")
            continue
        print()
        boot.session_id = agent.session_id


def _shutdown(boot: Boot) -> None:
    """收尾：断开 MCP、关闭追踪文件。"""
    try:
        mcp_client.stop()
    except Exception:  # noqa: BLE001
        pass
    try:
        tracing.close()
    except Exception:  # noqa: BLE001
        pass


def main() -> int:
    boot = _bootstrap()

    if "--cli" in sys.argv:
        return run_cli(boot)

    print(LINE)
    print(f" 流明 Lumen v{__version__} · 启动中…")
    print(
        f" 模型：{config.get('llm', 'model')}   工具：{len(TOOLS)} 个"
        f"   Skill：{len(boot.skills_info)} 个"
    )
    print(f" 项目根目录：{boot.project_root}")
    if boot.rules is not None:
        print(f" 项目规则：{boot.rules.summary()}")
    if boot.mcp is not None and boot.mcp.states:
        print(f" MCP：{boot.mcp.summary()}")
    if boot.warning:
        print(f" [提示] {boot.warning}")
    print(LINE)

    try:
        from tui.app import LumenApp
    except ImportError as exc:
        print(f"[提示] 无法加载 TUI（{exc}），改用 CLI 模式。")
        print("       如需 TUI，请执行：pip install textual")
        return run_cli(boot)

    app = LumenApp(
        boot.project_root,
        system_provider=boot.system_provider,
        skills=boot.skills_info,
        warning=boot.warning,
        rules=boot.rules,
        mcp=boot.mcp,
        store=boot.store,
        session_id=boot.session_id,
    )
    try:
        app.run()
    finally:
        _shutdown(boot)
    return 0


if __name__ == "__main__":
    sys.exit(main())
