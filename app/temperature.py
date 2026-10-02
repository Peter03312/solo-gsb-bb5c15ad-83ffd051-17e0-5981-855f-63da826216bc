"""运输测温证据复核（未核定样本）。

规则（与 README 一致）：
  * 每份未核定样本一次性设定允许摄氏温度闭区间与正数最大测温间隔（秒），设定后不可改：
    同内容重试幂等（200）、异内容冲突（409 temp_config_conflict）；核定入库后拒绝新配置；
  * 读数按全局唯一读数号记录设备号、发生时间与有限数值：采集前（422）、重复发生时刻
    （409）或同号异内容（409 reading_conflict）拒绝；同号同内容重试返回原记录（200）；
    核定后拒绝新读数；
  * 查询按发生时间列出超温点（读数落在允许闭区间之外）与相邻读数间超过最大测温间隔的
    缺测区间；跨移交/签收时刻的缺测段按当前有效链的移交、签收事件发生时刻拆分；
  * 每段依当前有效保管链标明当时保管人（custodian）或在途移交（in_transit）；
    保管链断链（移交人非当前保管人、在途期间再次移交、签收无对应在途移交）导致归属
    不能确定时，自该事件发生时刻起标为待核（pending_review）且其后不再恢复；
    重复签收、签收要素不符（在途保持）、入库异常与封签检查不改变保管归属，不触发待核；
  * 乱序补录与移交/签收纠错后，查询即时按当前有效链重算归属；原始读数与旧纠错记录不改；
  * 测温配置与读数不参与保管链核验结论、不产生保管链版本（既有结论与版本规则保持兼容）。
"""
import json
import sqlite3
from datetime import datetime
from typing import Any

from .db import utcnow_iso
from .schemas import TempConfigIn, TempReadingIn
from .service import (
    DomainError,
    LAB_CUSTODIAN,
    _fingerprint,
    ensure_unlocked,
    get_sample_or_404,
    to_utc_iso,
)


# --------------------------------------------------------------------------- 通用工具

def _parse_iso(s: str) -> datetime:
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def _seconds_between(start: str, end: str) -> int:
    return int((_parse_iso(end) - _parse_iso(start)).total_seconds())


def _get_config_row(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM sample_temp_config WHERE sample_id = ?", (sample_id,)
    ).fetchone()


# --------------------------------------------------------------------------- 测温配置（一次性设定）

def _config_fingerprint(data: TempConfigIn) -> str:
    return _fingerprint({
        "sample_code": data.sample_code,
        "min_celsius": data.min_celsius,
        "max_celsius": data.max_celsius,
        "max_interval_seconds": data.max_interval_seconds,
    })


def _config_view(row: sqlite3.Row, sample_code: str) -> dict[str, Any]:
    return {
        "sample_code": sample_code,
        "min_celsius": row["min_celsius"],
        "max_celsius": row["max_celsius"],
        "max_interval_seconds": row["max_interval_seconds"],
        "configured_at": row["created_at"],
    }


def set_temp_config(conn: sqlite3.Connection, data: TempConfigIn) -> tuple[dict[str, Any], bool]:
    """一次性设定测温配置。返回 (配置视图, 是否幂等重试)。

    每份样本至多一条配置：同内容重试幂等（200，核定后亦保持），异内容冲突（409）；
    新配置仅未核定样本可设（核定后 409 history_locked）。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    h = _config_fingerprint(data)

    # 幂等查重优先于冻结检查（与既有事件接口语义一致）
    existing = _get_config_row(conn, sample["id"])
    if existing is not None:
        if existing["request_hash"] != h:
            raise DomainError(
                409,
                "temp_config_conflict",
                f"样本 {data.sample_code} 已设定测温配置且内容不一致"
                "（一次性设定，不可更改；同内容重试幂等）",
                {"sample_code": data.sample_code,
                 "existing_request_hash": existing["request_hash"],
                 "incoming_request_hash": h},
            )
        return _config_view(existing, sample["sample_code"]), True

    ensure_unlocked(sample)
    cur = conn.execute(
        "INSERT INTO sample_temp_config"
        " (sample_id, min_celsius, max_celsius, max_interval_seconds, request_hash, created_at)"
        " VALUES (?,?,?,?,?,?)",
        (sample["id"], data.min_celsius, data.max_celsius,
         data.max_interval_seconds, h, utcnow_iso()),
    )
    row = conn.execute("SELECT * FROM sample_temp_config WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _config_view(row, sample["sample_code"]), False


# --------------------------------------------------------------------------- 测温读数

def _reading_fingerprint(data: TempReadingIn, occurred: str) -> str:
    return _fingerprint({
        "sample_code": data.sample_code,
        "reading_id": data.reading_id,
        "device_id": data.device_id,
        "occurred_at": occurred,
        "value_celsius": data.value_celsius,
    })


def _reading_view(row: sqlite3.Row, sample_code: str) -> dict[str, Any]:
    return {
        "reading_id": row["reading_id"],
        "sample_code": sample_code,
        "device_id": row["device_id"],
        "occurred_at": row["occurred_at"],
        "value_celsius": row["value_celsius"],
        "recorded_at": row["created_at"],
    }


def create_temp_reading(conn: sqlite3.Connection, data: TempReadingIn) -> tuple[dict[str, Any], bool]:
    """记录一条测温读数。返回 (读数视图, 是否幂等重试)。

    同读数号同内容重试返回原记录（200，核定后亦保持）；同号异内容 409；
    采集前（422）、同一样本重复发生时刻（409）、未设定测温配置（409）、
    样本已核定（409 history_locked）均拒绝且不写入。
    """
    sample = get_sample_or_404(conn, data.sample_code)
    occurred = to_utc_iso(data.occurred_at)
    h = _reading_fingerprint(data, occurred)

    # 幂等查重优先于冻结检查（与既有事件接口语义一致）：同号同内容重试返回原记录
    existing = conn.execute(
        "SELECT * FROM temp_reading WHERE reading_id = ?", (data.reading_id,)
    ).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "reading_conflict",
                f"读数号 {data.reading_id} 已存在但提交内容不一致（同读数号异内容冲突）",
                {"reading_id": data.reading_id,
                 "existing": {"occurred_at": existing["occurred_at"],
                              "device_id": existing["device_id"],
                              "value_celsius": existing["value_celsius"],
                              "content_hash": existing["content_hash"]},
                 "incoming": {"occurred_at": occurred, "device_id": data.device_id,
                              "value_celsius": data.value_celsius, "content_hash": h}},
            )
        orig_sample = conn.execute(
            "SELECT sample_code FROM sample WHERE id = ?", (existing["sample_id"],)
        ).fetchone()
        return _reading_view(existing, orig_sample["sample_code"]), True

    ensure_unlocked(sample)

    if _get_config_row(conn, sample["id"]) is None:
        raise DomainError(
            409,
            "temp_config_missing",
            f"样本 {data.sample_code} 尚未设定测温配置，须先一次性设定允许温度区间与最大测温间隔",
            {"sample_code": data.sample_code},
        )
    if occurred < sample["collected_at"]:
        raise DomainError(
            422,
            "reading_before_collection",
            f"读数发生时间 {occurred} 早于样本采集时间 {sample['collected_at']}，拒绝记录",
            {"reading_id": data.reading_id, "occurred_at": occurred,
             "collected_at": sample["collected_at"]},
        )
    dup = conn.execute(
        "SELECT reading_id FROM temp_reading WHERE sample_id = ? AND occurred_at = ?",
        (sample["id"], occurred),
    ).fetchone()
    if dup is not None:
        raise DomainError(
            409,
            "reading_time_duplicate",
            f"样本 {data.sample_code} 在时刻 {occurred} 已存在读数 {dup['reading_id']}，"
            "同一样本同一发生时刻只允许一条读数",
            {"reading_id": data.reading_id, "occurred_at": occurred,
             "existing_reading_id": dup["reading_id"]},
        )

    try:
        cur = conn.execute(
            "INSERT INTO temp_reading"
            " (sample_id, reading_id, device_id, occurred_at, value_celsius, content_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (sample["id"], data.reading_id, data.device_id, occurred,
             data.value_celsius, h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        # 并发下唯一键竞争（读数号或发生时刻被抢先占用）：按已提交结果判定
        raced = conn.execute(
            "SELECT * FROM temp_reading WHERE reading_id = ?", (data.reading_id,)
        ).fetchone()
        if raced is not None:
            if raced["content_hash"] == h:
                orig_sample = conn.execute(
                    "SELECT sample_code FROM sample WHERE id = ?", (raced["sample_id"],)
                ).fetchone()
                return _reading_view(raced, orig_sample["sample_code"]), True
            raise DomainError(
                409, "reading_conflict",
                f"读数号 {data.reading_id} 并发写入且内容冲突",
            )
        raise DomainError(
            409, "reading_time_duplicate",
            f"样本 {data.sample_code} 在时刻 {occurred} 的读数被并发提交占用",
            {"reading_id": data.reading_id, "occurred_at": occurred},
        )

    row = conn.execute("SELECT * FROM temp_reading WHERE id = ?", (cur.lastrowid,)).fetchone()
    return _reading_view(row, sample["sample_code"]), False


# --------------------------------------------------------------------------- 保管归属重放

def _custody_periods(sample: sqlite3.Row, events: list[sqlite3.Row]) -> list[dict[str, Any]]:
    """沿当前有效链重放移交/签收/入库事件，产出覆盖 [采集时间, +∞) 的连续归属时段。

    每段 {"start": ISO, "end": ISO|None, "attribution": {...}}；attribution 形态：
      * {"kind": "custodian", "custodian": 保管人}
      * {"kind": "in_transit", "transfer_no", "from_custodian", "to_custodian", "transfer_event_id"}
      * {"kind": "pending_review", "detail": 断链原因}（待核：断链后归属不能确定）
    断链（移交人非当前保管人 / 在途期间再次移交 / 签收无对应在途移交）自该事件发生
    时刻起生效且其后不再恢复；重复签收、签收要素不符、入库异常与封签检查不改变归属。
    """
    periods: list[dict[str, Any]] = []
    current = sample["collector"]
    open_transfer: dict[str, Any] | None = None     # 在途移交（内部含 occurred_at）
    closed_nos: set[str] = set()
    last_receive_at: str | None = None
    broken_detail: str | None = None
    cursor = sample["collected_at"]

    def attribution() -> dict[str, Any]:
        if broken_detail is not None:
            return {"kind": "pending_review", "detail": broken_detail}
        if open_transfer is not None:
            return {
                "kind": "in_transit",
                "transfer_no": open_transfer["transfer_no"],
                "from_custodian": open_transfer["from_custodian"],
                "to_custodian": open_transfer["to_custodian"],
                "transfer_event_id": open_transfer["transfer_event_id"],
            }
        return {"kind": "custodian", "custodian": current}

    for ev in events:
        et = ev["event_type"]
        if et == "seal_check":
            continue                                    # 封签检查不影响保管归属
        t = ev["occurred_at"]
        p = json.loads(ev["payload_json"])
        ref = f"[事件 {ev['event_id']}]"
        if t < sample["collected_at"]:
            continue                                    # 采集前事件无效，保管归属尚未开始
        # 先以事件发生前状态关闭 [cursor, t) 时段，再应用该事件
        if t > cursor:
            periods.append({"start": cursor, "end": t, "attribution": attribution()})
            cursor = t

        if et == "transfer":
            if open_transfer is not None:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 移交至签收期间再次移交：在途移交单 {open_transfer['transfer_no']}"
                        f"（{open_transfer['from_custodian']} → {open_transfer['to_custodian']}）尚未签收，"
                        "保管链断链，归属不能确定"
                    )
            elif p["from_custodian"] != current:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 移交人 {p['from_custodian']} 不是当前保管人 {current}"
                        f"（移交单 {p['transfer_no']}），保管链断链，归属不能确定"
                    )
            else:
                open_transfer = {
                    "transfer_no": p["transfer_no"],
                    "from_custodian": p["from_custodian"],
                    "to_custodian": p["to_custodian"],
                    "transfer_event_id": ev["event_id"],
                    "occurred_at": t,
                }
        elif et == "receive":
            no = p["transfer_no"]
            if no in closed_nos:
                pass                                    # 重复签收：无效但不改变已闭合的保管状态
            elif open_transfer is None or open_transfer["transfer_no"] != no:
                if broken_detail is None:
                    broken_detail = (
                        f"{ref} 签收无对应在途移交（单号 {no}），保管链断链，归属不能确定"
                    )
            elif (p["receiver"] != open_transfer["to_custodian"]
                  or p["seal_no"] != sample["seal_no"]
                  or t <= open_transfer["occurred_at"]):
                pass                                    # 签收要素不符：在途保持，归属仍可确定
            else:
                closed_nos.add(no)
                current = p["receiver"]
                last_receive_at = t
                open_transfer = None
        else:  # storage
            if last_receive_at is not None and t < last_receive_at:
                pass                                    # 入库早于最终签收：无效，保管状态不变
            elif open_transfer is not None or last_receive_at is None:
                pass                                    # 缺前序链的入库：待补证，保管状态不变
            else:
                current = p.get("custodian") or LAB_CUSTODIAN

    periods.append({"start": cursor, "end": None, "attribution": attribution()})
    return periods


def _attribution_at(periods: list[dict[str, Any]], t: str) -> dict[str, Any]:
    """时刻 t 的保管归属；时刻与事件对齐时按事件已发生处理。"""
    for period in periods:
        if period["start"] <= t and (period["end"] is None or t < period["end"]):
            return period["attribution"]
    return {"kind": "pending_review", "detail": "时刻早于样本采集时间，归属不能确定"}


# --------------------------------------------------------------------------- 测温证据复核查询

def get_temperature_review(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按发生时间列出超温点与缺测区间（跨移交/签收时刻拆分），并标明每段归属。

    归属按查询时的当前有效保管链即时重算：乱序补录读数、补录/纠错移交签收事件后
    再次查询即反映最新归属；原始读数与旧纠错记录不被改写。
    """
    sample = get_sample_or_404(conn, sample_code)
    config = _get_config_row(conn, sample["id"])
    if config is None:
        raise DomainError(
            404, "temp_config_not_found",
            f"样本 {sample_code} 尚未设定测温配置",
            {"sample_code": sample_code},
        )

    readings = conn.execute(
        "SELECT * FROM temp_reading WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    events = conn.execute(
        "SELECT * FROM custody_event WHERE sample_id = ? AND superseded_by IS NULL"
        " ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    periods = _custody_periods(sample, events)

    min_c = config["min_celsius"]
    max_c = config["max_celsius"]
    max_interval = config["max_interval_seconds"]

    reading_views: list[dict[str, Any]] = []
    out_of_range: list[dict[str, Any]] = []
    for r in readings:
        value = r["value_celsius"]
        in_range = min_c <= value <= max_c
        reading_views.append({
            "reading_id": r["reading_id"],
            "device_id": r["device_id"],
            "occurred_at": r["occurred_at"],
            "value_celsius": value,
            "in_range": in_range,
            "recorded_at": r["created_at"],
        })
        if not in_range:
            below = value < min_c
            out_of_range.append({
                "reading_id": r["reading_id"],
                "device_id": r["device_id"],
                "occurred_at": r["occurred_at"],
                "value_celsius": value,
                "direction": "below_min" if below else "above_max",
                "limit_celsius": min_c if below else max_c,
                "attribution": _attribution_at(periods, r["occurred_at"]),
            })

    # 缺测区间：相邻读数间隔超过最大测温间隔；跨当前有效链移交/签收时刻的段须拆分
    split_times = sorted({
        ev["occurred_at"] for ev in events if ev["event_type"] in ("transfer", "receive")
    })
    gaps: list[dict[str, Any]] = []
    for prev, nxt in zip(readings, readings[1:]):
        duration = _seconds_between(prev["occurred_at"], nxt["occurred_at"])
        if duration <= max_interval:
            continue
        bounds = [prev["occurred_at"]]
        bounds += [t for t in split_times if prev["occurred_at"] < t < nxt["occurred_at"]]
        bounds.append(nxt["occurred_at"])
        segments = [
            {
                "start": a,
                "end": b,
                "duration_seconds": _seconds_between(a, b),
                "attribution": _attribution_at(periods, a),
            }
            for a, b in zip(bounds, bounds[1:])
        ]
        gaps.append({
            "start": prev["occurred_at"],
            "end": nxt["occurred_at"],
            "duration_seconds": duration,
            "max_interval_seconds": max_interval,
            "before_reading_id": prev["reading_id"],
            "after_reading_id": nxt["reading_id"],
            "segments": segments,
        })

    return {
        "sample_code": sample["sample_code"],
        "config": _config_view(config, sample["sample_code"]),
        "reading_count": len(reading_views),
        "readings": reading_views,
        "out_of_range_points": out_of_range,
        "gap_intervals": gaps,
    }
