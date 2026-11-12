# -*- coding: utf-8 -*-
"""
PetHospitalMCP · 离线单测
=========================

不启动 pethospital.exe，用 httpx.MockTransport 拦下真实网络，
只验证「调用层」的行为是否符合设计：

    1. 5xx 会重试，重试后成功就正常返回
    2. 5xx 重试耗尽后抛中文错误，不泄漏上游堆栈
    3. 4xx 不重试（重试也没用，只会拖长等待）
    4. 业务错误码（code != 200/201）不重试
    5. 连接失败重试 N 次后给出可操作的中文提示
    6. 每次调用都写一条结构化日志，且日志不进 stdout

运行：
    py -m unittest discover -s . -p "test_*.py" -v
"""

from __future__ import annotations

import io
import logging
import os
import unittest
from contextlib import redirect_stdout

import httpx

os.environ.setdefault("PET_HOSPITAL_RETRIES", "2")

import server  # noqa: E402  需在设置环境变量之后导入


class _Recorder:
    """记录 MockTransport 收到几次请求、按顺序返回什么。"""

    def __init__(self, responses: list[httpx.Response | Exception]):
        self.responses = responses
        self.calls: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        item = self.responses[min(len(self.calls) - 1, len(self.responses) - 1)]
        if isinstance(item, Exception):
            raise item
        return item


class CallRetryTest(unittest.TestCase):
    def setUp(self) -> None:
        # 退避等待缩短到几乎为 0，让测试跑得快
        self._old_base = server.BACKOFF_BASE
        server.BACKOFF_BASE = 0.001
        self._old_client = server._client
        # 日志收集到内存，断言用
        self.log_stream = io.StringIO()
        server.logger.handlers.clear()
        handler = logging.StreamHandler(self.log_stream)
        handler.setFormatter(logging.Formatter("%(levelname)s %(message)s"))
        server.logger.addHandler(handler)
        server.logger.setLevel(logging.DEBUG)
        server.logger.propagate = False      # 只进内存缓冲，不刷屏
        # 与 setup_logging() 一致：压掉 httpx 自己的逐请求日志，避免污染断言
        for noisy in ("httpx", "httpcore"):
            logging.getLogger(noisy).setLevel(logging.WARNING)

    def tearDown(self) -> None:
        server.BACKOFF_BASE = self._old_base
        server._client = self._old_client
        server.logger.handlers.clear()

    def _install(self, responses: list[httpx.Response | Exception]) -> _Recorder:
        recorder = _Recorder(responses)
        server._client = httpx.Client(
            base_url=server.BASE_URL,
            transport=httpx.MockTransport(recorder.handler),
        )
        return recorder

    # -- 1 -------------------------------------------------------------- #
    def test_5xx_then_success_retries(self) -> None:
        recorder = self._install([
            httpx.Response(503, text="upstream busy"),
            httpx.Response(200, json={"code": 200, "data": {"ok": True}}),
        ])
        result = server.call("GET", "/api/v1/stats")
        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(recorder.calls), 2, "5xx 后应当再试一次")

    # -- 2 -------------------------------------------------------------- #
    def test_5xx_exhausted_raises(self) -> None:
        recorder = self._install([httpx.Response(500, text="boom")])
        with self.assertRaises(server.ToolError) as ctx:
            server.call("GET", "/api/v1/stats")
        # 1 次原始 + RETRIES 次重试
        self.assertEqual(len(recorder.calls), server.RETRIES + 1)
        self.assertIn("500", str(ctx.exception))

    # -- 3 -------------------------------------------------------------- #
    def test_4xx_not_retried(self) -> None:
        recorder = self._install([httpx.Response(404, text="not found")])
        with self.assertRaises(server.ToolError) as ctx:
            server.call("GET", "/api/v1/pets/NOPE")
        self.assertEqual(len(recorder.calls), 1, "4xx 不应重试")
        # 结构化错误：带状态码，而不是把上游 HTML 原样抛出去
        self.assertIn("404", str(ctx.exception))

    # -- 4 -------------------------------------------------------------- #
    def test_business_error_not_retried(self) -> None:
        recorder = self._install([
            httpx.Response(200, json={"code": 40001, "message": "编号不存在"}),
        ])
        with self.assertRaises(server.ToolError) as ctx:
            server.call("GET", "/api/v1/pets/NOPE")
        self.assertIn("40001", str(ctx.exception))
        self.assertIn("编号不存在", str(ctx.exception))
        self.assertEqual(len(recorder.calls), 1, "业务错误不应重试")

    # -- 5 -------------------------------------------------------------- #
    def test_connect_error_retried_then_friendly(self) -> None:
        recorder = self._install([httpx.ConnectError("refused")])
        with self.assertRaises(server.ToolError) as ctx:
            server.call("GET", "/api/v1/stats")
        self.assertEqual(len(recorder.calls), server.RETRIES + 1)
        message = str(ctx.exception)
        self.assertIn("pethospital.exe", message, "应给出可操作的提示")
        self.assertIn("已重试", message)

    # -- 6 -------------------------------------------------------------- #
    def test_log_line_written_and_stdout_clean(self) -> None:
        self._install([httpx.Response(200, json={"code": 200, "data": [1]})])
        buffer = io.StringIO()
        with redirect_stdout(buffer):
            server.call("GET", "/api/v1/stats")
        self.assertEqual(buffer.getvalue(), "", "stdio 传输下 stdout 必须是干净的")
        log = self.log_stream.getvalue()
        self.assertIn("GET /api/v1/stats", log)
        self.assertIn("200", log)

    def test_retry_is_logged(self) -> None:
        self._install([
            httpx.Response(502),
            httpx.Response(200, json={"code": 200, "data": None}),
        ])
        server.call("GET", "/api/v1/stats")
        log = self.log_stream.getvalue()
        self.assertIn("重试", log)
        self.assertIn("WARNING", log)


class ToolContractTest(unittest.TestCase):
    """工具声明本身的约定：有没有参数校验、描述是否可读。"""

    @staticmethod
    def _input_schema(tool) -> dict:
        """不同 SDK 版本字段名不一样，逐个试：
        2.x 内部叫 parameters，协议序列化时才是 inputSchema；1.x 直接叫 inputSchema。"""
        for attr in ("parameters", "input_schema", "inputSchema"):
            schema = getattr(tool, attr, None)
            if isinstance(schema, dict):
                return schema
        return {}

    def test_tool_count(self) -> None:
        tools = server.mcp._tool_manager.list_tools()
        self.assertGreaterEqual(len(tools), 1)

    def test_every_tool_has_description(self) -> None:
        for tool in server.mcp._tool_manager.list_tools():
            with self.subTest(tool=tool.name):
                self.assertTrue((tool.description or "").strip(),
                                f"{tool.name} 缺少描述，模型难以正确选择")

    def test_write_tools_declare_required_params(self) -> None:
        tools = {t.name: t for t in server.mcp._tool_manager.list_tools()}
        for name in ("create_pet", "add_pet_record", "add_pet_charge"):
            with self.subTest(tool=name):
                self.assertIn(name, tools)
                schema = self._input_schema(tools[name])
                self.assertTrue(schema.get("required"), f"{name} 未声明必填参数")
                self.assertEqual(schema.get("type"), "object")


if __name__ == "__main__":
    unittest.main(verbosity=2)
