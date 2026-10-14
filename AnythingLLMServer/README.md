# AnythingLLMServer · 文件上传网页 + 服务

> 第三次开发任务

一个「简单、美观」的文件上传网页：用户选择本地文件 → 一键上传到 **AnythingLLM 的第一个工作区** →
自动解析并向量化，随后就能在该工作区里对该文件提问。

```
AnythingLLMServer/
├── index.html        上传网页（拖拽 / 点击选择 / 进度条 / 结果提示）
├── server.py         后端：托管网页 + 转发文件到 AnythingLLM
└── requirements.txt
```

---

## 一、先启动 AnythingLLM

打开 AnythingLLM 桌面端，确认至少有一个工作区，端口为 **3001**。

## 二、安装依赖并启动

```bash
pip install -r requirements.txt
python server.py
```

浏览器打开 <http://127.0.0.1:8092/>。

### 命令行参数 / 环境变量

| 参数 | 环境变量 | 默认值 | 说明 |
| --- | --- | --- | --- |
| `--host` | — | `127.0.0.1` | 监听地址 |
| `--port` | — | `8092` | 监听端口（8090/8091 已被另外两个任务占用） |
| `--api` | `ANYTHINGLLM_API` | `http://127.0.0.1:3001` | AnythingLLM 后端地址 |
| `--key` | `ANYTHINGLLM_API_KEY` | 空 | 开发者 API Key，留空则走本地免鉴权通道 |
| `--workspace` | `ANYTHINGLLM_WORKSPACE` | 空 | 指定目标工作区，留空 = 第一个工作区 |

> **关于鉴权**：AnythingLLM 网页端自己调用的接口在本地是免鉴权的，
> 所以不填 Key 也能用。若要更规范，可在 AnythingLLM 的
> **设置 → 工具 → 开发者 API** 生成 Key 后通过 `ANYTHINGLLM_API_KEY` 传入，
> 后端会自动改走 `/api/v1/document/upload` + `/api/v1/workspace/{slug}/update-embeddings`。

## 三、页面功能

- 点击或拖拽选择文件
- 显示文件名与大小，可随时移除重选
- 上传进度条（浏览器 → 本服务，以及等待向量化的过程）
- 顶部实时显示目标工作区，多个工作区时可展开列表切换
- 成功 / 失败都有明确的中文提示，失败会带上 AnythingLLM 的原始错误

## 四、接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `GET` | `/` | 上传网页 |
| `GET` | `/api/workspaces` | 工作区列表，`{"success":true,"workspaces":[{name,slug}]}` |
| `POST` | `/api/upload` | 表单字段 `file`（必填）、`slug`（可选）；返回 `{success, workspace, location}` |
| `GET` | `/api/health` | 健康检查 |

命令行直接测试上传：

```bash
curl -F "file=@D:\test.txt" http://127.0.0.1:8092/api/upload
```

## 五、上传后如何验证

1. 回到 AnythingLLM，进入目标工作区
2. 展开右上角的文档列表（或点 **Documents**）
3. 应该能看到刚上传的文件名；状态从 `Processing` 变成可用的绿点
4. 在对话框里问「这个文件讲了什么」，若回答引用了该文件内容即说明成功

## 六、常见问题

| 现象 | 原因与处理 |
| --- | --- |
| 页面显示「未连接」 | AnythingLLM 没打开，或端口不是 3001；用 `--api` 指定 |
| 上传返回 502 | 后端地址不对，或工作区列表为空（先去 AnythingLLM 新建工作区） |
| 上传成功但工作区里看不到 | 大文件向量化需要时间，稍等后刷新；也可能是上传到了别的工作区，看页面顶部提示 |
| 超过 200MB 报错 | 后端限制了单文件 200MB，可在 `server.py` 里调整 `MAX_CONTENT_LENGTH` |
| 端口被占用 | `python server.py --port 8099` |
