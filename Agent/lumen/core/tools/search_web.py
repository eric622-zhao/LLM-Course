# -*- coding: utf-8 -*-
"""
core.tools.search_web · 联网搜索（第七周 · 任务二）
===================================================

工具：searchWeb
    给一句 query → 调百度千帆「AI 搜索 web_search」→ 把摘要 + 来源标题/URL 整理后交给模型。

为什么不用自己抓百度搜索结果页？
    因为结果页有反爬、有验证码、结构随时改。走官方 API 拿的是**结构化数据**，
    稳定、合法，而且它已经帮你做了摘要，token 效率更高。

鉴权
----
API Key 从 config.ini 的 [search] baidu_qianfan_api_key 读取，
**不硬编码、不落日志**。缺配置时返回明确的错误，引导用户去填。

接口（见 https://cloud.baidu.com/doc/qianfan-api/s/Wmbq4z7e5）
    POST https://qianfan.baidubce.com/v2/ai_search/web_search
    Authorization: Bearer <API Key>
    body: {"messages":[{"role":"user","content":query}],
           "search_source":"baidu_search_v2",
           "resource_type_filter":[{"type":"web","top_k":top_k}]}

返回体是分模态的 references 数组，字段名在不同版本略有差异，
所以下面的解析做了**多字段名兼容**（url/link、content/snippet/summary/abstract），
避免接口小改一次工具就废掉。
"""

from __future__ import annotations

import re
from urllib.parse import urlparse

from core import config
from ..registry import tool, truncate

ENDPOINT = "https://qianfan.baidubce.com/v2/ai_search/web_search"
DEFAULT_TOP_K = 8
MAX_TOP_K = 20
SNIPPET_CHARS = 360
RECENCY_VALUES = ("week", "month", "semiyear", "year")


def _query_units(text: str) -> int:
    """按百度的口径算长度：一个汉字算 2。"""
    return sum(2 if ord(ch) > 127 else 1 for ch in text)


def _clip_query(text: str, limit: int = 72) -> tuple[str, bool]:
    """query 超过 72 个单位会被服务端截断，这里主动截好并告知模型。"""
    if _query_units(text) <= limit:
        return text, False
    out: list[str] = []
    used = 0
    for ch in text:
        cost = 2 if ord(ch) > 127 else 1
        if used + cost > limit:
            break
        out.append(ch)
        used += cost
    return "".join(out), True


def _pick(item: dict, *keys: str) -> str:
    for key in keys:
        value = item.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def _site_of(url: str) -> str:
    try:
        host = urlparse(url).netloc
        return host[4:] if host.startswith("www.") else host
    except ValueError:
        return ""


def _error_from_body(status: int, body: str) -> str:
    """把接口返回的错误体翻译成模型能据此行动的说明。"""
    detail = re.sub(r"\s+", " ", body or "").strip()
    if status in (401, 403):
        return (
            f"错误：搜索 API 鉴权失败（HTTP {status}）。\n"
            f"原因：config.ini 的 [search] baidu_qianfan_api_key 无效、过期，"
            f"或该 Key 没有开通「百度搜索」服务。\n"
            f"接口返回：{detail[:300]}\n"
            f"建议：请用户在千帆控制台确认 API Key 与服务开通状态，"
            f"本工具无法自行修复，不要重试。"
        )
    if status == 429:
        return (
            f"错误：搜索 API 触发限流（HTTP 429），可能是免费额度用尽或请求过快。\n"
            f"接口返回：{detail[:300]}\n"
            f"建议：稍后再试，或直接告知用户当前配额受限。"
        )
    return (
        f"错误：搜索 API 返回 HTTP {status}。\n"
        f"接口返回：{detail[:400]}"
    )


@tool(
    name="searchWeb",
    description=(
        "联网搜索互联网上的最新信息。传入搜索关键词或问题，返回搜索摘要、参考来源的标题与 URL。"
        "使用场景：用户问时事、新闻、价格、版本、人物近况等「模型训练数据之后可能已变化」的信息；"
        "或者你不确定答案、需要外部依据时。拿到结果后如果某个来源看起来最权威，"
        "可以再用 fetchWeb 打开该 URL 读全文。"
        "注意：query 应简短精准（不超过 72 个字符，汉字算 2 个），不要整段粘贴长文。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "搜索关键词或问句，例如「2026 年诺贝尔物理学奖 得主」",
            },
            "top_k": {
                "type": "integer",
                "description": f"返回结果条数，默认 {DEFAULT_TOP_K}，范围 1~{MAX_TOP_K}",
            },
            "recency": {
                "type": "string",
                "enum": list(RECENCY_VALUES),
                "description": "按发布时间过滤：week 近7天 / month 近30天 / semiyear 近180天 / year 近365天。不填则不限",
            },
        },
        "required": ["query"],
    },
    title="联网搜索",
    category="网络",
)
def search_web(args: dict) -> str:
    raw_query = str(args.get("query") or "").strip()
    if not raw_query:
        return "错误：query 不能为空。请提供要搜索的关键词或问题。"

    api_key = config.get("search", "baidu_qianfan_api_key")
    if config.missing_or_placeholder(api_key):
        return (
            "错误：尚未配置百度千帆的搜索 API Key，无法联网搜索。\n"
            "请打开 config.ini，在 [search] 小节填写：\n"
            "    baidu_qianfan_api_key = 你的千帆 API Key\n"
            "（Key 在 https://console.bce.baidu.com/qianfan/ 的「API Key 管理」里创建）\n"
            "当前请先告知用户：联网搜索不可用，只能用已有知识回答或让用户提供资料。"
        )

    try:
        import requests
    except ImportError:
        return "错误：缺少 requests 库，无法发起搜索请求。请执行 pip install requests。"

    default_k = config.get_int("tools", "search_top_k", DEFAULT_TOP_K)
    try:
        top_k = int(args.get("top_k") or default_k)
    except (TypeError, ValueError):
        top_k = default_k
    top_k = max(1, min(top_k, MAX_TOP_K))

    query, clipped = _clip_query(raw_query)

    payload: dict = {
        "messages": [{"role": "user", "content": query}],
        "search_source": "baidu_search_v2",
        "resource_type_filter": [{"type": "web", "top_k": top_k}],
        "query_policy": {"enable_rewrite": True},
    }

    recency = str(args.get("recency") or "").strip().lower()
    if recency in RECENCY_VALUES:
        payload["search_recency_filter"] = recency

    timeout = config.get_int("tools", "search_timeout", 25)

    try:
        resp = requests.post(
            ENDPOINT,
            json=payload,
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            timeout=(6, timeout),
        )
    except requests.exceptions.Timeout:
        return f"错误：搜索请求超时（{timeout} 秒）。可能是网络不稳定或搜索服务繁忙，可以稍后重试一次。"
    except requests.exceptions.SSLError as exc:
        return f"错误：搜索服务 SSL 连接失败 — {exc}"
    except requests.exceptions.ConnectionError as exc:
        return (
            f"错误：无法连接搜索服务（{ENDPOINT}）。\n"
            f"这通常是本机网络或代理问题：{type(exc).__name__}。\n"
            f"建议：如实告知用户联网搜索暂时不可用。"
        )
    except requests.exceptions.RequestException as exc:
        return f"错误：搜索请求失败 — {type(exc).__name__}: {exc}"

    if resp.status_code != 200:
        return _error_from_body(resp.status_code, resp.text)

    try:
        data = resp.json()
    except ValueError:
        return (
            f"错误：搜索接口返回的不是合法 JSON（前 300 字符）：{resp.text[:300]!r}"
        )

    if isinstance(data, dict) and data.get("code") and str(data.get("code")) not in ("0", "200"):
        return (
            f"错误：搜索接口业务错误 code={data.get('code')}，message={data.get('message') or data.get('msg')}\n"
            f"建议：如果是鉴权或权限类错误，直接告知用户需要检查 API Key 与服务开通状态，不要重复调用。"
        )

    # --- 解析摘要 ---
    summary = ""
    choices = data.get("choices") or []
    if isinstance(choices, list) and choices:
        message = choices[0].get("message") or {}
        if isinstance(message, dict):
            summary = str(message.get("content") or "").strip()

    # --- 解析来源（兼容多种字段名）---
    raw_refs = (
        data.get("references")
        or data.get("search_results")
        or data.get("results")
        or []
    )
    sources: list[dict] = []
    if isinstance(raw_refs, list):
        for item in raw_refs:
            if not isinstance(item, dict):
                continue
            url = _pick(item, "url", "link", "href")
            title = _pick(item, "title", "name")
            content = _pick(item, "content", "snippet", "summary", "abstract", "text")
            if not url and not title:
                continue
            sources.append(
                {
                    "title": title or _site_of(url) or "（无标题）",
                    "url": url,
                    "content": content,
                    "date": _pick(item, "date", "page_time", "publish_time", "time"),
                    "site": _pick(item, "website", "site", "site_name") or _site_of(url),
                }
            )

    usage = data.get("usage") or {}
    total_tokens = usage.get("total_tokens") if isinstance(usage, dict) else None

    if not sources and not summary:
        return (
            f"错误：搜索「{query}」没有返回任何结果。\n"
            f"可能原因：关键词过于生僻 / 被安全策略过滤 / 接口返回结构变更。\n"
            f"建议：换一组更常见的关键词重试一次；仍为空就如实告诉用户没搜到。"
        )

    lines: list[str] = [f"搜索完成：{query}"]
    meta = [f"引擎：百度千帆 AI 搜索（baidu_search_v2）", f"来源数：{len(sources)}"]
    if recency in RECENCY_VALUES:
        meta.append(f"时间范围：{ {'week': '近7天', 'month': '近30天', 'semiyear': '近180天', 'year': '近365天'}[recency] }")
    if total_tokens:
        meta.append(f"搜索 tokens：{total_tokens}")
    lines.append("  ·  ".join(meta))
    if clipped:
        lines.append(f"提示：query 超出 72 字符上限，已截断为「{query}」。")
    lines.append("")

    if summary:
        lines.append("【搜索摘要】")
        lines.append(summary)
        lines.append("")

    if sources:
        lines.append("【参考来源】")
        for index, src in enumerate(sources, 1):
            lines.append(f"{index}. {src['title']}")
            if src["site"]:
                lines.append(f"   站点：{src['site']}")
            if src["url"]:
                lines.append(f"   链接：{src['url']}")
            if src["date"]:
                lines.append(f"   时间：{src['date']}")
            if src["content"]:
                snippet = re.sub(r"\s+", " ", src["content"]).strip()
                lines.append(f"   摘要：{truncate(snippet, SNIPPET_CHARS, label='摘要')}")
            lines.append("")

    lines.append(
        "使用建议：以上是搜索结果的原文摘录，可能含过时或不准确的信息。"
        "回答时请注明来源，必要时先用 fetchWeb 打开其中最相关的一两条核对。"
    )

    return "\n".join(lines).strip()
