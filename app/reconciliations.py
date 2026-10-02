"""批量移交到达后的整批对账签收（接收方）。

规则（与 README 一致）：
  * 接收方以全局唯一签收单号、签收人、统一签收时间，逐份提交覆盖原批次全部样本的
    对账清单：收妥项填写唯一事件号及实核封签号；异常项填写原因、不生成签收事件；
  * 收妥项须对应原批次该份样本仍有效（未被作废/替换失效）且处于该笔移交在途状态的
    事件，且签收人=在途移交接收人、实核封签=登记封签、签收时间晚于移交时间
    （既有单份签收规则）；成功时各收妥项作为既有 receive 事件与各样本新版本同事务提交；
  * 异常项仅留对账记录、不产生签收事件与版本；
  * 批次不存在、缺项/重复/外来项、事件号占用、任一收妥项不合格 → 整单拒绝且不留记录；
  * 一个批次只能成功对账一次；同签收单号同内容重试返回原结果（200 幂等），
    异内容（含跨批次复用单号）→ 409 reconciliation_conflict；
    批次已对账再以新单号提交 → 409 batch_already_reconciled；
    写事务 BEGIN IMMEDIATE 串行，并发按已提交状态判定（批次唯一键/单号唯一键兜底）；
  * 查询只呈现：当时的收妥/异常明细（留痕不改写）与各收妥事件的现时有效性
    （作废/替换后 event_effective/current_event_id 反映有效链）。
"""
import json
import sqlite3
from typing import Any

from .db import utcnow_iso
from .schemas import BatchReconciliationIn
from .service import (
    DomainError,
    _effective_event_id,
    _fingerprint,
    evaluate,
    get_batch_item_effective_transfer,
    to_utc_iso,
)
from .versions import CHANGE_EVENT, latest_version_no, record_version


def _reconciliation_fingerprint(batch_id: str, data: BatchReconciliationIn) -> tuple[str, str]:
    """返回 (归一化 UTC 签收时间, 请求指纹)。

    指纹含批次号（跨批次复用同单号即异内容）、签收人、统一时间，以及按样本编号排序、
    与提交顺序无关的逐份清单（收妥/异常形态、事件号、实核封签、异常原因）。
    """
    occurred = to_utc_iso(data.occurred_at)
    items = sorted(
        (
            {
                "sample_code": it.sample_code,
                "outcome": "exception" if it.is_exception else "received",
                "event_id": it.event_id,
                "verified_seal_no": it.verified_seal_no,
                "reason": it.reason,
            }
            for it in data.items
        ),
        key=lambda x: x["sample_code"],
    )
    canonical = {
        "reconciliation_no": data.reconciliation_no,
        "batch_id": batch_id,
        "receiver": data.receiver,
        "occurred_at": occurred,
        "items": items,
    }
    return occurred, _fingerprint(canonical)


def _get_batch(conn: sqlite3.Connection, batch_id: str) -> sqlite3.Row:
    batch = conn.execute(
        "SELECT * FROM transfer_batch WHERE batch_id = ?", (batch_id,)
    ).fetchone()
    if batch is None:
        raise DomainError(404, "batch_not_found", f"批次不存在: {batch_id}")
    return batch


def _get_reconciliation_by_no(conn: sqlite3.Connection, reconciliation_no: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM batch_reconciliation WHERE reconciliation_no = ?", (reconciliation_no,)
    ).fetchone()


def _evaluation_with_version(conn: sqlite3.Connection, sample: sqlite3.Row) -> dict[str, Any]:
    from .service import evaluation_summary

    result = evaluation_summary(evaluate(conn, sample), sample)
    result["current_version"] = latest_version_no(conn, sample["id"])
    return result


def _validate_received_item(
    conn: sqlite3.Connection,
    *,
    index: int,
    item: Any,
    batch_item: sqlite3.Row,
    sample: sqlite3.Row,
    receiver: str,
    occurred_iso: str,
    evaluations: dict[int, dict[str, Any]],
    conflict_items: list[dict[str, Any]],
    invalid_items: list[dict[str, Any]],
) -> sqlite3.Row | None:
    """逐项核验一个收妥项；不合格时向 conflict/invalid 列表追加明细。

    通过时返回该份当前有效的在途 transfer 事件行（receive 落库取其移交单号）。
    409 类（已核定）入 conflict_items，其余 422 入 invalid_items。
    """
    base = {"index": index, "sample_code": item.sample_code, "event_id": item.event_id}

    # 已核定入库：历史冻结，不得再追加签收
    if sample["approved_storage_at"]:
        conflict_items.append({
            **base,
            "code": "sample_approved",
            "message": f"样本 {item.sample_code} 已核定入库（{sample['approved_storage_at']}），禁止追加签收",
        })
        return None

    # 原批次移交（或其替换后继）必须仍是当前有效 transfer 事件
    eff = get_batch_item_effective_transfer(conn, batch_item["event_row_id"])
    if eff is None:
        invalid_items.append({
            **base,
            "code": "transfer_not_effective",
            "message": f"批次内样本 {item.sample_code} 的移交事件已失效（被作废/替换且无有效后继），无对应在途移交",
        })
        return None
    eff_payload = json.loads(eff["payload_json"])

    result = evaluations.setdefault(sample["id"], evaluate(conn, sample))
    pending = result["pending_transfer"]
    if pending is None or pending.get("event_id") != eff["event_id"]:
        invalid_items.append({
            **base,
            "code": "transfer_not_in_transit",
            "message": f"收妥项须对应仍处于该笔移交在途状态的事件：样本 {item.sample_code} 的"
                       f"有效移交 {eff['event_id']}（单号 {eff_payload['transfer_no']}）当前不在途",
        })
        return None

    # 既有单份签收规则：签收人=接收方、实核封签=登记封签、签收时间晚于移交时间
    qualified = True
    if receiver != eff_payload["to_custodian"]:
        invalid_items.append({
            **base,
            "code": "receiver_mismatch",
            "message": f"签收人 {receiver} ≠ 移交单 {eff_payload['transfer_no']} 指定接收人 {eff_payload['to_custodian']}",
        })
        qualified = False
    if item.verified_seal_no != sample["seal_no"]:
        invalid_items.append({
            **base,
            "code": "seal_mismatch",
            "message": f"实核封签号 {item.verified_seal_no} ≠ 登记封签号 {sample['seal_no']}",
        })
        qualified = False
    if occurred_iso <= eff["occurred_at"]:
        invalid_items.append({
            **base,
            "code": "receive_time_invalid",
            "message": f"签收时间 {occurred_iso} 不晚于移交时间 {eff['occurred_at']}"
                       f"（单号 {eff_payload['transfer_no']}）",
        })
        qualified = False
    if occurred_iso < sample["collected_at"]:
        invalid_items.append({
            **base,
            "code": "receive_time_invalid",
            "message": f"签收时间 {occurred_iso} 早于样本采集时间 {sample['collected_at']}",
        })
        qualified = False
    return eff if qualified else None


def submit_batch_reconciliation(
    conn: sqlite3.Connection, batch_id: str, data: BatchReconciliationIn
) -> tuple[dict[str, Any], bool]:
    """提交整批对账签收。返回 (对账查询视图, 是否幂等重试)。

    全部校验通过后：收妥项 receive 事件 + 各样本新版本 + 对账头/明细同一事务提交，
    异常项仅写对账明细；任一不合格整单回滚不留记录。
    """
    occurred_iso, request_hash = _reconciliation_fingerprint(batch_id, data)

    # 1) 批次必须存在
    batch = _get_batch(conn, batch_id)

    # 2) 同签收单号：同内容幂等 / 异内容（含跨批次）冲突——优先于业务校验，
    #    与批次、事件接口语义一致。
    existing_no = _get_reconciliation_by_no(conn, data.reconciliation_no)
    if existing_no is not None:
        existing_batch = conn.execute(
            "SELECT batch_id FROM transfer_batch WHERE id = ?", (existing_no["batch_pk"],)
        ).fetchone()
        if existing_no["request_hash"] != request_hash or existing_batch["batch_id"] != batch_id:
            raise DomainError(
                409,
                "reconciliation_conflict",
                f"签收单号 {data.reconciliation_no} 已使用但提交内容不一致"
                "（同签收单号异内容或跨批次复用，冲突）",
                {"reconciliation_no": data.reconciliation_no,
                 "existing_batch_id": existing_batch["batch_id"],
                 "existing_request_hash": existing_no["request_hash"],
                 "incoming_request_hash": request_hash},
            )
        return get_batch_reconciliation(conn, existing_batch["batch_id"]), True

    # 3) 一个批次只能成功对账一次
    existing_batch_recon = conn.execute(
        "SELECT * FROM batch_reconciliation WHERE batch_pk = ?", (batch["id"],)
    ).fetchone()
    if existing_batch_recon is not None:
        raise DomainError(
            409,
            "batch_already_reconciled",
            f"批次 {batch_id} 已完成对账签收（签收单号 {existing_batch_recon['reconciliation_no']}），"
            "一个批次只能成功对账一次",
            {"batch_id": batch_id, "reconciliation_no": existing_batch_recon["reconciliation_no"]},
        )

    # 4) 整单预校验（此刻尚未写入任何行）。
    batch_item_rows = conn.execute(
        "SELECT * FROM transfer_batch_item WHERE batch_pk = ? ORDER BY item_index ASC",
        (batch["id"],),
    ).fetchall()
    batch_items_by_code = {r["sample_code"]: r for r in batch_item_rows}

    conflict_items: list[dict[str, Any]] = []
    invalid_items: list[dict[str, Any]] = []
    seen_samples: dict[str, int] = {}
    seen_events: dict[str, int] = {}
    sample_cache: dict[str, sqlite3.Row] = {}
    evaluations: dict[int, dict[str, Any]] = {}
    # 结构/资格校验通过的收妥项 → (请求项, 批次明细行, 样本行, 有效 transfer 行)
    accepted_received: list[tuple[Any, sqlite3.Row, sqlite3.Row, sqlite3.Row]] = []

    for idx, it in enumerate(data.items):
        # 4.1 清单结构：批内重复 / 外来项
        if it.sample_code in seen_samples:
            invalid_items.append({
                "index": idx, "sample_code": it.sample_code, "event_id": it.event_id,
                "code": "duplicate_sample_in_reconciliation",
                "message": f"对账清单样本编号重复：{it.sample_code}"
                           f"（第 {seen_samples[it.sample_code] + 1} 份与第 {idx + 1} 份）",
            })
            continue
        seen_samples[it.sample_code] = idx

        batch_item = batch_items_by_code.get(it.sample_code)
        if batch_item is None:
            invalid_items.append({
                "index": idx, "sample_code": it.sample_code, "event_id": it.event_id,
                "code": "unknown_sample_in_reconciliation",
                "message": f"样本 {it.sample_code} 不属于批次 {batch_id}（外来项）",
            })
            continue

        sample = conn.execute("SELECT * FROM sample WHERE id = ?", (batch_item["sample_id"],)).fetchone()
        sample_cache[it.sample_code] = sample

        if not it.is_exception:
            # 4.2 收妥项事件号：清单内重复 / 全局已占用（含历史留痕的失效事件号）
            if it.event_id in seen_events:
                invalid_items.append({
                    "index": idx, "sample_code": it.sample_code, "event_id": it.event_id,
                    "code": "duplicate_event_in_reconciliation",
                    "message": f"对账清单事件号重复：{it.event_id}"
                               f"（第 {seen_events[it.event_id] + 1} 份与第 {idx + 1} 份）",
                })
                continue
            seen_events[it.event_id] = idx
            occupied = conn.execute(
                "SELECT 1 FROM custody_event WHERE event_id = ?", (it.event_id,)
            ).fetchone()
            if occupied is not None:
                conflict_items.append({
                    "index": idx, "sample_code": it.sample_code, "event_id": it.event_id,
                    "code": "event_id_occupied",
                    "message": f"收妥事件号 {it.event_id} 已被占用",
                })
                continue

            eff = _validate_received_item(
                conn,
                index=idx,
                item=it,
                batch_item=batch_item,
                sample=sample,
                receiver=data.receiver,
                occurred_iso=occurred_iso,
                evaluations=evaluations,
                conflict_items=conflict_items,
                invalid_items=invalid_items,
            )
            if eff is not None:
                accepted_received.append((it, batch_item, sample, eff))

    # 4.3 缺项：原批次每份样本必须恰好出现一次
    for batch_item in batch_item_rows:
        if batch_item["sample_code"] not in seen_samples:
            invalid_items.append({
                "index": None, "sample_code": batch_item["sample_code"], "event_id": None,
                "code": "missing_sample_in_reconciliation",
                "message": f"对账清单缺少批次内样本 {batch_item['sample_code']}"
                           f"（须覆盖原批次全部 {len(batch_item_rows)} 份）",
            })
    invalid_items.sort(key=lambda x: (x["index"] is None, x["index"] if x["index"] is not None else 0))

    if conflict_items or invalid_items:
        if conflict_items:
            raise DomainError(
                409,
                "reconciliation_rejected",
                f"批次 {batch_id} 对账签收存在冲突项（已核定/事件号占用），整单拒绝且不留记录",
                {"batch_id": batch_id,
                 "reconciliation_no": data.reconciliation_no,
                 "items": conflict_items + invalid_items},
            )
        raise DomainError(
            422,
            "reconciliation_rejected",
            "对账清单不合法或存在不合格收妥项（缺项/重复/外来项/在途状态或签收要素不符），整单拒绝且不留记录",
            {"batch_id": batch_id, "reconciliation_no": data.reconciliation_no, "items": invalid_items},
        )

    # 5) 全部通过：对账头 + 收妥项 receive 事件与各样本新版本 + 对账明细，同一事务提交。
    created_at = utcnow_iso()
    received_count = sum(1 for it in data.items if not it.is_exception)
    exception_count = len(data.items) - received_count
    try:
        hc = conn.execute(
            "INSERT INTO batch_reconciliation"
            " (reconciliation_no, batch_pk, receiver, occurred_at, item_count,"
            "  received_count, exception_count, request_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            (data.reconciliation_no, batch["id"], data.receiver, occurred_iso,
             len(data.items), received_count, exception_count, request_hash, created_at),
        )
    except sqlite3.IntegrityError:
        # 并发兜底：同批次对账唯一键或同单号唯一键竞争落败
        race_no = _get_reconciliation_by_no(conn, data.reconciliation_no)
        if race_no is not None:
            if race_no["request_hash"] == request_hash:
                race_batch = conn.execute(
                    "SELECT batch_id FROM transfer_batch WHERE id = ?", (race_no["batch_pk"],)
                ).fetchone()
                if race_batch["batch_id"] == batch_id:
                    return get_batch_reconciliation(conn, batch_id), True
            raise DomainError(
                409, "reconciliation_conflict",
                f"签收单号 {data.reconciliation_no} 并发写入且内容冲突",
            )
        raise DomainError(
            409, "batch_already_reconciled",
            f"批次 {batch_id} 并发下已被其他对账签收提交，按已提交状态判定",
            {"batch_id": batch_id},
        )
    recon_pk = hc.lastrowid

    # 收妥项先落 receive 事件（与批量移交对称：不挂 batch_id，经对账明细双向追溯）
    item_by_code = {it.sample_code: it for it in data.items}
    for req_item, batch_item, sample, eff in accepted_received:
        eff_payload = json.loads(eff["payload_json"])
        payload = {
            "transfer_no": eff_payload["transfer_no"],
            "receiver": data.receiver,
            "seal_no": req_item.verified_seal_no,
        }
        canonical = {
            "event_id": req_item.event_id,
            "event_type": "receive",
            "sample_code": sample["sample_code"],
            "occurred_at": occurred_iso,
            **payload,
        }
        h = _fingerprint(canonical)
        payload_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        try:
            ec = conn.execute(
                "INSERT INTO custody_event"
                " (sample_id, event_id, event_type, occurred_at, payload_json, content_hash,"
                "  created_at, superseded_by, replaces_event_id, batch_id)"
                " VALUES (?,?,?,?,?,?,?,NULL,NULL,NULL)",
                (sample["id"], req_item.event_id, "receive", occurred_iso,
                 payload_json, h, created_at),
            )
        except sqlite3.IntegrityError:
            raise DomainError(
                409,
                "reconciliation_rejected",
                f"批次 {batch_id} 并发提交冲突：收妥事件号 {req_item.event_id} 已被占用，整单失败",
                {"batch_id": batch_id, "reconciliation_no": data.reconciliation_no,
                 "items": [{"index": seen_samples[req_item.sample_code],
                            "sample_code": req_item.sample_code,
                            "event_id": req_item.event_id, "code": "event_id_occupied"}]},
            )
        event_row_id = ec.lastrowid

        # 收妥项作为既有 receive 事件真正落库 → 该样本版本递增一次；与对账记录同一事务，
        # 任一后续写入失败整体回滚（异常项不产生版本）。
        fresh_sample = conn.execute("SELECT * FROM sample WHERE id = ?", (sample["id"],)).fetchone()
        record_version(conn, fresh_sample, CHANGE_EVENT, req_item.event_id)

    # 对账明细：按原批次顺序逐份落库（收妥/异常留痕）
    for batch_item in batch_item_rows:
        req_item = item_by_code[batch_item["sample_code"]]
        sample = sample_cache[req_item.sample_code]
        if req_item.is_exception:
            conn.execute(
                "INSERT INTO batch_reconciliation_item"
                " (reconciliation_pk, batch_item_pk, item_index, sample_id, sample_code,"
                "  transfer_no, outcome, event_id, event_row_id, verified_seal_no, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (recon_pk, batch_item["id"], batch_item["item_index"], sample["id"],
                 batch_item["sample_code"], batch_item["transfer_no"], "exception",
                 None, None, None, req_item.reason),
            )
        else:
            event_row = conn.execute(
                "SELECT id FROM custody_event WHERE event_id = ?", (req_item.event_id,)
            ).fetchone()
            conn.execute(
                "INSERT INTO batch_reconciliation_item"
                " (reconciliation_pk, batch_item_pk, item_index, sample_id, sample_code,"
                "  transfer_no, outcome, event_id, event_row_id, verified_seal_no, reason)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (recon_pk, batch_item["id"], batch_item["item_index"], sample["id"],
                 batch_item["sample_code"], batch_item["transfer_no"], "received",
                 req_item.event_id, event_row["id"], req_item.verified_seal_no, None),
            )

    return get_batch_reconciliation(conn, batch_id), False


def _reconciliation_item_view(
    conn: sqlite3.Connection, row: sqlite3.Row
) -> dict[str, Any]:
    sample = conn.execute("SELECT * FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
    view: dict[str, Any] = {
        "index": row["item_index"],
        "sample_code": row["sample_code"],
        "transfer_no": row["transfer_no"],
        "outcome": row["outcome"],
        "event_id": row["event_id"],
        "verified_seal_no": row["verified_seal_no"],
        "reason": row["reason"],
        "evaluation": _evaluation_with_version(conn, sample),
    }
    if row["outcome"] == "received" and row["event_row_id"] is not None:
        evt = conn.execute(
            "SELECT superseded_by FROM custody_event WHERE id = ?", (row["event_row_id"],)
        ).fetchone()
        view["event_effective"] = evt["superseded_by"] is None
        view["superseded_by"] = evt["superseded_by"]
        view["current_event_id"] = _effective_event_id(conn, row["event_row_id"])
    else:
        # 异常项不生成签收事件：现时有效性字段为 null
        view["event_effective"] = None
        view["superseded_by"] = None
        view["current_event_id"] = None
    return view


def get_batch_reconciliation(conn: sqlite3.Connection, batch_id: str) -> dict[str, Any]:
    """批次对账查询：当时的收妥/异常明细（原样留痕）+ 各份当前核验结论 +
    各收妥事件纠错后的现时有效性。原对账记录永不被查询改写。"""
    batch = _get_batch(conn, batch_id)
    recon = conn.execute(
        "SELECT * FROM batch_reconciliation WHERE batch_pk = ?", (batch["id"],)
    ).fetchone()
    if recon is None:
        raise DomainError(
            404, "reconciliation_not_found",
            f"批次 {batch_id} 尚无成功对账签收记录",
            {"batch_id": batch_id},
        )

    rows = conn.execute(
        "SELECT * FROM batch_reconciliation_item WHERE reconciliation_pk = ? ORDER BY item_index ASC",
        (recon["id"],),
    ).fetchall()
    return {
        "reconciliation_no": recon["reconciliation_no"],
        "batch_id": batch_id,
        "receiver": recon["receiver"],
        "occurred_at": recon["occurred_at"],
        "item_count": recon["item_count"],
        "received_count": recon["received_count"],
        "exception_count": recon["exception_count"],
        "created_at": recon["created_at"],
        "items": [_reconciliation_item_view(conn, r) for r in rows],
    }


def batch_reconciliation_summary(conn: sqlite3.Connection, batch_pk: int) -> dict[str, Any] | None:
    """批次查询中的对账摘要（无对账为 None）；明细走对账查询端点。"""
    recon = conn.execute(
        "SELECT * FROM batch_reconciliation WHERE batch_pk = ?", (batch_pk,)
    ).fetchone()
    if recon is None:
        return None
    return {
        "reconciliation_no": recon["reconciliation_no"],
        "receiver": recon["receiver"],
        "occurred_at": recon["occurred_at"],
        "received_count": recon["received_count"],
        "exception_count": recon["exception_count"],
        "created_at": recon["created_at"],
    }
