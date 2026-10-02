"""端到端自测：运输测温证据（配置一次性设定、读数记录、超温点/缺测区间复核查询、
归属重算、核定冻结、与既有保管链结论及版本规则兼容、并发）。

运行：python -m tests.test_temperature
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_temperature.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import init_db  # noqa: E402
from app.main import app  # noqa: E402

init_db()
client = TestClient(app)
client.__enter__()  # 触发 lifespan（启动钩子）
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


def reg(code, collector="王采集", seal="SEAL-T", when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when, "collector": collector, "seal_no": seal
    })


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def cfg(code, lo=2.0, hi=8.0, interval=1800):
    return client.post(f"{API}/temperature-configs", json={
        "sample_code": code, "min_celsius": lo, "max_celsius": hi,
        "max_interval_seconds": interval})


def rd(code, rid, when, value, dev="DEV-01"):
    return client.post(f"{API}/temperature-readings", json={
        "sample_code": code, "reading_id": rid, "device_id": dev,
        "occurred_at": when, "value_celsius": value})


def review(code):
    return client.get(f"{API}/samples/{code}/temperature")


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def kinds(review_json):
    """复核查询中全部缺测段归属 kind 序列（便于断言）。"""
    return [[s["attribution"]["kind"] for s in g["segments"]] for g in review_json["gap_intervals"]]


# ---------------------------------------------------------------- 1. 测温配置：一次性设定
print("1) 测温配置：一次性设定、幂等/冲突、载荷校验")
reg("T-CFG")
r = client.get(f"{API}/samples/T-CFG/temperature")
check("未设定配置查询 404 temp_config_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "temp_config_not_found", r.text)
r = cfg("T-CFG")
check("设定配置 201", r.status_code == 201, r.text)
body = r.json()
check("配置响应字段", body["config"]["min_celsius"] == 2.0 and body["config"]["max_celsius"] == 8.0
      and body["config"]["max_interval_seconds"] == 1800 and body["idempotent"] is False, str(body))
r = cfg("T-CFG")
check("同内容重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = cfg("T-CFG", hi=9.0)
check("异内容 409 temp_config_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_config_conflict", r.text)
r = cfg("T-CFG", lo=9.0, hi=2.0)
check("下限大于上限 422", r.status_code == 422, r.text)
r = cfg("T-CFG", interval=0)
check("最大间隔为 0 422（须正数）", r.status_code == 422, r.text)
r = cfg("T-CFG", interval=-5)
check("最大间隔为负 422", r.status_code == 422, r.text)
r = client.post(f"{API}/temperature-configs", json={
    "sample_code": "T-CFG", "min_celsius": 2.0, "max_celsius": 8.0, "max_interval_seconds": 1800.5})
check("最大间隔非整数 422", r.status_code == 422, r.text)
r = client.post(f"{API}/temperature-configs", content=(
    '{"sample_code":"T-CFG","min_celsius":NaN,"max_celsius":8.0,"max_interval_seconds":1800}'
), headers={"Content-Type": "application/json"})
check("配置温度非有限数值 422", r.status_code == 422, r.text)
r = cfg("T-CFG-NONE")
check("样本不存在设定配置 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
r = client.get(f"{API}/samples/T-CFG-NONE/temperature")
check("样本不存在查询 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)

# ---------------------------------------------------------------- 2. 测温读数：记录与拒绝
print("2) 测温读数：未配置拒绝、采集前拒绝、重复时刻拒绝、同号幂等/异内容冲突")
reg("T-RD")
r = rd("T-RD", "RD-000", "2026-09-30T02:00:00Z", 4.0)
check("未设定配置记录读数 409 temp_config_missing",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_config_missing", r.text)
cfg("T-RD")
r = rd("T-RD", "RD-001", "2026-09-30T02:00:00Z", 4.5)
check("记录读数 201", r.status_code == 201, r.text)
body = r.json()
check("读数响应字段", body["reading"]["reading_id"] == "RD-001" and body["reading"]["device_id"] == "DEV-01"
      and body["reading"]["occurred_at"] == "2026-09-30T02:00:00Z" and body["reading"]["value_celsius"] == 4.5
      and body["idempotent"] is False, str(body))
r = rd("T-RD", "RD-001", "2026-09-30T02:00:00Z", 4.5)
check("同号同内容重试 200 返回原记录",
      r.status_code == 200 and r.json()["idempotent"] is True
      and r.json()["reading"]["recorded_at"] == body["reading"]["recorded_at"], r.text)
r = rd("T-RD", "RD-001", "2026-09-30T02:00:00Z", 5.5)
check("同号异内容 409 reading_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "reading_conflict", r.text)
r = rd("T-RD", "RD-002", "2026-09-30T02:00:00Z", 5.0)
check("重复发生时刻（异读数号）409 reading_time_duplicate",
      r.status_code == 409 and r.json()["error"]["code"] == "reading_time_duplicate", r.text)
r = rd("T-RD", "RD-003", "2026-09-30T00:59:59Z", 5.0)
check("采集前发生时刻 422 reading_before_collection",
      r.status_code == 422 and r.json()["error"]["code"] == "reading_before_collection", r.text)
r = rd("T-RD", "RD-004", "2026-09-30T01:00:00Z", 5.0)
check("恰为采集时刻允许记录 201", r.status_code == 201, r.text)
r = client.post(f"{API}/temperature-readings", content=(
    '{"sample_code":"T-RD","reading_id":"RD-005","device_id":"DEV-01",'
    '"occurred_at":"2026-09-30T03:00:00Z","value_celsius":Infinity}'
), headers={"Content-Type": "application/json"})
check("读数非有限数值 422", r.status_code == 422, r.text)
r = rd("T-RD-NONE", "RD-X1", "2026-09-30T02:00:00Z", 4.0)
check("样本不存在记录读数 404 sample_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "sample_not_found", r.text)
# 时区表示归一：同一时刻的不同表示视为同一发生时刻
r = rd("T-RD", "RD-006", "2026-09-30T04:30:00+02:00", 6.0)   # = 02:30:00Z
check("带时区偏移读数 201", r.status_code == 201, r.text)
check("发生时间归一 UTC", r.json()["reading"]["occurred_at"] == "2026-09-30T02:30:00Z", r.text)
r = rd("T-RD", "RD-006", "2026-09-30T02:30:00Z", 6.0)
check("同号同时刻（时区表示不同）重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = rd("T-RD", "RD-007", "2026-09-30T02:30:00Z", 6.0)
check("归一后重复发生时刻 409", r.status_code == 409
      and r.json()["error"]["code"] == "reading_time_duplicate", r.text)

# ---------------------------------------------------------------- 3. 复核查询：超温点与缺测区间
print("3) 复核查询：超温点、缺测区间、跨移交/签收拆分、逐段归属")
reg("T-MAIN")
cfg("T-MAIN")
ev({"event_id": "EVT-TM-T1", "event_type": "transfer", "sample_code": "T-MAIN",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-TM",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TM-R1", "event_type": "receive", "sample_code": "T-MAIN",
    "occurred_at": "2026-09-30T05:00:00Z", "transfer_no": "TR-TM",
    "receiver": "李运输", "seal_no": "SEAL-T"})
ev({"event_id": "EVT-TM-S1", "event_type": "storage", "sample_code": "T-MAIN",
    "occurred_at": "2026-09-30T06:00:00Z", "custodian": "实验室"})
# 读数故意乱序写入
rd("T-MAIN", "RD-M4", "2026-09-30T04:20:00Z", 1.0)    # 低于下限（在途）
rd("T-MAIN", "RD-M1", "2026-09-30T01:30:00Z", 4.0)
rd("T-MAIN", "RD-M6", "2026-09-30T06:30:00Z", 7.9)
rd("T-MAIN", "RD-M3", "2026-09-30T04:00:00Z", 9.5)    # 高于上限（在途）
rd("T-MAIN", "RD-M2", "2026-09-30T02:00:00Z", 5.0)
rd("T-MAIN", "RD-M5", "2026-09-30T05:30:00Z", 6.0)
rd("T-MAIN", "RD-M7", "2026-09-30T07:00:00Z", 2.0)    # 边界值=下限，在范围内
rd("T-MAIN", "RD-M8", "2026-09-30T07:30:00Z", 8.0)    # 边界值=上限，在范围内

v = review("T-MAIN").json()
check("读数按发生时间排列",
      [x["reading_id"] for x in v["readings"]]
      == ["RD-M1", "RD-M2", "RD-M3", "RD-M4", "RD-M5", "RD-M6", "RD-M7", "RD-M8"], str(v["readings"]))
check("读数计数", v["reading_count"] == 8, str(v["reading_count"]))
check("边界值判在范围内",
      v["readings"][6]["in_range"] is True and v["readings"][7]["in_range"] is True, str(v["readings"]))
oor = v["out_of_range_points"]
check("超温点 2 个且按发生时间排列", [x["reading_id"] for x in oor] == ["RD-M3", "RD-M4"], str(oor))
check("超温点方向与限值", oor[0]["direction"] == "above_max" and oor[0]["limit_celsius"] == 8.0
      and oor[1]["direction"] == "below_min" and oor[1]["limit_celsius"] == 2.0, str(oor))
check("超温点归属在途移交",
      all(x["attribution"]["kind"] == "in_transit" and x["attribution"]["transfer_no"] == "TR-TM"
          and x["attribution"]["from_custodian"] == "王采集" and x["attribution"]["to_custodian"] == "李运输"
          for x in oor), str(oor))

gaps = v["gap_intervals"]
check("缺测区间 3 段（等于限值 1800s 的相邻读数不报）", len(gaps) == 3, str(gaps))
g1, g2, g3 = gaps
check("缺测区间 1 起讫与时长", g1["start"] == "2026-09-30T02:00:00Z" and g1["end"] == "2026-09-30T04:00:00Z"
      and g1["duration_seconds"] == 7200 and g1["max_interval_seconds"] == 1800
      and g1["before_reading_id"] == "RD-M2" and g1["after_reading_id"] == "RD-M3", str(g1))
check("缺测段 1 跨移交时刻拆分为 2 段",
      len(g1["segments"]) == 2 and g1["segments"][0]["end"] == "2026-09-30T03:00:00Z"
      and g1["segments"][1]["start"] == "2026-09-30T03:00:00Z", str(g1["segments"]))
check("缺测段 1 归属：保管人→在途",
      g1["segments"][0]["attribution"] == {"kind": "custodian", "custodian": "王采集"}
      and g1["segments"][1]["attribution"]["kind"] == "in_transit"
      and g1["segments"][1]["attribution"]["transfer_event_id"] == "EVT-TM-T1", str(g1["segments"]))
check("缺测段时长求和=区间时长",
      all(g["duration_seconds"] == sum(s["duration_seconds"] for s in g["segments"]) for g in gaps), str(gaps))
check("缺测区间 2 跨签收时刻拆分：在途→签收人",
      [s["attribution"]["kind"] for s in g2["segments"]] == ["in_transit", "custodian"]
      and g2["segments"][1]["attribution"]["custodian"] == "李运输"
      and g2["segments"][1]["start"] == "2026-09-30T05:00:00Z", str(g2))
check("缺测区间 3 不跨移交/签收（入库不拆分）为单段",
      len(g3["segments"]) == 1 and g3["start"] == "2026-09-30T05:30:00Z"
      and g3["end"] == "2026-09-30T06:30:00Z", str(g3))
check("缺测区间 3 归属按段起始时刻（入库前保管人）",
      g3["segments"][0]["attribution"] == {"kind": "custodian", "custodian": "李运输"}, str(g3))

# ---------------------------------------------------------------- 4. 断链 → 待核
print("4) 断链导致归属不能确定时标为待核（pending_review）")
reg("T-BROKEN", collector="王采集")
cfg("T-BROKEN")
ev({"event_id": "EVT-TB-T1", "event_type": "transfer", "sample_code": "T-BROKEN",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-TB",
    "from_custodian": "张三", "to_custodian": "李四"})   # 移交人非当前保管人 → 断链
rd("T-BROKEN", "RD-B1", "2026-09-30T01:30:00Z", 4.0)
rd("T-BROKEN", "RD-B2", "2026-09-30T03:00:00Z", 9.0)    # 超温，断链后
v = review("T-BROKEN").json()
check("断链后超温点归属待核",
      v["out_of_range_points"][0]["attribution"]["kind"] == "pending_review"
      and "断链" in v["out_of_range_points"][0]["attribution"]["detail"], str(v["out_of_range_points"]))
check("断链缺测段拆分：断链前保管人、断链后待核",
      kinds(v) == [["custodian", "pending_review"]]
      and v["gap_intervals"][0]["segments"][0]["attribution"]["custodian"] == "王采集", str(v["gap_intervals"]))

reg("T-NOLINK", collector="王采集")
cfg("T-NOLINK")
ev({"event_id": "EVT-TN-R1", "event_type": "receive", "sample_code": "T-NOLINK",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-NONE",
    "receiver": "李四", "seal_no": "SEAL-T"})            # 签收无对应在途移交 → 断链
rd("T-NOLINK", "RD-N1", "2026-09-30T01:30:00Z", 4.0)
rd("T-NOLINK", "RD-N2", "2026-09-30T03:00:00Z", 5.0)
v = review("T-NOLINK").json()
check("签收无在途移交：其后归属待核",
      kinds(v) == [["custodian", "pending_review"]], str(v["gap_intervals"]))

reg("T-RETR", collector="王采集")
cfg("T-RETR")
ev({"event_id": "EVT-TR-T1", "event_type": "transfer", "sample_code": "T-RETR",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-RT1",
    "from_custodian": "王采集", "to_custodian": "李四"})
ev({"event_id": "EVT-TR-T2", "event_type": "transfer", "sample_code": "T-RETR",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-RT2",
    "from_custodian": "王采集", "to_custodian": "赵六"})  # 在途再移交 → 断链
rd("T-RETR", "RD-R1", "2026-09-30T02:30:00Z", 4.0)
rd("T-RETR", "RD-R2", "2026-09-30T04:00:00Z", 5.0)
v = review("T-RETR").json()
check("在途再移交：断链前在途、其后待核",
      kinds(v) == [["in_transit", "pending_review"]], str(v["gap_intervals"]))

# 不触发待核的情形：签收要素不符（在途保持）与重复签收
reg("T-MISREC", collector="王采集")
cfg("T-MISREC")
ev({"event_id": "EVT-TMS-T1", "event_type": "transfer", "sample_code": "T-MISREC",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-MM",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TMS-R1", "event_type": "receive", "sample_code": "T-MISREC",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-MM",
    "receiver": "李运输", "seal_no": "SEAL-WRONG"})       # 封签不符：在途保持
rd("T-MISREC", "RD-S1", "2026-09-30T03:30:00Z", 4.0)
rd("T-MISREC", "RD-S2", "2026-09-30T05:00:00Z", 5.0)
v = review("T-MISREC").json()
check("签收要素不符不触发待核（仍在途）",
      kinds(v) == [["in_transit"]] and chain("T-MISREC")["status"] == "invalid", str(v["gap_intervals"]))

reg("T-DUPREC", collector="王采集")
cfg("T-DUPREC")
ev({"event_id": "EVT-TD-T1", "event_type": "transfer", "sample_code": "T-DUPREC",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-DD",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TD-R1", "event_type": "receive", "sample_code": "T-DUPREC",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-DD",
    "receiver": "李运输", "seal_no": "SEAL-T"})
ev({"event_id": "EVT-TD-R2", "event_type": "receive", "sample_code": "T-DUPREC",
    "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": "TR-DD",
    "receiver": "李运输", "seal_no": "SEAL-T"})           # 重复签收：无效但不改变归属
rd("T-DUPREC", "RD-D1", "2026-09-30T04:30:00Z", 4.0)
rd("T-DUPREC", "RD-D2", "2026-09-30T06:00:00Z", 5.0)
v = review("T-DUPREC").json()
check("重复签收不触发待核（仍为签收人保管）",
      kinds(v) == [["custodian"]]
      and v["gap_intervals"][0]["segments"][0]["attribution"]["custodian"] == "李运输", str(v["gap_intervals"]))

# ---------------------------------------------------------------- 5. 乱序补录与纠错后重算归属
print("5) 乱序补录与移交/签收纠错后重算归属，原始读数不改")
reg("T-BACKFILL", collector="王采集")
cfg("T-BACKFILL")
rd("T-BACKFILL", "RD-F1", "2026-09-30T01:30:00Z", 4.0)
rd("T-BACKFILL", "RD-F2", "2026-09-30T02:30:00Z", 5.0)
rd("T-BACKFILL", "RD-F3", "2026-09-30T03:30:00Z", 6.0)
v = review("T-BACKFILL").json()
check("无事件时缺测段归属采集人",
      kinds(v) == [["custodian"], ["custodian"]]
      and all(s["attribution"]["custodian"] == "王采集" for g in v["gap_intervals"] for s in g["segments"]),
      str(v["gap_intervals"]))
# 补录发生时间更早的移交事件 → 归属即时重算
ev({"event_id": "EVT-TF-T1", "event_type": "transfer", "sample_code": "T-BACKFILL",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-TF",
    "from_custodian": "王采集", "to_custodian": "李运输"})
v = review("T-BACKFILL").json()
check("补录移交后缺测段按移交时刻拆分",
      kinds(v) == [["custodian", "in_transit"], ["in_transit"]], str(v["gap_intervals"]))
check("原始读数不被补录改写",
      [(x["reading_id"], x["value_celsius"]) for x in v["readings"]]
      == [("RD-F1", 4.0), ("RD-F2", 5.0), ("RD-F3", 6.0)], str(v["readings"]))

reg("T-CORR", collector="王采集")
cfg("T-CORR")
ev({"event_id": "EVT-TC-T1", "event_type": "transfer", "sample_code": "T-CORR",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-TC",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TC-R1", "event_type": "receive", "sample_code": "T-CORR",
    "occurred_at": "2026-09-30T05:00:00Z", "transfer_no": "TR-TC",
    "receiver": "李运输", "seal_no": "SEAL-T"})
rd("T-CORR", "RD-C1", "2026-09-30T01:30:00Z", 4.0)
rd("T-CORR", "RD-C2", "2026-09-30T04:00:00Z", 5.0)
rd("T-CORR", "RD-C3", "2026-09-30T06:00:00Z", 6.0)
v = review("T-CORR").json()
check("纠错前缺测段归属", kinds(v) == [["custodian", "in_transit"], ["in_transit", "custodian"]],
      str(v["gap_intervals"]))
# 替换移交事件：发生时间 02:00 → 03:00，拆分点与归属重算
r = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-TC-1", "sample_code": "T-CORR", "target_event_id": "EVT-TC-T1",
    "action": "replace", "operator": "质控员", "reason": "移交时间登记错误",
    "new_event": {"event_id": "EVT-TC-T2", "event_type": "transfer",
                  "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-TC",
                  "from_custodian": "王采集", "to_custodian": "李运输"}})
check("移交替换纠错 201", r.status_code == 201, r.text)
v = review("T-CORR").json()
check("移交替换后拆分点与归属重算",
      kinds(v) == [["custodian", "in_transit"], ["in_transit", "custodian"]]
      and v["gap_intervals"][0]["segments"][1]["start"] == "2026-09-30T03:00:00Z"
      and v["gap_intervals"][0]["segments"][1]["attribution"]["transfer_event_id"] == "EVT-TC-T2",
      str(v["gap_intervals"]))
# 作废签收 → 其后归属回到在途
r = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-TC-2", "sample_code": "T-CORR", "target_event_id": "EVT-TC-R1",
    "action": "void", "operator": "质控员", "reason": "签收记录作废"})
check("签收作废纠错 201", r.status_code == 201, r.text)
v = review("T-CORR").json()
check("签收作废后归属重算为在途",
      kinds(v) == [["custodian", "in_transit"], ["in_transit"]], str(v["gap_intervals"]))
check("纠错后原始读数仍不改",
      [(x["reading_id"], x["value_celsius"]) for x in v["readings"]]
      == [("RD-C1", 4.0), ("RD-C2", 5.0), ("RD-C3", 6.0)], str(v["readings"]))
c = chain("T-CORR")
check("旧纠错记录留痕不改", [x["correction_id"] for x in c["corrections"]] == ["FIX-TC-1", "FIX-TC-2"],
      str(c["corrections"]))

# ---------------------------------------------------------------- 6. 核定后拒绝新配置与读数
print("6) 核定后拒绝新配置与读数（同号同内容重试保持幂等）")
reg("T-APPROVE", collector="王采集")
cfg("T-APPROVE")
ev({"event_id": "EVT-TA-T1", "event_type": "transfer", "sample_code": "T-APPROVE",
    "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": "TR-TA",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TA-R1", "event_type": "receive", "sample_code": "T-APPROVE",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-TA",
    "receiver": "李运输", "seal_no": "SEAL-T"})
ev({"event_id": "EVT-TA-S1", "event_type": "storage", "sample_code": "T-APPROVE",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
rd("T-APPROVE", "RD-A1", "2026-09-30T02:30:00Z", 4.0)
r = client.post(f"{API}/samples/T-APPROVE/approve", json={"approver": "质量主管"})
check("有效链核定 200", r.status_code == 200, r.text)
r = rd("T-APPROVE", "RD-A2", "2026-09-30T03:30:00Z", 5.0)
check("核定后新读数 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)
r = rd("T-APPROVE", "RD-A1", "2026-09-30T02:30:00Z", 4.0)
check("核定后同号同内容读数重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = rd("T-APPROVE", "RD-A1", "2026-09-30T02:30:00Z", 9.9)
check("核定后同号异内容读数 409 reading_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "reading_conflict", r.text)
r = cfg("T-APPROVE")
check("核定后同内容配置重试 200 幂等", r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = cfg("T-APPROVE", hi=9.0)
check("核定后异内容配置 409 temp_config_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "temp_config_conflict", r.text)
r = review("T-APPROVE")
check("核定后复核查询仍可用", r.status_code == 200 and r.json()["reading_count"] == 1, r.text)

reg("T-APPROVE2", collector="王采集")
ev({"event_id": "EVT-TB2-S1", "event_type": "storage", "sample_code": "T-APPROVE2",
    "occurred_at": "2026-09-30T02:00:00Z", "custodian": "实验室"})
# 缺移交-签收链 → pending，先补链再核定
ev({"event_id": "EVT-TB2-T1", "event_type": "transfer", "sample_code": "T-APPROVE2",
    "occurred_at": "2026-09-30T01:30:00Z", "transfer_no": "TR-TA2",
    "from_custodian": "王采集", "to_custodian": "李运输"})
ev({"event_id": "EVT-TB2-R1", "event_type": "receive", "sample_code": "T-APPROVE2",
    "occurred_at": "2026-09-30T01:45:00Z", "transfer_no": "TR-TA2",
    "receiver": "李运输", "seal_no": "SEAL-T"})
r = client.post(f"{API}/samples/T-APPROVE2/approve", json={"approver": "质量主管"})
check("第二份样本核定 200", r.status_code == 200, r.text)
r = cfg("T-APPROVE2")
check("核定后新配置 409 history_locked",
      r.status_code == 409 and r.json()["error"]["code"] == "history_locked", r.text)

# ---------------------------------------------------------------- 7. 与既有结论及版本规则兼容
print("7) 测温数据不影响保管链核验结论、不产生保管链版本")
reg("T-VER", collector="王采集")
check("登记后版本 0", versions("T-VER")["current_version"] == 0, str(versions("T-VER")))
cfg("T-VER")
check("设定测温配置不产生版本", versions("T-VER")["current_version"] == 0, str(versions("T-VER")))
rd("T-VER", "RD-V1", "2026-09-30T02:00:00Z", 99.0)   # 严重超温
check("记录读数不产生版本", versions("T-VER")["current_version"] == 0, str(versions("T-VER")))
before = chain("T-VER")
check("超温读数不影响核验结论",
      before["status"] == "pending" and before["current_custodian"] == "王采集"
      and all("温" not in reason for reason in before["reasons"]), str(before["reasons"]))
ev({"event_id": "EVT-TV-T1", "event_type": "transfer", "sample_code": "T-VER",
    "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-TV",
    "from_custodian": "王采集", "to_custodian": "李运输"})
check("既有事件仍按原规则递增版本", versions("T-VER")["current_version"] == 1, str(versions("T-VER")))
rd("T-VER", "RD-V2", "2026-09-30T04:00:00Z", 5.0)
check("事件后再录读数仍不产生版本", versions("T-VER")["current_version"] == 1, str(versions("T-VER")))
v = review("T-VER").json()
check("复核查询反映最新有效链归属",
      v["out_of_range_points"][0]["attribution"] == {"kind": "custodian", "custodian": "王采集"},
      str(v["out_of_range_points"]))

# ---------------------------------------------------------------- 8. 空读数与边界
print("8) 空读数查询与缺测边界")
reg("T-EMPTY", collector="王采集")
cfg("T-EMPTY")
v = review("T-EMPTY").json()
check("无读数时查询 200 且结果为空",
      v["reading_count"] == 0 and v["readings"] == [] and v["out_of_range_points"] == []
      and v["gap_intervals"] == [], str(v))
rd("T-EMPTY", "RD-E1", "2026-09-30T02:00:00Z", 4.0)
v = review("T-EMPTY").json()
check("单条读数无缺测区间", v["reading_count"] == 1 and v["gap_intervals"] == [], str(v))
rd("T-EMPTY", "RD-E2", "2026-09-30T02:30:00Z", 4.0)   # 间隔恰为限值 1800s
v = review("T-EMPTY").json()
check("间隔等于限值不报缺测", v["gap_intervals"] == [], str(v["gap_intervals"]))
rd("T-EMPTY", "RD-E3", "2026-09-30T03:00:01Z", 4.0)   # 间隔 1801s > 限值
v = review("T-EMPTY").json()
check("间隔超过限值 1 秒即报缺测",
      len(v["gap_intervals"]) == 1 and v["gap_intervals"][0]["duration_seconds"] == 1801,
      str(v["gap_intervals"]))

# ---------------------------------------------------------------- 9. 并发
print("9) 并发：同号同内容一建一幂等；同号异内容/同时刻一胜一败")
reg("T-CONC", collector="王采集")
cfg("T-CONC")
results: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_reading(tag, rid, when, value):
    barrier.wait()
    results[tag] = rd("T-CONC", rid, when, value).status_code


t1 = threading.Thread(target=post_reading, args=("A", "RD-CC1", "2026-09-30T02:00:00Z", 4.0))
t2 = threading.Thread(target=post_reading, args=("B", "RD-CC1", "2026-09-30T02:00:00Z", 4.0))
t1.start(); t2.start(); t1.join(); t2.join()
check("同读数号同内容并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=post_reading, args=("C", "RD-CC2", "2026-09-30T03:00:00Z", 4.0))
t2 = threading.Thread(target=post_reading, args=("D", "RD-CC2", "2026-09-30T03:00:00Z", 5.0))
t1.start(); t2.start(); t1.join(); t2.join()
check("同读数号异内容并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))

results.clear()
barrier = threading.Barrier(2)
t1 = threading.Thread(target=post_reading, args=("E", "RD-CC3", "2026-09-30T04:00:00Z", 4.0))
t2 = threading.Thread(target=post_reading, args=("F", "RD-CC4", "2026-09-30T04:00:00Z", 4.0))
t1.start(); t2.start(); t1.join(); t2.join()
check("同时刻异读数号并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))
v = review("T-CONC").json()
check("并发后读数落库 3 条（每对竞争恰一条胜出）",
      v["reading_count"] == 3
      and sorted(x["reading_id"] for x in v["readings"]) == ["RD-CC1", "RD-CC2"]
      + [rid for rid in ("RD-CC3", "RD-CC4") if any(x["reading_id"] == rid for x in v["readings"])],
      str(v["readings"]))

results.clear()
barrier = threading.Barrier(2)


def post_config(tag, hi):
    barrier.wait()
    results[tag] = cfg("T-CONC2", hi=hi).status_code


reg("T-CONC2", collector="王采集")
t1 = threading.Thread(target=post_config, args=("G", 8.0))
t2 = threading.Thread(target=post_config, args=("H", 8.0))
t1.start(); t2.start(); t1.join(); t2.join()
check("同内容配置并发：一 201 一 200", sorted(results.values()) == [200, 201], str(results))

results.clear()
barrier = threading.Barrier(2)
reg("T-CONC3", collector="王采集")
t1 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__("I", cfg("T-CONC3", hi=8.0).status_code)))
t2 = threading.Thread(target=lambda: (barrier.wait(), results.__setitem__("J", cfg("T-CONC3", hi=9.0).status_code)))
t1.start(); t2.start(); t1.join(); t2.join()
check("异内容配置并发：一 201 一 409", sorted(results.values()) == [201, 409], str(results))

check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
