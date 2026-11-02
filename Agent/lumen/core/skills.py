# -*- coding: utf-8 -*-
"""
core.skills · Agent Skills 动态加载机制（第七周 · 任务四）
=========================================================

这一节解决的问题：**怎么让 Agent 拥有「很多技能」而不用把技能全塞进 System Prompt。**

如果每个 Skill 的完整说明都常驻上下文，20 个 Skill 就要吃掉几万 token，
而且 99% 的时间它们都用不上。Agent Skills 规范的答案是「懒加载」：

    启动时    只扫 .agents/skills/*/SKILL.md 的 YAML Frontmatter
              → 只取 name + description（一行一个，几十 token）
    运行时    把这份"菜单"写进 System Prompt，模型知道有什么能力
    需要时    模型主动调 loadSkill(name) → 这一刻全文才进入上下文

这和「函数调用」是同一个思路：先暴露签名，调用时才取实现。

目录约定
--------
<项目根目录>/.agents/skills/
    └── <skill-name>/
        ├── SKILL.md          ← 必须有；YAML Frontmatter 声明 name / description
        ├── reference.md      ← 可选附件，由 loadSkill 的 filename 参数按需读取
        └── scripts/...
项目根目录 = 程序启动时的当前工作目录（CWD），不是 python.exe 的位置，
也不是代码所在目录。这样同一个 Agent 换个目录启动，就换一套技能。

SKILL.md 长这样：
    ---
    name: pdf-report
    description: 把 Markdown 内容排成中文 PDF 报告，含封面、页眉页码
    ---
    # 使用步骤
    1. ...

安全边界
--------
loadSkill 只允许读**该 Skill 目录内部**的文件。
即使模型被诱导传 `../../config.ini` 或 `C:\\Windows\\win.ini`，
realpath + commonpath 校验也会拦下来并返回明确错误。
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from core import config

SKILLS_DIRNAME = os.path.join(".agents", "skills")
ENTRY_FILENAME = "SKILL.md"

# Frontmatter 必须是文件最开头的一对 --- 之间的内容
_FRONTMATTER_RE = re.compile(
    r"\A(?:\ufeff)?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)",
    re.S,
)

MAX_FILE_BYTES = 2 * 1024 * 1024  # 单个附件最大 2MB，超过基本是误传
MAX_LISTED_FILES = 40


# ---------------------------------------------------------------------------
# 数据模型
# ---------------------------------------------------------------------------
@dataclass
class Skill:
    name: str
    description: str
    directory: str  # Skill 子目录的绝对路径
    entry: str      # SKILL.md 的绝对路径
    meta: dict = field(default_factory=dict)

    def files(self, limit: int = 30) -> list[str]:
        """列出目录内所有文件（相对路径），用于报错时提示模型可选范围。"""
        found: list[str] = []
        for dirpath, dirnames, filenames in os.walk(self.directory):
            dirnames[:] = [d for d in dirnames if not d.startswith(".") or d == ".agents"]
            for name in sorted(filenames):
                full = os.path.join(dirpath, name)
                found.append(os.path.relpath(full, self.directory).replace("\\", "/"))
                if len(found) >= limit:
                    return found
        return found


# ---------------------------------------------------------------------------
# Frontmatter 解析
# ---------------------------------------------------------------------------
def _mini_yaml(text: str) -> dict:
    """不依赖 PyYAML 的极简解析，只认 `key: value` 这种一层结构。"""
    data: dict = {}
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        value = value.strip()
        if value[:1] in ("'", '"') and value[-1:] == value[:1] and len(value) >= 2:
            value = value[1:-1]
        data[key.strip()] = value
    return data


def parse_frontmatter(text: str) -> tuple[dict, str]:
    """返回 (frontmatter 字典, 去掉 frontmatter 后的正文)。"""
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text

    raw = match.group(1)
    body = text[match.end():]

    try:
        import yaml  # 装了 PyYAML 就用正经解析器（支持多行、数组）
    except ImportError:
        return _mini_yaml(raw), body

    try:
        data = yaml.safe_load(raw)
    except Exception:  # noqa: BLE001 - YAML 写错不该让整个程序起不来
        return _mini_yaml(raw), body
    if not isinstance(data, dict):
        return {}, body
    return data, body


def _read_text(path: str, limit_bytes: int = 512 * 1024) -> str:
    """按 UTF-8 → GB18030 读取文本；只读前 limit_bytes 字节用于解析头部。"""
    with open(path, "rb") as fh:
        raw = fh.read(limit_bytes)
    for enc in ("utf-8", "gb18030"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------
class SkillRegistry:
    def __init__(self, project_root: str) -> None:
        self.root = os.path.realpath(project_root)
        self.base = os.path.join(self.root, SKILLS_DIRNAME)
        self.skills: dict[str, Skill] = {}
        self.warnings: list[str] = []

    # -- 发现 ---------------------------------------------------------------
    def discover(self) -> "SkillRegistry":
        if not os.path.isdir(self.base):
            # 没有 skills 目录是完全正常的（不是每个项目都写 Skill）
            return self

        try:
            entries = sorted(os.listdir(self.base))
        except OSError as exc:
            self.warnings.append(f"无法读取 Skills 目录 {self.base}：{exc}")
            return self

        for entry in entries:
            directory = os.path.join(self.base, entry)
            if not os.path.isdir(directory):
                continue
            self._load_one(entry, directory)
        return self

    def _load_one(self, dirname: str, directory: str) -> None:
        entry = os.path.join(directory, ENTRY_FILENAME)
        if not os.path.isfile(entry):
            self.warnings.append(f"[{dirname}] 缺少 {ENTRY_FILENAME}，已跳过")
            return

        try:
            head = _read_text(entry, limit_bytes=64 * 1024)
        except OSError as exc:
            self.warnings.append(f"[{dirname}] 读取 {ENTRY_FILENAME} 失败：{exc}")
            return

        meta, _body = parse_frontmatter(head)
        name = str(meta.get("name") or "").strip()
        description = str(meta.get("description") or "").strip()

        if not name:
            self.warnings.append(f"[{dirname}] Frontmatter 缺少 name 字段，已跳过")
            return
        if not description:
            self.warnings.append(f"[{dirname}] Frontmatter 缺少 description 字段，已跳过")
            return
        if os.sep in name or "/" in name:
            self.warnings.append(f"[{dirname}] name 不能包含路径分隔符：{name!r}，已跳过")
            return

        existing = self.get(name)
        if existing is not None:
            self.warnings.append(
                f"[{dirname}] Skill 名称 {name!r} 与 {os.path.basename(existing.directory)!r} 重复，已跳过"
            )
            return

        self.skills[name] = Skill(
            name=name,
            description=re.sub(r"\s+", " ", description),
            directory=os.path.realpath(directory),
            entry=os.path.realpath(entry),
            meta=meta,
        )

    # -- 查询 ---------------------------------------------------------------
    def get(self, name: str) -> Skill | None:
        if not name:
            return None
        if name in self.skills:
            return self.skills[name]
        lowered = name.strip().casefold()
        for key, skill in self.skills.items():
            if key.casefold() == lowered:
                return skill
        return None

    def names(self) -> list[str]:
        return list(self.skills)

    def describe(self) -> list[tuple[str, str]]:
        return [(s.name, s.description) for s in self.skills.values()]

    # -- 注入 System Prompt -------------------------------------------------
    def prompt_block(self) -> str:
        """只包含 name + description —— 这是"初始化阶段"能进上下文的全部信息。"""
        if not self.skills:
            return ""
        lines = ["You have access to the following skills:", ""]
        for skill in self.skills.values():
            lines.append(f"- `{skill.name}`: {skill.description}")
        lines.append("")
        lines.append(
            "Use the `loadSkill` tool to load the full instructions of a skill when needed. "
            "You can also load additional files referenced by a skill by passing the `filename` parameter."
        )
        lines.append(
            "（当用户的任务与某个 Skill 的描述相符时，先调 loadSkill 读取它的完整说明，再按说明执行；"
            "不要在没读说明的情况下凭猜测使用技能。）"
        )
        return "\n".join(lines)

    # -- 读取 ---------------------------------------------------------------
    def load(self, name: str, filename: str | None = None) -> str:
        skill = self.get(name)
        if skill is None:
            available = "、".join(self.names()) or "（当前没有任何 Skill）"
            return f"错误：不存在名为 {name!r} 的 Skill。可用 Skill：{available}"

        if filename is None or not str(filename).strip():
            return self._read_file(skill, skill.entry, kind="SKILL.md")

        return self._load_attachment(skill, str(filename).strip().strip('"').strip("'"))

    def _load_attachment(self, skill: Skill, filename: str) -> str:
        skill_dir = os.path.realpath(skill.directory)

        # 1) 精确路径匹配：绝对路径 / Skill 相对 / 项目相对
        #
        # 注意顺序：**先做包含性校验，再看文件是否存在**。
        # 反过来写会留下一个存在性探针 —— 模型传 ../../secret.txt 时，
        # "文件存在但被拒绝" 与 "文件不存在" 两种回答本身就泄露了信息。
        candidates: list[str] = []
        if os.path.isabs(filename):
            candidates.append(filename)
        else:
            candidates.append(os.path.join(skill.directory, filename))
            project_relative = os.path.join(self.root, filename)
            # 项目相对路径只有落在本 Skill 目录内才作为候选（否则必然越界）
            if _inside(project_relative, skill_dir):
                candidates.append(project_relative)

        for candidate in candidates:
            real = os.path.realpath(candidate)
            if not _inside(real, skill_dir):
                return (
                    f"错误：拒绝读取 Skill {skill.name!r} 目录之外的路径。\n"
                    f"请求：{filename}\n"
                    f"解析为：{real}\n"
                    f"该 Skill 的目录：{skill_dir}\n"
                    f"loadSkill 只能读取本 Skill 目录内的附件，不能向上跳目录、不能用绝对路径指向别处。"
                )
            if not os.path.exists(real):
                continue
            if os.path.isdir(real):
                listing = _list_dir(real, skill_dir)
                return (
                    f"错误：{filename} 是一个目录，不是文件。\n"
                    f"该目录下的文件：\n{listing}\n"
                    f"请指定具体文件名。"
                )
            return self._read_file(skill, real)

        # 2) 只给了文件名（或路径写错）→ 在 Skill 目录内递归找
        base = os.path.basename(filename.replace("\\", "/"))
        matches = self._search_by_name(skill_dir, base)

        if len(matches) == 1:
            return self._read_file(skill, matches[0])

        if len(matches) > 1:
            rels = "\n".join(f"  - {os.path.relpath(m, skill_dir).replace(os.sep, '/')}" for m in matches)
            return (
                f"错误：Skill {skill.name!r} 内有 {len(matches)} 个文件都叫 {base!r}，无法确定读哪一个。\n"
                f"候选路径：\n{rels}\n"
                f"请用完整相对路径重试，例如 loadSkill(name=\"{skill.name}\", filename=\"<上面的某一项>\")。"
            )

        listing = "\n".join(f"  - {p}" for p in skill.files(MAX_LISTED_FILES)) or "  （空）"
        return (
            f"错误：在 Skill {skill.name!r} 中找不到文件 {filename!r}。\n"
            f"该 Skill 目录内的文件：\n{listing}\n"
            f"请从上面选择一个存在的路径；只给文件名时会在整个 Skill 目录内递归查找。"
        )

    @staticmethod
    def _search_by_name(skill_dir: str, basename: str) -> list[str]:
        exact: list[str] = []
        folded: list[str] = []
        target = basename.casefold()
        for dirpath, dirnames, filenames in os.walk(skill_dir):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".git")]
            for name in filenames:
                if name == basename:
                    exact.append(os.path.join(dirpath, name))
                elif name.casefold() == target:
                    folded.append(os.path.join(dirpath, name))
        return exact or folded

    def _read_file(self, skill: Skill, path: str, kind: str = "") -> str:
        real = os.path.realpath(path)
        if not _inside(real, skill.directory):
            return f"错误：拒绝读取 Skill 目录之外的路径：{real}"

        try:
            size = os.path.getsize(real)
        except OSError as exc:
            return f"错误：无法获取文件信息 — {exc}"

        if size > MAX_FILE_BYTES:
            return (
                f"错误：文件过大（{size / 1024 / 1024:.1f} MB），超过 {MAX_FILE_BYTES // 1024 // 1024} MB 上限。\n"
                f"文件：{real}\n"
                f"建议：让用户拆分文件，或改用 RunCli / readFile 分批处理。"
            )

        try:
            with open(real, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            return f"错误：读取文件失败 — {exc}"

        if b"\x00" in raw[:8192]:
            return (
                f"错误：{os.path.basename(real)} 看起来是二进制文件，无法作为文本加载。\n"
                f"文件：{real}"
            )

        for enc in ("utf-8", "gb18030"):
            try:
                text = raw.decode(enc)
                break
            except UnicodeDecodeError:
                continue
        else:
            text = raw.decode("utf-8", "replace")

        text = text.strip()
        limit = config.get_int("tools", "skill_max_chars", 20000)
        if len(text) > limit:
            text = (
                text[:limit]
                + f"\n\n…（内容已截断：原文共 {len(text)} 字符，此处保留前 {limit} 字符）"
            )

        rel = os.path.relpath(real, skill.directory).replace(os.sep, "/")
        label = "完整说明（SKILL.md）" if kind == "SKILL.md" else "附件文件"
        return (
            f"Skill：{skill.name} · {label}\n"
            f"文件：{rel}\n"
            f"长度：{len(text)} 字符\n"
            f"{'─' * 46}\n"
            f"{text}"
        )


# ---------------------------------------------------------------------------
# 工具函数
# ---------------------------------------------------------------------------
def _inside(path: str, parent: str) -> bool:
    try:
        return os.path.commonpath([os.path.realpath(path), os.path.realpath(parent)]) == os.path.realpath(parent)
    except ValueError:
        return False


def _list_dir(directory: str, skill_dir: str) -> str:
    try:
        entries = sorted(os.listdir(directory))[:MAX_LISTED_FILES]
    except OSError:
        return "  （无法读取）"
    lines = []
    for name in entries:
        full = os.path.join(directory, name)
        suffix = "/" if os.path.isdir(full) else ""
        lines.append(f"  - {os.path.relpath(full, skill_dir).replace(os.sep, '/')}{suffix}")
    return "\n".join(lines) or "  （空）"


# ---------------------------------------------------------------------------
# 全局实例：与 file_ops.PROJECT_ROOT 同样的思路，启动时注入
# ---------------------------------------------------------------------------
_registry: SkillRegistry | None = None


def discover(project_root: str) -> SkillRegistry:
    global _registry
    _registry = SkillRegistry(project_root).discover()
    return _registry


def get_registry() -> SkillRegistry | None:
    return _registry
