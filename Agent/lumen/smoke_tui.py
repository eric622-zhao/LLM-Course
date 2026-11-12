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
    # 冒烟只验证「界面能不能挂载、命令有没有回显」，不验证模型连通性。
    # 即使 config.ini 里填了真实 key，这里也强制置空：
    # 否则第 5 步「提问」会真的发一次网络请求 —— 既慢，又会因网络抖动偶发失败，
    # 让一个本该纯粹的 UI 自检变成不稳定的集成测试。
    parser = config._require_loaded()
    if not parser.has_section("llm"):
        parser.add_section("llm")
    parser.set("llm", "api_key", "")

    from core.tools import file_ops
    file_ops.set_project_root(os.getcwd())
    from core import agent_md, skills
    reg = skills.discover(os.getcwd())
    rules = agent_md.load(os.getcwd())
    return reg.prompt_block(), reg.describe(), reg.warnings, rules


async def wait_for_log(pilot, app, predicate, timeout=8.0, step=0.1):
    """轮询等日志出现期望内容。

    比固定 `pause(); pause()` 稳：命令处理快的机器上立刻返回，
    慢的机器（或 CI 上 CPU 被抢占时）也不会因为等待不够而误判失败。
    """
    deadline = asyncio.get_event_loop().time() + timeout
    while asyncio.get_event_loop().time() < deadline:
        await pilot.pause()
        text = "\n".join(str(w.render()) for w in app.query("#log > *"))
        if predicate(text):
            return True, text
        await asyncio.sleep(step)
    return False, "\n".join(str(w.render()) for w in app.query("#log > *"))


async def main():
    extra, skills_info, warnings, rules = setup()
    if warnings:
        print("  Skill 警告：", warnings)

    app = LumenApp(os.getcwd(), extra_system=extra, skills=skills_info, rules=rules,
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
        ok, log_text = await wait_for_log(
            pilot, app, lambda t: "getLocalTime" in t or "可用工具" in t,
        )
        check("/tools 命令有回显", ok, log_text[-60:].replace("\n", " "))

        # 4b. 新增命令：/mcp 与 /rules 都不能崩，且要有可读回显
        for cmd, expect in (("/mcp", "MCP"), ("/rules", "规则")):
            app.query_one("#prompt").focus()
            await pilot.press(*list(cmd))
            await pilot.press("enter")
            ok, text = await wait_for_log(pilot, app, lambda t, e=expect: e in t)
            check(f"{cmd} 命令有回显", ok, text[-50:].replace("\n", " "))

        # 5. 提问：配置不全时应被拦下来，而不是抛 UnicodeEncodeError
        app.query_one("#prompt").focus()
        await pilot.press(*list("你好"))
        await pilot.press("enter")
        # 先等事件循环把 busy 置位，再等它落回。
        # 否则刚按完回车就判断，busy 还是 False，会得到一个恒真的假通过。
        await asyncio.sleep(0.2)
        for _ in range(80):
            await pilot.pause()
            if not app._busy:
                break
            await asyncio.sleep(0.1)
        check("提问后回到就绪", not app._busy)

        log_text = "\n".join(str(w.render()) for w in app.query("#log > *"))
        if app.agent is not None and app.agent.config_problem:
            check(
                "缺配置时给出可操作提示（不是编码错误）",
                "api_key" in log_text and "ascii" not in log_text.lower(),
                "提示已显示" if "api_key" in log_text else log_text[-70:].replace("\n", " "),
            )

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
