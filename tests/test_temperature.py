"""端到端自测：运输测温复核（温度配置 / 读数 / 超温 / 缺测分段归属 / 纠错 / 冻结）。

运行：python -m tests.test_temperature
"""
import json
import os
import tempfile

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_temperature.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan
API = "/api/v1"
PASS, FAIL = 0, 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✓ {name}")
    else:
        FAIL += 1
        print(f"  ✗ {name} {detail}")


def reg(code, collector="王采集", seal="SEAL-1", when="2026-10-01T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def cfg(code, lo=2.0, hi=8.0, interval=3600):
    return client.put(f"{API}/samples/{code}/temperature-config", json={
        "min_celsius": lo, "max_celsius": hi, "max_interval_seconds": interval})


def rd(code, rid, dev, when, t):
    return client.post(f"{API}/samples/{code}/temperature-readings", json={
        "reading_id": rid, "device_id": dev, "occurred_at": when, "celsius": t})


def report(code):
    return client.get(f"{API}/samples/{code}/temperature-report").json()["report"]


def correction(body):
    return client.post(f"{API}/temperature-corrections", json=body)


# ---------------------------------------------------------------- 1. 配置一次性设定
print("1) 测温配置：设定、幂等、异内容拒绝、核定后拒绝")
reg("T-001")
r = cfg("T-001", 2.0, 8.0, 1800)
check("配置 201", r.status_code == 201, r.text)
c = r.json()["config"]
check("配置回显区间与间隔", c["min_celsius"] == 2.0 and c["max_celsius"] == 8.0 and c["max_interval_seconds"] == 1800)
check("同内容重试 200 幂等", cfg("T-001", 2.0, 8.0, 1800).status_code == 200)
r = cfg("T-001", 2.0, 8.0, 1900)
check("异内容 409 temp_config_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_config_conflict", r.text)
r = client.put(f"{API}/samples/T-UNKNOWN/temperature-config", json={
    "min_celsius": 2.0, "max_celsius": 8.0, "max_interval_seconds": 1800})
check("未知样本配置 404", r.status_code == 404, r.text)
for bad in [
    {"min_celsius": 9.0, "max_celsius": 8.0, "max_interval_seconds": 1800},     # 下限>上限
    {"min_celsius": 2.0, "max_celsius": 8.0, "max_interval_seconds": 0},        # 间隔非正
    {"min_celsius": 2.0, "max_celsius": 8.0, "max_interval_seconds": -5},       # 负数
    {"min_celsius": "NaN", "max_celsius": 8.0, "max_interval_seconds": 1800},   # 非有限
    {"min_celsius": 2.0, "max_celsius": 8.0, "max_interval_seconds": 1.5},      # 非整数
]:
    r = client.put(f"{API}/samples/T-BADCFG/temperature-config", json=bad)
    check(f"非法配置 422: {json.dumps(bad, ensure_ascii=False)}", r.status_code == 422, r.text)
r = client.get(f"{API}/samples/T-001/temperature-config")
check("GET 配置", r.status_code == 200 and r.json()["config"]["max_interval_seconds"] == 1800)
r = client.get(f"{API}/samples/T-NOCFG-XXX/temperature-config")
check("GET 未知样本配置 404", r.status_code == 404)
reg("T-NOCFG")
r = client.get(f"{API}/samples/T-NOCFG/temperature-config")
check("GET 未配置 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)

# ---------------------------------------------------------------- 2. 读数准入与幂等
print("2) 读数：未配置拒绝、采集前拒绝、同号幂等/异内容冲突、非有限值拒绝")
r = rd("T-NOCFG", "R-NOCFG-1", "DEV-1", "2026-10-01T01:30:00Z", 5.0)
check("未配置先报读数 422 temp_config_required",
      r.status_code == 422 and r.json()["error"]["code"] == "temp_config_required", r.text)
r = rd("T-001", "R-001-PRE", "DEV-1", "2026-10-01T00:30:00Z", 5.0)
check("采集前读数 422 reading_before_collection",
      r.status_code == 422 and r.json()["error"]["code"] == "reading_before_collection", r.text)
check("被拒读数不占号（采集前修正时间后可再用同号）",
      rd("T-001", "R-001-PRE", "DEV-1", "2026-10-01T01:30:00Z", 5.0).status_code == 201)
r = rd("T-001", "R-001-PRE", "DEV-1", "2026-10-01T01:30:00Z", 5.0)
check("同号同内容重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = rd("T-001", "R-001-PRE", "DEV-1", "2026-10-01T01:31:00Z", 5.0)
check("同号异内容 409 temp_reading_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_reading_conflict", r.text)
for raw_t in ("NaN", "Infinity", "-Infinity", "1e999"):
    raw = json.dumps({"reading_id": f"R-BAD-{raw_t}", "device_id": "DEV-1",
                      "occurred_at": "2026-10-01T01:40:00Z", "celsius": None})
    raw = raw.replace("null", raw_t)
    r = client.post(f"{API}/samples/T-001/temperature-readings",
                    content=raw.encode("utf-8"),
                    headers={"Content-Type": "application/json"})
    check(f"非有限温度 {raw_t} → 422", r.status_code == 422, r.text)
r = rd("T-001", "", "DEV-1", "2026-10-01T01:40:00Z", 5.0)
check("空读数号 422", r.status_code == 422, r.text)

# ---------------------------------------------------------------- 3. 超温点（闭区间边界与方向）
print("3) 超温点：闭区间边界合规、上下限外判超温，按发生时间列出")
reg("T-003")
cfg("T-003", 2.0, 8.0, 3600)
# 乱序补录：先报后面的，再补前面的
rd("T-003", "R-3-4", "DEV-3", "2026-10-01T04:00:00Z", 2.0)    # 等于下限，合规
rd("T-003", "R-3-1", "DEV-3", "2026-10-01T01:00:00Z", 8.0)    # 等于上限，合规
rd("T-003", "R-3-2", "DEV-3", "2026-10-01T02:00:00Z", 9.5)    # 超高
rd("T-003", "R-3-3", "DEV-3", "2026-10-01T03:00:00Z", 1.2)    # 超低
rep = report("T-003")
check("超温点 2 个", rep["excursion_count"] == 2, json.dumps(rep["excursions"], ensure_ascii=False))
check("超温点按发生时间排序", [e["reading_id"] for e in rep["excursions"]] == ["R-3-2", "R-3-3"])
check("方向 high/low", [e["direction"] for e in rep["excursions"]] == ["high", "low"])
check("超温点附限值", rep["excursions"][0]["limit"] == {"min_celsius": 2.0, "max_celsius": 8.0})
check("有效读数 4、总数 4", rep["readings"]["effective"] == 4 and rep["readings"]["total"] == 4)

# ---------------------------------------------------------------- 4. 缺测区间与跨移交/签收拆分 + 归属
print("4) 缺测区间：相邻读数超阈识别、跨移交/签收时刻拆分并标保管人/在途")
reg("T-004", collector="王采集", seal="SEAL-4")
cfg("T-004", 2.0, 8.0, 3600)
# 保管链：02:00 王采集→李运输在途，04:00 李运输签收，05:00 实验室入库
ev({"event_id": "T4-T1", "event_type": "transfer", "sample_code": "T-004",
    "occurred_at": "2026-10-01T02:00:00Z", "transfer_no": "TR-4",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "T4-R1", "event_type": "receive", "sample_code": "T-004",
    "occurred_at": "2026-10-01T04:00:00Z", "transfer_no": "TR-4",
    "receiver": "李运输", "seal_no": "SEAL-4"})
ev({"event_id": "T4-S1", "event_type": "storage", "sample_code": "T-004",
    "occurred_at": "2026-10-01T05:00:00Z", "custodian": "实验室"})
# 读数：01:00 → 06:00，相邻间隔均 1.5h（>1h），01:00 与 02:30 为保管王采集/在途分段
rd("T-004", "T4-1", "DEV-4", "2026-10-01T01:00:00Z", 5.0)
rd("T-004", "T4-2", "DEV-4", "2026-10-01T02:30:00Z", 5.0)
rd("T-004", "T4-3", "DEV-4", "2026-10-01T04:30:00Z", 5.0)
rd("T-004", "T4-4", "DEV-4", "2026-10-01T06:00:00Z", 5.0)
rep = report("T-004")
check("无超温点", rep["excursion_count"] == 0, str(rep["excursions"]))
check("缺测区间 3 段", rep["gap_count"] == 3, str(rep["gap_count"]))
g0, g1, g2 = rep["gaps"]
check("缺测时长与超出量", g0["duration_seconds"] == 5400 and g0["exceeds_by_seconds"] == 1800
      and g0["max_interval_seconds"] == 3600)
# g0 = [01:00,02:30]：02:00 移交边界在内 → 拆为王采集保管 + 在途
seg0 = g0["segments"]
check("g0 跨移交拆成 2 段", len(seg0) == 2, json.dumps(seg0, ensure_ascii=False))
check("g0 首段=王采集 custody", seg0[0]["custody"]["attribution"] == "custody" and seg0[0]["custody"]["custodian"] == "王采集")
check("g0 首段 1 小时", seg0[0]["duration_seconds"] == 3600)
check("g0 次段=在途 TR-4 王→李",
      seg0[1]["custody"]["attribution"] == "in_transit"
      and seg0[1]["custody"]["transfer_no"] == "TR-4"
      and seg0[1]["custody"]["from_custodian"] == "王采集"
      and seg0[1]["custody"]["to_custodian"] == "李运输")
check("g0 次段 30 分钟", seg0[1]["duration_seconds"] == 1800)
# g1 = [02:30,04:30]：04:00 签收边界在内 → 在途 + 李运输保管
seg1 = g1["segments"]
check("g1 跨签收拆成 2 段", len(seg1) == 2, json.dumps(seg1, ensure_ascii=False))
check("g1 首段在途", seg1[0]["custody"]["attribution"] == "in_transit")
check("g1 次段=李运输 custody",
      seg1[1]["custody"]["attribution"] == "custody" and seg1[1]["custody"]["custodian"] == "李运输")
# g2 = [04:30,06:00]：05:00 入库边界在内 → 李运输 + 实验室
seg2 = g2["segments"]
check("g2 跨入库拆成 2 段", len(seg2) == 2, json.dumps(seg2, ensure_ascii=False))
check("g2 保管人序列 李运输→实验室",
      [s["custody"].get("custodian") for s in seg2] == ["李运输", "实验室"])
check("分段时长之和=缺测时长（g1）", sum(s["duration_seconds"] for s in seg1) == g1["duration_seconds"])
# 归属时间线展示：王采集→在途→李运输→实验室
tl = rep["custody_timeline"]
seq = [(seg["custody"]["attribution"], seg["custody"].get("custodian"),
        seg["custody"].get("transfer_no"), seg["start_at"], seg["end_at"]) for seg in tl]
check("归属时间线 4 段", len(tl) == 4, json.dumps(tl, ensure_ascii=False))
check("时间线起点采集时间且末段开放",
      tl[0]["start_at"] == "2026-10-01T01:00:00Z" and tl[-1]["end_at"] is None)
check("时间线状态序列",
      [s["custody"]["attribution"] for s in tl] == ["custody", "in_transit", "custody", "custody"])

# ---------------------------------------------------------------- 5. 边界精确性
print("5) 边界精确性：端点事件归邻段、同时刻不产生零长度段、等于限值无缺测")
reg("T-005")
cfg("T-005", 2.0, 8.0, 3600)
ev({"event_id": "T5-T1", "event_type": "transfer", "sample_code": "T-005",
    "occurred_at": "2026-10-01T02:00:00Z", "transfer_no": "TR-5",
    "from_custodian": "王采集", "to_custodian": "李运输"})
# 两读数恰隔 1 小时（=限值）且端点 02:00 正好为移交时刻
rd("T-005", "T5-1", "DEV-5", "2026-10-01T02:00:00Z", 9.0)  # 移交时刻超温：按移交后在途归属
rd("T-005", "T5-2", "DEV-5", "2026-10-01T03:00:00Z", 5.0)
rep = report("T-005")
check("恰为限值间隔不判缺测", rep["gap_count"] == 0, str(rep["gaps"]))
# 超温点状态校验：02:00 的读数（恰为移交时刻）按生效后状态标 in_transit
ex = {e["reading_id"]: e for e in rep["excursions"]}
check("移交时刻超温点标在途",
      "T5-1" in ex and ex["T5-1"]["custody"]["attribution"] == "in_transit",
      json.dumps(ex, ensure_ascii=False))
rd("T-005", "T5-3", "DEV-5", "2026-10-01T02:30:00Z", 9.0)
rep = report("T-005")
ex = {e["reading_id"]: e for e in rep["excursions"]}
check("02:30 超温点标在途", ex["T5-3"]["custody"]["attribution"] == "in_transit")

# ---------------------------------------------------------------- 6. 断链 → 待核
print("6) 保管人断链：签收无对应在途移交 → 超温点与缺测段标 pending_verify")
reg("T-006")
cfg("T-006", 2.0, 8.0, 1800)
# 只有签收、没有匹配移交 → 断链；其后归属待核
ev({"event_id": "T6-RX", "event_type": "receive", "sample_code": "T-006",
    "occurred_at": "2026-10-01T03:00:00Z", "transfer_no": "TR-GHOST",
    "receiver": "陌生人", "seal_no": "SEAL-1"})
rd("T-006", "T6-1", "DEV-6", "2026-10-01T02:00:00Z", 5.0)
rd("T-006", "T6-2", "DEV-6", "2026-10-01T02:40:00Z", 5.0)
rd("T-006", "T6-3", "DEV-6", "2026-10-01T04:00:00Z", 10.0)
rep = report("T-006")
check("有超温点", rep["excursion_count"] == 1)
ex = rep["excursions"][0]
check("断链后超温点归属待核并给原因",
      ex["custody"]["attribution"] == "pending_verify" and "TR-GHOST" in ex["custody"]["reason"],
      json.dumps(ex, ensure_ascii=False))
# 缺测 [02:40,04:00] 跨 03:00 断链点 → 保管 + 待核
gap = rep["gaps"][-1]
atts = [s["custody"]["attribution"] for s in gap["segments"]]
check("跨断链点拆分为 custody + pending_verify",
      atts == ["custody", "pending_verify"], json.dumps(gap["segments"], ensure_ascii=False))
# 断链前的缺测段仍是保管人
gap0 = rep["gaps"][0]
check("断链前缺测段仍有保管人",
      all(s["custody"]["attribution"] == "custody" for s in gap0["segments"]))
tl_att = [s["custody"]["attribution"] for s in rep["custody_timeline"]]
check("时间线含待核段", "pending_verify" in tl_att and tl_att[-1] == "pending_verify")

# ---------------------------------------------------------------- 7. 作废/替换纠错后重算、留痕
print("7) 读数纠错：作废/替换、重算、留痕、跨样本、非有效版本、幂等与冲突")
reg("T-007")
cfg("T-007", 2.0, 8.0, 1800)
rd("T-007", "T7-1", "DEV-7", "2026-10-01T01:00:00Z", 5.0)
rd("T-007", "T7-2", "DEV-7", "2026-10-01T01:30:00Z", 50.0)   # 异常值，疑似录入错误
rd("T-007", "T7-3", "DEV-7", "2026-10-01T02:00:00Z", 5.0)
rep = report("T-007")
check("替换前有 1 超温点", rep["excursion_count"] == 1)
# 替换 T7-2 为合规温度（乱序时刻 01:30 不变，查询按时间重排）
r = correction({"correction_id": "FIX-T7-1", "sample_code": "T-007",
                "target_reading_id": "T7-2", "action": "replace",
                "operator": "质控员", "reason": "设备小数点录入错误，5.0 误录为 50.0",
                "new_reading": {"reading_id": "T7-2B", "device_id": "DEV-7",
                                "occurred_at": "2026-10-01T01:30:00Z", "celsius": 5.0}})
check("替换 201 且响应带重算报告", r.status_code == 201 and r.json()["report"]["excursion_count"] == 0, r.text)
rep = report("T-007")
check("替换后无超温点", rep["excursion_count"] == 0)
check("读数总数 4（含被替换留痕）、有效 3",
      rep["readings"]["total"] == 4 and rep["readings"]["effective"] == 3)
old = [x for x in rep["readings"]["items"] if x["reading_id"] == "T7-2"][0]
new = [x for x in rep["readings"]["items"] if x["reading_id"] == "T7-2B"][0]
check("原读数标记失效且不被改写",
      old["effective"] is False and old["celsius"] == 50.0 and old["superseded_by"] == "FIX-T7-1")
check("新读数指向原读数", new["effective"] is True and new["replaces_reading_id"] == "T7-2")
# 同纠错号同内容重试幂等
r = correction({"correction_id": "FIX-T7-1", "sample_code": "T-007",
                "target_reading_id": "T7-2", "action": "replace",
                "operator": "质控员", "reason": "设备小数点录入错误，5.0 误录为 50.0",
                "new_reading": {"reading_id": "T7-2B", "device_id": "DEV-7",
                                "occurred_at": "2026-10-01T01:30:00Z", "celsius": 5.0}})
check("同纠错号同内容 200 幂等", r.status_code == 200 and r.json().get("idempotent") is True, r.text)
r = correction({"correction_id": "FIX-T7-1", "sample_code": "T-007",
                "target_reading_id": "T7-2", "action": "void",
                "operator": "质控员", "reason": "改动作"})
check("同纠错号异内容 409",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_correction_conflict", r.text)
# 对已失效读数再纠错
r = correction({"correction_id": "FIX-T7-2", "sample_code": "T-007",
                "target_reading_id": "T7-2", "action": "void",
                "operator": "质控员", "reason": "再次纠错"})
check("对失效读数纠错 409 reading_not_effective",
      r.status_code == 409 and r.json()["error"]["code"] == "reading_not_effective", r.text)
# 跨样本
reg("T-007B")
cfg("T-007B")
r = correction({"correction_id": "FIX-T7-3", "sample_code": "T-007B",
                "target_reading_id": "T7-1", "action": "void",
                "operator": "质控员", "reason": "跨样本"})
check("跨样本纠错 409 temp_correction_cross_sample",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_correction_cross_sample", r.text)
# 目标不存在
r = correction({"correction_id": "FIX-T7-4", "sample_code": "T-007",
                "target_reading_id": "NOPE", "action": "void",
                "operator": "质控员", "reason": "无"})
check("目标读数不存在 404",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_reading_not_found", r.text)
# replace 新号已占用
r = correction({"correction_id": "FIX-T7-5", "sample_code": "T-007",
                "target_reading_id": "T7-2B", "action": "replace",
                "operator": "质控员", "reason": "再替换",
                "new_reading": {"reading_id": "T7-3", "device_id": "DEV-7",
                                "occurred_at": "2026-10-01T01:30:00Z", "celsius": 5.1}})
check("新读数号占用 409 new_reading_id_occupied",
      r.status_code == 409 and r.json()["error"]["code"] == "new_reading_id_occupied", r.text)
# 纠错审计查询
r = client.get(f"{API}/samples/T-007/temperature-corrections")
check("纠错审计 1 条且留痕", r.status_code == 200 and r.json()["total"] == 1
      and r.json()["items"][0]["new_reading_id"] == "T7-2B", r.text)

# 作废使相邻读数重新配对产生缺测
reg("T-007C")
cfg("T-007C", 2.0, 8.0, 1800)
rd("T-007C", "C-1", "DEV-7", "2026-10-01T01:00:00Z", 5.0)
rd("T-007C", "C-2", "DEV-7", "2026-10-01T01:20:00Z", 5.0)
rd("T-007C", "C-3", "DEV-7", "2026-10-01T01:40:00Z", 5.0)
check("作废前无缺测（间隔 20 分钟）", report("T-007C")["gap_count"] == 0)
correction({"correction_id": "FIX-C-1", "sample_code": "T-007C",
            "target_reading_id": "C-2", "action": "void",
            "operator": "质控员", "reason": "重复读数作废"})
rep = report("T-007C")
check("作废后相邻有效读数间隔 40 分钟 > 限值 30 分钟 → 1 缺测", rep["gap_count"] == 1,
      json.dumps(rep["gaps"], ensure_ascii=False))
check("缺测区间两端为 C-1/C-3",
      rep["gaps"][0]["from_reading_id"] == "C-1" and rep["gaps"][0]["to_reading_id"] == "C-3")
# 再作废 C-3 → 仅 1 个有效读数，无相邻对，无缺测
correction({"correction_id": "FIX-C-2", "sample_code": "T-007C",
            "target_reading_id": "C-3", "action": "void",
            "operator": "质控员", "reason": "作废"})
check("仅一条有效读数无缺测", report("T-007C")["gap_count"] == 0
      and report("T-007C")["readings"]["effective"] == 1)

# ---------------------------------------------------------------- 8. 乱序补录后缺测重算
print("8) 乱序补录：后补中间读数后缺测自动消除")
reg("T-008")
cfg("T-008", 2.0, 8.0, 3600)
rd("T-008", "T8-1", "DEV-8", "2026-10-01T01:00:00Z", 5.0)
rd("T-008", "T8-3", "DEV-8", "2026-10-01T03:00:00Z", 5.0)
check("两小时间隔判 1 缺测", report("T-008")["gap_count"] == 1)
rd("T-008", "T8-2", "DEV-8", "2026-10-01T02:00:00Z", 5.0)   # 乱序补录
rep = report("T-008")
check("补录后无缺测且按发生时间排列",
      rep["gap_count"] == 0
      and [x["reading_id"] for x in rep["readings"]["items"]] == ["T8-1", "T8-2", "T8-3"])

# ---------------------------------------------------------------- 9. 移交/签收纠错后归属重算
print("9) 移交事件替换纠错后，测温归属按当前有效保管链重算")
reg("T-009", collector="王采集", seal="SEAL-9")
cfg("T-009", 2.0, 8.0, 1800)
# 原移交：王采集→错误接收人“黑运输”，实际应是李运输（替换纠错）
ev({"event_id": "T9-T1", "event_type": "transfer", "sample_code": "T-009",
    "occurred_at": "2026-10-01T02:00:00Z", "transfer_no": "TR-9",
    "from_custodian": "王采集", "to_custodian": "黑运输"})
rd("T-009", "T9-1", "DEV-9", "2026-10-01T02:30:00Z", 5.0)
rep = report("T-009")
check("在途段接收人为黑运输", rep["custody_timeline"][1]["custody"]["to_custodian"] == "黑运输")
client.post(f"{API}/corrections", json={
    "correction_id": "FIX-E9-1", "sample_code": "T-009", "target_event_id": "T9-T1",
    "action": "replace", "operator": "质控员", "reason": "接收人录入错误",
    "new_event": {"event_id": "T9-T1B", "event_type": "transfer",
                  "occurred_at": "2026-10-01T02:00:00Z", "transfer_no": "TR-9",
                  "from_custodian": "王采集", "to_custodian": "李运输"}})
rep = report("T-009")
check("移交替换后在途接收人重算为李运输（原事件不改）",
      rep["custody_timeline"][1]["custody"]["to_custodian"] == "李运输",
      json.dumps(rep["custody_timeline"], ensure_ascii=False))

# 作废唯一移交 → 全程王采集保管
client.post(f"{API}/corrections", json={
    "correction_id": "FIX-E9-2", "sample_code": "T-009", "target_event_id": "T9-T1B",
    "action": "void", "operator": "质控员", "reason": "移交流程撤销"})
rep = report("T-009")
check("移交作废后无在途段，全程王采集",
      all(s["custody"]["attribution"] == "custody" and s["custody"]["custodian"] == "王采集"
          for s in rep["custody_timeline"]) and len(rep["custody_timeline"]) == 1,
      json.dumps(rep["custody_timeline"], ensure_ascii=False))

# ---------------------------------------------------------------- 10. 核定后冻结，但查询/幂等兼容
print("10) 核定冻结：核定后拒绝新配置/读数/纠错；同号同内容重试仍 200；报告仍可查")
# 构造一条可核定样本（完整有效保管链）
reg("T-010", collector="王采集", seal="SEAL-10")
cfg("T-010", 2.0, 8.0, 7200)
ev({"event_id": "T10-T1", "event_type": "transfer", "sample_code": "T-010",
    "occurred_at": "2026-10-01T02:00:00Z", "transfer_no": "TR-10",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "T10-R1", "event_type": "receive", "sample_code": "T-010",
    "occurred_at": "2026-10-01T03:00:00Z", "transfer_no": "TR-10",
    "receiver": "李运输", "seal_no": "SEAL-10"})
ev({"event_id": "T10-C1", "event_type": "seal_check", "sample_code": "T-010",
    "occurred_at": "2026-10-01T03:30:00Z", "checked_seal_no": "SEAL-10", "seal_intact": True})
ev({"event_id": "T10-S1", "event_type": "storage", "sample_code": "T-010",
    "occurred_at": "2026-10-01T04:00:00Z", "custodian": "实验室"})
rd("T-010", "T10-1", "DEV-10", "2026-10-01T02:30:00Z", 5.0)
r = client.post(f"{API}/samples/T-010/approve", json={"approver": "质量主管"})
check("核定成功 200", r.status_code == 200, r.text)
r = cfg("T-010", 2.0, 8.0, 7200)
check("核定后同内容配置重试仍 200（幂等兼容）", r.status_code == 200, r.text)
r = cfg("T-010", 2.0, 9.0, 7200)
check("核定后改配置 409（temp_config_conflict，一次性）",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_config_conflict", r.text)
r = rd("T-010", "T10-NEW", "DEV-10", "2026-10-01T05:00:00Z", 5.0)
check("核定后新读数 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
r = rd("T-010", "T10-1", "DEV-10", "2026-10-01T02:30:00Z", 5.0)
check("核定后同号同内容读数重试 200（幂等兼容）", r.status_code == 200, r.text)
r = correction({"correction_id": "FIX-T10-1", "sample_code": "T-010",
                "target_reading_id": "T10-1", "action": "void",
                "operator": "质控员", "reason": "核定后应拒"})
check("核定后测温纠错 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
r = client.get(f"{API}/samples/T-010/temperature-report")
check("核定后报告仍可查询", r.status_code == 200 and r.json()["report"]["readings"]["effective"] == 1, r.text)

# ---------------------------------------------------------------- 11. 版本规则不受测温写入影响
print("11) 版本兼容：测温配置/读数/测温纠错不产生保管链版本；读数报告不依赖版本")
reg("T-011")
v0 = client.get(f"{API}/samples/T-011/versions").json()["current_version"]
cfg("T-011", 2.0, 8.0, 1800)
rd("T-011", "T11-1", "DEV-11", "2026-10-01T01:30:00Z", 5.0)
rd("T-011", "T11-2", "DEV-11", "2026-10-01T02:00:00Z", 9.0)
correction({"correction_id": "FIX-T11-1", "sample_code": "T-011",
            "target_reading_id": "T11-2", "action": "void",
            "operator": "质控员", "reason": "作废"})
v1 = client.get(f"{API}/samples/T-011/versions").json()["current_version"]
check("测温写入前后版本号不变（仅版本 0）", v0 == 0 and v1 == 0, f"{v0} -> {v1}")

# ---------------------------------------------------------------- 12. 读数列表与全局唯一
print("12) 读数列表留痕与读数号跨样本全局唯一")
reg("T-012A")
cfg("T-012A")
reg("T-012B")
cfg("T-012B")
r = rd("T-012A", "GLOBAL-1", "DEV-A", "2026-10-01T01:30:00Z", 5.0)
check("样本 A 记录成功", r.status_code == 201, r.text)
r = rd("T-012B", "GLOBAL-1", "DEV-B", "2026-10-01T01:30:00Z", 6.0)
check("读数号跨样本异内容 409",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_reading_conflict", r.text)
r = rd("T-012B", "GLOBAL-1", "DEV-A", "2026-10-01T01:30:00Z", 5.0)
check("读数号跨样本提交（指纹含样本归属）→ 409 temp_reading_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_reading_conflict", r.text)
r = client.get(f"{API}/samples/T-012A/temperature-readings")
items = r.json()["items"]
check("读数列表含设备号/时间/数值/录入时间",
      items[0]["device_id"] == "DEV-A" and items[0]["celsius"] == 5.0
      and items[0]["occurred_at"] == "2026-10-01T01:30:00Z" and items[0]["recorded_at"])

# ---------------------------------------------------------------- 汇总
print()
if FAIL == 0:
    print(f"结果: {PASS} 通过, 0 失败")
else:
    print(f"结果: {PASS} 通过, {FAIL} 失败")
    raise SystemExit(1)
