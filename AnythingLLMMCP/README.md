# AnythingLLMMCP · AnythingLLM 工作区 MCP 服务器

> 第二次开发任务

把本地 **AnythingLLM** 的工作区信息包装成 MCP 工具，以 **Streamable HTTP** 常驻在
`http://127.0.0.1:8091/mcp`，供 AnythingLLM 自己的智能体或其它 MCP 客户端调用。

---

## 一、先启动 AnythingLLM

打开 AnythingLLM 桌面端，确认左下角能看到工作区（默认端口 **3001**）。
验证后端接口：

```bash
curl -s http://127.0.0.1:3001/api/workspaces
```

能返回 JSON 数组即说明后端就绪。

## 二、安装依赖

```bash
pip install -r requirements.txt
```

## 三、运行

```bash
python server.py                  # 默认 Streamable HTTP -> http://127.0.0.1:8091/mcp
python server.py --port 8099      # 端口被占用时换一个
python server.py --stdio          # 也可以改成 stdio 由客户端拉起
```

启动成功会打印：

```text
[anythingllm-mcp] Streamable HTTP -> http://127.0.0.1:8091/mcp
[anythingllm-mcp] 后端 AnythingLLM -> http://127.0.0.1:3001
```

环境变量（可选）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANYTHINGLLM_API` | `http://127.0.0.1:3001` | AnythingLLM 后端地址 |
| `ANYTHINGLLM_API_KEY` | 空 | 填了就以 `Bearer` 方式带上，用于需要鉴权的开发者 API |
| `ANYTHINGLLM_TIMEOUT` | `15` | 请求超时秒数 |

## 四、工具清单

| 工具 | 作用 |
| --- | --- |
| `list_workspaces` | 列出全部工作区（id / name / slug / 创建时间） |
| `get_workspace` | 按 slug 查单个工作区详情 |
| `list_documents` | 列出某工作区已嵌入的文档 |
| `first_workspace` | 直接取「第一个工作区」 |

## 五、接入 AnythingLLM 智能体

1. 保持 `python server.py` 运行
2. AnythingLLM → **设置 → 智能体技能 / MCP Servers → 新增**
3. 类型选 **Streamable HTTP**，地址填 `http://127.0.0.1:8091/mcp`
4. 保存后点连接，工具列表里应出现 `list_workspaces` 等条目
5. 新建对话，问它「列出我所有的工作区」，即可看到工具被真正调用

> 如果 AnythingLLM 跑在 Docker 里，`127.0.0.1` 要换成宿主机的实际 IP。

## 六、自测

```bash
# 后端是否通
curl -s http://127.0.0.1:3001/api/workspaces

# MCP 服务是否活着（Inspector 图形界面最直观）
npx @modelcontextprotocol/inspector --url http://127.0.0.1:8091/mcp
```

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 提示「连接 AnythingLLM 失败」 | 桌面端没打开，或端口不是 3001；用 `ANYTHINGLLM_API` 覆盖 |
| 8091 端口被占用 | `python server.py --port 8099`，同时改客户端里配置的地址 |
| 返回 401 | 该接口需要鉴权，设置 `ANYTHINGLLM_API_KEY` |
| 连上了但工具为空 | 先确认工作区已创建；AnythingLLM 里需要手动刷新一次连接 |
| `list_workspaces` 返回空数组 | 后端通了但还没有工作区，去 AnythingLLM 里新建一个 |
