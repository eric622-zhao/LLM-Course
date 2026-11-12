# PetHospitalMCP · 宠物医院 MCP 服务器

> 第一次开发任务

把本地运行的 **宠物医院 REST API**（`pethospital.exe`，默认 `http://127.0.0.1:8080`）
包装成一组 MCP 工具，让支持 MCP 的 AI 客户端直接查询档案、录入病历、追加收费。

```
PetHospitalMCP/
├── server.py           MCP 服务器（15 个工具 · 重试 · 结构化错误 · 调用日志）
├── test_server.py      离线单测 10 项（MockTransport，不需要后端）
├── check_stdio.py      协议层联调自检（自己起客户端连上来）
├── requirements.txt
└── README.md
```

---

## 一、先启动后端

从课程提供的发行包解压后双击运行：

```bat
pethospital.exe
```

浏览器打开 <http://127.0.0.1:8080/> 能看到界面即说明后端就绪。
本 MCP 服务器只是「翻译层」，**不启动它就无法工作**。

## 二、安装依赖

```bash
pip install -r requirements.txt
```

## 三、运行

```bash
# stdio 方式（本地客户端自己拉起进程，最常用）
python server.py

# Streamable HTTP 方式，供 AnythingLLM 等远程 MCP 客户端连接
python server.py --http                      # http://127.0.0.1:8090/mcp
python server.py --http --port 8095
```

环境变量（可选）：

| 变量 | 默认值 | 说明 |
| --- | --- | --- |
| `PET_HOSPITAL_API` | `http://127.0.0.1:8080` | 后端地址，端口换了就改这里 |
| `PET_HOSPITAL_TIMEOUT` | `15` | 单次请求超时秒数 |
| `PET_HOSPITAL_RETRIES` | `2` | 连接失败 / 超时 / 5xx 的重试次数 |
| `PET_HOSPITAL_LOG` | （不设） | 日志文件路径；不设则输出到 stderr |
| `PET_HOSPITAL_LOG_LEVEL` | `INFO` | 日志级别，`DEBUG` 可看到逐次重试 |

> 依赖装的是 **MCP SDK 2.x**。2.x 把 `FastMCP` 改名成了 `MCPServer`，
> `server.py` 对 1.x / 2.x 都做了兼容导入，所以 `pip install "mcp>=1.2"` 也能跑；
> 但整套仓库（含 `Agent/lumen` 的 MCP 客户端）统一按 2.x 验证。

## 四、工具清单

| 工具 | 作用 |
| --- | --- |
| `health` | 健康检查，确认后端是否已启动 |
| `get_meta` | 枚举字典与字段说明（**建档案前先查**） |
| `get_stats` | 经营统计：档案数 / 总收入 / 客单价 / 医生排行 |
| `list_pets` | 列表查询：关键词、字段过滤、排序、分页 |
| `get_pet` | 按编号查单只宠物完整档案 |
| `create_pet` | 新建档案，编号服务端自动生成 |
| `update_pet` | 局部更新（PATCH 语义，不会清空其他字段） |
| `delete_pet` | 删除档案 |
| `search_pets` | 跨字段全文检索（含病历全文） |
| `top_spenders` | 消费排行榜 |
| `get_pet_records` / `add_pet_record` | 查询 / 追加历史病历 |
| `get_pet_charges` / `add_pet_charge` | 查询 / 追加消费明细（总花费自动累计） |
| `get_pet_summary` | 单只宠物费用与就诊汇总 |

### 数据字段速查

`species`：犬 / 猫 / 兔 / 鸟 / 仓鼠 / 爬宠 / 其他
`gender`：公 / 母
`status`：待就诊 / 就诊中 / 住院中 / 已康复 / 慢性病随访
`category`：检查 / 药品 / 手术 / 住院 / 疫苗 / 驱虫 / 美容 / 其他

> `totalCost`（总花费）与 `visitCount`（就诊次数）是服务端自动汇总的只读字段。

## 五、接入 AI 客户端

### AnythingLLM

1. 浏览器打开 AnythingLLM → **设置 → 智能体技能 / MCP Servers**
2. 新增一个 MCP Server：
   - 类型：**Streamable HTTP**
   - 地址：`http://127.0.0.1:8090/mcp`
3. 先 `python server.py --http` 把本服务跑起来，再回到 AnythingLLM 点连接

### Claude Desktop / Cherry Studio（stdio）

编辑客户端的 MCP 配置文件：

```json
{
  "mcpServers": {
    "pet-hospital": {
      "command": "python",
      "args": ["D:\\LLM-Course\\PetHospitalMCP\\server.py"],
      "env": { "PET_HOSPITAL_API": "http://127.0.0.1:8080" }
    }
  }
}
```

> Windows 上路径请写成双反斜杠或正斜杠。若 `python` 不在 PATH，
> 换成解释器的绝对路径。

## 六、自测

### 1. 离线单测（不需要后端、不需要联网）

```bash
python -m unittest test_server -v
```

用 `httpx.MockTransport` 拦下真实网络，共 10 项，验证调用层的约定：

| 用例 | 验证的行为 |
| --- | --- |
| `test_5xx_then_success_retries` | 503 后自动重试，第二次成功就正常返回 |
| `test_5xx_exhausted_raises` | 5xx 重试耗尽后抛可读错误，不泄漏上游 HTML |
| `test_4xx_not_retried` | 404 不重试（重试也不会变好） |
| `test_business_error_not_retried` | 业务错误码 `code != 200` 原样带回，不重试 |
| `test_connect_error_retried_then_friendly` | 连不上时提示「请先运行 pethospital.exe」 |
| `test_log_line_written_and_stdout_clean` | 每次调用写日志，且 **stdout 必须干净** |
| `ToolContractTest` | 每个工具都有描述、写操作都声明了必填参数 |

### 2. 协议层联调（自己起一个 MCP 客户端连上来）

```bash
python check_stdio.py                    # 默认调 health
python check_stdio.py list_pets          # 调指定工具
python check_stdio.py get_pet pet_id=PET-000001
```

它会走完整链路：拉起子进程 → 握手 → `tools/list` → `tools/call`，
并检查「后端没启动时，模型侧拿到的是不是可读中文提示」。
不需要 AnythingLLM 或 Claude 之类的客户端。

### 3. 直接打后端

```bash
curl -s http://127.0.0.1:8080/api/v1/meta
curl -s "http://127.0.0.1:8080/api/v1/pets?page=1&pageSize=3"
```

### 4. 官方 Inspector（可视化点选工具）

```bash
npx @modelcontextprotocol/inspector python server.py
```

## 六点五、错误语义与重试策略

**错误一律是结构化中文，不把上游 HTML 甩给模型。**

工具里抛的是 `ToolError` 而不是普通 `RuntimeError` —— 这一点很关键：
普通异常会被 MCP 框架压成一句 `Error executing tool health`，模型拿不到
「要先启动 pethospital.exe」这类可操作信息；`ToolError` 的 message 会原样透传。

实际返回给模型的样子：

```
Error executing tool health: 连接宠物医院服务失败（http://127.0.0.1:8080），
已重试 2 次。请先运行 pethospital.exe 再重试。
```

重试策略：

| 情况 | 是否重试 | 理由 |
| --- | --- | --- |
| 连接失败 / 超时 | 是，指数退避 0.3s→0.6s→1.2s（上限 3s） | 后端刚启动时的瞬时故障 |
| 5xx | 是 | 服务端临时故障，通常再试一次就好 |
| 4xx | 否 | 请求本身有问题，重试不会变好 |
| 业务错误码（`code != 200`） | 否 | 属于正常业务分支，重试无意义 |

日志走 **stderr 或 `PET_HOSPITAL_LOG` 指定的文件**，绝不写 stdout ——
stdio 传输下 stdout 是 JSON-RPC 协议通道，混进一行日志就会让客户端解析失败。
单测里有一条 `test_log_line_written_and_stdout_clean` 专门守着这件事。

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 提示「连接宠物医院服务失败」 | `pethospital.exe` 没启动，或端口不是 8080。启动后重试，或设置 `PET_HOSPITAL_API` |
| 加了新档案但列表没变 | 后端是实时读写 `data/pet.db`，刷新即可；确认两次调用的是同一个后端地址 |
| AI 传了非法枚举值 | 先调用 `get_meta` 拿枚举，再让它按枚举填 |
| 工具报错信息只有一句 `Error executing tool xxx` | 说明抛的是普通异常而非 `ToolError`，检查 `server.py` 的 `call()` |
| 启动报 `FastMCP was renamed to MCPServer` | 装成了 MCP 2.x。`server.py` 已兼容两种版本；若仍报错，确认导入的是 `mcp.server.mcpserver.MCPServer` |
| 客户端连不上但进程没报错 | 日志被写进了 stdout。检查 `PET_HOSPITAL_LOG`，日志必须走 stderr 或文件 |
| Windows 控制台中文乱码 | 执行 `chcp 65001` 切到 UTF-8 |
