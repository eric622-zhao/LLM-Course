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
import os
import sys

import requests
from flask import Flask, jsonify, request, send_from_directory

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


def fetch_workspaces() -> list[dict]:
    """读取工作区列表。优先免鉴权的本地接口，失败再退到开发者 API。"""
    api = CONFIG["api"]
    errors: list[str] = []

    for url, hdr in (
        (f"{api}/api/workspaces", {}),
        (f"{api}/api/v1/workspaces", headers()),
    ):
        try:
            resp = requests.get(url, headers=hdr, timeout=15)
        except requests.RequestException as exc:
            errors.append(f"{url}: {exc}")
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

    raise RuntimeError("无法读取工作区；" + "；".join(errors))


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
            r1 = requests.post(
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
                    r2 = requests.post(
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
        resp = requests.post(
            f"{api}/api/workspace/{slug}/update-embeddings",
            files={"adds": (filename, content, mimetype)},
            timeout=CONFIG["timeout"],
        )
    except requests.RequestException as exc:
        raise RuntimeError(f"连接 AnythingLLM 失败：{exc}") from exc

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
