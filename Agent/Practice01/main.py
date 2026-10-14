# -*- coding: utf-8 -*-
"""
Practice01 · 最简版：单次请求，一次性拿到完整回答
================================================

学习目标
--------
1. 用 config.ini 管理 api_key / base_url / model，不把密钥写死在代码里
2. 用 openai 这个库调用任意「OpenAI 兼容」的大模型接口
3. 理解最核心的三件事：client、messages、model

运行
----
    pip install -r requirements.txt
    copy config.example.ini config.ini   # 然后填入你自己的 key
    python main.py
"""

import configparser
import os
import sys

from openai import OpenAI

HERE = os.path.dirname(os.path.abspath(__file__))


def load_config(path: str = "config.ini") -> configparser.SectionProxy:
    """读取同目录下的 config.ini。"""
    full = os.path.join(HERE, path)
    if not os.path.exists(full):
        sys.exit(
            "找不到 config.ini。\n"
            "请先执行：  copy config.example.ini config.ini\n"
            "然后打开 config.ini 填入你的 api_key。"
        )
    parser = configparser.ConfigParser()
    parser.read(full, encoding="utf-8")
    cfg = parser["llm"]

    if "替换" in cfg.get("api_key", "") or not cfg.get("api_key", "").strip():
        sys.exit("config.ini 里的 api_key 还没填，请填写后再运行。")
    return cfg


def main() -> None:
    cfg = load_config()

    # 关键：只要 base_url 是「OpenAI 兼容」的接口，换任何平台都只改 config.ini
    client = OpenAI(
        api_key=cfg.get("api_key"),
        base_url=cfg.get("base_url"),
    )

    question = input("请输入你的问题：").strip()
    if not question:
        sys.exit("问题不能为空。")

    # messages 就是「对话历史」：role 可以是 system / user / assistant
    messages = [
        {"role": "system", "content": "你是一个乐于助人的中文助手，回答尽量简洁准确。"},
        {"role": "user", "content": question},
    ]

    print("\nAI 正在思考……\n")

    response = client.chat.completions.create(
        model=cfg.get("model"),
        messages=messages,
        temperature=cfg.getfloat("temperature", fallback=0.7),
    )

    # 非流式：服务端把整段话算完才返回，所以这里一次就能取到全部内容
    print("AI：", response.choices[0].message.content)

    # 顺手打印本次消耗，方便观察不同问题的成本差异
    usage = response.usage
    if usage:
        print(f"\n[用量] 输入 {usage.prompt_tokens} tokens，"
              f"输出 {usage.completion_tokens} tokens，"
              f"合计 {usage.total_tokens} tokens")


if __name__ == "__main__":
    main()
