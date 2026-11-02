# -*- coding: utf-8 -*-
"""
main.py · 流明 Lumen 启动入口
=============================

运行：

    cd <你的项目根目录>          # 这个目录会作为「项目根目录」传给文件类工具
    python <lumen路径>/main.py

    # 例如
    cd D:\\LLM-Course\\Agent\\lumen
    python main.py

可选参数：
    --cli        不用 TUI，走最朴素的命令行循环（终端不支持时用）
    --verbose    打印工具返回的内容（调试用）

如果启动时报「找不到配置文件」，先执行：
    copy config.example.ini config.ini
然后填好 [llm] api_key。
"""

from __future__ import annotations

import os
import sys

# Windows 终端默认可能是 GBK，遇到不能编码的字符直接替换，不要崩
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(errors="replace")  # type: ignore[union-attr]
    except Exception:  # noqa: BLE001
        pass

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from core import __version__, config  # noqa: E402
from core.agent import Agent  # noqa: E402
import core.tools  # noqa: E402,F401  —— 导入即注册所有工具
from core.registry import TOOLS  # noqa: E402
from core.tools import file_ops  # noqa: E402

LINE = "─" * 62


def _bootstrap():
    """读取配置并设定项目根目录。返回 (project_root, extra_system, skill_names)。"""
    try:
        config.load()
    except config.ConfigError as exc:
        print(f"[错误] {exc}")
        return None

    api_key = config.get("llm", "api_key") or ""
    if config.missing_or_placeholder(api_key):
        print("[错误] config.ini 里的 [llm] api_key 还没填写。")
        print(f"       配置文件：{config.CONFIG_PATH}")
        print("       请复制 config.example.ini 为 config.ini 并填写 api_key。")
        return None

    # 项目根目录 = 启动时的当前工作目录（文件工具与 Skill 都以它为界）
    project_root = os.getcwd()
    file_ops.set_project_root(project_root)
    return project_root, "", []


def run_cli(project_root: str, extra_system: str) -> int:
    """最朴素的命令行循环，用于排查 TUI 或远程终端问题。"""
    agent = Agent(extra_system=extra_system)

    print(LINE)
    print(f" 流明 Lumen v{__version__} · CLI 模式")
    print(f" 模型：{config.get('llm', 'model')}")
    print(f" 项目根目录：{project_root}")
    print(f" 工具 {len(TOOLS)} 个：{'、'.join(TOOLS)}")
    print(LINE)
    print("输入 /exit 退出，/clear 清空上下文。")

    while True:
        try:
            question = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n已退出。")
            return 0

        if not question:
            continue
        if question in ("/exit", "/quit"):
            print("再见！")
            return 0
        if question == "/clear":
            agent.reset()
            print("上下文已清空。")
            continue

        print("流明：", end="", flush=True)
        try:
            answer = agent.ask(
                question,
                on_delta=lambda chunk: print(chunk, end="", flush=True),
                on_tool_call=lambda name, args: print(f"\n  [调用] {name} {args[:80]}", flush=True),
                on_tool_result=lambda name, result, size: print(f"  [返回] {name}（{size} 字符）", flush=True),
            )
        except Exception as exc:  # noqa: BLE001
            print(f"\n[错误] {type(exc).__name__}: {exc}")
            continue
        print()


def main() -> int:
    boot = _bootstrap()
    if boot is None:
        return 1
    project_root, extra_system, skill_names = boot

    if "--cli" in sys.argv:
        return run_cli(project_root, extra_system)

    print(LINE)
    print(f" 流明 Lumen v{__version__} · 启动中…")
    print(f" 模型：{config.get('llm', 'model')}   工具：{len(TOOLS)} 个")
    print(f" 项目根目录：{project_root}")
    print(LINE)

    try:
        from tui.app import LumenApp
    except ImportError as exc:
        print(f"[提示] 无法加载 TUI（{exc}），改用 CLI 模式。")
        print("       如需 TUI，请执行：pip install textual")
        return run_cli(project_root, extra_system)

    app = LumenApp(project_root, extra_system=extra_system, skill_names=skill_names)
    app.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
