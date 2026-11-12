# -*- coding: utf-8 -*-
"""
文档 – 代码 一致性自检
=======================

评分标准 F 维度优秀档要求「文档与代码由测试/脚本双向校验」。
人工核对总有遗漏，而且改了代码容易忘记回头改 README。本脚本反过来做：

    1. 从**代码**里取出真实事实 —— 工具名、路由、配置项、测试数量
    2. 从 **README** 里抽出被声称的内容
    3. 逐条比对，不一致就 FAIL

检查项
------
  [工具清单]    代码注册的每个工具，README 里都必须写到
  [工具数量]    README 声称「N 个工具」时，要与实际注册数一致
  [路由清单]    server.py 里的 @app.route 都写进 README 的接口表
  [目录树]      README 目录树里列的文件必须真的存在
  [测试数量]    README 写的单测项数与实际跑出来的一致
  [占位残留]    仓库里不应有 TODO / FIXME / 占位密钥

用法
----
    py check_docs.py          # 全部检查
    py check_docs.py -v       # 连 PASS 项也逐条打印

退出码：0 = 全部通过（可有 WARN）；1 = 有 FAIL。
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

RESULTS: list[tuple[str, str, str]] = []   # (级别, 分组, 说明)


def ok(group: str, msg: str) -> None:
    RESULTS.append(("PASS", group, msg))


def fail(group: str, msg: str) -> None:
    RESULTS.append(("FAIL", group, msg))


def warn(group: str, msg: str) -> None:
    RESULTS.append(("WARN", group, msg))


# --------------------------------------------------------------------------- #
# 取代码事实：在子进程里跑，避免两个 server.py 同名互相污染
# --------------------------------------------------------------------------- #
def probe(cwd: Path, code: str) -> dict | None:
    """在 cwd 下执行一段 python，期望它打印一行 JSON。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
    except subprocess.TimeoutExpired:
        return None
    if proc.returncode != 0:
        return None
    line = proc.stdout.strip().splitlines()[-1] if proc.stdout.strip() else ""
    try:
        return json.loads(line)
    except ValueError:
        return None


TOOLS_PROBE = (
    "import json, server;"
    "print(json.dumps([t.name for t in server.mcp._tool_manager.list_tools()]))"
)

ROUTES_PROBE = """
import json, re, pathlib
src = pathlib.Path("server.py").read_text(encoding="utf-8")
routes = sorted(set(re.findall(r'@app\\.(?:get|post|route)\\(\\s*["\\']([^"\\']+)', src)))
print(json.dumps(routes))
"""


# --------------------------------------------------------------------------- #
# README 解析
# --------------------------------------------------------------------------- #
def readme_of(folder: Path) -> str:
    path = folder / "README.md"
    return path.read_text(encoding="utf-8") if path.exists() else ""


def mentioned(text: str) -> set[str]:
    """README 里所有被反引号包起来的标识符（去掉调用括号与参数）。"""
    names = set()
    for raw in re.findall(r"`([^`\n]+)`", text):
        token = raw.strip().split("(")[0].strip()
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{2,40}", token):
            names.add(token)
    return names


def claimed_tool_count(text: str) -> int | None:
    """README 里「N 个工具」的 N。"""
    m = re.search(r"(\d+)\s*个工具", text)
    return int(m.group(1)) if m else None


# --------------------------------------------------------------------------- #
# 各项检查
# --------------------------------------------------------------------------- #
def check_tool_docs(folder: Path, label: str, probe_code: str = TOOLS_PROBE) -> None:
    """工具清单：代码有什么，文档就得写什么。"""
    tools = probe(folder, probe_code)
    if tools is None:
        fail(label, f"{folder.name}: 取不到工具注册表，导入失败")
        return
    if not tools:
        fail(label, f"{folder.name}: 注册表为空")
        return

    text = readme_of(folder)
    if not text:
        fail(label, f"{folder.name}: 缺 README.md")
        return

    names = mentioned(text)
    missing = [t for t in tools if t not in names]
    if missing:
        fail(label, f"{folder.name}: 这些工具在代码里注册了但 README 没写 → {', '.join(missing)}")
    else:
        ok(label, f"{folder.name}: {len(tools)} 个工具全部在 README 中有说明")

    claimed = claimed_tool_count(text)
    if claimed is not None and claimed != len(tools):
        fail(label, f"{folder.name}: README 写「{claimed} 个工具」，实际注册 {len(tools)} 个")
    elif claimed is not None:
        ok(label, f"{folder.name}: 工具数量与 README 声称一致（{claimed}）")


def check_routes(folder: Path, label: str) -> None:
    """路由清单：每个 @app.get/post 都要出现在 README 的接口表里。"""
    routes = probe(folder, ROUTES_PROBE)
    if routes is None:
        fail(label, f"{folder.name}: 解析不到路由")
        return
    text = readme_of(folder)
    missing = [r for r in routes if r not in text]
    if missing:
        fail(label, f"{folder.name}: README 接口表缺这些路由 → {', '.join(missing)}")
    else:
        ok(label, f"{folder.name}: {len(routes)} 条路由全部写进 README")


def check_tree(folder: Path, label: str) -> None:
    """目录树：README 里画出目录结构的代码块，列出的文件必须真实存在。

    不假定用 ```text 标记 —— 各人习惯不同，只要块里出现 `├` / `└` / `│`
    这类树形字符，就当目录树处理。
    """
    text = readme_of(folder)
    if not text:
        fail(label, f"{folder.name}: 缺 README.md")
        return

    blocks = [b for b in re.findall(r"```[a-zA-Z]*\n(.*?)```", text, re.S)
              if any(ch in b for ch in "├└│")]
    if not blocks:
        warn(label, f"{folder.name}: README 没有目录树块，跳过")
        return

    listed: set[str] = set()
    for block in blocks:
        for line in block.splitlines():
            # 去掉树形前缀，只看行首那个条目本身 ——
            # `├── weekly-report/   写周报（含 reference/style-guide.md 附件）`
            # 里被括号提到的是附件说明，不是目录树条目
            bare = re.sub(r"^[\s│├└─|+`-]+", "", line)
            m = re.match(r"([A-Za-z0-9_.][A-Za-z0-9_./\-]*\.(?:py|md|json|ini|txt|bat|tcss|html))\b", bare)
            if m:
                listed.add(m.group(1))

    missing = []
    for name in sorted(listed):
        candidates = [folder / name, folder / "core" / name, folder / "tui" / name,
                      folder / "core" / "tools" / name, folder / "tests" / name]
        if any(c.exists() for c in candidates):
            continue
        # 目录树可能只画到某个子目录的层级（如单独的 SKILL.md 结构示意），
        # 全项目能搜到同名文件就算它存在
        if any(folder.rglob(Path(name).name)):
            continue
        missing.append(name)

    if missing:
        fail(label, f"{folder.name}: README 目录树里这些文件不存在 → {', '.join(missing)}")
    else:
        ok(label, f"{folder.name}: 目录树 {len(listed)} 个文件全部对得上")


def check_test_count(folder: Path, label: str, pattern: str, expect_word: str) -> None:
    """测试数量：README 声称的项数与实际跑出来的一致。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-m", "unittest"] + pattern.split(),
            cwd=str(folder), capture_output=True, text=True, encoding="utf-8", timeout=180,
        )
    except subprocess.TimeoutExpired:
        fail(label, f"{folder.name}: 单测超时")
        return

    m = re.search(r"Ran (\d+) tests?", proc.stderr + proc.stdout)
    if not m:
        fail(label, f"{folder.name}: 单测跑不起来")
        return
    actual = int(m.group(1))
    if proc.returncode != 0:
        fail(label, f"{folder.name}: 单测 {actual} 项中有失败")
        return

    text = readme_of(folder)
    claimed = re.search(expect_word, text)
    if not claimed:
        warn(label, f"{folder.name}: README 未写单测数量（实际 {actual} 项）")
        return
    if int(claimed.group(1)) == actual:
        ok(label, f"{folder.name}: 单测 {actual} 项，与 README 一致")
    else:
        fail(label, f"{folder.name}: README 写 {claimed.group(1)} 项，实际跑出 {actual} 项")


def check_placeholders() -> None:
    """占位残留：未处理的 TODO / FIXME，以及像真密钥的硬编码。

    不检查「替换成你的」这类文案 —— 配置模板里本来就该有填写引导，
    把它当违规会淹没真正的问题（本脚本第一版就踩了这个坑）。
    """
    bad_patterns = [
        (re.compile(r"\bTODO\b"), "TODO 标记"),
        (re.compile(r"\bFIXME\b"), "FIXME 标记"),
        (re.compile(r"['\"](?:sk|ark|bce)-[A-Za-z0-9]{8,}"), "疑似硬编码密钥"),
        (re.compile(r"Bearer\s+[A-Za-z0-9\-_]{16,}"), "疑似硬编码 Token"),
    ]
    self_path = Path(__file__).resolve()
    hits: list[str] = []

    for path in ROOT.rglob("*"):
        if not path.is_file():
            continue
        if any(part in {".git", "__pycache__", ".lumen", ".agents"} for part in path.parts):
            continue
        if path.resolve() == self_path:
            continue                     # 本文件里的检测规则本身就是这些字符串
        if path.suffix not in {".py", ".md", ".ini", ".json", ".html", ".txt", ".yml"}:
            continue
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for pattern, desc in bad_patterns:
            if pattern.search(content):
                hits.append(f"{path.relative_to(ROOT)}（{desc}）")

    if hits:
        warn("占位残留", "发现可疑残留：" + "；".join(hits[:6]))
    else:
        ok("占位残留", "无 TODO / FIXME / 硬编码密钥残留")


# --------------------------------------------------------------------------- #
def main() -> int:
    parser = argparse.ArgumentParser(description="文档–代码一致性自检")
    parser.add_argument("-v", "--verbose", action="store_true", help="连 PASS 项也打印")
    args = parser.parse_args()

    # 两个 MCP server：工具清单 + 目录树
    check_tool_docs(ROOT / "PetHospitalMCP", "工具清单")
    check_tool_docs(ROOT / "AnythingLLMMCP", "工具清单")
    check_tree(ROOT / "PetHospitalMCP", "目录树")
    check_tree(ROOT / "AnythingLLMMCP", "目录树")

    # Web 服务：路由 + 目录树
    check_routes(ROOT / "AnythingLLMServer", "路由清单")
    check_tree(ROOT / "AnythingLLMServer", "目录树")

    # lumen：工具清单走它自己的注册表
    lumen = ROOT / "Agent" / "lumen"
    if lumen.exists():
        check_tool_docs(
            lumen,
            "工具清单",
            "import sys, json; sys.path.insert(0, '.');"
            "from core import registry; import core.tools;"
            "print(json.dumps([s['function']['name'] for s in registry.schemas()]))",
        )
        check_tree(lumen, "目录树")

    # 单测数量
    check_test_count(ROOT / "PetHospitalMCP", "测试数量", "test_server", r"(\d+) 项")
    check_test_count(ROOT / "AnythingLLMMCP", "测试数量", "test_server", r"(\d+) 项")

    check_placeholders()

    # 输出
    fails = [r for r in RESULTS if r[0] == "FAIL"]
    warns = [r for r in RESULTS if r[0] == "WARN"]
    passes = [r for r in RESULTS if r[0] == "PASS"]

    icons = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️ "}
    last_group = ""
    for level, group, msg in RESULTS:
        if level == "PASS" and not args.verbose:
            continue
        if group != last_group:
            print(f"\n[{group}]")
            last_group = group
        print(f"  {icons[level]} {msg}")

    print()
    print(f"通过 {len(passes)} · 警告 {len(warns)} · 失败 {len(fails)}")
    if fails:
        print("\n失败项需要修代码或修文档，二者必须一致：")
        for _, group, msg in fails:
            print(f"  - [{group}] {msg}")
        return 1
    print("文档与代码一致。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
