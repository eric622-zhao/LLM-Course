# -*- coding: utf-8 -*-
"""
core.tools.file_ops · 文件与目录操作（第五、六周的 10 个方法）
============================================================

    1. getCurrentDirectory  取当前目录
    2. listDirectory        列出指定目录的文件和子目录
    3. getFileType          获取指定文件的类型
    4. readFile             读取指定文件内容（限纯文本）
    5. writeFile            修改指定文件内容（限纯文本）
    6. createFile           在指定目录创建新文件
    7. createDirectory      创建目录
    8. renamePath           文件/目录重命名（或移动）
    9. deleteDirectory      删除目录（支持非空目录）
   10. deleteFile           删除文件

统一约定
--------
* 所有路径都限制在「项目根目录」内（启动时的当前工作目录），越界直接拒绝。
  这样即使模型被提示注入，也删不到项目之外的东西。
* 每个工具都返回人话描述（做了什么事、体积多大、绝对路径是什么），
  既能给用户看，也能作为下一轮的工具结果给模型看。
* 失败一律返回以「错误：」开头的字符串，不抛异常。
"""

from __future__ import annotations

import mimetypes
import os
import shutil

from ..registry import tool

# 启动时由 main / tui 设置
PROJECT_ROOT = os.getcwd()
MAX_READ_CHARS = 60_000

# 常见类型归类，让 getFileType 的返回更像人话
_TEXT_EXT = {".txt", ".md", ".py", ".js", ".ts", ".json", ".yaml", ".yml", ".ini", ".cfg",
             ".csv", ".log", ".html", ".htm", ".css", ".xml", ".toml", ".sh", ".bat", ".sql"}
_IMAGE_EXT = {".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".svg", ".ico"}
_AUDIO_EXT = {".mp3", ".wav", ".flac", ".aac", ".ogg", ".m4a"}
_VIDEO_EXT = {".mp4", ".avi", ".mkv", ".mov", ".wmv", ".flv", ".webm"}
_ARCHIVE_EXT = {".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz"}


class PathError(RuntimeError):
    """路径不合法或越界。"""


def set_project_root(path: str) -> None:
    global PROJECT_ROOT
    PROJECT_ROOT = os.path.abspath(path)


def _resolve(path: str, must_exist: bool = False) -> str:
    """把用户给的路径解析成绝对路径，并确认它在项目目录内。"""
    if path is None or not str(path).strip():
        raise PathError("path 不能为空")
    candidate = str(path).strip().strip('"').strip("'")
    if not os.path.isabs(candidate):
        candidate = os.path.join(PROJECT_ROOT, candidate)
    resolved = os.path.realpath(candidate)
    root = os.path.realpath(PROJECT_ROOT)
    try:
        inside = os.path.commonpath([resolved, root]) == root
    except ValueError:
        inside = False
    if not inside:
        raise PathError(f"拒绝访问项目目录之外的路径：{resolved}（项目根目录：{root}）")
    if must_exist and not os.path.exists(resolved):
        raise PathError(f"路径不存在：{resolved}")
    return resolved


def _rel(path: str) -> str:
    """转成相对项目根目录的显示形式，回答更简洁。"""
    try:
        rel = os.path.relpath(path, os.path.realpath(PROJECT_ROOT))
        return rel if not rel.startswith("..") else path
    except ValueError:
        return path


def _human_size(size: int) -> str:
    units = ("B", "KB", "MB", "GB")
    value = float(size)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{size} B"


def _looks_binary(path: str) -> bool:
    """读前 8KB，含 NUL 字节就当成二进制。"""
    try:
        with open(path, "rb") as fh:
            chunk = fh.read(8192)
    except OSError:
        return False
    return b"\x00" in chunk


# ---------------------------------------------------------------------------
# 1. 当前目录
# ---------------------------------------------------------------------------
@tool(
    name="getCurrentDirectory",
    description="获取当前工作目录的绝对路径，以及项目根目录。用户问「我现在在哪个目录」时调用。",
    parameters={"type": "object", "properties": {}, "required": []},
    title="当前目录",
    category="文件",
)
def get_current_directory(_args: dict) -> str:
    cwd = os.getcwd()
    return (
        f"当前工作目录：{cwd}\n"
        f"项目根目录：{os.path.realpath(PROJECT_ROOT)}\n"
        f"（文件类工具只能访问项目根目录之内的路径）"
    )


# ---------------------------------------------------------------------------
# 2. 列出目录
# ---------------------------------------------------------------------------
@tool(
    name="listDirectory",
    description=(
        "列出指定目录下的文件和子目录，返回名称、类型、大小。"
        "path 不填则列出项目根目录。用户问「这个目录里有什么」时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "目录路径，相对项目根目录即可；不填则用项目根目录"},
            "show_hidden": {"type": "boolean", "description": "是否显示以 . 开头的隐藏项，默认 false"},
        },
        "required": [],
    },
    title="列出目录",
    category="文件",
)
def list_directory(args: dict) -> str:
    raw = str(args.get("path") or "").strip() or PROJECT_ROOT
    show_hidden = bool(args.get("show_hidden"))
    try:
        target = _resolve(raw, must_exist=True)
    except PathError as exc:
        return f"错误：{exc}"
    if not os.path.isdir(target):
        return f"错误：{_rel(target)} 不是目录，它是文件。可以改用 getFileType 查看。" 

    try:
        names = sorted(os.listdir(target))
    except OSError as exc:
        return f"错误：无法列出目录 {_rel(target)} — {exc}"

    if not show_hidden:
        names = [n for n in names if not n.startswith(".")]

    if not names:
        return f"目录 {_rel(target)} 是空的。"

    dirs, files = [], []
    for name in names:
        full = os.path.join(target, name)
        if os.path.isdir(full):
            try:
                count = len([x for x in os.listdir(full) if not x.startswith(".")])
                dirs.append(f"  [目录] {name}/  （{count} 项）")
            except OSError:
                dirs.append(f"  [目录] {name}/")
        else:
            try:
                size = _human_size(os.path.getsize(full))
            except OSError:
                size = "未知大小"
            files.append(f"  [文件] {name}  （{size}）")

    lines = [f"目录 {_rel(target)} 共 {len(names)} 项（{len(dirs)} 个目录，{len(files)} 个文件）："]
    lines += dirs + files
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 3. 文件类型
# ---------------------------------------------------------------------------
@tool(
    name="getFileType",
    description=(
        "获取指定路径的类型信息：是文件还是目录、扩展名、MIME 类型、大小、修改时间、是否文本文件。"
        "用户问「这个文件是什么类型」「有多大」时调用。"
    ),
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "要检查的文件或目录路径"}},
        "required": ["path"],
    },
    title="文件类型",
    category="文件",
)
def get_file_type(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""), must_exist=True)
    except PathError as exc:
        return f"错误：{exc}"

    name = os.path.basename(target) or target
    ext = os.path.splitext(name)[1].lower()

    if os.path.isdir(target):
        try:
            count = len(os.listdir(target))
        except OSError:
            count = -1
        return (
            f"路径：{_rel(target)}\n"
            f"类型：目录\n"
            f"直接子项数：{count if count >= 0 else '无法读取'}\n"
            f"绝对路径：{target}"
        )

    stat = os.stat(target)
    mime, _ = mimetypes.guess_type(name)
    if ext in _IMAGE_EXT:
        kind = "图片"
    elif ext in _AUDIO_EXT:
        kind = "音频"
    elif ext in _VIDEO_EXT:
        kind = "视频"
    elif ext in _ARCHIVE_EXT:
        kind = "压缩包"
    elif ext in _TEXT_EXT or (mime or "").startswith("text/"):
        kind = "文本"
    elif not ext:
        kind = "无扩展名"
    else:
        kind = "其他"

    is_text = kind == "文本" and not _looks_binary(target)
    return (
        f"路径：{_rel(target)}\n"
        f"类型：文件（{kind}）\n"
        f"扩展名：{ext or '（无）'}\n"
        f"MIME：{mime or '未知'}\n"
        f"大小：{_human_size(stat.st_size)}（{stat.st_size} 字节）\n"
        f"修改时间：{_dt_str(stat.st_mtime)}\n"
        f"是否可当纯文本读取：{'是' if is_text else '否'}"
    )


def _dt_str(timestamp: float) -> str:
    import datetime as _dt

    return _dt.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------------------
# 4. 读取文件
# ---------------------------------------------------------------------------
@tool(
    name="readFile",
    description=(
        "读取指定纯文本文件的内容（如 .txt/.md/.py/.json/.csv）。"
        "二进制文件会被拒绝。内容过长会截断并说明截断情况。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要读取的文件路径"},
            "max_chars": {"type": "integer", "description": f"最多读取多少字符，默认 {MAX_READ_CHARS}"},
        },
        "required": ["path"],
    },
    title="读取文件",
    category="文件",
)
def read_file(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""), must_exist=True)
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.isdir(target):
        return f"错误：{_rel(target)} 是目录。要查看目录内容请用 listDirectory。"

    limit = args.get("max_chars")
    try:
        limit = int(limit) if limit else MAX_READ_CHARS
    except (TypeError, ValueError):
        limit = MAX_READ_CHARS

    if _looks_binary(target):
        return f"错误：{_rel(target)} 看起来是二进制文件（含 NUL 字节），无法按纯文本读取。可以改用 getFileType 查看类型。"

    try:
        with open(target, encoding="utf-8", errors="replace") as fh:
            content = fh.read()
    except OSError as exc:
        return f"错误：读取 {_rel(target)} 失败 — {exc}"

    header = f"文件：{_rel(target)}（共 {len(content)} 字符）\n" + "-" * 40 + "\n"
    if not content.strip():
        return header + "（文件为空）"
    if len(content) > limit:
        return header + content[:limit] + (
            f"\n\n…（已截断：原文共 {len(content)} 字符，此处保留前 {limit} 字符，"
            f"省略 {len(content) - limit} 字符。可调大 max_chars 或分段读取）"
        )
    return header + content


# ---------------------------------------------------------------------------
# 5. 修改文件内容
# ---------------------------------------------------------------------------
@tool(
    name="writeFile",
    description=(
        "修改指定纯文本文件的内容。mode=overwrite 覆盖整个文件（默认），mode=append 追加到末尾。"
        "文件不存在时会自动创建。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要写入的文件路径"},
            "content": {"type": "string", "description": "要写入的文本内容"},
            "mode": {"type": "string", "enum": ["overwrite", "append"], "description": "写入方式，默认 overwrite"},
        },
        "required": ["path", "content"],
    },
    title="修改文件",
    category="文件",
)
def write_file(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""))
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.isdir(target):
        return f"错误：{_rel(target)} 是目录，不能用 writeFile 写入。"

    content = args.get("content")
    if content is None:
        return "错误：缺少 content 参数。"
    content = str(content)

    mode = str(args.get("mode") or "overwrite").strip().lower()
    if mode not in ("overwrite", "append"):
        return f"错误：mode 只能是 overwrite 或 append，收到 {mode}。"

    existed = os.path.exists(target)
    try:
        os.makedirs(os.path.dirname(target) or PROJECT_ROOT, exist_ok=True)
        with open(target, "w" if mode == "overwrite" else "a", encoding="utf-8") as fh:
            fh.write(content)
    except OSError as exc:
        return f"错误：写入 {_rel(target)} 失败 — {exc}"

    return (
        f"已{'追加' if mode == 'append' else '写入'} {len(content)} 个字符到 {_rel(target)}"
        f"（{'原有文件' if existed else '新建文件'}，当前大小 {_human_size(os.path.getsize(target))}）。"
    )


# ---------------------------------------------------------------------------
# 6. 创建文件
# ---------------------------------------------------------------------------
@tool(
    name="createFile",
    description="在指定目录下创建一个新的纯文本文件。文件已存在时不会覆盖，会返回提示。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要创建的文件路径，如 notes/todo.md"},
            "content": {"type": "string", "description": "初始内容，可留空"},
        },
        "required": ["path"],
    },
    title="新建文件",
    category="文件",
)
def create_file(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""))
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.exists(target):
        return f"错误：{_rel(target)} 已存在（{'目录' if os.path.isdir(target) else '文件'}）。如需修改内容请用 writeFile。"

    content = str(args.get("content") or "")
    try:
        os.makedirs(os.path.dirname(target) or PROJECT_ROOT, exist_ok=True)
        # "x" 模式：文件已存在就报错，避免并发下误覆盖
        with open(target, "x", encoding="utf-8") as fh:
            fh.write(content)
    except FileExistsError:
        return f"错误：{_rel(target)} 已存在。"
    except OSError as exc:
        return f"错误：创建 {_rel(target)} 失败 — {exc}"

    return f"已创建文件 {_rel(target)}（{_human_size(os.path.getsize(target))}，初始 {len(content)} 字符）。"


# ---------------------------------------------------------------------------
# 7. 创建目录
# ---------------------------------------------------------------------------
@tool(
    name="createDirectory",
    description="创建目录，支持多级（如 a/b/c）。目录已存在时不报错，会说明情况。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "要创建的目录路径"}},
        "required": ["path"],
    },
    title="新建目录",
    category="文件",
)
def create_directory(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""))
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.isdir(target):
        return f"目录 {_rel(target)} 已存在，无需创建。"
    if os.path.exists(target):
        return f"错误：{_rel(target)} 已存在且是一个文件，无法创建同名目录。"

    try:
        os.makedirs(target, exist_ok=False)
    except FileExistsError:
        return f"目录 {_rel(target)} 已存在，无需创建。"
    except OSError as exc:
        return f"错误：创建目录 {_rel(target)} 失败 — {exc}"

    return f"已创建目录 {_rel(target)}（绝对路径 {target}）。"


# ---------------------------------------------------------------------------
# 8. 重命名
# ---------------------------------------------------------------------------
@tool(
    name="renamePath",
    description="重命名文件或目录，也可以用来移动到项目内的其他位置。new_path 若已存在，会拒绝以免覆盖。",
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "原路径"},
            "new_path": {"type": "string", "description": "新路径（同目录改名就只写新名字）"},
        },
        "required": ["path", "new_path"],
    },
    title="重命名",
    category="文件",
)
def rename_path(args: dict) -> str:
    try:
        source = _resolve(args.get("path", ""), must_exist=True)
        dest = _resolve(args.get("new_path", ""))
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.exists(dest):
        return f"错误：目标已存在 —— {_rel(dest)}。为避免覆盖，请换一个名字，或先删除目标。"

    try:
        os.makedirs(os.path.dirname(dest) or PROJECT_ROOT, exist_ok=True)
        os.rename(source, dest)
    except OSError as exc:
        return f"错误：重命名失败 — {exc}"

    kind = "目录" if os.path.isdir(dest) else "文件"
    return f"已将{kind} {_rel(source)} 重命名为 {_rel(dest)}。"


# ---------------------------------------------------------------------------
# 9. 删除目录（含非空）
# ---------------------------------------------------------------------------
@tool(
    name="deleteDirectory",
    description=(
        "删除目录。目录非空时必须显式传 recursive=true 才会连同内容一起删除；"
        "项目根目录本身不允许删除。这是不可恢复操作，删除前应向用户确认。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "path": {"type": "string", "description": "要删除的目录路径"},
            "recursive": {"type": "boolean", "description": "目录非空时必须为 true 才会递归删除，默认 false"},
        },
        "required": ["path"],
    },
    title="删除目录",
    category="文件",
)
def delete_directory(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""), must_exist=True)
    except PathError as exc:
        return f"错误：{exc}"

    if not os.path.isdir(target):
        return f"错误：{_rel(target)} 不是目录。删除文件请用 deleteFile。"

    root = os.path.realpath(PROJECT_ROOT)
    if os.path.realpath(target) == root:
        return "错误：不允许删除项目根目录本身。"

    try:
        entries = os.listdir(target)
    except OSError as exc:
        return f"错误：无法读取目录内容 — {exc}"

    recursive = bool(args.get("recursive"))
    if entries and not recursive:
        return (
            f"错误：目录 {_rel(target)} 非空（{len(entries)} 项）。"
            f"如确实要连同内容一起删除，请再次调用并传 recursive=true。"
        )

    try:
        if recursive:
            shutil.rmtree(target)
            return f"已递归删除目录 {_rel(target)}（含 {len(entries)} 项内容），不可恢复。"
        os.rmdir(target)
    except OSError as exc:
        return f"错误：删除目录失败 — {exc}"
    return f"已删除空目录 {_rel(target)}。"


# ---------------------------------------------------------------------------
# 10. 删除文件
# ---------------------------------------------------------------------------
@tool(
    name="deleteFile",
    description="删除指定的单个文件。不给目录用（目录请用 deleteDirectory）。这是不可恢复操作。",
    parameters={
        "type": "object",
        "properties": {"path": {"type": "string", "description": "要删除的文件路径"}},
        "required": ["path"],
    },
    title="删除文件",
    category="文件",
)
def delete_file(args: dict) -> str:
    try:
        target = _resolve(args.get("path", ""), must_exist=True)
    except PathError as exc:
        return f"错误：{exc}"

    if os.path.isdir(target):
        return f"错误：{_rel(target)} 是目录。删除目录请用 deleteDirectory。"
    if not os.path.isfile(target):
        return f"错误：{_rel(target)} 不是普通文件（可能是符号链接或设备文件），不处理。"

    try:
        size = os.path.getsize(target)
        os.remove(target)
    except OSError as exc:
        return f"错误：删除失败 — {exc}"

    return f"已删除文件 {_rel(target)}（原大小 {_human_size(size)}），不可恢复。"
