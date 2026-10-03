"""FastAPI 应用：样本交接链核验 HTTP API。"""
import json
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from . import config
from .db import db_session, init_db, row_to_dict
from .dossiers import get_dossier, submit_dossier, verify_dossier
from .identities import add_alias, get_person, list_persons, register_person
from .reconciliations import get_batch_reconciliation, submit_batch_reconciliation
from .reviews import get_review, submit_review
from .schemas import (
    AliasIn,
    ApproveIn,
    BatchReconciliationIn,
    CorrectionIn,
    DossierIn,
    EventIn,
    PersonIn,
    ReviewIn,
    SampleIn,
    TemperatureConfigIn,
    TemperatureCorrectionIn,
    TemperatureReadingIn,
    TransferBatchIn,
)
from .service import (
    DomainError,
    approve_storage,
    create_event,
    evaluate,
    evaluation_summary,
    get_sample_or_404,
    get_transfer_batch,
    list_corrections,
    register_sample,
    submit_correction,
    submit_transfer_batch,
)
from .temperature import (
    add_reading,
    build_report,
    config_to_dict,
    get_config_row,
    list_readings,
    list_temperature_corrections,
    set_temperature_config,
    submit_temperature_correction,
)
from .versions import get_version, latest_version_no, list_versions, parse_version

API_PREFIX = "/api/v1"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    if config.SEED_ON_START:
        # 仅当库中尚无样本时写入演示数据，重启不重复
        from .seed import seed_demo

        with db_session() as conn:
            count = conn.execute("SELECT COUNT(*) FROM sample").fetchone()[0]
        if count == 0:
            seed_demo()
    yield


app = FastAPI(
    title="样本交接链核验服务",
    description="环境检测机构样本保管链（Chain of Custody）登记、事件核验、入库核定、版本化复核、人员标识复核回避与运输测温复核",
    version="1.8.0",
    lifespan=lifespan,
)


@app.exception_handler(DomainError)
async def domain_error_handler(request: Request, exc: DomainError) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"error": {"code": exc.code, "message": exc.message, "details": exc.details}})


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    """请求体字段校验失败统一为 422 validation_error。

    逐字段给出 loc/msg；不回显原始输入值——测温值可能为 NaN/Infinity，
    回显会使 JSON 响应本身不可序列化（且无必要把非法值再返回给调用方）。
    """
    items = [
        {"field": ".".join(str(p) for p in err.get("loc", []) if p != "body"),
         "message": err.get("msg", "")}
        for err in exc.errors()
    ]
    return JSONResponse(
        status_code=422,
        content={"error": {"code": "validation_error",
                           "message": "请求参数校验失败（字段缺失/类型不符/取值非法）",
                           "details": {"items": items}}},
    )


def _summary(result: dict[str, Any], sample: dict[str, Any], conn=None) -> dict[str, Any]:
    body = evaluation_summary(result, sample)
    if conn is not None:
        # 当前最新版本号（版本 0 起；便于质控复核定位）
        body["current_version"] = latest_version_no(conn, sample["id"])
    return body


# --------------------------------------------------------------------------- 基本信息

@app.get("/", tags=["meta"])
def root() -> dict[str, Any]:
    return {
        "service": "样本交接链核验服务",
        "docs": "/docs",
        "health": "/health",
        "api_prefix": API_PREFIX,
        "status_values": {"valid": "有效", "pending": "待补证", "invalid": "无效"},
    }


@app.get("/health", tags=["meta"])
def health() -> dict[str, str]:
    return {"status": "ok"}


# --------------------------------------------------------------------------- 样本登记

@app.post(f"{API_PREFIX}/samples", tags=["samples"], status_code=201)
def register_sample_api(data: SampleIn) -> JSONResponse:
    with db_session(immediate=True) as conn:
        sample, idempotent = register_sample(conn, data)
        result = evaluate(conn, sample)
        body = {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no", "created_at")},
            "evaluation": _summary(result, dict(sample), conn),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples", tags=["samples"])
def list_samples_api() -> dict[str, Any]:
    with db_session() as conn:
        samples = conn.execute("SELECT * FROM sample ORDER BY id").fetchall()
        items = [_summary(evaluate(conn, s), dict(s), conn) for s in samples]
    return {"total": len(items), "items": items}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}", tags=["samples"])
def get_sample_api(sample_code: str) -> dict[str, Any]:
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {"sample": row_to_dict(sample), "evaluation": _summary(result, dict(sample), conn)}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/chain", tags=["samples"])
def get_chain_api(sample_code: str) -> dict[str, Any]:
    """完整证据链：按时间排列的事件、当前保管人、结论与原因。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        result = evaluate(conn, sample)
        return {
            "sample": {k: sample[k] for k in ("sample_code", "collected_at", "collector", "seal_no")},
            "status": result["status"],
            "reasons": result["reasons"],
            "current_custodian": result["current_custodian"],
            "pending_transfer": result["pending_transfer"],
            "has_storage": result["has_storage"],
            "approved_storage_at": sample["approved_storage_at"],
            "timeline": result["timeline"],
            "corrections": result["corrections"],
            "superseded_events": result["superseded_events"],
            "current_version": latest_version_no(conn, sample["id"]),
        }


# --------------------------------------------------------------------------- 保管链版本复核

@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions", tags=["versions"])
def list_versions_api(sample_code: str) -> dict[str, Any]:
    """版本索引：列出该样本全部版本（版本号、提交时间、触发类型、当时结论）。

    版本按提交先后排列；核定后旧版仍可查询。
    """
    with db_session() as conn:
        return list_versions(conn, sample_code)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions/latest", tags=["versions"])
def get_latest_version_api(sample_code: str) -> dict[str, Any]:
    """当前最新版本的完整保管链快照（便捷端点，等价于按 current_version 取版本）。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        version = latest_version_no(conn, sample["id"])
        return {"version_snapshot": get_version(conn, sample_code, version)}


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/versions/{{version_raw}}", tags=["versions"])
def get_version_api(sample_code: str, version_raw: str) -> dict[str, Any]:
    """指定版本复核：返回该版本提交时点的核验结论、原因、当前保管人、
    有效事件时间线、纠错记录与核定信息（不可变快照）。

    未知样本 → 404 sample_not_found；不存在的版本 → 404 version_not_found。
    """
    version = parse_version(version_raw)
    with db_session() as conn:
        return {"version_snapshot": get_version(conn, sample_code, version)}


# --------------------------------------------------------------------------- 事件上报

@app.post(f"{API_PREFIX}/events", tags=["events"], status_code=201)
def create_event_api(data: EventIn) -> JSONResponse:
    """移交 / 签收 / 封签检查 / 入库 统一上报入口。

    同 event_id 同内容重试 → 200 幂等返回；同 event_id 异内容 → 409 冲突。
    """
    with db_session(immediate=True) as conn:
        row, idempotent = create_event(conn, data)
        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
        result = evaluate(conn, sample)
        body = {
            "event": {
                "event_id": row["event_id"],
                "event_type": row["event_type"],
                "sample_code": data.sample_code,
                "occurred_at": row["occurred_at"],
                "payload": json.loads(row["payload_json"]),
                "recorded_at": row["created_at"],
            },
            "evaluation": _summary(result, dict(sample), conn),
            "idempotent": idempotent,
        }
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


# --------------------------------------------------------------------------- 批量移交（同一交接车次多份样本）

@app.post(f"{API_PREFIX}/transfer-batches", tags=["transfer-batches"], status_code=201)
def submit_transfer_batch_api(data: TransferBatchIn) -> JSONResponse:
    """同一交接车次多份样本一次提交，各份作为既有 transfer 事件原子写入。

    * 整批失败不留痕：批内样本/事件号重复、样本不存在/已核定、任一事件号已占用
      → 事务回滚，不创建批次与任何事件（409/422 batch_rejected，逐项给出）；
    * 同批次号同内容重试 → 200 幂等（样本此后已核定也成立）；异内容 → 409 batch_conflict；
    * 并发提交由 BEGIN IMMEDIATE 串行化，按已提交顺序判定。
    """
    with db_session(immediate=True) as conn:
        batch, idempotent = submit_transfer_batch(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"batch": batch, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/transfer-batches/{{batch_id}}", tags=["transfer-batches"])
def get_transfer_batch_api(batch_id: str) -> dict[str, Any]:
    """批次查询：逐份样本的当前核验结论及原事件是否仍有效（纠错后反映有效链）。"""
    with db_session() as conn:
        return {"batch": get_transfer_batch(conn, batch_id)}


# --------------------------------------------------------------------------- 整批对账签收（批量移交到达接收方）

@app.post(f"{API_PREFIX}/transfer-batches/{{batch_id}}/reconciliation", tags=["reconciliations"], status_code=201)
def submit_batch_reconciliation_api(batch_id: str, data: BatchReconciliationIn) -> JSONResponse:
    """同一批次到达接收方后的整批对账签收：逐份清单覆盖原批次全部样本。

    * 收妥项（event_id + verified_seal_no）：须对应仍有效且处于该笔移交在途状态的事件，
      签收人/封签/时间满足既有单份签收规则；成功时作为既有 receive 事件与各样本新版本同事务提交；
    * 异常项（reason）：仅留对账记录、不生成签收事件；
    * 批次不存在、缺项/重复/外来项、事件号占用、任一收妥项不合格 → 整单拒绝且不留记录
      （409 reconciliation_rejected=已核定/事件号占用；422=清单不合法/收妥项不合格，逐项给出）；
    * 同签收单号同内容重试 → 200 幂等返回原结果；异内容（含跨批次）→ 409 reconciliation_conflict；
    * 一个批次只能成功对账一次：批次已对账再提交 → 409 batch_already_reconciled；
    * 并发由 BEGIN IMMEDIATE 串行，按已提交状态判定。
    """
    with db_session(immediate=True) as conn:
        reconciliation, idempotent = submit_batch_reconciliation(conn, batch_id, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"reconciliation": reconciliation, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/transfer-batches/{{batch_id}}/reconciliation", tags=["reconciliations"])
def get_batch_reconciliation_api(batch_id: str) -> dict[str, Any]:
    """批次对账查询：当时的收妥/异常明细（原样留痕）与各收妥事件纠错后的现时有效性。"""
    with db_session() as conn:
        return {"reconciliation": get_batch_reconciliation(conn, batch_id)}


# --------------------------------------------------------------------------- 事件纠错

@app.post(f"{API_PREFIX}/corrections", tags=["corrections"], status_code=201)
def submit_correction_api(data: CorrectionIn) -> JSONResponse:
    """核定前事件纠错：作废（void）或替换（replace）。

    * 仅核定入库前、且目标为当前有效版本时可提交；不得跨样本；
    * 同 correction_id 同内容重试 → 200 幂等；异内容 → 409 correction_conflict；
    * replace 须给完整新事件内容与未占用新事件号；
    * 并发针对同一有效版本仅一次成功，失败不改变链；
    * 响应 evaluation 为纠错后仅按有效版本重算的结论/原因/当前保管人。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = submit_correction(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/corrections", tags=["corrections"])
def list_corrections_api(sample_code: str) -> dict[str, Any]:
    """样本的纠错审计记录（原事件与纠错关系永久留痕）。"""
    with db_session() as conn:
        items = list_corrections(conn, sample_code)
    return {"sample_code": sample_code, "total": len(items), "items": items}


# --------------------------------------------------------------------------- 入库核定

@app.post(f"{API_PREFIX}/samples/{{sample_code}}/approve", tags=["approval"])
def approve_api(sample_code: str, data: ApproveIn) -> dict[str, Any]:
    with db_session(immediate=True) as conn:
        result = approve_storage(conn, sample_code, data)
        sample = get_sample_or_404(conn, sample_code)
        return {"evaluation": _summary(result, dict(sample), conn), "approval": result["approval"]}


# --------------------------------------------------------------------------- 运输测温复核

@app.put(f"{API_PREFIX}/samples/{{sample_code}}/temperature-config", tags=["temperature"])
def set_temperature_config_api(sample_code: str, data: TemperatureConfigIn) -> JSONResponse:
    """一次性设定样本运输测温配置：允许摄氏温度闭区间 + 正数最大测温间隔（秒）。

    * 同内容重试 → 200 幂等；配置已存在且异内容 → 409 temp_config_conflict；
    * 样本已核定入库 → 409 history_locked（拒绝新配置）；样本不存在 → 404。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = set_temperature_config(conn, sample_code, data)
    body["idempotent"] = idempotent
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/temperature-config", tags=["temperature"])
def get_temperature_config_api(sample_code: str) -> dict[str, Any]:
    """查询样本的一次性测温配置；尚未设定 → 404 temp_config_not_found。"""
    with db_session() as conn:
        sample = get_sample_or_404(conn, sample_code)
        row = get_config_row(conn, sample["id"])
        if row is None:
            raise DomainError(
                404, "temp_config_not_found",
                f"样本 {sample_code} 尚未设定测温配置", {"sample_code": sample_code},
            )
        return {"config": config_to_dict(row)}


@app.post(f"{API_PREFIX}/samples/{{sample_code}}/temperature-readings", tags=["temperature"], status_code=201)
def add_temperature_reading_api(sample_code: str, data: TemperatureReadingIn) -> JSONResponse:
    """记录测温读数：全局唯一读数号、设备号、发生时间、有限摄氏数值。

    * 发生时间早于采集 → 422 reading_before_collection；
    * 同读数号同内容重试 → 200 返回原记录；同号异内容 → 409 temp_reading_conflict；
    * 尚未配置测温 → 422 temp_config_required；样本已核定 → 409 history_locked；
    * 支持乱序补录，查询按发生时间重算。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = add_reading(conn, sample_code, data)
    body["idempotent"] = idempotent
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/temperature-readings", tags=["temperature"])
def list_temperature_readings_api(sample_code: str) -> dict[str, Any]:
    """样本全部测温读数（含作废留痕），按发生时间排列。"""
    with db_session() as conn:
        return list_readings(conn, sample_code)


@app.post(f"{API_PREFIX}/temperature-corrections", tags=["temperature"], status_code=201)
def submit_temperature_correction_api(data: TemperatureCorrectionIn) -> JSONResponse:
    """测温读数纠错（核定前）：作废 void / 替换 replace，永久留痕、原始读数不改。

    * 同纠错号同内容重试 → 200 幂等（带重算报告）；异内容 → 409 temp_correction_conflict；
    * 目标读数不存在 → 404 temp_reading_not_found；跨样本 → 409 temp_correction_cross_sample；
    * 目标已被作废/替换 → 409 reading_not_effective；新读数号占用 → 409 new_reading_id_occupied；
    * 样本已核定 → 409 history_locked；纠错后归属按当前有效保管链与有效读数重算。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = submit_temperature_correction(conn, data)
    body["idempotent"] = idempotent
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/temperature-corrections", tags=["temperature"])
def list_temperature_corrections_api(sample_code: str) -> dict[str, Any]:
    """样本测温读数纠错审计记录（永久留痕）。"""
    with db_session() as conn:
        return list_temperature_corrections(conn, sample_code)


@app.get(f"{API_PREFIX}/samples/{{sample_code}}/temperature-report", tags=["temperature"])
def temperature_report_api(sample_code: str) -> dict[str, Any]:
    """测温复核报告：按发生时间列超温点与超阈缺测区间（跨移交/签收时刻拆分，
    逐段标明当时保管人 custody / 在途 in_transit / 断链待核 pending_verify）。

    依当前有效保管链与当前有效读数实时重算（乱序补录、移交/签收纠错后自动反映）；
    尚未配置测温 → 404 temp_config_not_found。
    """
    with db_session() as conn:
        return {"report": build_report(conn, sample_code)}


# --------------------------------------------------------------------------- 案卷封存（同一抽检多份样本）

@app.post(f"{API_PREFIX}/dossiers", tags=["dossiers"], status_code=201)
def submit_dossier_api(data: DossierIn) -> JSONResponse:
    """同一抽检的多份样本保管链封存为案卷。

    * 至少两份不重复样本；可逐份指定非负历史版本，未指定者取封存事务中的最新版本
      （整卷选取在同一事务的一致视图中完成）；
    * 任一样本/版本不存在、编号重复 → 整卷失败且不留案卷；
    * 案卷永久保存所选版本完整快照与按样本编号排序的清单，并生成可复算 SHA-256 摘要；
    * 同案卷号同内容重试 → 200 幂等返回原卷（即使最新版本已变化）；异内容 → 409 dossier_conflict；
    * 并发提交由 BEGIN IMMEDIATE 串行化，遵循已提交结果。
    """
    with db_session(immediate=True) as conn:
        dossier, idempotent = submit_dossier(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"dossier": dossier, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/dossiers/{{dossier_id}}", tags=["dossiers"])
def get_dossier_api(dossier_id: str) -> dict[str, Any]:
    """案卷查询：按样本编号排序的清单、各份所选版本完整快照与封存摘要。"""
    with db_session() as conn:
        return {"dossier": get_dossier(conn, dossier_id)}


@app.post(f"{API_PREFIX}/dossiers/{{dossier_id}}/verify", tags=["dossiers"])
def verify_dossier_api(dossier_id: str) -> dict[str, Any]:
    """案卷核验：按封存内容重算 SHA-256 摘要并返回一致与否（只读，不静默改写）。"""
    with db_session() as conn:
        return verify_dossier(conn, dossier_id)


# --------------------------------------------------------------------------- 案卷双人独立复核

@app.post(f"{API_PREFIX}/dossiers/{{dossier_id}}/reviews", tags=["reviews"], status_code=201)
def submit_review_api(dossier_id: str, data: ReviewIn) -> JSONResponse:
    """已封存案卷双人独立复核 / 第三人裁决提交。

    * 提交必须携带案卷封存摘要 expected_digest，与库存摘要不一致拒绝写入；
    * 前两份由两名不同复核人各提交一次覆盖卷内每份样本的完整清单，意见提交后不可改；
    * 逐份意见一致即终局，分歧份项仅允许未参与前两次复核的第三人裁决；
    * 全部份项终局后形成案卷结论，任一份不通过则案卷不通过；
    * 利益冲突回避：复核人（及其登记别名）命中封存份项的采集/移交/签收/核定参与人
      → 409 review_recusal 整单拒绝（两人复核按全部份项、第三人裁决按分歧份项检查）；
    * 复核人互异与第三人资格按本次提交时的人员标识/别名归属判定；
      成功意见记录当时的人员归属（person_id）供查询；
    * 同复核单号同内容重试 → 200 幂等返回原结果；异内容 → 409 review_conflict；
    * 缺项 / 重复份项 / 重复复核人 / 越阶段提交拒绝且不留部分记录；
    * 并发提交由 BEGIN IMMEDIATE 串行，按已提交状态判定。
    """
    with db_session(immediate=True) as conn:
        body, idempotent = submit_review(conn, dossier_id, data)
    body["idempotent"] = idempotent
    return JSONResponse(status_code=200 if idempotent else 201, content=body)


@app.get(f"{API_PREFIX}/dossiers/{{dossier_id}}/reviews", tags=["reviews"])
def get_review_api(dossier_id: str) -> dict[str, Any]:
    """案卷复核查询：复核进度、原始意见（含提交时人员归属）、分歧对照与终局结论（含逐份状态）。"""
    with db_session() as conn:
        return get_review(conn, dossier_id)


# --------------------------------------------------------------------------- 人员标识与历史别名（复核回避）

@app.post(f"{API_PREFIX}/persons", tags=["identities"], status_code=201)
def register_person_api(data: PersonIn) -> JSONResponse:
    """登记人员标识（主标识 + 可选初始历史别名）。

    * 主标识与全部别名构成全局唯一命名空间：任何名称只归属一人，
      别名不得归属多人、不得转移归属；
    * 登记成功后修订号为 1；同主标识同内容（含初始别名集合）重试 → 200 幂等，
      异内容 → 409 person_conflict；名称已被占用 → 409 alias_conflict。
    """
    with db_session(immediate=True) as conn:
        person, idempotent = register_person(conn, data)
    return JSONResponse(status_code=200 if idempotent else 201,
                        content={"person": person, "idempotent": idempotent})


@app.get(f"{API_PREFIX}/persons", tags=["identities"])
def list_persons_api() -> dict[str, Any]:
    """全部已登记人员：主标识、当前修订号与全部别名。"""
    with db_session() as conn:
        return list_persons(conn)


@app.get(f"{API_PREFIX}/persons/{{person_id}}", tags=["identities"])
def get_person_api(person_id: str) -> dict[str, Any]:
    """人员查询：主标识、当前修订号（增补别名时须携带）与全部别名。"""
    with db_session() as conn:
        return {"person": get_person(conn, person_id)}


@app.post(f"{API_PREFIX}/persons/{{person_id}}/aliases", tags=["identities"], status_code=201)
def add_alias_api(person_id: str, data: AliasIn) -> dict[str, Any]:
    """增补历史别名：须携带所见修订号（expected_revision）。

    * 与人员当前修订号不一致（过期）→ 409 identity_revision_stale；
    * 别名已归属任何人（含本人、含任何主标识）→ 409 alias_conflict；
    * 成功：别名落库且修订号 +1；并发按 BEGIN IMMEDIATE 串行后的已提交修订号判定。
    """
    with db_session(immediate=True) as conn:
        person = add_alias(conn, person_id, data)
    return {"person": person}
