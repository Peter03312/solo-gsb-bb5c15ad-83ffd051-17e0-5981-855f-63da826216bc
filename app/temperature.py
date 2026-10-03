"""运输测温复核：温度配置、读数上报、读数纠错（作废/替换）与超温/缺测定点。

规则（与 README 一致）：
  * 每份未核定样本一次性设定允许摄氏温度闭区间 [min, max] 与正数最大测温间隔；
    同内容重试幂等，异内容冲突；核定后拒绝新配置。
  * 读数按全局唯一读数号记录设备号、发生时间与有限摄氏数值：
    采集前、同号异内容拒绝；同号同内容重试返回原记录；读数可乱序补录，
    查询按发生时间重算；核定后拒绝新读数。
  * 读数纠错仅核定前可提交（作废/替换，永久留痕，原始读数与旧纠错记录不改），
    乱序补录与移交、签收纠错后按当前有效保管链与有效读数重算归属。
  * 查询按发生时间列出超温点，以及相邻有效读数间超过限值的缺测区间；
    缺测区间跨移交、签收时刻的须拆分；每段依当前有效保管链标明当时保管人
    （custody）/在途移交（in_transit）；断链导致归属不能确定时标为待核（pending_verify）。

保管链归属重算与 service.evaluate 采用同一套事件有效性与状态迁移规则（仅纳入
当前有效 custody_event，按 occurred_at 重放），独立实现是为了在不改动既有核验
结论与版本规则的前提下输出带时间边界的归属时间线。
"""
import json
import sqlite3
from bisect import bisect_right
from datetime import datetime, timezone
from typing import Any

from .db import utcnow_iso
from .schemas import (
    TemperatureConfigIn,
    TemperatureCorrectionIn,
    TemperatureReadingIn,
)
from .service import (
    DomainError,
    _fingerprint,
    get_sample_or_404,
    to_utc_iso,
)

# 归属状态类型
CUSTODY = "custody"                 # 当时有明确保管人
IN_TRANSIT = "in_transit"           # 在途移交（已移交未签收）
PENDING_VERIFY = "pending_verify"   # 保管链断裂，归属不能确定 → 待核


# --------------------------------------------------------------------------- 工具

def _parse_ts(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(timezone.utc)


def _seconds_between(start_iso: str, end_iso: str) -> int:
    return int((_parse_ts(end_iso) - _parse_ts(start_iso)).total_seconds())


def _custody_state_custody(custodian: str) -> dict[str, Any]:
    return {"attribution": CUSTODY, "custodian": custodian}


def _custody_state_transit(open_transfer: dict[str, Any]) -> dict[str, Any]:
    return {
        "attribution": IN_TRANSIT,
        "transfer_no": open_transfer["transfer_no"],
        "event_id": open_transfer["event_id"],
        "from_custodian": open_transfer["from_custodian"],
        "to_custodian": open_transfer["to_custodian"],
    }


def _custody_state_pending(reason: str) -> dict[str, Any]:
    return {"attribution": PENDING_VERIFY, "reason": reason}


# --------------------------------------------------------------------------- 有效保管链归属时间线

def custody_boundaries(conn: sqlite3.Connection, sample: sqlite3.Row) -> list[dict[str, Any]]:
    """重放当前有效保管事件，输出归属状态变更边界（按时间升序、同时刻合并为最终状态）。

    状态机与 service.evaluate 完全一致：仅纳入 superseded_by IS NULL 的事件，
    按 (occurred_at, id) 重放；早于采集的事件不产生任何归属变更。
    返回 [{"at": UTC ISO, "state": {...}}]；初始状态（采集时起）为采集人保管。
    """
    events = [
        e for e in conn.execute(
            "SELECT * FROM custody_event WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
            (sample["id"],),
        ).fetchall()
        if e["superseded_by"] is None
    ]
    # 与 evaluate 相同的预索引：签收早于其移交时按"时间不合法"而非"断链"处理
    transfer_by_no: dict[str, dict[str, Any]] = {}
    for ev0 in events:
        if ev0["event_type"] == "transfer":
            transfer_by_no[json.loads(ev0["payload_json"])["transfer_no"]] = ev0

    current = sample["collector"]
    open_transfer: dict[str, Any] | None = None
    closed_transfer_nos: set[str] = set()
    last_receive_at: str | None = None
    boundaries: list[dict[str, Any]] = []

    def set_boundary(at: str, state: dict[str, Any]) -> None:
        if boundaries and boundaries[-1]["at"] == at:
            boundaries[-1]["state"] = state          # 同时刻多事件：以重放最终状态为准
        else:
            boundaries.append({"at": at, "state": state})

    for ev in events:
        # 采集前事件直接无效，不改变归属
        if ev["occurred_at"] < sample["collected_at"]:
            continue
        p = json.loads(ev["payload_json"])
        et = ev["event_type"]

        if et == "transfer":
            if open_transfer is not None:
                continue                                # 在途再移交：无效，在途状态不变
            if p["from_custodian"] != current:
                continue                                # 非保管人移交：断链无效，现状态不变
            open_transfer = {
                "transfer_no": p["transfer_no"],
                "event_id": ev["event_id"],
                "from_custodian": p["from_custodian"],
                "to_custodian": p["to_custodian"],
                "occurred_at": ev["occurred_at"],
            }
            set_boundary(ev["occurred_at"], _custody_state_transit(open_transfer))

        elif et == "receive":
            no = p["transfer_no"]
            if no in closed_transfer_nos:
                continue                                # 重复签收：无效
            if open_transfer is None or open_transfer["transfer_no"] != no:
                known = transfer_by_no.get(no)
                if known is not None and ev["occurred_at"] <= known["occurred_at"]:
                    continue                            # 签收早于/同时于移交：时间不合法，不改状态
                # 签收无对应在途移交：保管人断链，自此时刻起归属不能确定 → 待核
                set_boundary(
                    ev["occurred_at"],
                    _custody_state_pending(
                        f"签收无对应在途移交：单号 {no}（事件 {ev['event_id']}），保管人断链，归属待核"
                    ),
                )
                continue

            mismatch = (
                p["receiver"] != open_transfer["to_custodian"]
                or p["seal_no"] != sample["seal_no"]
                or ev["occurred_at"] <= open_transfer["occurred_at"]
            )
            if mismatch:
                continue                                # 在途移交保留，归属仍为在途
            closed_transfer_nos.add(no)
            current = p["receiver"]
            last_receive_at = ev["occurred_at"]
            open_transfer = None
            set_boundary(ev["occurred_at"], _custody_state_custody(current))

        elif et == "seal_check":
            pass                                        # 封签检查不改变保管归属

        else:  # storage
            if last_receive_at is not None and ev["occurred_at"] < last_receive_at:
                continue
            if open_transfer is not None or last_receive_at is None:
                continue                                # 缺前序链的入库不成立，归属不变
            current = p.get("custodian") or "实验室"
            set_boundary(ev["occurred_at"], _custody_state_custody(current))

    return boundaries


class CustodyAttribution:
    """按归属边界对任意时刻/区间求当时保管状态。"""

    def __init__(self, conn: sqlite3.Connection, sample: sqlite3.Row):
        self.sample = sample
        self.boundaries = custody_boundaries(conn, sample)
        self._times = [b["at"] for b in self.boundaries]
        self.initial_state = _custody_state_custody(sample["collector"])

    def state_at(self, at_iso: str) -> dict[str, Any]:
        """时刻 at_iso 的归属状态（该时刻事件按生效后状态计，与核验时间线一致）。"""
        idx = bisect_right(self._times, at_iso) - 1
        if idx < 0:
            return dict(self.initial_state)
        return dict(self.boundaries[idx]["state"])

    def split_interval(self, start_iso: str, end_iso: str) -> list[dict[str, Any]]:
        """把 [start, end] 按严格位于内部的移交/签收边界拆分为归属不变的分段。

        端点时刻的事件分别归入其相邻段（端点按生效后状态计）；同时刻边界已在
        边界构造时合并，故不会产生零长度分段。
        """
        cuts = [at for at in self._times if start_iso < at < end_iso]
        points = [start_iso, *cuts, end_iso]
        segments: list[dict[str, Any]] = []
        for i in range(len(points) - 1):
            seg_start, seg_end = points[i], points[i + 1]
            if seg_start == seg_end:
                continue
            segments.append({
                "start_at": seg_start,
                "end_at": seg_end,
                "duration_seconds": _seconds_between(seg_start, seg_end),
                "custody": self.state_at(seg_start),
            })
        return segments

    def display_timeline(self) -> list[dict[str, Any]]:
        """对外展示的归属时间线：从采集时起，相邻同状态合并，末段开放（end_at=null）。"""
        raw: list[dict[str, Any]] = [
            {"start_at": self.sample["collected_at"], "end_at": None, "custody": dict(self.initial_state)}
        ]
        for b in self.boundaries:
            if raw[-1]["custody"] == b["state"]:
                continue
            raw[-1]["end_at"] = b["at"]
            raw.append({"start_at": b["at"], "end_at": None, "custody": dict(b["state"])})
        return raw


# --------------------------------------------------------------------------- 温度配置（一次性）

def _config_canonical(data: TemperatureConfigIn) -> dict[str, Any]:
    return {
        "min_celsius": float(data.min_celsius),
        "max_celsius": float(data.max_celsius),
        "max_interval_seconds": int(data.max_interval_seconds),
    }


def get_config_row(conn: sqlite3.Connection, sample_id: int) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM temperature_config WHERE sample_id = ?", (sample_id,)
    ).fetchone()


def config_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "sample_code": row["sample_code"],
        "min_celsius": row["min_celsius"],
        "max_celsius": row["max_celsius"],
        "max_interval_seconds": row["max_interval_seconds"],
        "created_at": row["created_at"],
    }


def set_temperature_config(
    conn: sqlite3.Connection, sample_code: str, data: TemperatureConfigIn
) -> tuple[dict[str, Any], bool]:
    """一次性设定温度配置。返回 (配置视图, 是否幂等重试)。"""
    sample = get_sample_or_404(conn, sample_code)
    canonical = _config_canonical(data)
    h = _fingerprint(canonical)

    existing = get_config_row(conn, sample["id"])
    if existing is not None:
        if existing["request_hash"] != h:
            raise DomainError(
                409,
                "temp_config_conflict",
                f"样本 {sample_code} 的测温配置已一次性设定，与本次提交内容不一致（不得修改）",
                {"sample_code": sample_code,
                 "existing": config_to_dict(existing),
                 "incoming": canonical},
            )
        return {"config": config_to_dict(existing)}, True

    # 新配置：核定后拒绝（同内容重试在上方幂等返回）
    if sample["approved_storage_at"]:
        raise DomainError(
            409,
            "history_locked",
            f"样本 {sample_code} 已核定入库（{sample['approved_storage_at']}），拒绝新测温配置",
        )

    conn.execute(
        "INSERT INTO temperature_config"
        " (sample_id, sample_code, min_celsius, max_celsius, max_interval_seconds, request_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?)",
        (sample["id"], sample_code, canonical["min_celsius"], canonical["max_celsius"],
         canonical["max_interval_seconds"], h, utcnow_iso()),
    )
    row = get_config_row(conn, sample["id"])
    return {"config": config_to_dict(row)}, False


# --------------------------------------------------------------------------- 读数上报

def _reading_canonical(
    *, reading_id: str, device_id: str, occurred_iso: str, celsius: float, sample_code: str
) -> dict[str, Any]:
    return {
        "reading_id": reading_id,
        "sample_code": sample_code,
        "device_id": device_id,
        "occurred_at": occurred_iso,
        "celsius": float(celsius),
    }


def add_reading(
    conn: sqlite3.Connection, sample_code: str, data: TemperatureReadingIn
) -> tuple[dict[str, Any], bool]:
    """记录一条测温读数。返回 (读数视图, 是否幂等重试)。"""
    sample = get_sample_or_404(conn, sample_code)
    occurred = to_utc_iso(data.occurred_at)
    canonical = _reading_canonical(
        reading_id=data.reading_id, device_id=data.device_id,
        occurred_iso=occurred, celsius=float(data.celsius), sample_code=sample_code,
    )
    h = _fingerprint(canonical)

    # 同读数号：同内容幂等返回原记录 / 异内容拒绝（优先于一切，核定后亦兼容）
    existing = conn.execute(
        "SELECT * FROM temperature_reading WHERE reading_id = ?", (data.reading_id,)
    ).fetchone()
    if existing is not None:
        if existing["content_hash"] != h:
            raise DomainError(
                409,
                "temp_reading_conflict",
                f"读数号 {data.reading_id} 已存在但提交内容不一致（同号异内容拒绝）",
                {"reading_id": data.reading_id,
                 "existing_content_hash": existing["content_hash"],
                 "incoming_content_hash": h},
            )
        return {"reading": _reading_view(conn, existing, sample_code)}, True

    # 核定后拒绝新读数
    if sample["approved_storage_at"]:
        raise DomainError(
            409,
            "history_locked",
            f"样本 {sample_code} 已核定入库（{sample['approved_storage_at']}），拒绝新测温读数",
        )
    # 采集前读数拒绝
    if occurred < sample["collected_at"]:
        raise DomainError(
            422,
            "reading_before_collection",
            f"读数发生时间 {occurred} 早于样本采集时间 {sample['collected_at']}，采集前读数拒绝",
            {"reading_id": data.reading_id, "occurred_at": occurred,
             "collected_at": sample["collected_at"]},
        )
    # 须先一次性设定测温配置
    if get_config_row(conn, sample["id"]) is None:
        raise DomainError(
            422,
            "temp_config_required",
            f"样本 {sample_code} 尚未设定测温配置（允许温度区间与最大测温间隔），请先配置再记录读数",
            {"sample_code": sample_code},
        )

    try:
        cur = conn.execute(
            "INSERT INTO temperature_reading"
            " (sample_id, reading_id, device_id, occurred_at, celsius, content_hash,"
            "  created_at, superseded_by, replaces_reading_id)"
            " VALUES (?,?,?,?,?,?,?,NULL,NULL)",
            (sample["id"], data.reading_id, data.device_id, occurred,
             float(data.celsius), h, utcnow_iso()),
        )
    except sqlite3.IntegrityError:
        existing = conn.execute(
            "SELECT * FROM temperature_reading WHERE reading_id = ?", (data.reading_id,)
        ).fetchone()
        if existing is not None and existing["content_hash"] == h:
            return {"reading": _reading_view(conn, existing, sample_code)}, True
        raise DomainError(409, "temp_reading_conflict",
                          f"读数号 {data.reading_id} 并发写入且内容冲突")
    row = conn.execute("SELECT * FROM temperature_reading WHERE id = ?", (cur.lastrowid,)).fetchone()
    return {"reading": _reading_view(conn, row, sample_code)}, False


def _reading_view(conn: sqlite3.Connection, row: sqlite3.Row, sample_code: str) -> dict[str, Any]:
    sample = conn.execute("SELECT sample_code FROM sample WHERE id = ?", (row["sample_id"],)).fetchone()
    code = sample_code or (sample["sample_code"] if sample else None)
    return {
        "reading_id": row["reading_id"],
        "sample_code": code,
        "device_id": row["device_id"],
        "occurred_at": row["occurred_at"],
        "celsius": row["celsius"],
        "recorded_at": row["created_at"],
        "effective": row["superseded_by"] is None,
        "superseded_by": row["superseded_by"],
        "replaces_reading_id": row["replaces_reading_id"],
    }


def list_readings(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """样本全部读数（含作废留痕），按发生时间、录入次序排列。"""
    sample = get_sample_or_404(conn, sample_code)
    rows = conn.execute(
        "SELECT * FROM temperature_reading WHERE sample_id = ? ORDER BY occurred_at ASC, id ASC",
        (sample["id"],),
    ).fetchall()
    items = [_reading_view(conn, r, sample_code) for r in rows]
    return {
        "sample_code": sample_code,
        "total": len(items),
        "effective": sum(1 for it in items if it["effective"]),
        "items": items,
    }


# --------------------------------------------------------------------------- 读数纠错（作废/替换）

def _temp_correction_fingerprint(
    data: TemperatureCorrectionIn, new_canonical: dict[str, Any] | None
) -> str:
    obj = {
        "correction_id": data.correction_id,
        "sample_code": data.sample_code,
        "target_reading_id": data.target_reading_id,
        "action": data.action,
        "operator": data.operator,
        "reason": data.reason,
    }
    if new_canonical is not None:
        obj["new_reading"] = new_canonical
    return _fingerprint(obj)


def temp_correction_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "correction_id": row["correction_id"],
        "sample_id": row["sample_id"],
        "target_reading_id": row["target_reading_id"],
        "action": row["action"],
        "operator": row["operator"],
        "reason": row["reason"],
        "new_reading_id": row["new_reading_id"],
        "created_at": row["created_at"],
    }


def submit_temperature_correction(
    conn: sqlite3.Connection, data: TemperatureCorrectionIn
) -> tuple[dict[str, Any], bool]:
    """测温读数作废/替换纠错。返回 (纠错结果含重算报告, 是否幂等重试)。"""
    sample = get_sample_or_404(conn, data.sample_code)

    new_canonical: dict[str, Any] | None = None
    if data.new_reading is not None:
        new_canonical = _reading_canonical(
            reading_id=data.new_reading.reading_id,
            device_id=data.new_reading.device_id,
            occurred_iso=to_utc_iso(data.new_reading.occurred_at),
            celsius=float(data.new_reading.celsius),
            sample_code=data.sample_code,
        )
    request_hash = _temp_correction_fingerprint(data, new_canonical)

    existing = conn.execute(
        "SELECT * FROM temperature_correction WHERE correction_id = ?", (data.correction_id,)
    ).fetchone()
    if existing is not None:
        if existing["request_hash"] != request_hash:
            raise DomainError(
                409,
                "temp_correction_conflict",
                f"测温纠错号 {data.correction_id} 已使用但提交内容不一致（同号异内容冲突）",
                {"correction_id": data.correction_id},
            )
        body = {"correction": temp_correction_to_dict(existing),
                "report": build_report(conn, data.sample_code)}
        return body, True

    if sample["approved_storage_at"]:
        raise DomainError(
            409,
            "history_locked",
            f"样本 {data.sample_code} 已核定入库（{sample['approved_storage_at']}），测温历史冻结",
        )

    target = conn.execute(
        "SELECT * FROM temperature_reading WHERE reading_id = ?", (data.target_reading_id,)
    ).fetchone()
    if target is None:
        raise DomainError(404, "temp_reading_not_found",
                          f"目标读数不存在: {data.target_reading_id}")
    if target["sample_id"] != sample["id"]:
        raise DomainError(
            409,
            "temp_correction_cross_sample",
            f"测温纠错不得跨样本：读数 {data.target_reading_id} 不属于样本 {data.sample_code}",
            {"target_reading_id": data.target_reading_id,
             "request_sample_code": data.sample_code},
        )
    if target["superseded_by"] is not None:
        raise DomainError(
            409,
            "reading_not_effective",
            f"读数 {data.target_reading_id} 已被纠错 {target['superseded_by']} 作废/替换，"
            "仅可针对当前有效读数纠错",
            {"target_reading_id": data.target_reading_id,
             "superseded_by": target["superseded_by"]},
        )

    if data.action == "replace":
        occupied = conn.execute(
            "SELECT 1 FROM temperature_reading WHERE reading_id = ?",
            (data.new_reading.reading_id,),
        ).fetchone()
        if occupied is not None:
            raise DomainError(
                409,
                "new_reading_id_occupied",
                f"新读数号 {data.new_reading.reading_id} 已被占用，替换须使用未占用的新读数号",
                {"new_reading_id": data.new_reading.reading_id},
            )
        # 替换新读数同样不得早于采集（配置已存在——原读数准入时即要求配置）
        if new_canonical["occurred_at"] < sample["collected_at"]:
            raise DomainError(
                422,
                "reading_before_collection",
                f"新读数发生时间 {new_canonical['occurred_at']} 早于样本采集时间 "
                f"{sample['collected_at']}，采集前读数拒绝",
                {"reading_id": data.new_reading.reading_id,
                 "occurred_at": new_canonical["occurred_at"],
                 "collected_at": sample["collected_at"]},
            )

    cur = conn.execute(
        "UPDATE temperature_reading SET superseded_by = ? WHERE id = ? AND superseded_by IS NULL",
        (data.correction_id, target["id"]),
    )
    if cur.rowcount == 0:
        raise DomainError(
            409,
            "reading_not_effective",
            f"读数 {data.target_reading_id} 已被并发纠错，同一有效版本仅允许一次纠错成功",
            {"target_reading_id": data.target_reading_id},
        )

    new_row_id: int | None = None
    if data.action == "replace":
        ins = conn.execute(
            "INSERT INTO temperature_reading"
            " (sample_id, reading_id, device_id, occurred_at, celsius, content_hash,"
            "  created_at, superseded_by, replaces_reading_id)"
            " VALUES (?,?,?,?,?,?,?,NULL,?)",
            (sample["id"], data.new_reading.reading_id, data.new_reading.device_id,
             new_canonical["occurred_at"], new_canonical["celsius"],
             _fingerprint(new_canonical), utcnow_iso(), data.target_reading_id),
        )
        new_row_id = ins.lastrowid

    cc = conn.execute(
        "INSERT INTO temperature_correction"
        " (correction_id, sample_id, target_reading_id, action, operator, reason,"
        "  new_reading_id, new_reading_row_id, request_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)",
        (data.correction_id, sample["id"], data.target_reading_id, data.action,
         data.operator, data.reason,
         data.new_reading.reading_id if data.new_reading else None, new_row_id,
         request_hash, utcnow_iso()),
    )
    corr_row = conn.execute(
        "SELECT * FROM temperature_correction WHERE id = ?", (cc.lastrowid,)
    ).fetchone()
    return {
        "correction": temp_correction_to_dict(corr_row),
        "report": build_report(conn, data.sample_code),
    }, False


def list_temperature_corrections(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    sample = get_sample_or_404(conn, sample_code)
    rows = conn.execute(
        "SELECT * FROM temperature_correction WHERE sample_id = ? ORDER BY id ASC",
        (sample["id"],),
    ).fetchall()
    items = [temp_correction_to_dict(r) for r in rows]
    return {"sample_code": sample_code, "total": len(items), "items": items}


# --------------------------------------------------------------------------- 测温复核报告

def _effective_readings(conn: sqlite3.Connection, sample_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT * FROM temperature_reading"
        " WHERE sample_id = ? AND superseded_by IS NULL ORDER BY occurred_at ASC, id ASC",
        (sample_id,),
    ).fetchall()


def build_report(conn: sqlite3.Connection, sample_code: str) -> dict[str, Any]:
    """按当前有效保管链与当前有效读数重算超温点与缺测区间（含逐段归属）。"""
    sample = get_sample_or_404(conn, sample_code)
    cfg = get_config_row(conn, sample["id"])
    if cfg is None:
        raise DomainError(
            404,
            "temp_config_not_found",
            f"样本 {sample_code} 尚未设定测温配置，无测温复核报告",
            {"sample_code": sample_code},
        )

    readings = list_readings(conn, sample_code)
    effective_rows = _effective_readings(conn, sample["id"])
    attribution = CustodyAttribution(conn, sample)

    # ---- 超温点（闭区间外：x < min 或 x > max），按发生时间列出
    excursions: list[dict[str, Any]] = []
    for r in effective_rows:
        c = r["celsius"]
        if c < cfg["min_celsius"] or c > cfg["max_celsius"]:
            direction = "high" if c > cfg["max_celsius"] else "low"
            excursions.append({
                "reading_id": r["reading_id"],
                "device_id": r["device_id"],
                "occurred_at": r["occurred_at"],
                "recorded_at": r["created_at"],
                "celsius": c,
                "direction": direction,
                "limit": {
                    "min_celsius": cfg["min_celsius"],
                    "max_celsius": cfg["max_celsius"],
                },
                "custody": attribution.state_at(r["occurred_at"]),
            })

    # ---- 相邻有效读数间超过最大间隔的缺测区间，跨移交/签收时刻拆分并标归属
    gaps: list[dict[str, Any]] = []
    for i in range(1, len(effective_rows)):
        prev, cur = effective_rows[i - 1], effective_rows[i]
        delta = _seconds_between(prev["occurred_at"], cur["occurred_at"])
        if delta <= cfg["max_interval_seconds"]:
            continue
        gaps.append({
            "index": len(gaps),
            "from_reading_id": prev["reading_id"],
            "to_reading_id": cur["reading_id"],
            "start_at": prev["occurred_at"],
            "end_at": cur["occurred_at"],
            "duration_seconds": delta,
            "max_interval_seconds": cfg["max_interval_seconds"],
            "exceeds_by_seconds": delta - cfg["max_interval_seconds"],
            "segments": attribution.split_interval(prev["occurred_at"], cur["occurred_at"]),
        })

    return {
        "sample_code": sample_code,
        "collected_at": sample["collected_at"],
        "config": config_to_dict(cfg),
        "readings": {
            "total": readings["total"],
            "effective": readings["effective"],
            "items": readings["items"],
        },
        "excursion_count": len(excursions),
        "excursions": excursions,
        "gap_count": len(gaps),
        "gaps": gaps,
        "custody_timeline": attribution.display_timeline(),
    }
