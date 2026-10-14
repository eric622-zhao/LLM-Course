# Practice01 · 调用大模型：从单次请求到多轮流式对话

> 第四周开发任务

用 `openai` 这一个库，配合一份 `config.ini`，把「怎么和大模型说话」这件事
从最简单一路做到能真正聊天。

```
Agent/Practice01/
├── main.py               ① 单次请求（非流式）
├── main-stream.py        ② 单次请求（流式输出，逐字打印）
├── main-stream-loop.py   ③ 多轮流式对话（带上下文记忆）
├── config.example.ini    配置模板 → 复制成 config.ini 使用
└── requirements.txt
```

---

## 一、准备

```bash
cd Agent/Practice01
pip install -r requirements.txt

copy config.example.ini config.ini     # macOS / Linux: cp
```

打开 `config.ini` 填三项：

```ini
[llm]
base_url = https://api.openai.com/v1
model    = gpt-4o-mini
api_key  = sk-你的key
```

| 想用哪家 | `base_url` | `model` 示例 |
| --- | --- | --- |
| OpenAI | `https://api.openai.com/v1` | `gpt-4o-mini` |
| DeepSeek | `https://api.deepseek.com/v1` | `deepseek-chat` |
| Kimi | `https://api.moonshot.cn/v1` | `moonshot-v1-8k` |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` |
| 本地 Ollama | `http://127.0.0.1:11434/v1` | `qwen2.5:7b` |

> 只要对方是「OpenAI 兼容」接口，**换平台只需要改这个文件**，代码一行都不用动。

## 二、三个脚本，层层递进

### ① `main.py` —— 单次请求

```bash
python main.py
# 请输入你的问题：天空为什么是蓝色的
```

服务端把整段答案算完才返回，所以你会「卡一下」然后整段出现。
最后会额外打印本次消耗的 token 数。

核心就三样：

```python
client = OpenAI(api_key=..., base_url=...)
messages = [{"role": "system", "content": "..."},
            {"role": "user", "content": question}]
response = client.chat.completions.create(model=..., messages=messages)
print(response.choices[0].message.content)
```

### ② `main-stream.py` —— 流式输出

```bash
python main-stream.py
```

只比 ① 多了一个参数 `stream=True`，回答就会像打字一样逐字出现。

要注意两点差别：

| | 非流式 | 流式 |
| --- | --- | --- |
| 返回 | 一个完整响应 | 一串 chunk |
| 取内容 | `choices[0].message.content` | `choices[0].delta.content` |
| 是否可能为空 | 否 | **是**，所以要 `if content:` 判断 |

### ③ `main-stream-loop.py` —— 多轮流式对话

```bash
python main-stream-loop.py
```

```
你：我叫小明
AI：你好小明！……
你：我叫什么名字？
AI：你叫小明。
```

**为什么能记住？** 大模型本身是无状态的，每一轮都把完整历史重新发一遍：

```
第 1 轮  [system, user1]
第 2 轮  [system, user1, assistant1, user2]
```

所以循环里必须做这一步，否则模型永远「失忆」：

```python
reply = ""
for chunk in stream:
    content = chunk.choices[0].delta.content
    if content:
        print(content, end="", flush=True)
        reply += content
messages.append({"role": "assistant", "content": reply})   # ← 关键
```

内置指令：`/clear` 清空上下文，`/exit` 或 `Ctrl+C` 退出。

## 三、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| `config.ini 里的 api_key 还没填` | 复制模板后忘了改，打开填上即可 |
| 401 / AuthenticationError | key 填错，或 base_url 与 key 不是同一家平台 |
| `model not found` | model 名字和平台对不上，对照上表改 |
| 中文乱码 | Windows 控制台执行 `chcp 65001` |
| 卡住不动 | 网络到不了该平台；换 base_url，或检查代理设置 |
| 多轮里模型忘记前文 | 检查是否真的执行了 `messages.append`，以及是否误触发了 `/clear` |

## 四、安全提醒

`config.ini` 里有你的 API Key，**已被 `.gitignore` 忽略，不要提交到 GitHub**。
如果不小心提交了，立刻去平台后台吊销该 Key 并重新生成。
