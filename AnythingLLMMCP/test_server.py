# -*- coding: utf-8 -*-
"""
AnythingLLMMCP · 离线单测
==========================

不启动 AnythingLLM，用 httpx.MockTransport 拦下真实网络，只验证调用层行为：

    1. 没配 API Key 时，写操作给出「去哪拿 Key」而不是干巴巴的 401
    2. 只读工具不依赖 Key（课堂环境通常不开 Key 也能列工作区）
    3. 上传走 multipart、字段名对、文件不存在/超限时提前拦下
    4. 上传后指定 workspace 会真的再发一次向量化请求，且只发一次
    5. 向量化参数全空时报错，不白发请求
    6. chat 的 mode 只接受 query/chat，sessionId 会透传（多轮上下文）
    7. chat 返回体做了裁剪：answer/sources/sessionId，同时保留 raw 备查
    8. 401 / 403 / 500 三种失败给出不同且可操作的中文提示
    9. 分页参数越界提前拦下，正常时透传给上游

运行：
    py -m unittest test_server -v
"""

from __future__ import annotations

import os
import logging
import tempfile
import unittest
from pathlib import Path

import httpx

os.environ.setdefault("ANYTHINGLLM_API_KEY", "")

# httpx 默认会把每个请求打成 INFO，测试输出会被刷屏，压到 WARNING
for _noisy in ("httpx", "httpcore"):
    logging.getLogger(_noisy).setLevel(logging.WARNING)

import server  # noqa: E402  需在设置环境变量之后导入


class _Recorder:
    """记录 MockTransport 收到的请求，按顺序回放预置响应。"""

    def __init__(self, responses: list[httpx.Response]):
        self.responses = responses
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        return self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]

    @property
    def paths(self) -> list[str]:
        return [c.url.path for c in self.calls]

    @property
    def bodies(self) -> list[str]:
        return [c.content.decode("utf-8", "replace") for c in self.calls]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        self._old_client = server._client
        self._old_key = server.API_KEY
        self._old_max = server.MAX_UPLOAD_MB

    def tearDown(self) -> None:
        server._client = self._old_client
        server.API_KEY = self._old_key
        server.MAX_UPLOAD_MB = self._old_max

    def wire(self, *responses: httpx.Response, api_key: str = "test-key") -> _Recorder:
        """把全局 client 换成走 MockTransport 的实例。"""
        server.API_KEY = api_key
        recorder = _Recorder(list(responses))
        server._client = httpx.Client(
            base_url=server._base,
            transport=httpx.MockTransport(recorder.handler),
            headers={"Accept": "application/json"},
        )
        return recorder


# --------------------------------------------------------------------------- #
# 1~2 鉴权与配置
# --------------------------------------------------------------------------- #
class AuthTest(_Base):
    def test_missing_key_gives_actionable_message(self) -> None:
        """没 Key 时不该让请求发出去再吃 401，而是直接说清去哪拿。"""
        recorder = self.wire(httpx.Response(200, json={}), api_key="")
        with self.assertRaises(server.ToolError) as ctx:
            server.embed_documents("demo", ["custom-documents/a.json"])
        message = str(ctx.exception)
        self.assertIn("ANYTHINGLLM_API_KEY", message)
        self.assertIn("API Keys", message)
        self.assertEqual(recorder.calls, [], "缺 Key 时不应发出任何请求")

    def test_read_tool_works_without_key(self) -> None:
        """只读接口走 /api/*，本地免鉴权，没 Key 也该能用。"""
        recorder = self.wire(
            httpx.Response(200, json={"workspaces": [{"slug": "demo"}]}),
            api_key="",
        )
        result = server.list_workspaces()
        self.assertEqual(result, [{"slug": "demo"}])
        self.assertEqual(recorder.paths, ["/api/workspaces"])

    def test_401_explains_key_invalid(self) -> None:
        self.wire(httpx.Response(401, json={"error": "unauthorized"}))
        with self.assertRaises(server.ToolError) as ctx:
            server.chat("demo", "你好")
        self.assertIn("401", str(ctx.exception))
        self.assertIn("失效", str(ctx.exception))

    def test_403_explains_permission(self) -> None:
        self.wire(httpx.Response(403, json={"error": "forbidden"}))
        with self.assertRaises(server.ToolError) as ctx:
            server.chat("demo", "你好")
        self.assertIn("403", str(ctx.exception))

    def test_500_surfaces_upstream_error_field(self) -> None:
        """上游的 error 字段要提出来，别把整个 JSON 甩给模型。"""
        self.wire(httpx.Response(500, json={"error": "vector db is down"}))
        with self.assertRaises(server.ToolError) as ctx:
            server.list_all_documents()
        message = str(ctx.exception)
        self.assertIn("500", message)
        self.assertIn("vector db is down", message)

    def test_connect_error_is_readable(self) -> None:
        def boom(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused", request=request)

        server.API_KEY = "k"
        server._client = httpx.Client(
            base_url=server._base, transport=httpx.MockTransport(boom)
        )
        with self.assertRaises(server.ToolError) as ctx:
            server.list_workspaces()
        self.assertIn("连接 AnythingLLM 失败", str(ctx.exception))
        self.assertIn("桌面端", str(ctx.exception))


# --------------------------------------------------------------------------- #
# 3~5 上传与向量化
# --------------------------------------------------------------------------- #
class UploadTest(_Base):
    def setUp(self) -> None:
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.file = Path(self.tmp.name) / "课程笔记.md"
        self.file.write_text("# 课堂笔记\n垃圾分类的几个大类。", encoding="utf-8")

    def tearDown(self) -> None:
        self.tmp.cleanup()
        super().tearDown()

    def test_missing_file_rejected_before_request(self) -> None:
        recorder = self.wire(httpx.Response(200, json={}))
        with self.assertRaises(server.ToolError) as ctx:
            server.upload_document(str(Path(self.tmp.name) / "不存在.md"))
        self.assertIn("不存在", str(ctx.exception))
        self.assertEqual(recorder.calls, [], "文件不存时不该发请求")

    def test_oversize_rejected(self) -> None:
        server.MAX_UPLOAD_MB = 0.000001        # 挤到 1 字节以下
        recorder = self.wire(httpx.Response(200, json={}))
        with self.assertRaises(server.ToolError) as ctx:
            server.upload_document(str(self.file))
        self.assertIn("过大", str(ctx.exception))
        self.assertIn("ANYTHINGLLM_MAX_UPLOAD_MB", str(ctx.exception))
        self.assertEqual(recorder.calls, [])

    def test_upload_uses_multipart_file_field(self) -> None:
        recorder = self.wire(
            httpx.Response(200, json={
                "success": True,
                "document": {"location": "custom-documents/课程笔记.md-abc.json"},
            })
        )
        result = server.upload_document(str(self.file))

        self.assertEqual(recorder.paths, ["/api/v1/document/upload"])
        request = recorder.calls[0]
        self.assertIn("multipart/form-data", request.headers["content-type"])
        body = request.content
        self.assertIn(b'name="file"', body)
        self.assertIn("课程笔记".encode("utf-8"), body)
        self.assertEqual(result["location"], "custom-documents/课程笔记.md-abc.json")
        self.assertNotIn("embedded", result, "没指定 workspace 时不应触发向量化")

    def test_upload_with_workspace_embeds_once(self) -> None:
        recorder = self.wire(
            httpx.Response(200, json={"document": {"location": "custom-documents/x.json"}}),
            httpx.Response(200, json={"workspace": {"slug": "demo"}}),
        )
        result = server.upload_document(str(self.file), workspace="demo")

        self.assertEqual(
            recorder.paths,
            ["/api/v1/document/upload", "/api/v1/workspace/demo/update-embeddings"],
        )
        self.assertIn("custom-documents/x.json", recorder.bodies[1])
        self.assertEqual(result["embeddedInto"], "demo")

    def test_upload_without_location_flags_hint(self) -> None:
        """上游结构变了、取不到 location 时要明说，不能假装成功。"""
        self.wire(httpx.Response(200, json={"success": True}))
        result = server.upload_document(str(self.file))
        self.assertIsNone(result["location"])
        self.assertIn("hint", result)

    def test_embed_requires_some_operation(self) -> None:
        recorder = self.wire(httpx.Response(200, json={}))
        with self.assertRaises(server.ToolError) as ctx:
            server.embed_documents("demo", [])
        self.assertIn("空的", str(ctx.exception))
        self.assertEqual(recorder.calls, [])

    def test_embed_sends_adds_and_deletes(self) -> None:
        recorder = self.wire(httpx.Response(200, json={"workspace": {}}))
        server.embed_documents("demo", ["a.json"], ["b.json"])
        self.assertEqual(recorder.paths, ["/api/v1/workspace/demo/update-embeddings"])
        self.assertIn('"adds"', recorder.bodies[0])
        self.assertIn("b.json", recorder.bodies[0])


# --------------------------------------------------------------------------- #
# 6~7 检索问答
# --------------------------------------------------------------------------- #
class ChatTest(_Base):
    def test_mode_validated(self) -> None:
        recorder = self.wire(httpx.Response(200, json={}))
        with self.assertRaises(server.ToolError) as ctx:
            server.chat("demo", "问题", mode="rag")
        self.assertIn("query", str(ctx.exception))
        self.assertIn("chat", str(ctx.exception))
        self.assertEqual(recorder.calls, [], "非法参数不该发出请求")

    def test_query_mode_and_session_passed_through(self) -> None:
        recorder = self.wire(httpx.Response(200, json={"textResponse": "ok", "id": "s-2"}))
        server.chat("demo", "资料里怎么说", mode="QUERY ", session_id="s-1")

        self.assertEqual(recorder.paths, ["/api/v1/workspace/demo/chat"])
        body = recorder.bodies[0]
        self.assertIn('"mode":"query"', body.replace(" ", ""))
        self.assertIn('"sessionId":"s-1"', body.replace(" ", ""))

    def test_response_is_trimmed(self) -> None:
        self.wire(httpx.Response(200, json={
            "id": "thread-9",
            "type": "textResponse",
            "textResponse": "垃圾分类分四类。",
            "sources": [{"title": "笔记.md", "score": 0.91, "text": "有害垃圾……", "chunkSource": "x"}],
            "metrics": {"model": "gpt"},
        }))
        result = server.chat("demo", "怎么分类")

        self.assertEqual(result["answer"], "垃圾分类分四类。")
        self.assertEqual(result["sessionId"], "thread-9")
        self.assertEqual(len(result["sources"]), 1)
        self.assertEqual(result["sources"][0]["title"], "笔记.md")
        self.assertNotIn("chunkSource", result["sources"][0], "无用字段不该透给模型")
        self.assertIn("metrics", result["raw"], "完整响应仍保留在 raw 里备查")

    def test_session_id_falls_back_when_upstream_omits(self) -> None:
        self.wire(httpx.Response(200, json={"textResponse": "嗯"}))
        result = server.chat("demo", "继续", session_id="s-1")
        self.assertEqual(result["sessionId"], "s-1")


# --------------------------------------------------------------------------- #
# 9 分页
# --------------------------------------------------------------------------- #
class PagingTest(_Base):
    def test_limit_bounds_checked(self) -> None:
        recorder = self.wire(httpx.Response(200, json={}))
        for bad in (0, 101, -5):
            with self.subTest(limit=bad):
                with self.assertRaises(server.ToolError):
                    server.list_all_documents(limit=bad)
        self.assertEqual(recorder.calls, [])

    def test_offset_negative_rejected(self) -> None:
        self.wire(httpx.Response(200, json={}))
        with self.assertRaises(server.ToolError):
            server.list_all_documents(offset=-1)

    def test_params_forwarded(self) -> None:
        recorder = self.wire(httpx.Response(200, json={"localFiles": [{"name": "a.pdf"}]}))
        result = server.list_all_documents(limit=5, offset=10)

        self.assertEqual(recorder.paths, ["/api/v1/documents"])
        self.assertEqual(recorder.calls[0].url.params["limit"], "5")
        self.assertEqual(recorder.calls[0].url.params["offset"], "10")
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["offset"], 10)


# --------------------------------------------------------------------------- #
# 工具契约
# --------------------------------------------------------------------------- #
class ToolContractTest(unittest.TestCase):
    """工具声明本身：描述是否可读、必填是否声明。"""

    @staticmethod
    def _tools() -> dict[str, object]:
        return {t.name: t for t in server.mcp._tool_manager.list_tools()}

    def test_covers_three_capability_classes(self) -> None:
        """评分口径的「三类能力」：工作区管理 / 文档上传向量化 / 对话检索问答。"""
        names = set(self._tools())
        self.assertTrue({"list_workspaces", "get_workspace"} & names, "缺工作区管理")
        self.assertTrue({"upload_document", "embed_documents"} & names, "缺文档上传/向量化")
        self.assertIn("chat", names, "缺对话/检索问答")

    def test_every_tool_has_description(self) -> None:
        for name, tool in self._tools().items():
            with self.subTest(tool=name):
                self.assertTrue((tool.description or "").strip(), f"{name} 缺少描述")

    def test_write_tools_declare_required(self) -> None:
        tools = self._tools()
        for name in ("upload_document", "embed_documents", "chat"):
            with self.subTest(tool=name):
                self.assertTrue(tools[name].parameters.get("required"), f"{name} 未声明必填参数")


if __name__ == "__main__":
    unittest.main(verbosity=2)
