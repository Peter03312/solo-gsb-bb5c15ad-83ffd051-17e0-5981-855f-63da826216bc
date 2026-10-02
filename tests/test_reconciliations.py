"""端到端自测：批量移交到达接收方后的整批对账签收（batch reconciliation）。

运行：python -m tests.test_reconciliations
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_reconciliations.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session, init_db  # noqa: E402
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


def reg(code, collector="张三", seal=None, when="2026-09-30T01:00:00Z"):
    return client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": when,
        "collector": collector, "seal_no": seal or f"SEAL-{code}"})


def ev(payload):
    return client.post(f"{API}/events", json=payload)


def chain(code):
    return client.get(f"{API}/samples/{code}/chain").json()


def versions(code):
    return client.get(f"{API}/samples/{code}/versions").json()


def make_batch(bid="BATCH-1", codes=("R1", "R2", "R3"), when="2026-09-30T02:00:00Z",
               frm="张三", to="李四", prefix="T"):
    items = [{"sample_code": c, "transfer_no": f"TR-{c}", "event_id": f"{prefix}-{c}"} for c in codes]
    return client.post(f"{API}/transfer-batches", json={
        "batch_id": bid, "occurred_at": when,
        "from_custodian": frm, "to_custodian": to, "items": items})


def recon(bid, body):
    return client.post(f"{API}/transfer-batches/{bid}/reconciliation", json=body)


def get_recon(bid):
    return client.get(f"{API}/transfer-batches/{bid}/reconciliation")


def recv_item(code, eid=None, seal=None):
    return {"sample_code": code, "event_id": eid or f"EV-R-{code}",
            "verified_seal_no": seal or f"SEAL-{code}"}


def exc_item(code, reason="封签破损，异常拒收"):
    return {"sample_code": code, "reason": reason}


def rec_body(no="REC-1", receiver="李四", when="2026-09-30T03:00:00Z", items=None):
    return {"reconciliation_no": no, "receiver": receiver, "occurred_at": when,
            "items": items if items is not None else [recv_item("R1"), recv_item("R2"), recv_item("R3")]}


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def err_codes(r):
    return {x["code"] for x in r.json().get("error", {}).get("details", {}).get("items", [])}


# ---------------------------------------------------------------- 1. 正常整批对账：收妥 + 异常混合
print("1) 整批对账 201：收妥项落为既有 receive 事件并升版本，异常项仅留记录")
for c in ("R1", "R2", "R3"):
    reg(c)
r = make_batch()
check("前置批量移交 201", r.status_code == 201, r.text)

body = rec_body(items=[recv_item("R1"), recv_item("R2"), exc_item("R3")])
r = recon("BATCH-1", body)
check("对账提交 201", r.status_code == 201, r.text)
d = r.json()
check("顶层 idempotent=false", d["idempotent"] is False, r.text)
recd = d["reconciliation"]
check("对账头回显（单号/批次/签收人/时间/份数）",
      recd["reconciliation_no"] == "REC-1" and recd["batch_id"] == "BATCH-1"
      and recd["receiver"] == "李四" and recd["occurred_at"] == "2026-09-30T03:00:00Z"
      and recd["item_count"] == 3 and recd["received_count"] == 2
      and recd["exception_count"] == 1, str(recd))
check("明细按原批次顺序、outcome 正确",
      [(i["index"], i["sample_code"], i["outcome"]) for i in recd["items"]]
      == [(0, "R1", "received"), (1, "R2", "received"), (2, "R3", "exception")],
      str(recd["items"]))
for i, code in enumerate(("R1", "R2")):
    it = recd["items"][i]
    check(f"{code} 收妥项回显事件号/实核封签/无原因，event_effective=true",
          it["event_id"] == f"EV-R-{code}" and it["verified_seal_no"] == f"SEAL-{code}"
          and it["reason"] is None and it["event_effective"] is True
          and it["superseded_by"] is None and it["current_event_id"] == f"EV-R-{code}",
          str(it))
    c = chain(code)
    check(f"{code} receive 已进入有效时间线，保管人变更为李四（既有 receive 事件）",
          [e["event_type"] for e in c["timeline"]] == ["transfer", "receive"]
          and c["current_custodian"] == "李四"
          and c["timeline"][1]["event_id"] == f"EV-R-{code}"
          and c["timeline"][1]["payload"]["transfer_no"] == f"TR-{code}"
          and c["timeline"][1]["batch_id"] is None, str(c["timeline"]))
    check(f"{code} 版本：登记→批量移交→收妥事件（+1）",
          [v["change_type"] for v in versions(code)["items"]]
          == ["register", "batch_transfer", "event"]
          and versions(code)["current_version"] == 2, str(versions(code)))
it3 = recd["items"][2]
check("R3 异常项回显原因/无事件字段，现时有效性为 null",
      it3["event_id"] is None and it3["verified_seal_no"] is None
      and it3["reason"] == "封签破损，异常拒收"
      and it3["event_effective"] is None and it3["current_event_id"] is None, str(it3))
c = chain("R3")
check("R3 异常项不生成签收事件（时间线仅 1 条 transfer，仍在途 pending，保管人不变）",
      len(c["timeline"]) == 1 and c["timeline"][0]["event_type"] == "transfer"
      and c["status"] == "pending" and c["current_custodian"] == "张三", str(c["timeline"]))
check("R3 异常项不产生版本（仍为版本 1）",
      [v["change_type"] for v in versions("R3")["items"]] == ["register", "batch_transfer"]
      and versions("R3")["current_version"] == 1, str(versions("R3")))
check("收妥项即时结论 pending（缺入库）",
      all(i["evaluation"]["status"] == "pending" for i in recd["items"][:2]), str(recd))

# 对账查询
r = get_recon("BATCH-1")
check("对账查询 200 且与提交视图一致",
      r.status_code == 200
      and [(i["sample_code"], i["outcome"]) for i in r.json()["reconciliation"]["items"]]
      == [("R1", "received"), ("R2", "received"), ("R3", "exception")], r.text)
# 批次查询附带对账摘要
b = client.get(f"{API}/transfer-batches/BATCH-1").json()["batch"]
check("批次查询 reconciliation 摘要指向 REC-1/收妥2异常1",
      b["reconciliation"] is not None and b["reconciliation"]["reconciliation_no"] == "REC-1"
      and b["reconciliation"]["received_count"] == 2
      and b["reconciliation"]["exception_count"] == 1, str(b.get("reconciliation")))

# ---------------------------------------------------------------- 2. 整单拒绝：不留任何记录
print("2) 整单拒绝：批次不存在/缺项/重复/外来项/事件号占用/收妥项不合格，均不留记录")

# 2.1 批次不存在
r = recon("NO-SUCH-BATCH", rec_body("REC-X"))
check("批次不存在 404 batch_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "batch_not_found", r.text)
check("不存在批次不占签收单号",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation WHERE reconciliation_no='REC-X'") == 0)

# 2.2 缺项
for c in ("M1", "M2", "M3"):
    reg(c)
make_batch("BATCH-M", codes=("M1", "M2", "M3"), prefix="MT")
r = recon("BATCH-M", {"reconciliation_no": "REC-M", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("M1", "MR-1"), recv_item("M2", "MR-2")]})
check("缺项 422 missing_sample_in_reconciliation",
      r.status_code == 422 and r.json()["error"]["code"] == "reconciliation_rejected"
      and err_codes(r) == {"missing_sample_in_reconciliation"}, r.text)
check("失败后可查询为无对账（404 reconciliation_not_found）",
      get_recon("BATCH-M").status_code == 404
      and get_recon("BATCH-M").json()["error"]["code"] == "reconciliation_not_found")
check("失败不留 receive 事件（M1/M2 时间线仍 1 条）",
      all(len(chain(c)["timeline"]) == 1 for c in ("M1", "M2", "M3")))
check("失败不留对账头/明细",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation") == 1
      and db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item") == 3)
check("失败不产生版本（M1/M2 仍版本 1）",
      all(versions(c)["current_version"] == 1 for c in ("M1", "M2", "M3")))

# 2.3 重复份项 + 外来项
r = recon("BATCH-M", {"reconciliation_no": "REC-M", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("M1", "MR-1"), recv_item("M1", "MR-1B"),
                                recv_item("GHOST", "MR-G"), exc_item("M2"), exc_item("M3")]})
check("重复份项 + 外来项 422（逐项给出）",
      r.status_code == 422
      and err_codes(r) == {"duplicate_sample_in_reconciliation", "unknown_sample_in_reconciliation"},
      r.text)

# 2.4 批内事件号重复
r = recon("BATCH-M", {"reconciliation_no": "REC-M", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("M1", "DUP-R"), recv_item("M2", "DUP-R"),
                                exc_item("M3")]})
check("批内收妥事件号重复 422 duplicate_event_in_reconciliation",
      r.status_code == 422
      and "duplicate_event_in_reconciliation" in err_codes(r), r.text)

# 2.5 收妥事件号已被占用（复用批内移交事件号 MT-M1）→ 整单 409
r = recon("BATCH-M", {"reconciliation_no": "REC-M", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("M1", "MT-M1"), recv_item("M2", "MR-2"),
                                exc_item("M3")]})
check("事件号已占用 409 reconciliation_rejected/event_id_occupied（冲突优先）",
      r.status_code == 409 and r.json()["error"]["code"] == "reconciliation_rejected"
      and err_codes(r) == {"event_id_occupied"}, r.text)
check("409 失败也不留记录",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation WHERE reconciliation_no='REC-M'") == 0
      and len(chain("M1")["timeline"]) == 1)

# 2.6 收妥项不合格：签收人不符 / 封签不符 / 时间不晚于移交
for c in ("Q1", "Q2", "Q3", "Q4"):
    reg(c)
make_batch("BATCH-Q", codes=("Q1", "Q2", "Q3", "Q4"), prefix="QT")
# 错误签收人：Q1 同时封签不符，Q2/Q3 签收人不符
r2 = recon("BATCH-Q", {"reconciliation_no": "REC-Q", "receiver": "王五",
                       "occurred_at": "2026-09-30T03:00:00Z",
                       "items": [recv_item("Q1", "QR-1", seal="SEAL-WRONG"),
                                 recv_item("Q2", "QR-2"), recv_item("Q3", "QR-3"),
                                 exc_item("Q4")]})
check("签收人不符 + 封签不符聚合 422",
      r2.status_code == 422
      and err_codes(r2) == {"receiver_mismatch", "seal_mismatch"}, r2.text)
# 签收时间不晚于移交（等于移交时间）
r3 = recon("BATCH-Q", {"reconciliation_no": "REC-Q", "receiver": "李四",
                       "occurred_at": "2026-09-30T02:00:00Z",
                       "items": [recv_item("Q1", "QR-1"), recv_item("Q2", "QR-2"),
                                 recv_item("Q3", "QR-3"), exc_item("Q4")]})
check("签收时间=移交时间 422 receive_time_invalid（三份收妥均不合格）",
      r3.status_code == 422 and err_codes(r3) == {"receive_time_invalid"}, r3.text)
# 签收时间早于采集
r4 = recon("BATCH-Q", {"reconciliation_no": "REC-Q", "receiver": "李四",
                       "occurred_at": "2026-09-30T00:30:00Z",
                       "items": [recv_item("Q1", "QR-1"), recv_item("Q2", "QR-2"),
                                 recv_item("Q3", "QR-3"), exc_item("Q4")]})
check("签收时间早于采集 422 receive_time_invalid",
      r4.status_code == 422 and "receive_time_invalid" in err_codes(r4), r4.text)
check("任一收妥项不合格整单拒绝：Q1-Q3 均无 receive 事件/版本不增",
      all(len(chain(c)["timeline"]) == 1 and versions(c)["current_version"] == 1
          for c in ("Q1", "Q2", "Q3", "Q4")))
check("不合格整单不留对账记录，批次可再对账",
      get_recon("BATCH-Q").status_code == 404
      and db_scalar("SELECT COUNT(*) FROM batch_reconciliation") == 1)

# 2.7 载荷形态校验（异常项带事件号 / 收妥项缺封签 / 空原因）
for body_bad, label in [
    ({"reconciliation_no": "REC-BAD", "receiver": "李四",
      "occurred_at": "2026-09-30T03:00:00Z",
      "items": [{"sample_code": "Q1", "reason": "破损", "event_id": "X"}]},
     "异常项携带 event_id 422"),
    ({"reconciliation_no": "REC-BAD", "receiver": "李四",
      "occurred_at": "2026-09-30T03:00:00Z",
      "items": [{"sample_code": "Q1", "event_id": "X"}]},
     "收妥项缺 verified_seal_no 422"),
    ({"reconciliation_no": "REC-BAD", "receiver": "李四",
      "occurred_at": "2026-09-30T03:00:00Z",
      "items": [{"sample_code": "Q1", "verified_seal_no": "SEAL-Q1"}]},
     "收妥项缺 event_id 422"),
    ({"reconciliation_no": "REC-BAD", "receiver": "李四",
      "occurred_at": "2026-09-30T03:00:00Z",
      "items": [{"sample_code": "Q1", "reason": "   "}]},
     "空原因 422"),
    ({"reconciliation_no": "", "receiver": "李四",
      "occurred_at": "2026-09-30T03:00:00Z", "items": []},
     "空单号/空清单 422"),
]:
    rr = recon("BATCH-Q", body_bad)
    check(label, rr.status_code == 422, rr.text)

# ---------------------------------------------------------------- 3. 在途状态与有效性：单签先到 / 作废 / 替换
print("3) 收妥项须对应仍有效且在途的该笔移交事件")

# 3.1 已被单份签收关闭在途 → 收妥项 transfer_not_in_transit（异常项仍可提）
for c in ("U1", "U2"):
    reg(c)
make_batch("BATCH-U", codes=("U1", "U2"), prefix="UT")
sr = ev({"event_id": "U-R-SINGLE", "event_type": "receive", "sample_code": "U1",
         "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-U1",
         "receiver": "李四", "seal_no": "SEAL-U1"})
check("前置单份签收 201", sr.status_code == 201, sr.text)
r = recon("BATCH-U", {"reconciliation_no": "REC-U", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("U1", "U-R-BATCH"), exc_item("U2")]})
check("已签收关闭在途：收妥项 422 transfer_not_in_transit",
      r.status_code == 422 and err_codes(r) == {"transfer_not_in_transit"}, r.text)
# U1 改异常、U2 收妥 → 成功（异常不受在途约束）
r = recon("BATCH-U", {"reconciliation_no": "REC-U", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [exc_item("U1", "已在现场单签，列入异常说明"),
                                recv_item("U2", "U2-R-BATCH")]})
check("异常项不要求在途：U1 异常 + U2 收妥 201", r.status_code == 201, r.text)
check("U1 不重复签收（时间线仍为单份那条 receive），U2 新增批量对账 receive",
      [e["event_id"] for e in chain("U1")["timeline"] if e["event_type"] == "receive"]
      == ["U-R-SINGLE"]
      and [e["event_id"] for e in chain("U2")["timeline"] if e["event_type"] == "receive"]
      == ["U2-R-BATCH"], str(r.text))

# 3.2 批次移交被作废（无替换后继）→ 收妥项 transfer_not_effective
for c in ("V1", "V2"):
    reg(c)
make_batch("BATCH-V", codes=("V1", "V2"), prefix="VT")
cr = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-V1", "sample_code": "V1", "target_event_id": "VT-V1",
    "action": "void", "operator": "质控员", "reason": "误发车次，作废移交"})
check("前置作废 V1 移交 201", cr.status_code == 201, cr.text)
r = recon("BATCH-V", {"reconciliation_no": "REC-V", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("V1", "V1-R"), recv_item("V2", "V2-R")]})
check("移交已作废：收妥项 422 transfer_not_effective",
      r.status_code == 422 and err_codes(r) == {"transfer_not_effective"}, r.text)
# V1 只能按异常项提交，整批成功
r = recon("BATCH-V", {"reconciliation_no": "REC-V", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [exc_item("V1", "原移交已作废，无有效在途事件"),
                                recv_item("V2", "V2-R")]})
check("作废份按异常提交 201", r.status_code == 201, r.text)

# 3.3 批次移交被替换为新移交（新单号/新接收人）：收妥按当前有效在途事件判定
for c in ("W1", "W2"):
    reg(c)
make_batch("BATCH-W", codes=("W1", "W2"), prefix="WT")
cr = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-W1", "sample_code": "W1", "target_event_id": "WT-W1",
    "action": "replace", "operator": "质控员", "reason": "接收方录错，替换移交",
    "new_event": {"event_id": "WT-W1-FIX", "event_type": "transfer",
                  "occurred_at": "2026-09-30T02:10:00Z",
                  "transfer_no": "TR-W1-FIX", "from_custodian": "张三",
                  "to_custodian": "周五"}})
check("前置替换 W1 移交（接收人→周五）201", cr.status_code == 201, cr.text)
r = recon("BATCH-W", {"reconciliation_no": "REC-W", "receiver": "李四",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("W1", "W1-R"), recv_item("W2", "W2-R")]})
check("替换后接收人变化：李四对 W1 收妥 422 receiver_mismatch（W2 合格也整单拒绝）",
      r.status_code == 422 and err_codes(r) == {"receiver_mismatch"}, r.text)
check("整单失败 W2 也无 receive", len(chain("W2")["timeline"]) == 1)
r = recon("BATCH-W", {"reconciliation_no": "REC-W", "receiver": "周五",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("W1", "W1-R"),
                                {"sample_code": "W2", "event_id": "W2-R",
                                 "verified_seal_no": "SEAL-W2"}]})
# W2 的接收人仍是李四：统一签收人周五对 W2 不合格 → 整单仍失败
check("统一签收人周五对 W2 不合格：422 receiver_mismatch 整单拒绝",
      r.status_code == 422 and err_codes(r) == {"receiver_mismatch"}, r.text)
# W2 改异常 → 周五对 W1 收妥成功，receive 落的是替换后的新单号 TR-W1-FIX
r = recon("BATCH-W", {"reconciliation_no": "REC-W", "receiver": "周五",
                      "occurred_at": "2026-09-30T03:00:00Z",
                      "items": [recv_item("W1", "W1-R"), exc_item("W2", "接收人不一致，异常")]})
check("W1 按替换后有效移交收妥 201", r.status_code == 201, r.text)
w1_recv = [e for e in chain("W1")["timeline"] if e["event_type"] == "receive"]
check("W1 receive 匹配替换后新移交单号、签收人周五、保管人=周五",
      len(w1_recv) == 1 and w1_recv[0]["payload"]["transfer_no"] == "TR-W1-FIX"
      and w1_recv[0]["payload"]["receiver"] == "周五"
      and chain("W1")["current_custodian"] == "周五", str(w1_recv))
# 对账明细 transfer_no 永久留原批次单号（不改写原对账）
it_w1 = next(i for i in get_recon("BATCH-W").json()["reconciliation"]["items"]
             if i["sample_code"] == "W1")
check("对账明细保留原批次单号 TR-W1（留痕）", it_w1["transfer_no"] == "TR-W1", str(it_w1))

# ---------------------------------------------------------------- 4. 幂等 / 冲突 / 一次性
print("4) 同单号同内容 200 幂等（换序/时区归一）；异内容 409；批次一次性；跨批次单号冲突")
# 4.1 换序幂等
reordered = {"reconciliation_no": "REC-1", "receiver": "李四",
             "occurred_at": "2026-09-30T03:00:00Z",
             "items": [exc_item("R3"), recv_item("R2"), recv_item("R1")]}
r = recon("BATCH-1", reordered)
check("items 换序同内容 200 idempotent=true",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
# 4.2 时区表示归一
tz_body = {"reconciliation_no": "REC-TZ", "receiver": "李四",
           "occurred_at": "2026-09-30T11:00:00+08:00",
           "items": [recv_item("M1", "TZ-1"), recv_item("M2", "TZ-2"), exc_item("M3")]}
# BATCH-M 尚未对账（此前均失败）→ 首次 201
r1 = recon("BATCH-M", tz_body)
check("带偏移时间首次对账 201", r1.status_code == 201, r1.text)
tz_body["occurred_at"] = "2026-09-30T03:00:00Z"
check("等效 UTC 时间重试 200 幂等", recon("BATCH-M", tz_body).status_code == 200)
# 不产生重复事件/版本
check("幂等不产生重复 receive（M1 时间线 2 条、版本 2）",
      len(chain("M1")["timeline"]) == 2 and versions("M1")["current_version"] == 2)

# 4.3 同单号异内容（改实核封签）冲突
diff = {"reconciliation_no": "REC-1", "receiver": "李四",
        "occurred_at": "2026-09-30T03:00:00Z",
        "items": [recv_item("R1", seal="SEAL-R1-CHANGED"), recv_item("R2"), exc_item("R3")]}
r = recon("BATCH-1", diff)
check("同单号异内容 409 reconciliation_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "reconciliation_conflict", r.text)
# 改签收人 / 时间 / 收妥↔异常 形态变化均属异内容
for mut, label in [
    (lambda b: b.update(receiver="王五"), "改签收人"),
    (lambda b: b.update(occurred_at="2026-09-30T03:30:00Z"), "改签收时间"),
    (lambda b: b.update(items=[recv_item("R1"), recv_item("R2"), recv_item("R3", "R3-NEW")]),
     "异常改收妥"),
]:
    b2 = rec_body()
    mut(b2)
    check(f"{label} 409", recon("BATCH-1", b2).status_code == 409)

# 4.4 同单号跨批次复用 → 冲突（即使内容恰好同形态）
r = recon("BATCH-Q", rec_body("REC-1", items=[recv_item("Q1", "QR-1"), recv_item("Q2", "QR-2"),
                                             recv_item("Q3", "QR-3"), exc_item("Q4")]))
check("同单号跨批次复用 409 reconciliation_conflict（详情给原批次）",
      r.status_code == 409 and r.json()["error"]["code"] == "reconciliation_conflict"
      and r.json()["error"]["details"]["existing_batch_id"] == "BATCH-1", r.text)

# 4.5 批次已对账：新单号新内容 → batch_already_reconciled
r = recon("BATCH-1", rec_body("REC-OTHER"))
check("批次已对账再提交 409 batch_already_reconciled（详情给原单号）",
      r.status_code == 409 and r.json()["error"]["code"] == "batch_already_reconciled"
      and r.json()["error"]["details"]["reconciliation_no"] == "REC-1", r.text)

# 4.6 失败过的签收单号修正后可复用（BATCH-Q 此前从未成功对账）
ok_q = {"reconciliation_no": "REC-1-Q", "receiver": "李四",
        "occurred_at": "2026-09-30T03:00:00Z",
        "items": [recv_item("Q1", "QR-1"), recv_item("Q2", "QR-2"),
                  recv_item("Q3", "QR-3"), exc_item("Q4")]}
r = recon("BATCH-Q", ok_q)
check("失败过的单号/批次修正后成功 201", r.status_code == 201, r.text)

# ---------------------------------------------------------------- 5. 核定交互与已核定
print("5) 已核定样本的收妥项 409 sample_approved；核定后同号同内容仍幂等")
for c in ("K1", "K2"):
    reg(c)
make_batch("BATCH-K", codes=("K1", "K2"), prefix="KT")
# K1 补齐链并核定（移交后立即单签 → 再检查入库核定）
ev({"event_id": "K1-R", "event_type": "receive", "sample_code": "K1",
    "occurred_at": "2026-09-30T02:30:00Z", "transfer_no": "TR-K1",
    "receiver": "李四", "seal_no": "SEAL-K1"})
ev({"event_id": "K1-C", "event_type": "seal_check", "sample_code": "K1",
    "occurred_at": "2026-09-30T02:45:00Z", "checked_seal_no": "SEAL-K1", "seal_intact": True})
ev({"event_id": "K1-S", "event_type": "storage", "sample_code": "K1",
    "occurred_at": "2026-09-30T03:30:00Z", "custodian": "实验室"})
# K1 的批量移交 KT-K1 已被单签关闭在途（在途再移交？不：单签匹配 TR-K1，批量是另一个单号 TR-K1？
# 批量移交单号也是 TR-K1 → 单签关闭的正是批量这笔）。核定：
ar = client.post(f"{API}/samples/K1/approve", json={"approver": "质量主管"})
check("K1 核定成功", ar.status_code == 200, ar.text)
r = recon("BATCH-K", {"reconciliation_no": "REC-K", "receiver": "李四",
                      "occurred_at": "2026-09-30T04:00:00Z",
                      "items": [recv_item("K1", "K1-RR"), exc_item("K2")]})
check("已核定样本的收妥项 409 sample_approved（冲突优先，整单不留）",
      r.status_code == 409 and err_codes(r) == {"sample_approved"}, r.text)
# 已核定份按异常提交、K2 收妥 → 成功（异常仅留记录，不触及冻结历史）
r = recon("BATCH-K", {"reconciliation_no": "REC-K", "receiver": "李四",
                      "occurred_at": "2026-09-30T04:00:00Z",
                      "items": [exc_item("K1", "样本此前已完成单签并核定入库，列异常说明"),
                                recv_item("K2", "K2-R")]})
check("已核定份按异常提交不写历史：201", r.status_code == 201, r.text)
check("K1 事件数不变（4）、无对账 receive",
      len(chain("K1")["timeline"]) == 4, str(len(chain("K1")["timeline"])))

# 核定后同单号同内容重试仍 200 幂等（对账查询照回原结果）
r = recon("BATCH-K", {"reconciliation_no": "REC-K", "receiver": "李四",
                      "occurred_at": "2026-09-30T04:00:00Z",
                      "items": [exc_item("K1", "样本此前已完成单签并核定入库，列异常说明"),
                                recv_item("K2", "K2-R")]})
check("核定后同单号同内容重试 200", r.status_code == 200 and r.json()["idempotent"] is True)

# ---------------------------------------------------------------- 6. 查询呈现现时有效性，不改写原对账
print("6) 纠错后查询：原对账收妥/异常明细不变，event_effective/current_event_id 反映现时")
d0 = get_recon("BATCH-1").json()["reconciliation"]
r1_before = next(i for i in d0["items"] if i["sample_code"] == "R1")
check("纠错前 R1 收妥事件有效", r1_before["event_effective"] is True
      and r1_before["current_event_id"] == "EV-R-R1")
# 作废 R1 的对账 receive
cr = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-R1-RECV", "sample_code": "R1", "target_event_id": "EV-R-R1",
    "action": "void", "operator": "质控员", "reason": "对账签收后发现封签复核异常，作废签收"})
check("作废对账 receive 201", cr.status_code == 201, cr.text)
d1 = get_recon("BATCH-1").json()["reconciliation"]
it = next(i for i in d1["items"] if i["sample_code"] == "R1")
check("查询反映 R1 收妥事件已失效（event_effective=false, current_event_id=null）",
      it["event_effective"] is False and it["superseded_by"] == "FIX-R1-RECV"
      and it["current_event_id"] is None, str(it))
check("原对账留痕不变：仍为 received、原事件号/实核封签/份数不变",
      it["outcome"] == "received" and it["event_id"] == "EV-R-R1"
      and it["verified_seal_no"] == "SEAL-R1"
      and d1["received_count"] == 2 and d1["exception_count"] == 1, str(it))
check("逐项 evaluation 为现时重算（R1 作废后保管人回到张三、重新在途 pending）",
      it["evaluation"]["current_custodian"] == "张三"
      and it["evaluation"]["status"] == "pending", str(it["evaluation"]))
# 异常项不受纠错影响
it3 = next(i for i in d1["items"] if i["sample_code"] == "R3")
check("R3 异常项字段恒定（无事件、原因留痕）",
      it3["outcome"] == "exception" and it3["event_id"] is None
      and it3["event_effective"] is None and it3["reason"] == "封签破损，异常拒收")
# 替换 R2 的对账 receive：current_event_id 指向新版本
cr2 = client.post(f"{API}/corrections", json={
    "correction_id": "FIX-R2-RECV", "sample_code": "R2", "target_event_id": "EV-R-R2",
    "action": "replace", "operator": "质控员", "reason": "封签扫码错误，替换签收",
    "new_event": {"event_id": "EV-R-R2-FIX", "event_type": "receive",
                  "occurred_at": "2026-09-30T03:10:00Z",
                  "transfer_no": "TR-R2", "receiver": "李四", "seal_no": "SEAL-R2"}})
check("替换对账 receive 201", cr2.status_code == 201, cr2.text)
d2 = get_recon("BATCH-1").json()["reconciliation"]
it2 = next(i for i in d2["items"] if i["sample_code"] == "R2")
check("查询反映 R2 current_event_id 指向替换版本，原收妥明细仍记 EV-R-R2",
      it2["event_effective"] is False and it2["current_event_id"] == "EV-R-R2-FIX"
      and it2["event_id"] == "EV-R-R2", str(it2))
check("对账明细行数不随纠错增减（始终 3 行）",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item WHERE reconciliation_pk="
                " (SELECT id FROM batch_reconciliation WHERE reconciliation_no='REC-1')") == 3)

# ---------------------------------------------------------------- 7. 与既有单事件接口契约
print("7) 对账 receive 事件走既有单事件接口：同内容 200 / 异内容 409；核定冻结仍生效")
same = ev({"event_id": "EV-R-R1", "event_type": "receive", "sample_code": "R1",
           "occurred_at": "2026-09-30T03:00:00Z", "transfer_no": "TR-R1",
           "receiver": "李四", "seal_no": "SEAL-R1"})
# EV-R-R1 已被作废，但同内容仍幂等 200（既有规则：失效历史事件同号同内容照样幂等）
check("单事件接口对对账事件号同内容 200（含已作废历史）", same.status_code == 200, same.text)
diffr = ev({"event_id": "EV-R-R2", "event_type": "receive", "sample_code": "R2",
            "occurred_at": "2026-09-30T03:01:00Z", "transfer_no": "TR-R2",
            "receiver": "李四", "seal_no": "SEAL-R2"})
check("单事件接口对异内容 409 event_conflict",
      diffr.status_code == 409 and diffr.json()["error"]["code"] == "event_conflict", diffr.text)
# 核定后写入冻结
w = ev({"event_id": "K1-W", "event_type": "storage", "sample_code": "K1",
        "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"})
check("核定样本经任意入口写入仍 409 history_locked",
      w.status_code == 409 and w.json()["error"]["code"] == "history_locked", w.text)

# ---------------------------------------------------------------- 8. 并发
print("8) 并发：同单号同内容一建一幂等；不同单号一个成功一个 batch_already_reconciled")
for c in ("C1", "C2", "C3"):
    reg(c)
make_batch("BATCH-C", codes=("C1", "C2", "C3"), prefix="CT")
body_c = rec_body("REC-C", items=[recv_item("C1", "C-R1"), recv_item("C2", "C-R2"),
                                  exc_item("C3")])
results_c: dict[str, int] = {}
barrier = threading.Barrier(2)


def post_c(tag):
    barrier.wait()
    results_c[tag] = recon("BATCH-C", body_c).status_code


t1 = threading.Thread(target=post_c, args=("A",))
t2 = threading.Thread(target=post_c, args=("B",))
t1.start(); t2.start(); t1.join(); t2.join()
check("同单号同内容并发：一次 201 一次 200",
      sorted(results_c.values()) == [200, 201], str(results_c))
check("并发后仅一条对账头/3 条明细/2 条 receive",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation WHERE batch_pk="
                " (SELECT id FROM transfer_batch WHERE batch_id='BATCH-C')") == 1
      and db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item WHERE reconciliation_pk="
                    " (SELECT id FROM batch_reconciliation WHERE reconciliation_no='REC-C')") == 3
      and db_scalar("SELECT COUNT(*) FROM custody_event WHERE event_id IN ('C-R1','C-R2')") == 2)

# 不同单号竞争同一批次
for c in ("D1", "D2"):
    reg(c)
make_batch("BATCH-D", codes=("D1", "D2"), prefix="DT")
body_da = rec_body("REC-DA", items=[recv_item("D1", "DA-R1"), exc_item("D2")])
body_db = rec_body("REC-DB", items=[exc_item("D1"), recv_item("D2", "DB-R2")])
results_d: dict[str, int] = {}
barrier2 = threading.Barrier(2)


def post_d(tag, body):
    barrier2.wait()
    results_d[tag] = recon("BATCH-D", body).status_code


t1 = threading.Thread(target=post_d, args=("A", body_da))
t2 = threading.Thread(target=post_d, args=("B", body_db))
t1.start(); t2.start(); t1.join(); t2.join()
check("不同单号并发：一个 201 一个 409",
      sorted(results_d.values()) == [201, 409], str(results_d))
check("落败方单号不留对账头、其 receive 事件不落库",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation") == 9
      and db_scalar("SELECT COUNT(*) FROM custody_event WHERE event_id IN ('DA-R1','DB-R2')") == 1)
winner_no = "REC-DA" if results_d["A"] == 201 else "REC-DB"
check("批次查询与对账查询均指向获胜单号",
      client.get(f"{API}/transfer-batches/BATCH-D").json()["batch"]["reconciliation"]["reconciliation_no"]
      == winner_no
      and get_recon("BATCH-D").json()["reconciliation"]["reconciliation_no"] == winner_no)

# ---------------------------------------------------------------- 9. 全收妥 / 全异常边界
print("9) 边界：全收妥补齐后可核定；全异常仅留记录")
for c in ("Z1",):
    reg(c)
make_batch("BATCH-Z", codes=("Z1",), prefix="ZT")
r = recon("BATCH-Z", rec_body("REC-Z", items=[recv_item("Z1", "Z-R1")]))
check("全收妥（单份批次）201，received_count=1/exception_count=0",
      r.status_code == 201
      and r.json()["reconciliation"]["received_count"] == 1
      and r.json()["reconciliation"]["exception_count"] == 0, r.text)
ev({"event_id": "Z-C1", "event_type": "seal_check", "sample_code": "Z1",
    "occurred_at": "2026-09-30T03:30:00Z", "checked_seal_no": "SEAL-Z1", "seal_intact": True})
ev({"event_id": "Z-S1", "event_type": "storage", "sample_code": "Z1",
    "occurred_at": "2026-09-30T04:00:00Z", "custodian": "实验室"})
check("全收妥补齐后 valid 可核定", chain("Z1")["status"] == "valid"
      and client.post(f"{API}/samples/Z1/approve", json={"approver": "质量主管"}).status_code == 200)

for c in ("X1", "X2"):
    reg(c)
make_batch("BATCH-X", codes=("X1", "X2"), prefix="XT")
r = recon("BATCH-X", rec_body("REC-X", items=[exc_item("X1", "车次延误样本失温"),
                                             exc_item("X2", "封签缺失")]))
check("全异常 201，received_count=0（无 receive、无版本、均仍在途）",
      r.status_code == 201 and r.json()["reconciliation"]["received_count"] == 0
      and r.json()["reconciliation"]["exception_count"] == 2
      and all(len(chain(c)["timeline"]) == 1 and versions(c)["current_version"] == 1
              for c in ("X1", "X2")), r.text)
# 全异常对账后在途移交仍在（异常不构成签收），此后单份签收仍可补
sr = ev({"event_id": "X1-R", "event_type": "receive", "sample_code": "X1",
         "occurred_at": "2026-09-30T03:30:00Z", "transfer_no": "TR-X1",
         "receiver": "李四", "seal_no": "SEAL-X1"})
check("全异常后该批不能再对账（一次性），但既有单签接口不受影响（异常不关闭在途）",
      recon("BATCH-X", rec_body("REC-X2", items=[recv_item("X1", "X1-RB"),
                                                exc_item("X2")])).status_code == 409
      and sr.status_code == 201)

# ---------------------------------------------------------------- 10. 数据库层终态核对
print("10) 数据库层终态：对账头/明细数量与收妥事件留痕一致")
check("对账头数量 = 成功批次数（BATCH-1,M,U,V,W,M,Q,K,C,D,Z,X 计 11）",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation") == 11)
check("异常明细不挂事件、收妥明细均挂事件",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item WHERE outcome='exception'"
                " AND event_id IS NOT NULL") == 0
      and db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item WHERE outcome='received'"
                    " AND event_id IS NULL") == 0)
check("收妥明细均能关联到 custody_event 中的 receive 行",
      db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item ri"
                " JOIN custody_event e ON e.id = ri.event_row_id"
                " WHERE ri.outcome='received' AND e.event_type='receive'")
      == db_scalar("SELECT COUNT(*) FROM batch_reconciliation_item WHERE outcome='received'"))
check("对账 receive 事件不挂 batch_id（经对账明细追溯）",
      db_scalar("SELECT COUNT(*) FROM custody_event ce"
                " JOIN batch_reconciliation_item ri ON ri.event_row_id = ce.id"
                " WHERE ce.batch_id IS NOT NULL") == 0)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
