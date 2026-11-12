# -*- coding: utf-8 -*-
"""
tests.test_agent_features · 第八周新增能力的离线测试
====================================================

覆盖四块：
    - AGENT.md 项目规则的读取、截断、开关、等价命名
    - 会话持久化的存取、列表、非法 id 拒绝
    - 上下文压缩的触发条件与「不切断工具调用配对」
    - 技能的创建、重名拒绝、命名校验、路径穿越拦截

全部离线运行，不需要 API Key，也不会写到仓库目录（用临时目录）。

跑法：
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import agent_md, config, session  # noqa: E402
import core.tools  # noqa: E402,F401
from core.registry import dispatch  # noqa: E402
from core.tools import file_ops  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini"
)


def _set(section: str, key: str, value: str) -> None:
    parser = config._require_loaded()
    if not parser.has_section(section):
        parser.add_section(section)
    parser.set(section, key, value)


def _remove(section: str, key: str) -> None:
    parser = config._require_loaded()
    if parser.has_section(section) and parser.has_option(section, key):
        parser.remove_option(section, key)


class _TempRootCase(unittest.TestCase):
    """每个用例一个临时项目根目录。"""

    def setUp(self):
        config.load(CONFIG_PATH if os.path.exists(CONFIG_PATH) else None)
        self.root = tempfile.mkdtemp(prefix="lumen-test-")
        self._old_root = file_ops.PROJECT_ROOT
        file_ops.set_project_root(self.root)

    def tearDown(self):
        file_ops.set_project_root(self._old_root)
        shutil.rmtree(self.root, ignore_errors=True)

    def write(self, rel: str, text: str) -> str:
        path = os.path.join(self.root, rel)
        os.makedirs(os.path.dirname(path) or self.root, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(text)
        return path


# ---------------------------------------------------------------------------
# AGENT.md
# ---------------------------------------------------------------------------
class AgentMdTest(_TempRootCase):
    def tearDown(self):
        _remove("agent", "rules_file")
        _remove("agent", "rules_enabled")
        _remove("agent", "rules_max_chars")
        super().tearDown()

    def test_loads_agent_md(self):
        self.write("AGENT.md", "# 规则\n\n- 回答用中文\n")
        doc = agent_md.load(self.root)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.filename, "AGENT.md")
        self.assertIn("回答用中文", doc.text)

    def test_missing_is_silent(self):
        self.assertIsNone(agent_md.load(self.root))
        self.assertEqual(agent_md.prompt_block(None), "")

    def test_alternate_name(self):
        self.write("AGENTS.md", "- 用 4 空格缩进\n")
        doc = agent_md.load(self.root)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.filename, "AGENTS.md")

    def test_configured_filename(self):
        self.write("docs/rules.md", "- 只用标准库\n")
        _set("agent", "rules_file", "docs/rules.md")
        doc = agent_md.load(self.root)
        self.assertIsNotNone(doc)
        self.assertEqual(doc.filename, "docs/rules.md")

    def test_truncation(self):
        self.write("AGENT.md", "x" * 5000)
        _set("agent", "rules_max_chars", "1000")
        doc = agent_md.load(self.root)
        self.assertTrue(doc.truncated)
        self.assertIn("只注入前 1000 字符", doc.text)
        self.assertLessEqual(len(doc.text), 1200)

    def test_disabled(self):
        self.write("AGENT.md", "- 规则\n")
        _set("agent", "rules_enabled", "false")
        self.assertIsNone(agent_md.load(self.root))

    def test_prompt_block_wraps(self):
        self.write("AGENT.md", "- 别用表情\n")
        doc = agent_md.load(self.root)
        block = agent_md.prompt_block(doc)
        self.assertIn("<project_rules", block)
        self.assertIn("别用表情", block)

    def test_empty_file_ignored(self):
        self.write("AGENT.md", "   \n\n  ")
        self.assertIsNone(agent_md.load(self.root))


# ---------------------------------------------------------------------------
# 会话持久化
# ---------------------------------------------------------------------------
class SessionTest(_TempRootCase):
    def test_save_and_load(self):
        store = session.SessionStore(self.root)
        messages = [
            {"role": "system", "content": "系统提示"},
            {"role": "user", "content": "帮我看看目录"},
            {"role": "assistant", "content": "好的"},
        ]
        info = store.save(messages, model="test-model")
        self.assertIsNotNone(info)
        self.assertTrue(os.path.isfile(info.path))

        data = store.load(info.id)
        self.assertIsNotNone(data)
        self.assertEqual(data["messages"], messages)
        self.assertEqual(data["turns"], 1)
        self.assertIn("帮我看看目录", data["title"])

    def test_continue_same_session(self):
        store = session.SessionStore(self.root)
        first = store.save([{"role": "user", "content": "第一问"}])
        second = store.save(
            [{"role": "user", "content": "第一问"}, {"role": "assistant", "content": "答"},
             {"role": "user", "content": "第二问"}],
            session_id=first.id,
        )
        self.assertEqual(second.id, first.id)
        self.assertEqual(second.turns, 2)

    def test_list_sorted_desc(self):
        store = session.SessionStore(self.root)
        a = store.save([{"role": "user", "content": "A"}], session_id="20260101-000001")
        b = store.save([{"role": "user", "content": "B"}], session_id="20260102-000002")
        ids = [s.id for s in store.list()]
        self.assertEqual(ids[0], b.id)
        self.assertIn(a.id, ids)

    def test_reject_bad_id(self):
        store = session.SessionStore(self.root)
        self.assertIsNone(store.load("../../etc/passwd"))
        self.assertIsNone(store.load("20260101-000001/../../x"))
        self.assertIsNone(store.load("not-an-id"))
        self.assertIsNone(store._path("..\\..\\secret"))

    def test_latest(self):
        store = session.SessionStore(self.root)
        self.assertIsNone(store.latest())
        store.save([{"role": "user", "content": "唯一的"}], session_id="20260303-101010")
        self.assertEqual(store.latest().id, "20260303-101010")

    def test_missing_file(self):
        store = session.SessionStore(self.root)
        self.assertIsNone(store.load("20260101-000001"))


# ---------------------------------------------------------------------------
# 上下文压缩
# ---------------------------------------------------------------------------
class CompressTest(_TempRootCase):
    def _agent(self, limit: int, keep: int = 4):
        from core.agent import Agent

        _set("agent", "max_context_chars", str(limit))
        _set("agent", "keep_recent_messages", str(keep))
        return Agent(system_prompt="你是测试助手。")

    def tearDown(self):
        _remove("agent", "max_context_chars")
        _remove("agent", "keep_recent_messages")
        super().tearDown()

    def test_no_compress_under_limit(self):
        agent = self._agent(100_000)
        agent.messages.append({"role": "user", "content": "短消息"})
        agent.messages.append({"role": "assistant", "content": "短回答"})
        self.assertEqual(agent.compress_if_needed(), "")
        self.assertEqual(len(agent.messages), 3)

    def test_compress_over_limit(self):
        agent = self._agent(5_000, keep=2)
        big = "内容" * 2000  # 4000 字符
        for i in range(4):
            agent.messages.append({"role": "user", "content": f"问题{i} {big}"})
            agent.messages.append({"role": "assistant", "content": big})

        note = agent.compress_if_needed()
        self.assertIn("上下文已压缩", note)
        self.assertLess(len(agent.messages), 9)
        self.assertIn("历史对话摘要", agent.messages[0]["content"])
        self.assertIn("问题0", agent.messages[0]["content"])

    def test_never_cut_tool_pairs(self):
        """切点落在 tool 消息上时，必须往前退到 assistant。"""
        agent = self._agent(1_500, keep=3)
        agent.messages += [
            {"role": "user", "content": "查文件" * 100},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": "call_1", "type": "function",
                     "function": {"name": "listDirectory", "arguments": "{}"}}
                ],
            },
            {"role": "tool", "tool_call_id": "call_1", "content": "结果" * 300},
            {"role": "assistant", "content": "查完了"},
            {"role": "user", "content": "再查一次"},
            {"role": "assistant", "content": "好"},
        ]
        agent.compress_if_needed()
        # 第一条非 system 消息不能是孤立的 tool 结果
        self.assertNotEqual(agent.messages[1].get("role"), "tool")

    def test_summary_kept_after_reset(self):
        agent = self._agent(5_000, keep=2)
        for i in range(4):
            agent.messages.append({"role": "user", "content": f"问题{i}" + "x" * 2000})
            agent.messages.append({"role": "assistant", "content": "y" * 2000})
        agent.compress_if_needed()
        agent.reset()
        self.assertNotIn("历史对话摘要", agent.messages[0]["content"])


# ---------------------------------------------------------------------------
# 技能创建
# ---------------------------------------------------------------------------
class CreateSkillTest(_TempRootCase):
    def tearDown(self):
        # 重置全局注册表：临时目录马上要被删掉，留着只会让后续用例误判重名
        from core import skills

        skills._registry = None
        super().tearDown()

    def _create(self, **kwargs):
        payload = {"name": "demo-skill", "description": "演示用技能", "body": "# 步骤\n1. 做事"}
        payload.update(kwargs)
        return dispatch("createSkill", json.dumps(payload, ensure_ascii=False))

    def test_create_ok(self):
        out = self._create()
        self.assertIn("已创建技能", out)
        entry = os.path.join(self.root, ".agents", "skills", "demo-skill", "SKILL.md")
        self.assertTrue(os.path.isfile(entry))
        with open(entry, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---"))
        self.assertIn("name: demo-skill", text)
        self.assertIn("description:", text)

    def test_refresh_makes_it_loadable(self):
        from core import skills

        skills.discover(self.root)
        self._create(name="hot-skill")
        registry = skills.get_registry()
        self.assertIsNotNone(registry.get("hot-skill"))

    def test_duplicate_rejected(self):
        self._create()
        again = self._create()
        self.assertIn("已存在", again)
        self.assertIn("不写入", again)

    def test_bad_name_rejected(self):
        for bad in ("Demo Skill", "../evil", "大写", "", "a"):
            out = self._create(name=bad)
            self.assertIn("错误", out, f"{bad!r} 应被拒绝")

    def test_missing_description(self):
        out = self._create(description="   ")
        self.assertIn("description", out)

    def test_attachment_escape_rejected(self):
        out = self._create(attachments={"../evil.md": "x"})
        self.assertIn("错误", out)
        self.assertFalse(os.path.exists(os.path.join(self.root, ".agents", "evil.md")))

    def test_attachment_ok(self):
        out = self._create(attachments={"reference.md": "- 参考"})
        self.assertIn("附件", out)
        path = os.path.join(self.root, ".agents", "skills", "demo-skill", "reference.md")
        self.assertTrue(os.path.isfile(path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
