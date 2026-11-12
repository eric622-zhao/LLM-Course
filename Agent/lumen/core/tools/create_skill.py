# -*- coding: utf-8 -*-
"""
core.tools.create_skill · 把技能落盘（第七周 · 技能机制的写入侧）
=================================================================

已有的 loadSkill 只能**读**技能。这一节补上另一半：**写**。

一个技能在磁盘上就是「一个目录 + 一个带 YAML frontmatter 的 SKILL.md」：

    .agents/skills/<name>/
        ├── SKILL.md          ← frontmatter 声明 name / description，正文是操作说明
        └── reference.md      ← 可选附件

为什么要让模型自己写技能？
    因为「这次的做法以后还会用到」是真实需求。让 Agent 把它总结成技能存下来，
    下次同类任务就不用重新摸索 —— 这是 Skill 机制真正的价值闭环。

三个必须守住的边界
------------------
1. **命名校验**：目录名只允许小写字母、数字、`-`、`_`，不允许路径分隔符和 `..`。
   否则 `name="../../etc"` 就能把文件写到项目外面去。
2. **拒绝覆盖**：目标目录已存在就报错，不覆盖。技能是用户资产，
   静默覆盖比拒绝更危险 —— 想改内容请用 writeFile 显式改。
3. **写入后热刷新**：新建的技能要能立刻被 loadSkill 找到，不用重启。
"""

from __future__ import annotations

import os
import re

from core import skills
from core.registry import tool
from ..skills import ENTRY_FILENAME, SKILLS_DIRNAME

# 小写字母/数字开头，后可跟小写字母、数字、- _ . ，总长 2~48
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{1,47}$")

# 与操作系统或常见工具冲突的名字，避免制造麻烦
RESERVED = {"con", "prn", "aux", "nul", "com1", "com2", "lpt1", "lpt2"}

MAX_DESCRIPTION_CHARS = 200
MAX_BODY_CHARS = 20000
MAX_ATTACHMENTS = 10


def _project_root() -> str:
    """与文件类工具共用同一个根目录边界。"""
    from . import file_ops

    return os.path.realpath(file_ops.PROJECT_ROOT)


def _inside(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(parent)]) == os.path.realpath(parent)
    except ValueError:
        return False


def _one_line(text: str) -> str:
    """description 必须是单行 —— frontmatter 的 `key: value` 结构容不下换行。"""
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _build_entry(name: str, description: str, body: str) -> str:
    """拼出 SKILL.md 全文。frontmatter 用双引号包住，避免中文冒号被误解析。"""
    safe_desc = description.replace('"', "'")
    content = body.strip() or f"# {name}\n\n（待补充具体步骤。）\n"
    return (
        "---\n"
        f"name: {name}\n"
        f'description: "{safe_desc}"\n'
        "---\n"
        "\n"
        f"{content}\n"
    )


@tool(
    name="createSkill",
    description=(
        "把一套可复用的做法保存成一个 Skill（技能），供以后同类任务直接调用。"
        "它会在 .agents/skills/<name>/ 下创建目录和 SKILL.md，正文写具体操作步骤。"
        "使用场景：用户说「记住这个做法」「以后都这么做」「把它变成技能」，"
        "或你刚刚完成了一套值得复用的多步流程。"
        "注意：name 只能用小写字母、数字和 - _ .，不能重名，也不会覆盖已存在的技能。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "技能标识，小写字母/数字/-/_/.，如 weekly-report",
            },
            "description": {
                "type": "string",
                "description": "一句话说明「什么时候该用它」，会注入 System Prompt 供模型判断，100 字以内",
            },
            "body": {
                "type": "string",
                "description": "技能正文（Markdown），写清楚步骤、注意事项、示例",
            },
            "attachments": {
                "type": "object",
                "description": "可选附件，键为相对技能目录的文件名（如 reference.md），值为文件内容",
            },
        },
        "required": ["name", "description", "body"],
    },
    title="创建技能",
    category="技能",
)
def create_skill(args: dict) -> str:
    name = str(args.get("name") or "").strip()
    if not name:
        return "错误：缺少 name。技能标识只能用小写字母、数字和 - _ .，例如 weekly-report。"

    if not NAME_RE.match(name):
        return (
            f"错误：技能名 {name!r} 不合法。\n"
            f"要求：以字母或数字开头，只含小写字母、数字、- _ .，长度 2~48。\n"
            f"建议改成：{_suggest_name(name)}"
        )
    if name.lower() in RESERVED:
        return f"错误：{name!r} 是系统保留名，请换一个（例如 my-{name}）。"

    description = _one_line(args.get("description"))
    if not description:
        return "错误：缺少 description。它是模型判断「何时该用这个技能」的唯一依据，必须写。"
    if len(description) > MAX_DESCRIPTION_CHARS:
        return (
            f"错误：description 过长（{len(description)} 字符，上限 {MAX_DESCRIPTION_CHARS}）。"
            f"它要放进 System Prompt，请压缩成一句话。"
        )

    body = str(args.get("body") or "")
    if len(body) > MAX_BODY_CHARS:
        return (
            f"错误：body 过长（{len(body)} 字符，上限 {MAX_BODY_CHARS}）。"
            f"建议把大段参考资料拆成附件，正文只保留步骤。"
        )

    root = _project_root()
    skills_base = os.path.join(root, SKILLS_DIRNAME)
    target_dir = os.path.join(skills_base, name)

    # 边界校验放在「是否存在」之前：否则错误信息本身会成为目录存在性探针
    if not _inside(target_dir, skills_base):
        return f"错误：拒绝在技能目录之外创建技能（解析结果越界）：{target_dir}"

    if os.path.exists(target_dir):
        kind = "目录" if os.path.isdir(target_dir) else "文件"
        return (
            f"错误：{name!r} 已存在（{kind}）：{target_dir}\n"
            f"为避免覆盖已有技能，本次不写入。\n"
            f"如果要修改现有技能，请先用 readFile 读 SKILL.md，再用 writeFile 改；"
            f"如果要新建，请换一个名字。"
        )

    # 以磁盘为准重新扫描一次。
    # 内存里的 registry 可能指向已被删除的目录（比如上次运行留下的），
    # 直接拿它判断「重名」会误伤 —— 明明磁盘上没有，却说你已经有了。
    current = skills.discover(root)
    existing = current.get(name)
    if existing is not None and os.path.isdir(existing.directory):
        return (
            f"错误：已存在同名技能 {name!r}（{existing.directory}）。\n"
            f"为避免覆盖已有技能，本次不写入。\n"
            f"如果要修改现有技能，请先用 readFile 读 SKILL.md，再用 writeFile 改；"
            f"如果要新建，请换一个名字。"
        )

    # --- 附件路径校验（先全部校验，再落盘，避免写一半失败）---
    attachments = args.get("attachments") or {}
    if attachments and not isinstance(attachments, dict):
        return "错误：attachments 必须是对象（文件名 → 内容）。"
    if len(attachments) > MAX_ATTACHMENTS:
        return f"错误：附件过多（{len(attachments)} 个，上限 {MAX_ATTACHMENTS}）。"

    planned: list[tuple[str, str]] = []
    for raw_name, raw_content in attachments.items():
        rel = str(raw_name).strip().replace("\\", "/")
        if not rel or rel.startswith("/") or rel.startswith("../") or "/../" in rel or rel == "..":
            return f"错误：附件名 {raw_name!r} 不合法，必须是技能目录内的相对路径。"
        if rel.split("/")[0] in ("..", ""):
            return f"错误：附件名 {raw_name!r} 不合法。"
        full = os.path.join(target_dir, rel)
        if not _inside(full, target_dir):
            return f"错误：附件 {raw_name!r} 解析后越出技能目录。"
        if os.path.basename(rel).upper() == ENTRY_FILENAME.upper():
            return f"错误：附件不能用 {ENTRY_FILENAME} 这个名字，它由技能正文生成。"
        planned.append((rel, str(raw_content or "")))

    # --- 落盘 ---
    try:
        os.makedirs(target_dir)
    except FileExistsError:
        return f"错误：{name!r} 已存在，未覆盖。"
    except OSError as exc:
        return f"错误：创建技能目录失败 — {exc}"

    entry_path = os.path.join(target_dir, ENTRY_FILENAME)
    try:
        with open(entry_path, "x", encoding="utf-8") as fh:
            fh.write(_build_entry(name, description, body))
        for rel, content in planned:
            full = os.path.join(target_dir, rel)
            os.makedirs(os.path.dirname(full) or target_dir, exist_ok=True)
            with open(full, "w", encoding="utf-8") as fh:
                fh.write(content)
    except OSError as exc:
        # 写失败就回滚，不留半个技能
        try:
            import shutil

            shutil.rmtree(target_dir, ignore_errors=True)
        except Exception:  # noqa: BLE001
            pass
        return f"错误：写入技能文件失败 — {exc}（已回滚，未留下残缺目录）"

    # --- 热刷新：让新技能立刻可被 loadSkill 找到 ---
    refreshed = skills.discover(root)
    for warning in refreshed.warnings:
        # 新建的技能若 frontmatter 解析失败，这里能第一时间暴露
        if name in warning:
            return (
                f"警告：技能目录已创建，但重新扫描时出现告警：{warning}\n"
                f"请检查 {entry_path}"
            )

    rel_dir = os.path.relpath(target_dir, root).replace(os.sep, "/")
    lines = [
        f"已创建技能 {name!r}。",
        f"目录：{rel_dir}/",
        f"说明文件：{rel_dir}/{ENTRY_FILENAME}",
        f"描述：{description}",
    ]
    if planned:
        lines.append(f"附件 {len(planned)} 个：" + "、".join(rel for rel, _ in planned))
    lines.append(
        f"该技能已在本轮会话中生效，可用 loadSkill(name=\"{name}\") 读取正文。"
        f"它会被记入 System Prompt 的技能清单，后续会话启动时自动发现。"
    )
    return "\n".join(lines)


def _suggest_name(raw: str) -> str:
    """把不合法的名字清洗成一个可用的建议，省一轮来回。"""
    slug = raw.strip().lower()
    slug = re.sub(r"[^a-z0-9._-]+", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug).strip("-._")
    if not slug or not slug[0].isalnum():
        slug = f"skill-{slug}" if slug else "my-skill"
    return slug[:48]
