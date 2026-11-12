# -*- coding: utf-8 -*-
"""
core.tracing · 结构化调用追踪
=============================

排查 Agent 问题最痛苦的是「它到底做了什么」。
`print` 只能看当下，日志能事后复盘 —— 所以每次 LLM 请求、工具调用、
MCP 调用、异常，都写成一行 JSON，落到 `.lumen/traces/` 下。

为什么用 JSONL 而不是普通日志
------------------------------
- 一行一条，追加写不会破坏已有内容（进程被杀也只丢最后一行）；
- 结构固定，可以直接 `py -c "import json..."` 统计耗时和失败率；
- 不需要额外的日志框架，标准库够用。

不记录什么
----------
不写完整的消息正文与工具返回值 —— 那里面可能有用户隐私或密钥。
只记录**长度、耗时、是否报错、名字**，既能定位问题又不至于泄露内容。

配置（config.ini）：

    [trace]
    enabled = true
    dir = .lumen/traces
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from typing import Any

from core import config

DEFAULT_DIR = os.path.join(".lumen", "traces")


def _enabled() -> bool:
    raw = config.get("trace", "enabled")
    if raw is None or not str(raw).strip():
        return True
    return str(raw).strip().lower() not in ("0", "false", "no", "off")


class Tracer:
    """一次运行对应一个 JSONL 文件。"""

    def __init__(self, project_root: str, session_id: str = "") -> None:
        self.root = os.path.realpath(project_root)
        self.session_id = session_id or datetime.now().strftime("%Y%m%d-%H%M%S")
        self._lock = threading.Lock()
        self._fh = None
        self.path = ""
        self.counts: dict[str, int] = {}
        self.errors = 0
        self.started = time.time()

        if not _enabled():
            return

        directory = (config.get("trace", "dir") or DEFAULT_DIR).strip()
        if not os.path.isabs(directory):
            directory = os.path.join(self.root, directory)

        try:
            os.makedirs(directory, exist_ok=True)
            self.path = os.path.join(directory, f"{self.session_id}.jsonl")
            self._fh = open(self.path, "a", encoding="utf-8")
        except OSError:
            # 写不了日志不该影响主流程
            self._fh = None

    # -- 写入 ---------------------------------------------------------------
    def event(self, kind: str, **fields: Any) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        if kind == "error":
            self.errors += 1
        if self._fh is None:
            return

        record = {
            "ts": round(time.time(), 3),
            "time": datetime.now().strftime("%H:%M:%S"),
            "kind": kind,
            "session": self.session_id,
        }
        for key, value in fields.items():
            if isinstance(value, str) and len(value) > 200:
                value = value[:200] + f"…({len(value)} 字符)"
            record[key] = value

        try:
            with self._lock:
                self._fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                self._fh.flush()
        except (OSError, ValueError):
            pass

    # -- 便捷封装 -----------------------------------------------------------
    def turn(self, index: int, user_chars: int) -> None:
        self.event("turn_start", turn=index, user_chars=user_chars)

    def llm(self, round_no: int, seconds: float, text_chars: int, tool_calls: int, tokens: int = 0) -> None:
        self.event(
            "llm_response",
            round=round_no,
            seconds=round(seconds, 2),
            text_chars=text_chars,
            tool_calls=tool_calls,
            tokens=tokens,
        )

    def tool(self, name: str, seconds: float, result_chars: int, ok: bool, args_chars: int = 0) -> None:
        self.event(
            "tool_call",
            name=name,
            seconds=round(seconds, 2),
            args_chars=args_chars,
            result_chars=result_chars,
            ok=ok,
        )

    def error(self, where: str, exc: BaseException) -> None:
        self.event("error", where=where, type=type(exc).__name__, message=str(exc)[:200])

    # -- 收尾 ---------------------------------------------------------------
    def summary(self) -> str:
        if self._fh is None:
            return "追踪未启用"
        elapsed = time.time() - self.started
        tools = self.counts.get("tool_call", 0)
        llm = self.counts.get("llm_response", 0)
        return f"{llm} 次模型请求 · {tools} 次工具调用 · {self.errors} 个错误 · {elapsed:.0f} 秒"

    def close(self) -> None:
        """收尾。注意锁的用法：event() 内部自己会加锁，
        不能在持有 self._lock 的情况下调用它 —— threading.Lock 不可重入，会死锁。
        """
        if self._fh is None:
            return
        self.event("session_end", seconds=round(time.time() - self.started, 1))
        with self._lock:
            fh, self._fh = self._fh, None
            try:
                fh.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 全局实例
# ---------------------------------------------------------------------------
_tracer: Tracer | None = None


def init(project_root: str, session_id: str = "") -> Tracer:
    global _tracer
    if _tracer is not None:
        _tracer.close()
    _tracer = Tracer(project_root, session_id)
    return _tracer


def get() -> Tracer | None:
    return _tracer


def close() -> None:
    global _tracer
    if _tracer is not None:
        _tracer.close()
        _tracer = None
