# PetHospitalMCP · 宠物医院 MCP 服务器

> 第一次开发任务

把本地运行的 **宠物医院 REST API**（`pethospital.exe`，默认 `http://127.0.0.1:8080`）
包装成一组 MCP 工具，让支持 MCP 的 AI 客户端直接查询档案、录入病历、追加收费。

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

不接 AI 客户端也能验证后端是否通：

```bash
curl -s http://127.0.0.1:8080/api/v1/meta
curl -s "http://127.0.0.1:8080/api/v1/pets?page=1&pageSize=3"
```

MCP 侧可用官方 Inspector：

```bash
npx @modelcontextprotocol/inspector python server.py
```

## 七、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 提示「连接宠物医院服务失败」 | `pethospital.exe` 没启动，或端口不是 8080。启动后重试，或设置 `PET_HOSPITAL_API` |
| 加了新档案但列表没变 | 后端是实时读写 `data/pet.db`，刷新即可；确认两次调用的是同一个后端地址 |
| AI 传了非法枚举值 | 先调用 `get_meta` 拿枚举，再让它按枚举填 |
| Windows 控制台中文乱码 | 执行 `chcp 65001` 切到 UTF-8 |
