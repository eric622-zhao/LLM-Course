# -*- coding: utf-8 -*-
"""
Practice01 · 多轮流式对话（带记忆的聊天循环）
=============================================

这是 Practice01 的重头戏：把「流式输出」放进一个 while 循环，
并维护 messages 列表，让模型记得前面说过的话。

核心机制 —— 大模型本身是无状态的
--------------------------------
它不会「记得」你上一句说了什么。所谓多轮对话，只是每一轮都把
**完整的历史** 重新发一遍：

    第 1 轮  [system, user1]
    第 2 轮  [system, user1, assistant1, user2]
    第 3 轮  [system, user1, assistant1, user2, assistant2, user3]

所以每轮结束后，必须把 assistant 的回答 append 回 messages。

内置指令
--------
    /clear  清空上下文，重新开始
    /exit   或 Ctrl+C  退出

运行
----
    python main-stream-loop.py
"""

import configparser
import os
import sys

from openai import OpenAI

HERE = os.path.dirname(os.path.abspath(__file__))
SYSTEM_PROMPT = "你是一个乐于助人的中文助手，回答尽量简洁准确。"


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
    model = cfg.get("model")
    temperature = cfg.getfloat("temperature", fallback=0.7)

    # 整个会话共享的上下文，system 只放一次
    messages: list[dict] = [{"role": "system", "content": SYSTEM_PROMPT}]

    print("=" * 56)
    print(" 多轮流式对话  ·  输入 /clear 清空上下文，/exit 退出")
    print("=" * 56)

    try:
        while True:
            question = input("\n你：").strip()
            if not question:
                continue

            if question in ("/exit", "/quit", "exit", "quit"):
                print("再见！")
                break

            if question == "/clear":
                messages = [{"role": "system", "content": SYSTEM_PROMPT}]
                print("✅ 上下文已清空。")
                continue

            # 1) 把用户这一句加入历史
            messages.append({"role": "user", "content": question})

            # 2) 请求流式回答
            print("AI：", end="", flush=True)

            stream = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=True,
            )

            # 3) 边收边打印，同时拼出完整回答
            reply = ""
            for chunk in stream:
                content = chunk.choices[0].delta.content
                if content:
                    print(content, end="", flush=True)
                    reply += content

            print()

            # 4) 把 AI 的回答也加入历史 —— 这一步是多轮记忆的关键
            messages.append({"role": "assistant", "content": reply})

    except KeyboardInterrupt:
        # Ctrl+C 时优雅退出，不打印一堆异常堆栈
        print("\n已退出。")


if __name__ == "__main__":
    main()
