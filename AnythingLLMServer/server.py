# -*- coding: utf-8 -*-
"""
AnythingLLM 文件上传服务
=========================

一个「简单、美观」的文件上传网页 + 一个把文件送进 AnythingLLM 工作区的后端。

    GET  /               上传网页（index.html）
    GET  /api/workspaces 读取 AnythingLLM 的工作区列表
    POST /api/upload     接收浏览器上传的文件，转发到目标工作区并向量化

默认把文件送进 **第一个工作区**；页面顶部可以切换。

运行
----
    python server.py
    python server.py --port 8099 --api http://127.0.0.1:3001

环境变量
--------
    ANYTHINGLLM_API        AnythingLLM 后端地址，默认 http://127.0.0.1:3001
    ANYTHINGLLM_API_KEY    Developer API Key（设置→工具→开发者 API）。留空则走本地免鉴权通道
    ANYTHINGLLM_WORKSPACE  指定目标工作区 slug，留空则用第一个
"""

from __future__ import annotations

import argparse
import json
import os
import sys

import requests
from urllib.parse import urlparse
from flask import Flask, Response, jsonify, request, send_from_directory

HERE = os.path.dirname(os.path.abspath(__file__))

app = Flask(__name__, static_folder=None)

CONFIG = {
    "api": os.environ.get("ANYTHINGLLM_API", "http://127.0.0.1:3001").rstrip("/"),
    "key": os.environ.get("ANYTHINGLLM_API_KEY", "").strip(),
    "workspace": os.environ.get("ANYTHINGLLM_WORKSPACE", "").strip(),
    "timeout": float(os.environ.get("ANYTHINGLLM_TIMEOUT", "120")),
}

# 单文件上限 200MB
app.config["MAX_CONTENT_LENGTH"] = 200 * 1024 * 1024


def headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {CONFIG['key']}"} if CONFIG["key"] else {}


_session: requests.Session | None = None
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0.0.0.0"}


def session() -> requests.Session:
    """统一的 HTTP 会话；目标是本机时**绕过系统代理**。

    踩过的坑：机器上开了代理（Clash / 公司网关）时，requests 会连
    `http://127.0.0.1:3001` 也丢给代理，代理再回 502 —— 页面显示
    「连不上 AnythingLLM」，其实本地服务好好的。trust_env=False 让它直连。
    """
    global _session
    if _session is None:
        _session = requests.Session()
        host = (urlparse(CONFIG["api"]).hostname or "").lower()
        if host in _LOCAL_HOSTS:
            _session.trust_env = False
    return _session


def _friendly_error(exc: Exception) -> str:
    """把 requests 的底层异常翻成一句人话。

    原始信息是 `HTTPConnectionPool(host='127.0.0.1', port=3001): Max retries
    exceeded ... [WinError 10061] 由于目标计算机积极拒绝`，对使用者没有意义 ——
    真正要传达的只有「服务没起来」这一个事实。
    """
    if isinstance(exc, requests.ConnectTimeout):
        return "连接超时"
    if isinstance(exc, requests.Timeout):
        return "请求超时"
    if isinstance(exc, requests.ConnectionError):
        text = str(exc)
        if any(k in text for k in ("10061", "Connection refused", "积极拒绝", "actively refused")):
            return "连接被拒绝（该端口上没有服务在监听）"
        if any(k in text for k in ("getaddrinfo", "Name or service not known", "nodename nor servname")):
            return "域名解析失败"
        if any(k in text for k in ("10060", "timed out", "超时")):
            return "连接超时"
        return "网络不可达"
    return type(exc).__name__


def fetch_workspaces() -> list[dict]:
    """读取工作区列表。优先免鉴权的本地接口，失败再退到开发者 API。"""
    api = CONFIG["api"]
    errors: list[str] = []

    for url, hdr in (
        (f"{api}/api/workspaces", {}),
        (f"{api}/api/v1/workspaces", headers()),
    ):
        try:
            resp = session().get(url, headers=hdr, timeout=15)
        except requests.RequestException as exc:
            errors.append(f"{url}: {_friendly_error(exc)}")
            continue
        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue
        try:
            payload = resp.json()
        except ValueError:
            errors.append(f"{url}: 返回不是 JSON")
            continue

        items = payload
        if isinstance(payload, dict):
            for key in ("workspaces", "data", "items"):
                if isinstance(payload.get(key), list):
                    items = payload[key]
                    break
        if isinstance(items, list):
            return [
                {"name": w.get("name") or w.get("slug"), "slug": w.get("slug")}
                for w in items
                if isinstance(w, dict) and w.get("slug")
            ]

    # 两条通道常因同一原因一起失败，去重后只说一次
    reasons = [e.split(": ", 1)[-1] for e in errors]
    detail = reasons[0] if reasons and len(set(reasons)) == 1 else "；".join(errors)
    raise RuntimeError("无法读取工作区：" + (detail or "未知原因"))


def pick_workspace(slug: str = "") -> str:
    if slug:
        return slug
    if CONFIG["workspace"]:
        return CONFIG["workspace"]
    items = fetch_workspaces()
    if not items:
        raise RuntimeError("AnythingLLM 里还没有任何工作区，请先新建一个。")
    return items[0]["slug"]


def push_document(slug: str, filename: str, content: bytes) -> dict:
    """把文件送进指定工作区，返回 {location, via} 之类的信息。"""
    api = CONFIG["api"]
    mimetype = "application/octet-stream"

    # 通道 A：开发者 API（需要 Key）——先上传文件，再把 location 加入工作区
    if CONFIG["key"]:
        try:
            r1 = session().post(
                f"{api}/api/v1/document/upload",
                headers=headers(),
                files={"file": (filename, content, mimetype)},
                timeout=CONFIG["timeout"],
            )
            if r1.status_code < 400:
                body = r1.json()
                docs = body.get("documents") or []
                location = (docs[0].get("location") if docs else None) or body.get("location")
                if location:
                    r2 = session().post(
                        f"{api}/api/v1/workspace/{slug}/update-embeddings",
                        headers={**headers(), "Content-Type": "application/json"},
                        json={"adds": [location]},
                        timeout=CONFIG["timeout"],
                    )
                    if r2.status_code < 400:
                        return {"location": location, "via": "developer-api"}
                else:
                    return {"location": None, "via": "developer-api", "raw": body}
        except (requests.RequestException, ValueError):
            pass  # 落到通道 B

    # 通道 B：本地免鉴权通道（AnythingLLM 网页端自己用的接口）
    try:
        resp = session().post(
            f"{api}/api/workspace/{slug}/update-embeddings",
            files={"adds": (filename, content, mimetype)},
            timeout=CONFIG["timeout"],
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"连接 AnythingLLM 失败：{_friendly_error(exc)}") from exc

    if resp.status_code >= 400:
        raise RuntimeError(
            f"AnythingLLM 返回 HTTP {resp.status_code}：{resp.text[:300]}"
        )

    try:
        body = resp.json()
    except ValueError:
        body = {}
    docs = body.get("documents") or []
    return {
        "location": (docs[0].get("location") if docs else None),
        "via": "local",
    }


# --------------------------------------------------------------------------- #
# 对话
# --------------------------------------------------------------------------- #
def _chat_body(message: str, mode: str, session_id: str) -> dict:
    body: dict = {"message": message, "mode": mode}
    if session_id:
        body["sessionId"] = session_id
    return body


def _sse(event: dict) -> str:
    return "data: " + json.dumps(event, ensure_ascii=False) + "\n\n"


def _stream_upstream(slug: str, message: str, mode: str, session_id: str):
    """有 Key 时：转发上游的 SSE，逐块 yield 事件。

    上游格式是 `data: {"type":"textResponseChunk","textResponse":"片段","close":false}`，
    我们把它翻译成前端好处理的三类事件：delta / sources / session。
    """
    url = f"{CONFIG['api']}/api/v1/workspace/{slug}/stream-chat"
    hdr = {**headers(), "Content-Type": "application/json", "Accept": "text/event-stream"}
    got_text = False
    sent_session = False

    with session().post(
        url,
        headers=hdr,
        json=_chat_body(message, mode, session_id),
        stream=True,
        timeout=CONFIG["timeout"],
    ) as resp:
        if resp.status_code >= 400:
            detail = resp.text[:300]
            yield _sse({
                "type": "error",
                "message": f"AnythingLLM 返回 {resp.status_code}：{detail}",
            })
            return

        for raw in resp.iter_lines(decode_unicode=True):
            if not raw:
                continue
            line = raw.strip()
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk or chunk == "[DONE]":
                continue
            try:
                payload = json.loads(chunk)
            except ValueError:
                continue

            kind = payload.get("type")
            # 上游每个 chunk 都带 id，只把第一次转给前端，避免重复设置会话
            if payload.get("id") and not sent_session:
                sent_session = True
                yield _sse({"type": "session", "sessionId": payload["id"]})

            if kind == "textResponseChunk":
                text = payload.get("textResponse") or ""
                if text:
                    got_text = True
                    yield _sse({"type": "delta", "text": text})
                if payload.get("sources"):
                    yield _sse({"type": "sources", "sources": _trim_sources(payload["sources"])})
                if payload.get("error"):
                    yield _sse({"type": "error", "message": str(payload["error"])})
            elif kind == "finalizeResponseStream":
                if payload.get("sources"):
                    yield _sse({"type": "sources", "sources": _trim_sources(payload["sources"])})

    if not got_text:
        yield _sse({"type": "error", "message": "上游没有返回任何文本，可能是工作区里还没有已向量化的文档。"})


def _blocking_upstream(slug: str, message: str, mode: str, session_id: str):
    """没有 Key 时：走本地免鉴权通道，拿到完整回答后当作一个 delta 发出。

    前端因此只需要一套 SSE 处理逻辑 —— 流式与否由后端吸收，
    这也是「通道降级对上层透明」的做法。
    """
    api = CONFIG["api"]
    errors: list[str] = []

    for url, hdr in (
        (f"{api}/api/workspace/{slug}/chat", {}),
        (f"{api}/api/v1/workspace/{slug}/chat", headers()),
    ):
        try:
            resp = session().post(
                url,
                headers={**hdr, "Content-Type": "application/json"},
                json=_chat_body(message, mode, session_id),
                timeout=CONFIG["timeout"],
            )
        except requests.RequestException as exc:
            errors.append(f"{url}: {_friendly_error(exc)}")
            continue

        if resp.status_code >= 400:
            errors.append(f"{url}: HTTP {resp.status_code}")
            continue
        try:
            payload = resp.json()
        except ValueError:
            errors.append(f"{url}: 返回不是 JSON")
            continue

        text = payload.get("textResponse") or payload.get("text") or ""
        if payload.get("id"):
            yield _sse({"type": "session", "sessionId": payload["id"]})
        if text:
            yield _sse({"type": "delta", "text": text})
        if payload.get("sources"):
            yield _sse({"type": "sources", "sources": _trim_sources(payload["sources"])})
        return

    # 两条通道往往因为同一个原因一起失败（比如服务没开），
    # 那就只说一次，否则同一句话重复两遍反而看不出重点
    reasons = [e.split(": ", 1)[-1] for e in errors]
    detail = reasons[0] if reasons and len(set(reasons)) == 1 else ("；".join(errors) or "未知原因")
    hint = (
        "对话失败。常见两种原因：\n"
        "1) AnythingLLM 桌面端没打开，或端口不是 3001；\n"
        "2) 使用对话接口需要 API Key —— 到 AnythingLLM → 设置 → API Keys 新建一个，"
        "设置环境变量 ANYTHINGLLM_API_KEY 后重启本服务。\n"
        f"检测结果：{detail}"
    )
    yield _sse({"type": "error", "message": hint})


def _trim_sources(sources) -> list[dict]:
    """只把展示用得上的字段传给前端，避免整包 chunk 塞进浏览器。"""
    out: list[dict] = []
    for item in sources or []:
        if not isinstance(item, dict):
            continue
        out.append({
            "title": item.get("title") or "未命名片段",
            "score": item.get("score"),
            "snippet": (item.get("text") or "")[:280],
        })
    return out


# --------------------------------------------------------------------------- #
# 路由
# --------------------------------------------------------------------------- #
@app.get("/")
def index():
    return send_from_directory(HERE, "index.html")


@app.get("/api/workspaces")
def api_workspaces():
    try:
        items = fetch_workspaces()
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc), "workspaces": []}), 502
    return jsonify({"success": True, "workspaces": items})


@app.post("/api/upload")
def api_upload():
    if "file" not in request.files:
        return jsonify({"success": False, "message": "没有收到文件字段 file"}), 400

    upload = request.files["file"]
    if not upload.filename:
        return jsonify({"success": False, "message": "文件名为空"}), 400

    try:
        slug = pick_workspace(request.form.get("slug", "").strip())
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

    content = upload.read()
    if not content:
        return jsonify({"success": False, "message": "文件内容为空"}), 400

    try:
        info = push_document(slug, upload.filename, content)
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc), "workspace": slug}), 502

    return jsonify({
        "success": True,
        "workspace": slug,
        "filename": upload.filename,
        "size": len(content),
        "location": info.get("location"),
        "via": info.get("via"),
    })


@app.get("/api/health")
def api_health():
    return jsonify({"success": True, "api": CONFIG["api"], "has_key": bool(CONFIG["key"])})


@app.post("/api/chat")
def api_chat():
    """在工作区里提问。

    统一返回 `text/event-stream`：有 Key 时转发上游流式，
    没有 Key 时把阻塞结果包装成一个 delta。前端因此只有一条渲染路径。
    """
    data = request.get_json(silent=True) or {}

    message = str(data.get("message") or "").strip()
    if not message:
        return jsonify({"success": False, "message": "问题不能为空"}), 400

    mode = str(data.get("mode") or "query").strip().lower()
    if mode not in ("query", "chat"):
        return jsonify({"success": False, "message": "mode 只能是 query 或 chat"}), 400

    session_id = str(data.get("sessionId") or "").strip()

    try:
        slug = pick_workspace(str(data.get("slug") or "").strip())
    except RuntimeError as exc:
        return jsonify({"success": False, "message": str(exc)}), 502

    want_stream = bool(data.get("stream")) and bool(CONFIG["key"])
    source = _stream_upstream if want_stream else _blocking_upstream

    def generate():
        try:
            for event in source(slug, message, mode, session_id):
                yield event
        except requests.RequestException as exc:
            yield _sse({"type": "error", "message": f"连接 AnythingLLM 失败：{_friendly_error(exc)}"})
        except Exception as exc:  # noqa: BLE001  流里抛异常前端只能看到断流，这里兜住
            yield _sse({"type": "error", "message": f"服务端异常：{exc}"})
        finally:
            yield _sse({"type": "done", "workspace": slug, "mode": mode})

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",   # 关掉 nginx 类反代的缓冲，否则流式会憋成一坨
            "Connection": "keep-alive",
        },
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="AnythingLLM 文件上传服务")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8092, help="默认 8092")
    parser.add_argument("--api", help="AnythingLLM 后端地址")
    parser.add_argument("--key", help="AnythingLLM Developer API Key")
    parser.add_argument("--workspace", help="目标工作区 slug")
    args = parser.parse_args()

    if args.api:
        CONFIG["api"] = args.api.rstrip("/")
    if args.key:
        CONFIG["key"] = args.key.strip()
    if args.workspace:
        CONFIG["workspace"] = args.workspace.strip()

    print(f"[anythingllm-server] 上传页 -> http://{args.host}:{args.port}/")
    print(f"[anythingllm-server] 后端   -> {CONFIG['api']}"
          f"（{'已带 API Key' if CONFIG['key'] else '本地免鉴权通道'}）")
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
