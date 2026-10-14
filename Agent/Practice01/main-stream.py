# -*- coding: utf-8 -*-
"""
Practice01 · 流式输出：让回答像打字一样逐字出现
================================================

和 main.py 的唯一区别：create(..., stream=True)
返回的不再是一个完整结果，而是一个「生成器」，每次给你一小块增量内容。

学习目标
--------
1. 认识 chunk.choices[0].delta.content —— 注意是 delta 不是 message
2. 用 flush=True 让文字真正逐字刷新，而不是等结束后一次性打印
3. 边打印边把内容拼成完整回答，为重头戏 main-stream-loop.py 做准备

运行
----
    python main-stream.py
"""

import configparser
import os
import sys

from openai import OpenAI

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str = "config.ini") -> configparser.SectionProxy:
    full = os.path.join(HERE, path)
    if not os.path.exists(full):
        sys.exit("找不到 config.ini，请先 copy config.example.ini config.ini 并填写 api_key。")
    parser = configparser.ConfigParser()
    parser.read(full, encoding="utf-8")
    cfg = parser["llm"]
    if "替换" in cfg.get("api_key", "") or not cfg.get("api_key", "").strip():
        sys.exit("config.ini 里的 api_key 还没填，请填写后再运行。")
    return cfg


def main() -> None:
    cfg = load_config()
    client = OpenAI(api_key=cfg.get("api_key"), base_url=cfg.get("base_url"))

    question = input("请输入你的问题：").strip()
    if not question:
        sys.exit("问题不能为空。")

    messages = [
        {"role": "system", "content": "你是一个乐于助人的中文助手，回答尽量简洁准确。"},
        {"role": "user", "content": question},
    ]

    print("\nAI：", end="", flush=True)

    stream = client.chat.completions.create(
        model=cfg.get("model"),
        messages=messages,
        temperature=cfg.getfloat("temperature", fallback=0.7),
        stream=True,  # ← 唯一的区别
    )

    reply = ""
    for chunk in stream:
        # 流式返回里内容在 delta 上，而且可能为空（例如首包和末包）
        content = chunk.choices[0].delta.content
        if content:
            print(content, end="", flush=True)
            reply += content

    print()  # 结束后换行，避免提示符黏在回答末尾


if __name__ == "__main__":
    main()
