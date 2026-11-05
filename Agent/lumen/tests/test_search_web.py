# -*- coding: utf-8 -*-
"""
tests.test_search_web · searchWeb 的离线测试
=============================================

不联网、不消耗额度：把 requests 换成假模块，专测三件事 ——

1. 真实响应结构（顶层只有 references，没有 choices）能正确解析；
2. 脏数据（JSON 当标题、站名填「无」）会被清洗成人话；
3. 网络全挂时降级到明确的错误说明，且措辞能阻止模型连环换词重试。

跑法：
    py -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import requests as real_requests  # noqa: E402  只为复用真实异常类

from core import config  # noqa: E402
import core.tools  # noqa: E402
from core.registry import dispatch  # noqa: E402

CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config.ini"
)

# 从真实接口抓下来的一条结构样本：顶层没有 choices，只有 references
REAL_PAYLOAD = {
    "request_id": "b86239a0-0a62-4902-992a-a6812f467815",
    "references": [
        {
            "id": 1,
            "url": "https://gz.nsu.edu.cn/2025/70043F21.html",
            "title": "祝贺!软件工程技术系谢心老师斩获四川省教育厅教学竞赛一等奖",
            "date": "2025-09-25 10:00:00",
            "content": "软件工程技术系谢心老师斩获四川省教育厅教学竞赛一等奖。近日，由四川省教育厅主办的"
            "首届教师人工智能应用能力大赛获奖名单正式揭晓。",
            "website": "成都东软学院",
            "rerank_score": 0.93,
        },
        {
            "id": 2,
            "url": "https://mp.weixin.qq.com/s?__biz=MzA4NDcyMzkwNw==",
            "title": '{"code":0,"stg_version":"v1","strategy_result":{"page_block":[]}}',
            "date": "2026-11-01 08:00:00",
            "content": "📢成都东软学院开展2026年下半年团员发展对象培训班第四堂团课 📢青年志愿者协会开展秋季团建活动",
            "website": "腾讯网",
        },
        {
            "id": 3,
            "url": "https://baike.baidu.com/item/成都东软学院",
            "title": "成都东软学院",
            "date": "2026-10-24 12:00:00",
            "content": "成都东软学院位于四川省成都市都江堰青城山东软大道1号，是经教育部批准设立的民办普通本科高校。",
            "website": "无",
        },
        {
            "id": 4,
            "url": "https://mp.weixin.qq.com/s?__biz=MzA4NDcyMzkwNw==",  # 与第 2 条重复
            "title": "重复条目",
            "content": "同一链接不应重复出现。",
            "website": "腾讯网",
        },
    ],
}


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or json.dumps(self._payload, ensure_ascii=False)

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


def _install_fake_requests(behavior: list):
    """把 requests 换成假的。

    behavior 是每次 post 调用的行为列表，元素为：
        异常类（直接抛出） | dict（作为 200 响应体） | (状态码, 文本)
    列表用完后一律抛 ConnectionError，模拟「所有通道都挂了」。
    """
    calls: list[bool] = []  # 记录每次是否 trust_env（True=走代理，False=直连）

    class _FakeSession:
        def __init__(self):
            self.trust_env = True

        def post(self, *args, **kwargs):
            calls.append(self.trust_env)
            item = behavior.pop(0) if behavior else real_requests.exceptions.ConnectionError
            if isinstance(item, type) and issubclass(item, BaseException):
                raise item("simulated")
            if isinstance(item, BaseException):
                raise item
            if isinstance(item, dict):
                return _FakeResponse(200, item)
            status, text = item
            return _FakeResponse(status, None, text)

        def close(self):
            pass

    fake = types.ModuleType("requests")
    fake.Session = _FakeSession
    fake.exceptions = real_requests.exceptions
    fake.post = lambda *a, **k: _FakeSession().post(*a, **k)
    return fake, calls


class SearchWebTest(unittest.TestCase):
    def setUp(self):
        # 用真实 config.ini 里的结构，只看格式，不发请求
        config.load(CONFIG_PATH if os.path.exists(CONFIG_PATH) else None)
        self._set_key("bce-v3/ALTAK-fake/key")
        self._real_requests = sys.modules.get("requests")

    @staticmethod
    def _set_key(value: str) -> None:
        parser = config._require_loaded()
        if not parser.has_section("search"):
            parser.add_section("search")
        parser.set("search", "baidu_qianfan_api_key", value)

    def tearDown(self):
        if self._real_requests is not None:
            sys.modules["requests"] = self._real_requests
        else:
            sys.modules.pop("requests", None)

    def _run(self, behavior, args=None):
        fake, calls = _install_fake_requests(list(behavior))
        sys.modules["requests"] = fake
        out = dispatch("searchWeb", json.dumps(args or {"query": "成都东软学院 谢心"}, ensure_ascii=False))
        return out, calls

    # --- 1. 正常解析 ---
    def test_parses_real_structure(self):
        out, _ = self._run([dict(REAL_PAYLOAD)])
        self.assertIn("搜索完成", out)
        self.assertIn("谢心老师斩获四川省教育厅教学竞赛一等奖", out)
        self.assertIn("https://gz.nsu.edu.cn/2025/70043F21.html", out)
        # 顶层没有 choices，不应凭空造出摘要
        self.assertNotIn("【搜索摘要】", out)

    def test_json_title_is_cleaned(self):
        out, _ = self._run([dict(REAL_PAYLOAD)])
        self.assertNotIn('"stg_version"', out)
        self.assertNotIn("strategy_result", out)
        # 用正文首句兜底
        self.assertIn("成都东软学院开展2026年下半年团员发展对象培训班", out)

    def test_placeholder_site_falls_back_to_domain(self):
        out, _ = self._run([dict(REAL_PAYLOAD)])
        self.assertIn("站点：baike.baidu.com", out)
        self.assertNotIn("站点：无", out)

    def test_duplicate_url_deduped(self):
        out, _ = self._run([dict(REAL_PAYLOAD)])
        self.assertEqual(out.count("__biz=MzA4NDcyMzkwNw=="), 1)
        self.assertIn("来源数：3", out)

    def test_date_trimmed_to_day(self):
        out, _ = self._run([dict(REAL_PAYLOAD)])
        self.assertIn("时间：2025-09-25", out)
        self.assertNotIn("2025-09-25 10:00:00", out)

    def test_snippet_truncated(self):
        long_text = "这是一段很长的正文。" * 200
        payload = {"references": [{"url": "https://e.com/a", "title": "长文", "content": long_text}]}
        out, _ = self._run([payload])
        self.assertIn("摘要已截断", out)

    def test_empty_result(self):
        out, _ = self._run([{"request_id": "x", "references": []}])
        self.assertIn("没有返回任何结果", out)

    # --- 2. 通道切换 ---
    def test_falls_back_to_direct_when_proxy_times_out(self):
        out, calls = self._run(
            [real_requests.exceptions.Timeout, dict(REAL_PAYLOAD)]
        )
        self.assertIn("谢心老师", out)
        self.assertEqual(calls, [True, False])  # 先代理，再直连

    # --- 3. 全挂时的措辞 ---
    def test_all_channels_down_message(self):
        out, calls = self._run([])  # 空列表 → 三次全抛 ConnectionError
        self.assertIn("联网搜索连不上", out)
        self.assertIn("网络故障", out)
        self.assertIn("不等于没有搜到结果", out)
        self.assertIn("不要", out)  # 明确劝阻换词重试
        self.assertEqual(len(calls), 3)

    def test_auth_error_not_retried(self):
        out, calls = self._run([(401, '{"error":"invalid key"}')])
        self.assertIn("鉴权失败", out)
        self.assertIn("不要重试", out)
        self.assertEqual(len(calls), 1)  # 401 不该走通道切换

    def test_empty_query_rejected(self):
        out = dispatch("searchWeb", json.dumps({"query": "   "}))
        self.assertIn("query 不能为空", out)

    def test_missing_key_message(self):
        self._set_key("替换成你的千帆key")
        out = dispatch("searchWeb", json.dumps({"query": "随便"}))
        self.assertIn("尚未配置", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
