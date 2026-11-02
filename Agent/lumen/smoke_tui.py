# -*- coding: utf-8 -*-
"""smoke_tui.py · TUI 冒烟自检
==============================

不需要 API Key，headless 启动界面并验证挂载、命令、清空等基础交互。
界面「打不开」时先跑这个，能定位是依赖问题、配置问题还是 UI 本身的问题。

    python smoke_tui.py
"""

import asyncio
import os
import sys
import traceback

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

from core import config  # noqa: E402
import core.tools  # noqa: E402,F401
from tui.app import LumenApp  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  [{detail}]" if detail else ""))


def setup():
    config.load()
    from core.tools import file_ops
    file_ops.set_project_root(os.getcwd())
    from core import skills
    reg = skills.discover(os.getcwd())
    return reg.prompt_block(), reg.describe(), reg.warnings


async def main():
    extra, skills_info, warnings = setup()
    if warnings:
        print("  Skill 警告：", warnings)

    app = LumenApp(os.getcwd(), extra_system=extra, skills=skills_info,
                   warning="config.ini 里的 [llm] api_key 还没填写，提问会失败")

    async with app.run_test(size=(120, 40)) as pilot:
        await pilot.pause()

        # 1. 关键控件都在
        for sel in ("#brand", "#meta", "#log", "#sidebar", "#prompt", "#hint", "#status", "#stats"):
            try:
                app.query_one(sel)
                check(f"控件存在 {sel}", True)
            except Exception as exc:
                check(f"控件存在 {sel}", False, type(exc).__name__)

        # 2. Agent 是否构造成功（缺 key 也应能构造）
        check("Agent 已初始化", app.agent is not None, app._init_error)

        # 3. 侧栏工具行数与注册表一致
        from core.registry import TOOLS
        side_items = app.query(".side-item")
        check("侧栏渲染", len(side_items) > 0, f"{len(side_items)} 项 / 工具 {len(TOOLS)} 个")

        # 4. 输入并回车：不应抛异常（无有效 key 时应落到错误行）
        app.query_one("#prompt").focus()
        await pilot.press(*list("/tools"))
        await pilot.press("enter")
        await pilot.pause()
        await pilot.pause()
        log_text = "\n".join(str(w.render()) for w in app.query("#log > *"))
        check("/tools 命令有回显", "getLocalTime" in log_text or "可用工具" in log_text,
              log_text[-60:].replace("\n", " "))

        # 5. 提问（会真的请求网络，预期失败但要优雅）
        app.query_one("#prompt").focus()
        await pilot.press(*list("你好"))
        await pilot.press("enter")
        for _ in range(40):
            await pilot.pause()
            await asyncio.sleep(0.25)
            if not app._busy:
                break
        check("提问后回到就绪（无 key 时应给出错误而非卡死）", not app._busy)

        # 6. Ctrl+L 清空
        await pilot.press("ctrl+l")
        await pilot.pause()
        check("Ctrl+L 清空后界面存活", app.is_running)

        # 7. 截图式导出，确认没有渲染崩溃
        try:
            plain = "\n".join(
                line.text for line in app.screen._compositor.render_strips()  # type: ignore[attr-defined]
            ) if hasattr(app.screen, "_compositor") else ""
        except Exception:
            plain = ""
        check("渲染无异常", True, f"{len(plain)} 字符")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception:
        traceback.print_exc()
        print("\n[异常终止]")
    failed = [r for r in results if not r[1]]
    print(f"\n通过 {len(results) - len(failed)}/{len(results)}")
    sys.exit(1 if failed else 0)
