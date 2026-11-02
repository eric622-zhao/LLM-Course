# -*- coding: utf-8 -*-
"""
core.config · 配置读取
======================

所有模块通过本模块读取 config.ini，配置文件路径固定在包根目录，
与「运行时的当前工作目录」解耦 —— 这样 Skill 的项目根目录才能是 CWD，
而配置文件始终跟着代码走。

用法：
    from core import config
    config.load()
    config.get("llm", "model")
    config.get_int("tools", "cli_timeout", 60)
"""

from __future__ import annotations

import configparser
import os

# lumen/ 目录（本文件在 lumen/core/ 下，往上一层就是包根）
PACKAGE_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(PACKAGE_ROOT, "config.ini")

_parser: configparser.ConfigParser | None = None


class ConfigError(RuntimeError):
    """配置文件缺失或内容不合法。"""


def load(path: str | None = None) -> configparser.ConfigParser:
    global _parser, CONFIG_PATH
    if path:
        CONFIG_PATH = path
    if not os.path.exists(CONFIG_PATH):
        raise ConfigError(
            f"找不到配置文件：{CONFIG_PATH}\n"
            f"请先执行：copy config.example.ini config.ini，然后填写其中的 api_key。"
        )
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(CONFIG_PATH, encoding="utf-8")
    _parser = parser
    return parser


def _require_loaded() -> configparser.ConfigParser:
    if _parser is None:
        raise ConfigError("配置尚未加载，请先在入口处调用 config.load()。")
    return _parser


def get(section: str, key: str, fallback: str | None = None) -> str | None:
    parser = _require_loaded()
    if not parser.has_section(section):
        return fallback
    value = parser.get(section, key, fallback=fallback)
    return value.strip() if isinstance(value, str) else value


def get_int(section: str, key: str, fallback: int) -> int:
    raw = get(section, key)
    if raw is None or raw == "":
        return fallback
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return fallback


def get_float(section: str, key: str, fallback: float) -> float:
    raw = get(section, key)
    if raw is None or raw == "":
        return fallback
    try:
        return float(raw)
    except (TypeError, ValueError):
        return fallback


def missing_or_placeholder(value: str | None) -> bool:
    """判断某个配置项是不是没填 / 还是占位符。"""
    if value is None:
        return True
    text = value.strip()
    if not text:
        return True
    lowered = text.lower()
    if "替换" in text:
        return True
    return lowered in ("your_api_key", "none", "null", "sk-xxx")
