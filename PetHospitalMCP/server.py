# -*- coding: utf-8 -*-
"""
宠物医院 MCP 服务器
====================

把本地运行的「宠物医院 REST API」（pethospital.exe，默认 http://127.0.0.1:8080）
包装成 MCP 工具，让任意支持 MCP 的 AI 客户端（AnythingLLM / Claude Desktop /
Cherry Studio / OpenCode 等）可以直接查询、录入宠物档案与病历。

设计与使用说明见同目录 README.md。

运行方式
--------
    python server.py                  # stdio 传输（本地客户端拉起，默认）
    python server.py --http           # Streamable HTTP，默认 127.0.0.1:8090/mcp
    python server.py --http --port 8095 --host 0.0.0.0

环境变量
--------
    PET_HOSPITAL_API        后端地址，默认 http://127.0.0.1:8080
    PET_HOSPITAL_TIMEOUT    单次请求超时秒数，默认 15
    PET_HOSPITAL_RETRIES    连接/超时/5xx 的重试次数，默认 2
    PET_HOSPITAL_LOG        日志文件路径；不填则输出到 stderr
    PET_HOSPITAL_LOG_LEVEL  日志级别，默认 INFO
"""

from __future__ import annotations

import argparse
import logging
import os
import sys
import time
from typing import Any

import httpx

try:  # mcp >= 2.0：FastMCP 改名为 MCPServer
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
except ImportError:  # 兼容 mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer  # type: ignore[assignment]

    class ToolError(Exception):  # type: ignore[no-redef]
        """1.x 没有专门的工具错误类型。"""


# 注意：工具里必须抛 ToolError 而不是普通异常。
# 普通异常会被框架压成一句 "Error executing tool xxx"，模型拿不到
# 「要先启动 pethospital.exe」这类可操作信息；ToolError 则会原样透传。


BASE_URL = os.environ.get("PET_HOSPITAL_API", "http://127.0.0.1:8080").rstrip("/")
TIMEOUT = float(os.environ.get("PET_HOSPITAL_TIMEOUT", "15"))

mcp = MCPServer(
    name="pet-hospital",
    instructions=(
        "提供本地宠物医院管理系统（Pet Hospital REST API）的查询与录入能力。"
        f"后端地址：{BASE_URL}。若连接失败，请先确认已双击运行 pethospital.exe。"
    ),
)

_client: httpx.Client | None = None

RETRIES = max(0, int(os.environ.get("PET_HOSPITAL_RETRIES", "2")))
BACKOFF_BASE = 0.3          # 退避基数：0.3s、0.6s、1.2s …
BACKOFF_CAP = 3.0

# --------------------------------------------------------------------------- #
# 调用日志
# --------------------------------------------------------------------------- #
# 注意：stdio 传输下 stdout 是 MCP 协议通道，任何日志都不能写进去，
# 否则会污染 JSON-RPC 流导致客户端解析失败。所以一律走 stderr 或文件。
logger = logging.getLogger("pet-hospital-mcp")


def setup_logging() -> None:
    """配置调用日志。文件路径可用 PET_HOSPITAL_LOG 指定，默认 stderr。"""
    if logger.handlers:                      # 避免重复添加 handler
        return
    level = os.environ.get("PET_HOSPITAL_LOG_LEVEL", "INFO").upper()
    logger.setLevel(getattr(logging, level, logging.INFO))

    path = os.environ.get("PET_HOSPITAL_LOG", "").strip()
    if path:
        try:
            os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
            handler: logging.Handler = logging.FileHandler(path, encoding="utf-8")
        except OSError as exc:               # 文件不可写就退回 stderr，不因日志而崩溃
            logger.warning("日志文件 %s 打不开（%s），改用 stderr", path, exc)
            handler = logging.StreamHandler(sys.stderr)
    else:
        handler = logging.StreamHandler(sys.stderr)

    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)-5s %(message)s", "%H:%M:%S")
    )
    logger.addHandler(handler)
    logger.propagate = False                 # 不向上冒泡，免得被 mcp 的 logging 再打一遍

    # httpx / httpcore 默认也往 root logger 抛 INFO，会把每次请求打两遍。
    # 我们自己的 logger 已经记了方法、路径、状态码和耗时，这里压到 WARNING。
    for noisy in ("httpx", "httpcore"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(base_url=BASE_URL, timeout=TIMEOUT)
    return _client


def _backoff(attempt: int) -> float:
    """指数退避，带上限。attempt 从 0 开始。"""
    return min(BACKOFF_BASE * (2 ** attempt), BACKOFF_CAP)


def call(method: str, path: str, **kwargs: Any) -> Any:
    """调用后端接口并拆掉统一响应信封，只把 data 交给模型。

    可恢复的失败（连接不上、超时、5xx）会做指数退避重试；
    4xx 与业务错误码不重试 —— 重试也不会变好，只会拖长等待。
    """
    started = time.monotonic()
    last_error: Exception | None = None

    for attempt in range(RETRIES + 1):
        try:
            resp = client().request(method, path, **kwargs)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.ReadTimeout,
                httpx.WriteTimeout, httpx.PoolTimeout) as exc:
            last_error = exc
            if attempt < RETRIES:
                delay = _backoff(attempt)
                logger.warning("%s %s 失败（%s），%.1fs 后第 %d/%d 次重试",
                               method, path, type(exc).__name__, delay, attempt + 1, RETRIES)
                time.sleep(delay)
                continue
            logger.error("%s %s 重试 %d 次后仍失败（%s）",
                         method, path, RETRIES, type(exc).__name__)
            if isinstance(exc, httpx.ConnectError):
                raise ToolError(
                    f"连接宠物医院服务失败（{BASE_URL}），已重试 {RETRIES} 次。"
                    f"请先运行 pethospital.exe 再重试。"
                ) from exc
            raise ToolError(
                f"请求超时：{method} {path}（已重试 {RETRIES} 次，单次超时 {TIMEOUT:.0f}s）"
            ) from exc
        except httpx.HTTPError as exc:       # 其余 httpx 异常不重试
            logger.error("%s %s 异常：%s", method, path, exc)
            raise ToolError(f"请求失败：{method} {path} — {exc}") from exc

        # 5xx 视为后端临时故障，值得重试；4xx 是调用方的问题，直接抛
        if resp.status_code >= 500 and attempt < RETRIES:
            delay = _backoff(attempt)
            logger.warning("%s %s 返回 %d，%.1fs 后第 %d/%d 次重试",
                           method, path, resp.status_code, delay, attempt + 1, RETRIES)
            time.sleep(delay)
            continue

        elapsed = (time.monotonic() - started) * 1000
        logger.info("%s %s -> %d (%.0fms, attempt=%d)",
                    method, path, resp.status_code, elapsed, attempt + 1)

        # 4xx/5xx 一律转成结构化中文错误。
        # 不能交给 raise_for_status()：那会把上游的 HTML 错误页原样带出来，
        # 模型看不懂，也污染上下文。这里只留状态码 + 截断后的响应片段。
        if resp.status_code >= 400:
            snippet = " ".join((resp.text or "").split())[:200]
            logger.warning("%s %s 返回 HTTP %d：%s",
                           method, path, resp.status_code, snippet)
            raise ToolError(
                f"后端返回 HTTP {resp.status_code}"
                f"{'（已重试 ' + str(RETRIES) + ' 次）' if resp.status_code >= 500 else ''}"
                f"：{snippet or resp.reason_phrase}"
            )

        try:
            body = resp.json()
        except ValueError:
            logger.warning("%s %s 返回非 JSON 内容", method, path)
            return resp.text

        if isinstance(body, dict) and "code" in body:
            if body.get("code") not in (200, 201):
                logger.warning("%s %s 业务错误 code=%s msg=%s",
                               method, path, body.get("code"), body.get("message"))
                raise ToolError(f"后端返回错误 {body.get('code')}：{body.get('message')}")
            return body.get("data", body)
        return body

    # 理论上不会走到这里：循环内要么 return 要么 raise
    raise ToolError(f"请求失败：{method} {path} — {last_error}")


# --------------------------------------------------------------------------- #
# 系统 / 字典 / 统计
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_meta() -> Any:
    """获取宠物医院的枚举字典与字段说明。

    返回种类、就诊状态、收费类别的可选值，以及 Pet 每个字段的名称、类型与含义。
    在调用 create_pet / update_pet 之前先查一次，可以避免字段值不合法。
    """
    return call("GET", "/api/v1/meta")


@mcp.tool()
def get_stats() -> Any:
    """获取经营统计：档案总数、病历总数、收费笔数、总收入、客单价、种类分布与医生排行。"""
    return call("GET", "/api/v1/stats")


@mcp.tool()
def health() -> Any:
    """健康检查：确认宠物医院服务是否已启动。"""
    return call("GET", "/health")


# --------------------------------------------------------------------------- #
# 档案 CRUD
# --------------------------------------------------------------------------- #
@mcp.tool()
def list_pets(
    q: str = "",
    name: str = "",
    ownerName: str = "",
    ownerPhone: str = "",
    species: str = "",
    doctor: str = "",
    disease: str = "",
    status: str = "",
    min_cost: float | None = None,
    max_cost: float | None = None,
    sort_by: str = "",
    order: str = "desc",
    page: int = 1,
    page_size: int = 10,
) -> Any:
    """分页查询宠物档案列表，支持关键词、字段过滤与排序。

    Args:
        q: 关键词，跨字段模糊匹配。
        name: 宠物姓名。
        ownerName: 主人姓名。
        ownerPhone: 主人电话。
        species: 种类，如「犬」「猫」。
        doctor: 主治医生。
        disease: 疾病名称。
        status: 就诊状态，如「待就诊」「住院中」。
        min_cost: 总花费下限（元）。
        max_cost: 总花费上限（元）。
        sort_by: 排序字段，常用 createdAt / totalCost / ageMonths / name。
        order: 排序方向，asc 或 desc。
        page: 页码，从 1 开始。
        page_size: 每页条数。
    """
    params: dict[str, Any] = {
        "page": page,
        "pageSize": page_size,
        "order": order or "desc",
    }
    for key, value in (
        ("q", q), ("name", name), ("ownerName", ownerName), ("ownerPhone", ownerPhone),
        ("species", species), ("doctor", doctor), ("disease", disease), ("status", status),
        ("sortBy", sort_by),
    ):
        if value:
            params[key] = value
    if min_cost is not None:
        params["min"] = min_cost
    if max_cost is not None:
        params["max"] = max_cost
    return call("GET", "/api/v1/pets", params=params)


@mcp.tool()
def get_pet(pet_id: str) -> Any:
    """按档案编号查询单只宠物的完整档案（含历史病历与消费明细）。

    Args:
        pet_id: 档案编号，形如 PET-000001。
    """
    return call("GET", f"/api/v1/pets/{pet_id}")


@mcp.tool()
def create_pet(
    name: str,
    species: str,
    ownerName: str,
    ownerPhone: str = "",
    breed: str = "",
    gender: str = "",
    ageMonths: int = 0,
    color: str = "",
    chipNo: str = "",
    ownerAddr: str = "",
    doctor: str = "",
    disease: str = "",
    status: str = "待就诊",
    allergy: str = "无",
    note: str = "",
) -> Any:
    """新建一条宠物档案，编号由服务端自动生成（PET-000001 递增）。

    Args:
        name: 宠物姓名（必填）。
        species: 种类（必填），如 犬 / 猫 / 兔 / 鸟 / 仓鼠 / 爬宠 / 其他。
        ownerName: 主人姓名（必填）。
        ownerPhone: 主人电话。
        breed: 品种。
        gender: 性别，公 或 母。
        ageMonths: 月龄。
        color: 毛色。
        chipNo: 芯片号，可为空。
        ownerAddr: 主人住址。
        doctor: 主治医生。
        disease: 主要疾病或主诉。
        status: 就诊状态，默认「待就诊」。
        allergy: 过敏史，无则填「无」。
        note: 备注。
    """
    payload = {
        "name": name, "species": species, "ownerName": ownerName,
        "ownerPhone": ownerPhone, "breed": breed, "gender": gender,
        "ageMonths": ageMonths, "color": color, "chipNo": chipNo,
        "ownerAddr": ownerAddr, "doctor": doctor, "disease": disease,
        "status": status, "allergy": allergy, "note": note,
    }
    return call("POST", "/api/v1/pets", json=payload)


@mcp.tool()
def update_pet(pet_id: str, fields: dict) -> Any:
    """局部更新宠物档案，只修改传入的字段（PATCH 语义，不会清空其他字段）。

    Args:
        pet_id: 档案编号，形如 PET-000001。
        fields: 需要更新的字段字典，例如 {"status": "已康复", "doctor": "王医生"}。
    """
    return call("PATCH", f"/api/v1/pets/{pet_id}", json=fields)


@mcp.tool()
def delete_pet(pet_id: str) -> Any:
    """删除一只宠物的档案（含其病历与收费记录），操作不可撤销。

    Args:
        pet_id: 档案编号，形如 PET-000001。
    """
    return call("DELETE", f"/api/v1/pets/{pet_id}")


# --------------------------------------------------------------------------- #
# 高级查询
# --------------------------------------------------------------------------- #
@mcp.tool()
def search_pets(q: str) -> Any:
    """跨字段全文检索：宠物名、主人、品种、疾病、医生，以及历史病历全文。

    多个关键词用空格分隔，按 AND 组合。

    Args:
        q: 检索关键词，例如「金毛 肠胃炎」。
    """
    return call("GET", "/api/v1/pets/search", params={"q": q})


@mcp.tool()
def top_spenders(limit: int = 5) -> Any:
    """消费排行榜：按总花费从高到低返回宠物档案。

    Args:
        limit: 返回条数，默认 5。
    """
    return call("GET", "/api/v1/pets/top-spenders", params={"limit": limit})


# --------------------------------------------------------------------------- #
# 病历与收费
# --------------------------------------------------------------------------- #
@mcp.tool()
def get_pet_records(pet_id: str) -> Any:
    """查询某只宠物的历史病历列表。

    Args:
        pet_id: 档案编号，形如 PET-000001。
    """
    return call("GET", f"/api/v1/pets/{pet_id}/records")


@mcp.tool()
def add_pet_record(
    pet_id: str,
    doctor: str,
    diagnosis: str,
    symptoms: str = "",
    treatment: str = "",
    prescription: list[str] | None = None,
    charge: float = 0,
    date: str = "",
) -> Any:
    """为某只宠物追加一条历史病历（就诊次数会自动 +1）。

    Args:
        pet_id: 档案编号。
        doctor: 接诊医生。
        diagnosis: 诊断结论。
        symptoms: 症状描述。
        treatment: 处置 / 治疗方案。
        prescription: 处方药列表。
        charge: 本次就诊费用（元）。
        date: 就诊日期，留空由服务端填当前时间。
    """
    payload: dict[str, Any] = {
        "doctor": doctor, "diagnosis": diagnosis, "symptoms": symptoms,
        "treatment": treatment, "prescription": prescription or [], "charge": charge,
    }
    if date:
        payload["date"] = date
    return call("POST", f"/api/v1/pets/{pet_id}/records", json=payload)


@mcp.tool()
def get_pet_charges(pet_id: str) -> Any:
    """查询某只宠物的消费明细。

    Args:
        pet_id: 档案编号，形如 PET-000001。
    """
    return call("GET", f"/api/v1/pets/{pet_id}/charges")


@mcp.tool()
def add_pet_charge(
    pet_id: str,
    item: str,
    category: str,
    amount: float,
    doctor: str = "",
    date: str = "",
) -> Any:
    """为某只宠物追加一笔收费，总花费会自动累计。

    Args:
        pet_id: 档案编号。
        item: 收费项目，如「血常规检查」。
        category: 类别，如 检查 / 药品 / 手术 / 住院 / 疫苗 / 驱虫 / 美容 / 其他。
        amount: 金额（元）。
        doctor: 经手医生。
        date: 日期，留空由服务端填当前时间。
    """
    payload: dict[str, Any] = {
        "item": item, "category": category, "amount": amount, "doctor": doctor,
    }
    if date:
        payload["date"] = date
    return call("POST", f"/api/v1/pets/{pet_id}/charges", json=payload)


@mcp.tool()
def get_pet_summary(pet_id: str) -> Any:
    """单只宠物的费用与就诊汇总：就诊次数、总花费、收费明细与病历摘要。

    Args:
        pet_id: 档案编号，形如 PET-000001。
    """
    return call("GET", f"/api/v1/pets/{pet_id}/summary")


def main() -> None:
    parser = argparse.ArgumentParser(description="宠物医院 MCP 服务器")
    parser.add_argument("--http", action="store_true", help="以 Streamable HTTP 方式运行")
    parser.add_argument("--host", default="127.0.0.1", help="HTTP 监听地址")
    parser.add_argument("--port", type=int, default=8090, help="HTTP 监听端口")
    args = parser.parse_args()

    setup_logging()
    logger.info("启动 pet-hospital-mcp · 后端 %s · 超时 %.0fs · 重试 %d 次",
                BASE_URL, TIMEOUT, RETRIES)

    if args.http:
        logger.info("传输方式 Streamable HTTP -> http://%s:%d/mcp", args.host, args.port)
        print(f"[pet-hospital-mcp] Streamable HTTP -> http://{args.host}:{args.port}/mcp", flush=True)
        mcp.run(transport="streamable-http", host=args.host, port=args.port)
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
