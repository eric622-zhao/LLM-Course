# AnythingLLMMCP · AnythingLLM MCP 服务器

> 第二次开发任务

把本地 **AnythingLLM** 的能力包装成 MCP 工具，以 **Streamable HTTP** 常驻在
`http://127.0.0.1:8091/mcp`，供 AnythingLLM 自己的智能体或其它 MCP 客户端调用。

覆盖三类能力：**工作区管理**、**文档上传与向量化**、**对话与检索问答**。

```
AnythingLLMMCP/
├── server.py           MCP 服务器（9 个工具 · 双通道 · 结构化错误）
├── test_server.py      离线单测 23 项（MockTransport，不需要 AnythingLLM）
├── requirements.txt
└── README.md
```

---

## 一、先启动 AnythingLLM

打开 AnythingLLM 桌面端，确认左下角能看到工作区（默认端口 **3001**）。
验证后端接口：

```bash
curl -s http://127.0.0.1:3001/api/workspaces
```

能返回 JSON 数组即说明后端就绪。

## 二、要 Key 才行的操作，先拿一个 Key

AnythingLLM 有两套路由，权限模型不一样，本项目按用途分开走：

| 路由 | 鉴权 | 用途 | 本项目用到的工具 |
| --- | --- | --- | --- |
| `/api/*` | 本地通常免鉴权 | 桌面端前端自用，字段随版本漂移 | `list_workspaces` `get_workspace` `list_documents` `first_workspace` |
| `/api/v1/*` | **必须** API Key | 官方文档承诺的开发者接口，结构稳定 | `upload_document` `embed_documents` `chat` `list_all_documents` `accepted_file_types` |

拿 Key：**AnythingLLM → 左下角设置（齿轮）→ API Keys → 新建 → 复制**，然后

```bash
# PowerShell
$env:ANYTHINGLLM_API_KEY = "你的key"

# cmd
set ANYTHINGLLM_API_KEY=你的key

# bash
export ANYTHINGLLM_API_KEY=你的key
```

> 不配 Key 也能用前一组只读工具；调用后一组时会收到明确提示
> 「去哪拿 Key、配到哪个环境变量」，而不是干巴巴的 401。

## 三、安装依赖

```bash
pip install -r requirements.txt
```

> 依赖装的是 **MCP SDK 2.x**。2.x 把 `FastMCP` 改名成了 `MCPServer`，
> `server.py` 对 1.x / 2.x 都做了兼容导入；整套仓库（含 `Agent/lumen`
> 的 MCP 客户端）统一按 2.x 验证。

## 四、运行

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

环境变量：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `ANYTHINGLLM_API` | `http://127.0.0.1:3001` | AnythingLLM 后端地址 |
| `ANYTHINGLLM_API_KEY` | 空 | 开发者 API 的 Bearer Key，写操作必需 |
| `ANYTHINGLLM_TIMEOUT` | `15` | 单次请求超时秒数 |
| `ANYTHINGLLM_MAX_UPLOAD_MB` | `50` | 单个文件上传上限（MB） |

## 五、工具清单

9 个工具，按用途分三组。

### 工作区管理（免 Key）

| 工具 | 必填参数 | 作用 |
| --- | --- | --- |
| `list_workspaces` | — | 列出全部工作区（id / name / slug / 创建时间） |
| `get_workspace` | `slug` | 按 slug 查单个工作区详情（含已嵌入文档、聊天设置） |
| `list_documents` | `slug` | 列出某工作区**已嵌入**的文档 |
| `first_workspace` | — | 直接取「第一个工作区」，省掉一次取值 |

### 文档上传与向量化（需 Key）

| 工具 | 必填参数 | 作用 |
| --- | --- | --- |
| `upload_document` | `path` | 上传本地文件到文档库；给 `workspace` 则顺便向量化进该工作区 |
| `embed_documents` | `slug` `adds` | 把文档库里的文件加入工作区，或从工作区移除（`deletes`） |
| `list_all_documents` | — | 分页浏览**文档库里的全部文件**（`limit` 1~100 / `offset`） |
| `accepted_file_types` | — | 查当前接受的上传扩展名 |

> `list_documents` 看的是「某工作区已向量化的」，`list_all_documents` 看的是
> 「库里全部」——两者的差集就是**上传了但还没向量化**的文件，
> 排查「为什么模型读不到这份资料」时先看这个。

### 对话与检索问答（需 Key）

| 工具 | 必填参数 | 作用 |
| --- | --- | --- |
| `chat` | `slug` `message` | 在该工作区提问，由 AnythingLLM 检索文档后作答 |

`chat` 的 `mode` 有两种，语义不同：

- `query`（默认）——只检索文档、基于文档回答，返回引用来源。问「资料里怎么写的」用它，答案不易编造。
- `chat` ——允许模型结合自身知识自由回答，适合闲聊或文档里没有的追问。

`session_id` 留空是一次性问答；要**多轮追问**就把上一次返回的 `sessionId` 传回来。

## 六、完整调用序列示例

「把一份笔记传进去，然后问它里面写了什么」——模型自己会按这个顺序调：

```text
1. first_workspace()                       -> 拿到 slug: "my-workspace"
2. upload_document("D:/notes/课程笔记.md",
                   workspace="my-workspace")
   -> 上传 + 向量化，返回 location
3. chat("my-workspace", "这份笔记讲了什么？", mode="query")
   -> 返回 answer + sources（命中的文档片段）
4. chat("my-workspace", "第三点再展开说说",
        mode="query", session_id="<上一步的 sessionId>")
   -> 带上下文的追问
```

只想上传、稍后再向量化，就把第 2 步的 `workspace` 留空，
需要时再调 `embed_documents(slug, adds=[location])`。

## 七、接入 AnythingLLM 智能体

1. 保持 `python server.py` 运行
2. AnythingLLM → **设置 → 智能体技能 / MCP Servers → 新增**
3. 类型选 **Streamable HTTP**，地址填 `http://127.0.0.1:8091/mcp`
4. 保存后点连接，工具列表里应出现 `list_workspaces` 等条目
5. 新建对话，问它「列出我所有的工作区」，即可看到工具被真正调用

> 如果 AnythingLLM 跑在 Docker 里，`127.0.0.1` 要换成宿主机的实际 IP。

## 八、自测

### 离线单测（不需要 AnythingLLM）

```bash
python -m unittest test_server -v
```

23 项，用 `httpx.MockTransport` 拦掉真实网络，验证的是调用层行为：
缺 Key 时的提示、上传是否真的走 multipart、上传后是否只向量化一次、
`mode` 校验、`sessionId` 透传、401/403/500 的差异化提示、分页越界拦截。

### 联调自测（需要 AnythingLLM 已启动）

```bash
# 后端是否通
curl -s http://127.0.0.1:3001/api/workspaces

# MCP 服务是否活着（Inspector 图形界面最直观）
npx @modelcontextprotocol/inspector --url http://127.0.0.1:8091/mcp
```

## 九、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 提示「连接 AnythingLLM 失败」 | 桌面端没打开，或端口不是 3001；用 `ANYTHINGLLM_API` 覆盖 |
| 8091 端口被占用 | `python server.py --port 8099`，同时改客户端里配置的地址 |
| 提示「需要 API Key」 | 写操作走 `/api/v1/*`，去设置 → API Keys 建一个，配 `ANYTHINGLLM_API_KEY` |
| 返回 401 | Key 无效或已失效，重新生成一个再配 |
| 返回 403 | Key 有效但没有该操作权限，检查它是不是管理员 Key |
| 连上了但工具为空 | 先确认工作区已创建；AnythingLLM 里需要手动刷新一次连接 |
| `list_workspaces` 返回空数组 | 后端通了但还没有工作区，去 AnythingLLM 里新建一个 |
| **上传成功但模型读不到** | 只上传≠能用。要看 `list_all_documents` 与 `list_documents` 的差集，再用 `embed_documents` 向量化进工作区 |
| 上传报「不支持的格式」 | 先调 `accepted_file_types` 确认扩展名 |
| 问答答非所问 | `mode` 用了 `chat` 会掺入模型自身知识；问文档内容请用 `query` |
