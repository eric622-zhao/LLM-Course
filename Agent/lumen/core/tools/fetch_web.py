# -*- coding: utf-8 -*-
"""
core.tools.fetch_web · 网页正文获取（第七周 · 任务一）
=====================================================

工具：fetchWeb
    给一个 URL → 拿回这篇网页的**正文**，转成 Markdown 交给模型。

两步走
------
1. 用 Python 的 subprocess 调本地 curl，取回 HTML 源码。
   * 参数**列表**方式传参，绝不拼命令行字符串，shell=False → 天然免疫
     `; rm -rf /`、`|` 这类 shell 注入；
   * `-o` 写到临时文件、`-w` 只回状态码，避免把 HTML 和元信息混在一起。
2. 用 trafilatura 从 HTML 里抽正文并转 Markdown。

为什么要抽正文而不是把整页 HTML 丢给模型？
    一个新闻页的 HTML 通常 200KB 起，其中正文不到 5%，剩下全是导航、广告、
    脚本、样式、页脚。整页塞进去既烧 token 又降低回答质量 ——
    正文抽取能把 token 消耗砍掉一个数量级，且保留标题/段落/列表/表格/链接。

异常处理（都会转成模型看得懂的中文，不抛异常）
    curl 不存在 / DNS 解析失败 / 连接失败 / 超时 / SSL 错误 /
    HTTP 4xx-5xx / 非 HTML 内容 / 正文抽不出来（多半是 JS 渲染的页面）。
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from core import config
from ..registry import tool, truncate

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# curl 退出码 → 人话。完整列表见 `man curl` 的 EXIT CODES
_CURL_ERRORS = {
    1: "不支持的协议",
    3: "URL 格式不正确",
    5: "无法解析代理地址",
    6: "域名解析失败（检查网址拼写或网络连接）",
    7: "无法连接到服务器（端口不通或被拒绝）",
    18: "传输提前中断（服务器只发了一部分就断开）",
    23: "本地写入失败",
    28: "请求超时（服务器响应太慢）",
    35: "SSL/TLS 握手失败（证书问题或连接被干扰）",
    47: "重定向次数过多",
    52: "服务器返回了空响应",
    56: "接收数据失败（连接被重置）",
    60: "HTTPS 证书校验失败",
}


# ---------------------------------------------------------------------------
# 辅助：定位 curl、解码 HTML
# ---------------------------------------------------------------------------
def _find_curl() -> str | None:
    """优先用 PATH 里的 curl；Windows 10+ 的 System32 也自带一个。"""
    found = shutil.which("curl")
    if found:
        return found
    if os.name == "nt":
        candidate = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "curl.exe")
        if os.path.exists(candidate):
            return candidate
    return None


def _decode_html(raw: bytes, content_type: str) -> str:
    """按 HTTP 头 → <meta charset> → UTF-8 → GB18030 的顺序尝试解码。"""
    candidates: list[str] = []

    m = re.search(r"charset=[\"']?([\w\-]+)", content_type or "", re.I)
    if m:
        candidates.append(m.group(1))

    head = raw[:4096].decode("ascii", "ignore")
    m = re.search(r"<meta[^>]+charset=[\"']?([\w\-]+)", head, re.I)
    if m:
        candidates.append(m.group(1))

    candidates += ["utf-8", "gb18030"]

    tried: set[str] = set()
    for enc in candidates:
        enc = enc.lower().strip()
        if enc in ("gb2312", "gbk"):
            enc = "gb18030"  # 超集，能覆盖绝大多数中文站点
        if enc in tried:
            continue
        tried.add(enc)
        try:
            return raw.decode(enc)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("utf-8", "replace")


def _curl_fetch(url: str, timeout: int) -> tuple[int, str, str, bytes]:
    """执行 curl。返回 (退出码, 错误说明, content-type, 响应体)。"""
    curl = _find_curl()
    if not curl:
        return 127, "本机没有找到 curl 程序，无法抓取网页。", "", b""

    fd, tmp_path = tempfile.mkstemp(prefix="lumen_fetch_", suffix=".html")
    os.close(fd)
    argv = [
        curl,
        "-sS",           # 静默进度条，但保留错误信息
        "-L",            # 跟随重定向
        "--compressed",  # 自动解压 gzip / br
        "--max-time", str(timeout),
        "-A", USER_AGENT,
        "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
        "-o", tmp_path,
        "-w", "%{http_code}\t%{content_type}",
        url,
    ]

    try:
        # 关键：参数列表 + shell=False
        proc = subprocess.run(argv, capture_output=True, timeout=timeout + 10, shell=False)
    except subprocess.TimeoutExpired:
        _safe_unlink(tmp_path)
        return 28, f"请求超时（超过 {timeout} 秒），该网站可能响应很慢或不可达。", "", b""
    except FileNotFoundError:
        _safe_unlink(tmp_path)
        return 127, "无法执行 curl（文件不存在）。", "", b""
    except OSError as exc:
        _safe_unlink(tmp_path)
        return 126, f"无法执行 curl：{exc}", "", b""

    stderr = (proc.stderr or b"").decode("utf-8", "replace").strip()
    meta = (proc.stdout or b"").decode("utf-8", "replace").strip()
    http_code, _, content_type = meta.partition("\t")

    body = b""
    try:
        if os.path.exists(tmp_path):
            with open(tmp_path, "rb") as fh:
                body = fh.read()
    finally:
        _safe_unlink(tmp_path)

    if proc.returncode != 0:
        reason = _CURL_ERRORS.get(proc.returncode, stderr or f"curl 退出码 {proc.returncode}")
        return proc.returncode, reason, content_type, body

    try:
        code = int(http_code)
    except (TypeError, ValueError):
        code = 0
    return code, stderr, content_type, body


def _safe_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _extract(html: str, url: str) -> tuple[str, dict]:
    """用 trafilatura 抽正文，返回 (markdown, 元信息)。任何异常都不外抛。"""
    info: dict = {"title": "", "sitename": "", "date": "", "author": ""}
    try:
        import trafilatura
    except ImportError:
        return "", {"error": "未安装 trafilatura，请执行 pip install trafilatura"}

    # 元信息（标题等）；失败不影响正文
    try:
        meta = trafilatura.extract_metadata(html, default_url=url)
        if meta is not None:
            info["title"] = (meta.title or "").strip()
            info["sitename"] = (meta.sitename or "").strip()
            info["date"] = (meta.date or "").strip()
            info["author"] = (meta.author or "").strip()
    except Exception:  # noqa: BLE001 - 元信息属于加分项
        pass

    # 第一轮：标准精度，保留标题层级 / 列表 / 表格 / 链接
    for kwargs in (
        dict(output_format="markdown", include_links=True, include_tables=True,
             include_formatting=True, include_comments=False, favor_recall=True),
        # 第二轮：放宽正文判定（有些博客页面结构松散，严格模式会判成"没正文"）
        dict(output_format="markdown", include_links=True, include_tables=True,
             include_formatting=True, include_comments=False, favor_recall=True,
             no_fallback=False),
    ):
        try:
            text = trafilatura.extract(html, url=url, **kwargs)
        except Exception:  # noqa: BLE001
            continue
        if text and len(text.strip()) >= 40:
            return text.strip(), info

    # 最后一轮：只要纯文本，聊胜于无
    try:
        text = trafilatura.extract(html, url=url, output_format="txt", favor_recall=True)
        if text and text.strip():
            return text.strip(), info
    except Exception:  # noqa: BLE001
        pass

    return "", info


def _fallback_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if not m:
        return ""
    return re.sub(r"\s+", " ", m.group(1)).strip()[:200]


def _normalize(markdown: str) -> str:
    """压缩多余空行，并把行尾空格清掉，省 token。"""
    text = re.sub(r"[ \t]+\n", "\n", markdown)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


# ---------------------------------------------------------------------------
# 工具本体
# ---------------------------------------------------------------------------
@tool(
    name="fetchWeb",
    description=(
        "获取指定网页的主要内容。传入一个 URL，返回该页面的正文（Markdown 格式，保留标题、段落、"
        "列表、表格和链接），已自动去掉导航栏、广告、页脚等无关内容。"
        "使用场景：用户给出一个网址让你阅读/总结；或者你需要查证某个具体页面的信息时。"
        "只适合 HTML 网页；PDF、视频、图片等无法提取。若返回「正文提取失败」，"
        "多半是该页面依赖 JavaScript 动态渲染，可改用 searchWeb 找其他来源。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "url": {
                "type": "string",
                "description": "要获取的网页完整地址，必须以 http:// 或 https:// 开头",
            },
            "max_chars": {
                "type": "integer",
                "description": "正文最多返回多少字符，默认 12000。超长正文会被截断并注明",
            },
            "timeout": {
                "type": "integer",
                "description": "请求超时秒数，默认 20，上限 60",
            },
        },
        "required": ["url"],
    },
    title="网页获取",
    category="网络",
)
def fetch_web(args: dict) -> str:
    url = str(args.get("url") or "").strip().strip('"').strip("'")
    if not url:
        return "错误：url 不能为空。请提供以 http:// 或 https:// 开头的完整网址。"

    if not re.match(r"^https?://", url, re.I):
        return f"错误：URL 必须以 http:// 或 https:// 开头，收到的是 {url!r}。"

    default_timeout = config.get_int("tools", "fetchweb_timeout", 20)
    try:
        timeout = int(args.get("timeout") or default_timeout)
    except (TypeError, ValueError):
        timeout = default_timeout
    timeout = max(5, min(timeout, 60))

    max_chars = config.get_int("tools", "fetchweb_max_chars", 12000)
    try:
        requested = args.get("max_chars")
        if requested is not None:
            max_chars = max(500, min(int(requested), 60000))
    except (TypeError, ValueError):
        pass

    code, detail, content_type, body = _curl_fetch(url, timeout)

    if code == 127:
        return f"错误：{detail}"
    if code in _CURL_ERRORS and code != 0 and not body:
        return f"错误：抓取失败 — {detail}"
    if code == 0:
        return f"错误：抓取失败 — {detail or '没有拿到 HTTP 状态码'}。"

    if code >= 400:
        hints = {
            401: "该页面需要登录或授权",
            403: "服务器拒绝访问（可能是反爬虫策略，需要浏览器 UA / Cookie）",
            404: "页面不存在，请检查网址是否正确",
            429: "请求过于频繁被限流",
        }
        hint = hints.get(code, "服务器返回错误")
        return (
            f"错误：网页返回 HTTP {code}（{hint}）。\n"
            f"URL：{url}\n"
            f"建议：换一个来源，或用 searchWeb 搜索同一主题。"
        )

    if not body:
        return f"错误：网页返回 HTTP {code}，但内容为空。URL：{url}"

    ctype = (content_type or "").lower()
    if ctype and not any(k in ctype for k in ("html", "xml", "text/plain")):
        return (
            f"错误：该地址返回的不是网页，而是 {content_type}。\n"
            f"fetchWeb 只能处理 HTML 网页；PDF / 图片 / 音视频请让用户直接提供内容或换用其他来源。"
        )

    html = _decode_html(body, content_type or "")

    if len(html) < 200 and "<html" not in html.lower():
        return (
            f"错误：返回内容过短（{len(html)} 字符），不像一个正常网页。\n"
            f"开头内容：{html[:200]!r}"
        )

    markdown, info = _extract(html, url)
    if info.get("error"):
        return f"错误：{info['error']}"

    title = info.get("title") or _fallback_title(html) or "（无标题）"

    if not markdown:
        return (
            f"错误：正文提取失败。页面抓到了（{len(html)} 字符 HTML），但抽不出正文。\n"
            f"URL：{url}\n"
            f"标题：{title}\n"
            f"常见原因：\n"
            f"  1. 页面正文由 JavaScript 动态渲染，curl 拿到的是空壳 HTML；\n"
            f"  2. 页面主体是图片或需要登录后才可见；\n"
            f"  3. 页面结构异常，trafilatura 判定为「无正文」。\n"
            f"建议：改用 searchWeb 搜索该主题，或让用户直接粘贴页面内容。"
        )

    body_text = _normalize(markdown)

    header = [f"网页获取成功：{title}", f"URL：{url}"]
    extras = []
    if info.get("sitename"):
        extras.append(f"站点：{info['sitename']}")
    if info.get("date"):
        extras.append(f"发布时间：{info['date']}")
    if info.get("author"):
        extras.append(f"作者：{info['author']}")
    extras.append(f"正文长度：{len(body_text)} 字符（已转 Markdown）")
    header.append("  ·  ".join(extras))
    header.append("─" * 40)

    return "\n".join(header) + "\n" + truncate(body_text, max_chars, label="网页正文")
