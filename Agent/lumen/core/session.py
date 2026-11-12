# -*- coding: utf-8 -*-
"""
core.session · 会话持久化
=========================

关掉窗口，上下文就没了 —— 这是最影响实用性的一处缺陷。
这里把 `Agent.messages` 原样存成 JSON，下次可以接着聊。

存什么
------
    .lumen/sessions/<id>.json
    {
      "id": "20261112-142530",
      "title": "帮我查一下…",          ← 取第一条用户消息，方便列表里认出来
      "created": 1699..., "updated": 1699...,
      "model": "doubao-seed-evolving",
      "turns": 3,
      "messages": [ ...原始消息数组... ],
      "meta": {"rules_file": "AGENT.md", "skills": 2}
    }

为什么存整个 messages 而不是自己定义的对话记录
----------------------------------------------
因为 Function Calling 的历史是有结构的：assistant 的 tool_calls 与后续
role=tool 的 tool_call_id **必须成对**，否则下一轮请求会被 API 拒绝。
原样保存是最不容易出错的做法。

安全
----
目录固定在项目根内，恢复时只认 `.lumen/sessions/` 下的文件名，
不接受任意路径 —— 避免「模型编个 id 让它去读别的文件」。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import datetime

SESSION_DIRNAME = os.path.join(".lumen", "sessions")
ID_RE = re.compile(r"^[0-9]{8}-[0-9]{6}(-[a-z0-9]{1,12})?$")
MAX_TITLE_CHARS = 40


@dataclass
class SessionInfo:
    id: str
    title: str
    updated: float
    turns: int
    model: str
    path: str

    def display(self) -> str:
        when = datetime.fromtimestamp(self.updated).strftime("%m-%d %H:%M")
        return f"{self.id}  {when}  {self.turns} 轮  {self.title}"


def _now_id() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def _title_from(messages: list[dict]) -> str:
    for message in messages:
        if message.get("role") == "user":
            text = re.sub(r"\s+", " ", str(message.get("content") or "")).strip()
            if text:
                return text[:MAX_TITLE_CHARS] + ("…" if len(text) > MAX_TITLE_CHARS else "")
    return "（空会话）"


class SessionStore:
    def __init__(self, project_root: str) -> None:
        self.root = os.path.realpath(project_root)
        self.base = os.path.join(self.root, SESSION_DIRNAME)

    def _ensure(self) -> bool:
        try:
            os.makedirs(self.base, exist_ok=True)
            return True
        except OSError:
            return False

    def _path(self, session_id: str) -> str | None:
        """校验 id 后返回路径。非法的 id 一律拒绝，不拼路径。"""
        if not ID_RE.match(session_id or ""):
            return None
        full = os.path.realpath(os.path.join(self.base, f"{session_id}.json"))
        if not full.startswith(os.path.realpath(self.base) + os.sep):
            return None
        return full

    # -- 写 -----------------------------------------------------------------
    def save(
        self,
        messages: list[dict],
        session_id: str | None = None,
        model: str = "",
        meta: dict | None = None,
    ) -> SessionInfo | None:
        """保存会话。session_id 为空则新建。返回会话信息，失败返回 None。"""
        if not self._ensure():
            return None

        session_id = (session_id or "").strip() or _now_id()
        path = self._path(session_id)
        if path is None:
            # id 是自己生成的，理论上不会走到这里；保底换一个合法的新 id
            session_id = _now_id()
            path = self._path(session_id)
            if path is None:
                return None

        now = datetime.now().timestamp()
        created = now
        if os.path.isfile(path):
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    created = float(json.load(fh).get("created") or now)
            except (OSError, ValueError, TypeError):
                created = now

        turns = sum(1 for m in messages if m.get("role") == "user")
        payload = {
            "id": session_id,
            "title": _title_from(messages),
            "created": created,
            "updated": now,
            "model": model,
            "turns": turns,
            "messages": messages,
            "meta": meta or {},
        }

        try:
            tmp = f"{path}.tmp"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, ensure_ascii=False, indent=1)
            os.replace(tmp, path)  # 原子替换，写一半断电也不会毁掉旧文件
        except (OSError, ValueError, TypeError):
            return None

        return SessionInfo(
            id=session_id,
            title=payload["title"],
            updated=now,
            turns=turns,
            model=model,
            path=path,
        )

    # -- 读 -----------------------------------------------------------------
    def load(self, session_id: str) -> dict | None:
        path = self._path(session_id)
        if path is None or not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(data, dict) or not isinstance(data.get("messages"), list):
            return None
        return data

    def list(self, limit: int = 20) -> list[SessionInfo]:
        if not os.path.isdir(self.base):
            return []

        found: list[SessionInfo] = []
        try:
            names = sorted(os.listdir(self.base), reverse=True)
        except OSError:
            return []

        for name in names:
            if not name.endswith(".json"):
                continue
            session_id = name[:-5]
            path = self._path(session_id)
            if path is None:
                continue
            try:
                with open(path, "r", encoding="utf-8") as fh:
                    data = json.load(fh)
                found.append(
                    SessionInfo(
                        id=session_id,
                        title=str(data.get("title") or "（无标题）"),
                        updated=float(data.get("updated") or 0),
                        turns=int(data.get("turns") or 0),
                        model=str(data.get("model") or ""),
                        path=path,
                    )
                )
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                continue
            if len(found) >= limit:
                break

        found.sort(key=lambda s: s.updated, reverse=True)
        return found

    def latest(self) -> SessionInfo | None:
        items = self.list(limit=1)
        return items[0] if items else None

    def delete(self, session_id: str) -> bool:
        path = self._path(session_id)
        if path is None or not os.path.isfile(path):
            return False
        try:
            os.remove(path)
            return True
        except OSError:
            return False
