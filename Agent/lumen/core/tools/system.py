# -*- coding: utf-8 -*-
"""
core.tools.system · 系统类工具（第五、六周）
===========================================

这里放了 Function Calling 最主要的**两种实现范式**，正好做对照：

    getLocalTime  → 调用外部命令（subprocess 执行本机 date 命令）
    isLeapYear    → 内部写代码计算（纯 Python，不依赖任何外部程序）

什么时候用哪种？
    * 系统已经有一个成熟可靠的命令，就别重复造轮子 —— 调它（date / git / ffmpeg）。
    * 只是一段几十行的逻辑，自己算更快更可控 —— 自己写（闰年判断、单位换算）。

Windows 上的一点坑
------------------
`date` 在 cmd 里是**内置命令**（还带交互式提问），不是独立 exe。
所以这里按优先级尝试：
    1. PATH 里有 date.exe（装了 Git for Windows / MSYS 就有）→ 直接 `date "+格式"`
    2. 没有则退回 `cmd /c date /T` 与 `cmd /c time /T`
    3. 都失败 → 用 Python 的 datetime 兜底，并在返回值里注明来源
"""

from __future__ import annotations

import datetime as _dt
import os
import shutil
import subprocess

from ..registry import tool

MAX_CMD_OUTPUT = 400


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _run(argv: list[str], timeout: int = 10) -> tuple[int, str, str]:
    """统一的外部命令执行封装：参数列表 + shell=False。"""
    try:
        proc = subprocess.run(argv, capture_output=True, timeout=timeout, shell=False)
    except FileNotFoundError:
        return 127, "", f"找不到命令：{argv[0]}"
    except subprocess.TimeoutExpired:
        return 124, "", f"命令执行超时（{timeout} 秒）"
    except OSError as exc:
        return 126, "", f"无法执行命令：{exc}"
    return proc.returncode, _decode(proc.stdout).strip(), _decode(proc.stderr).strip()


@tool(
    name="getLocalTime",
    description=(
        "获取本机当前的本地日期和时间。"
        "场景：用户问「现在几点」「今天几号」「今天是星期几」这类需要真实时间的问题时调用。"
        "返回年-月-日 时:分:秒、星期、时区信息，以及本次时间是通过哪条命令取到的。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "format": {
                "type": "string",
                "description": "可选。自定义输出格式（Unix strftime 风格，如 %Y/%m/%d %H:%M），不填则用默认完整格式",
            }
        },
        "required": [],
    },
    title="本地时间",
    category="系统",
)
def get_local_time(args: dict) -> str:
    fmt = str(args.get("format") or "").strip()

    # 路径 1：PATH 里有真正的 date 可执行文件（Git for Windows 自带）
    date_exe = shutil.which("date")
    if date_exe:
        pattern = fmt or "%Y-%m-%d %H:%M:%S %A %Z (%z)"
        code, out, err = _run([date_exe, f"+{pattern}"])
        if code == 0 and out:
            return f"本机当前时间：{out}\n来源：外部命令 {date_exe} +{pattern}"

    # 路径 2：Windows 的 date / time 内置命令（只能拿到较简略的信息）
    if os.name == "nt":
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        code_d, out_d, _ = _run([comspec, "/c", "date /T"])
        code_t, out_t, _ = _run([comspec, "/c", "time /T"])
        if code_d == 0 and code_t == 0 and (out_d or out_t):
            combined = " ".join(part for part in (out_d, out_t) if part)
            return (
                f"本机当前时间：{combined}\n"
                f"来源：外部命令 {comspec} /c date /T 与 time /T\n"
                f"提示：该方式不含星期与时区，需要更完整信息请使用系统 date 命令或询问时区。"
            )

    # 路径 3：兜底 —— 直接用 Python 取（不是外部命令，返回值里如实标注）
    now = _dt.datetime.now().astimezone()
    pattern = fmt or "%Y-%m-%d %H:%M:%S %A %Z (%z)"
    try:
        text = now.strftime(pattern)
    except ValueError:
        text = now.strftime("%Y-%m-%d %H:%M:%S %A %Z (%z)")
    return (
        f"本机当前时间：{text}\n"
        f"来源：Python datetime（未找到可用的 date 命令，属降级结果）\n"
        f"时区：{now.tzname() or '未知'}"
    )


def _leap_year_rule(year: int) -> tuple[bool, list[str]]:
    """格里高利历闰年规则，返回 (是否闰年, 命中的规则说明)。"""
    steps: list[str] = []
    if year % 4 != 0:
        steps.append(f"{year} 不能被 4 整除 → 平年")
        return False, steps
    steps.append(f"{year} 能被 4 整除")

    if year % 100 != 0:
        steps.append(f"{year} 不能被 100 整除 → 闰年")
        return True, steps
    steps.append(f"{year} 也能被 100 整除（世纪年）")

    if year % 400 == 0:
        steps.append(f"{year} 还能被 400 整除 → 闰年")
        return True, steps
    steps.append(f"{year} 不能被 400 整除 → 平年")
    return False, steps


@tool(
    name="isLeapYear",
    description=(
        "判断某个年份是不是闰年（格里高利历规则：能被4整除且不能被100整除，或能被400整除）。"
        "year 不填则判断当前年份。返回结论、命中的判断规则、该年天数，以及 2 月天数。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "year": {
                "type": "integer",
                "description": "要判断的年份，如 2024。不填则使用当前年份",
            }
        },
        "required": [],
    },
    title="闰年判断",
    category="系统",
)
def is_leap_year(args: dict) -> str:
    raw_year = args.get("year")
    if raw_year is None or str(raw_year).strip() == "":
        year = _dt.date.today().year
        note = "（未指定年份，使用当前年份）"
    else:
        try:
            year = int(str(raw_year).strip())
        except (TypeError, ValueError):
            return f"错误：year 必须是整数，收到的是 {raw_year!r}。"
        note = ""

    if year < 1 or year > 9999:
        return f"错误：year 超出合理范围（1~9999），收到 {year}。"

    leap, steps = _leap_year_rule(year)
    feb_days = 29 if leap else 28
    total_days = 366 if leap else 365

    lines = [
        f"{year} 年是{'闰年' if leap else '平年'}{note}",
        f"判断过程：",
    ]
    lines += [f"  - {step}" for step in steps]
    lines += [
        f"全年天数：{total_days} 天",
        f"2 月天数：{feb_days} 天",
        "实现方式：内部代码计算（未调用任何外部命令）",
    ]
    return "\n".join(lines)
