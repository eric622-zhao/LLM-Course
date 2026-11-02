# -*- coding: utf-8 -*-
"""
core.tools.run_cli · 本地命令行调用（第七周 · 任务三）
=====================================================

工具：RunCli
    让 Agent 调用本机已安装的任意命令行程序，拿回 stdout / stderr / 退出码。

三个硬性约束
------------
1. **参数列表 + shell=False**：`subprocess.run([exe, *args])`，
   绝不拼 `" ".join(...)` 再交给 shell。这样 `;`、`|`、`&&`、`$()` 都只是普通字符，
   不会被解释成命令分隔符 —— 这是命令注入的唯一有效防线。
2. **超时**：默认 60 秒（config.ini 的 [tools] cli_timeout），
   避免 `ping` / `tail -f` 这类不返回的命令把 Agent 卡死。
3. **输出截断**：stdout 与 stderr 分别截断，并**明确标注**截掉了多少，
   否则一次 `cat` 大文件就能把上下文窗口烧穿。

Windows 上的两个坑
------------------
* `npm` / `yarn` / `pnpm` 实际是 `.cmd` 批处理，不是 exe。
  CreateProcess 不会自动补 `.cmd`，所以先用 `shutil.which()`（尊重 PATHEXT）
  解析真实路径；批处理必须由 cmd.exe 解释，此时用
  `[cmd.exe, "/c", 批处理路径, *args]` —— 仍然是列表传参、shell=False。
* cmd 的**内置命令**（dir / type / copy）不是独立程序，本工具不支持，
  会返回明确提示，让模型改用等价的外部程序或专用文件工具。

安全边界（教学环境）
--------------------
按课程要求，本阶段**不做**命令白名单/黑名单/审批 —— 目的是让学生看清
「模型 → 工具 → 本机进程 → 结果回填」的完整链路。命令以运行 Lumen 的
当前用户权限执行，因此**只能在你自己信任的环境里用**。
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time

from core import config
from ..registry import tool, truncate

DEFAULT_TIMEOUT = 60
MAX_TIMEOUT = 300
DEFAULT_MAX_OUTPUT = 8000

# cmd 内置命令：不是独立可执行文件，shell=False 下无法直接调用
_CMD_BUILTINS = {
    "dir", "type", "copy", "move", "del", "erase", "md", "mkdir", "rd", "rmdir",
    "cls", "echo", "set", "cd", "chdir", "ren", "rename", "attrib", "call", "start",
}

_BUILTIN_HINTS = {
    "dir": "列目录请用 getCurrentDirectory / listDirectory 工具",
    "type": "读文件请用 readFile 工具",
    "copy": "复制文件可用 python -c 或系统 copy 之外的命令（如 robocopy）",
    "del": "删除文件请用 deleteFile 工具",
    "erase": "删除文件请用 deleteFile 工具",
    "md": "创建目录请用 createDirectory 工具",
    "mkdir": "创建目录请用 createDirectory 工具",
}


def _decode(raw: bytes | None) -> str:
    if not raw:
        return ""
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def _normalize_args(raw) -> tuple[list[str], str]:
    """把 args 统一成字符串列表。返回 (参数列表, 说明)。"""
    note = ""
    if raw is None or raw == "":
        return [], note
    if isinstance(raw, str):
        # 容错：模型偶尔会把参数写成字符串。按平台规则切分，不经过 shell
        parts = shlex.split(raw, posix=(os.name != "nt"))
        return parts, "（参数以字符串形式传入，已按空格切分为列表）"
    if isinstance(raw, (list, tuple)):
        return [str(item) for item in raw], note
    return [str(raw)], "（参数不是列表，已强制转为单个参数）"


def _resolve_command(command: str) -> tuple[list[str] | None, str]:
    """把命令名解析成可执行的 argv 前缀。返回 (前缀, 错误说明)。"""
    looks_like_path = os.path.isabs(command) or ("/" in command) or ("\\" in command)

    if looks_like_path:
        candidate = command if os.path.isabs(command) else os.path.abspath(command)
        if not os.path.exists(candidate):
            return None, f"错误：指定的程序不存在：{candidate}"
        exe = candidate
    else:
        if os.name == "nt" and command.lower() in _CMD_BUILTINS:
            hint = _BUILTIN_HINTS.get(command.lower(), "请改用等价的外部程序或专用工具")
            return None, (
                f"错误：{command} 是 cmd 的内置命令，不是独立可执行程序，"
                f"无法以 shell=False 的方式调用。\n提示：{hint}。"
            )
        exe = shutil.which(command) or ""
        if not exe:
            return None, (
                f"错误：找不到命令 {command!r}。\n"
                f"可能原因：未安装、或不在 PATH 中。\n"
                f"建议：先确认该程序已安装，或改用完整路径调用。"
            )

    if os.name == "nt" and os.path.splitext(exe)[1].lower() in (".cmd", ".bat"):
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        # .cmd / .bat 必须由 cmd.exe 解释；仍是列表传参 + shell=False
        return [comspec, "/c", exe], ""

    return [exe], ""


@tool(
    name="RunCli",
    description=(
        "在本机执行一个命令行程序，返回标准输出（stdout）、标准错误（stderr）和退出码。"
        "使用场景：需要获取只有命令行才能拿到的信息（git status、python --version、pip list、"
        "ffmpeg -version、系统信息等），或用户明确要求你运行某条命令时。"
        "参数必须拆成「程序名 + 参数列表」，例如 command=\"git\", args=[\"status\", \"--short\"]；"
        "不要把整条命令写进 command，也不要使用管道、重定向、&& 等 shell 语法（不会被解释）。"
        "默认执行 60 秒超时，输出过长会被截断并注明。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "程序名称或完整路径，例如 git、python、ffmpeg、C:\\\\Tools\\\\foo.exe",
            },
            "args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "参数列表，每个参数一个元素。例如 [\"status\", \"--short\"]；不带参数传 []",
            },
            "timeout": {
                "type": "integer",
                "description": f"超时秒数，默认 {DEFAULT_TIMEOUT}，上限 {MAX_TIMEOUT}",
            },
        },
        "required": ["command"],
    },
    title="本地命令",
    category="系统",
)
def run_cli(args: dict) -> str:
    command = str(args.get("command") or "").strip()
    if not command:
        return "错误：command 不能为空。请提供要执行的程序名，例如 git。"

    argv_prefix, error = _resolve_command(command)
    if argv_prefix is None:
        return error

    arg_list, args_note = _normalize_args(args.get("args"))

    default_timeout = config.get_int("tools", "cli_timeout", DEFAULT_TIMEOUT)
    try:
        timeout = int(args.get("timeout") or default_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    timeout = max(1, min(timeout, MAX_TIMEOUT))

    max_output = config.get_int("tools", "cli_max_output", DEFAULT_MAX_OUTPUT)

    # 命令在「项目根目录」下执行，与文件类工具看到的是同一个世界
    from . import file_ops

    workdir = os.path.realpath(file_ops.PROJECT_ROOT)

    argv = argv_prefix + arg_list
    started = time.monotonic()
    timed_out = False
    proc: subprocess.CompletedProcess | None = None

    try:
        # 关键：列表传参 + shell=False
        proc = subprocess.run(
            argv,
            capture_output=True,
            timeout=timeout,
            shell=False,
            cwd=workdir,
        )
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        stdout_raw = exc.stdout if isinstance(exc.stdout, bytes) else b""
        stderr_raw = exc.stderr if isinstance(exc.stderr, bytes) else b""
        proc = subprocess.CompletedProcess(argv, 124, stdout_raw, stderr_raw)
    except FileNotFoundError:
        return f"错误：无法启动程序 {argv[0]!r}（文件不存在或没有执行权限）。"
    except PermissionError:
        return f"错误：没有权限执行 {argv[0]!r}。"
    except OSError as exc:
        return f"错误：启动进程失败 — {exc}"

    elapsed = time.monotonic() - started
    stdout = _decode(proc.stdout).rstrip()
    stderr = _decode(proc.stderr).rstrip()
    code = proc.returncode

    lines = ["命令执行完成"]
    lines.append(f"程序：{command}")
    lines.append(f"参数：{len(arg_list)} 个" + (f" → {arg_list}" if arg_list else "（无）"))
    lines.append(f"实际执行：{subprocess.list2cmdline(argv)}")
    lines.append(f"工作目录：{workdir}")
    if args_note:
        lines.append(f"备注：{args_note.strip('（）')}")
    lines.append(f"耗时：{elapsed:.2f} 秒")

    if timed_out:
        lines.append(f"退出码：— （已在 {timeout} 秒超时后被终止）")
    else:
        status = "成功" if code == 0 else "失败"
        lines.append(f"退出码：{code}（{status}）")

    lines.append("")
    if stdout:
        lines.append(f"--- stdout（{len(stdout)} 字符）---")
        lines.append(truncate(stdout, max_output, label="标准输出"))
    else:
        lines.append("--- stdout ---")
        lines.append("（空）")

    if stderr:
        lines.append("")
        lines.append(f"--- stderr（{len(stderr)} 字符）---")
        lines.append(truncate(stderr, max_output, label="标准错误"))
    elif code != 0 or timed_out:
        lines.append("")
        lines.append("--- stderr ---")
        lines.append("（空）")

    if timed_out:
        lines.append("")
        lines.append(
            f"提示：命令未在规定时间内结束，已被强制终止。"
            f"如果这是预期行为（如服务类命令），请换用带「只执行一次就退出」参数的方式。"
        )
    elif code != 0:
        lines.append("")
        lines.append(
            "提示：命令以非零退出码结束，通常代表执行失败。"
            "请先读 stderr 判断原因，再决定换参数还是换方案，不要原样重试。"
        )
    elif not stdout and not stderr:
        lines.append("")
        lines.append("提示：命令执行成功，但没有产生任何输出。")

    return "\n".join(lines)
