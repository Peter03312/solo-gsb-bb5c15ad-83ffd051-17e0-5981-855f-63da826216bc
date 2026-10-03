"""零依赖（仅标准库）的 HTTP API 调用示例：完整演示登记→移交→签收→检查→入库→查询→核定。

使用前：
    pip install 无额外要求（urllib 为 Python 标准库）
    先启动服务：docker compose up -d   或   python -m uvicorn app.main:app --port 8080
运行：
    python examples/call_api.py
"""
import json
import os
import urllib.error
import urllib.request
import uuid

BASE = os.getenv("API_BASE", "http://127.0.0.1:8080/api/v1")


def call(method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    req = urllib.request.Request(
        BASE + path,
        data=data,
        method=method,
        headers={"Content-Type": "application/json; charset=utf-8"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read().decode("utf-8"))


def show(title: str, status: int, resp: dict, *keys: str) -> None:
    print(f"\n--- {title} [HTTP {status}] ---")
    out = resp
    for k in keys:
        out = out.get(k, {}) if isinstance(out, dict) else {}
    print(json.dumps(out, ensure_ascii=False, indent=2))


def main() -> None:
    # 每次运行使用独立编号，脚本可任意重复执行
    run_id = uuid.uuid4().hex[:8]
    code = f"API-DEMO-{run_id}"

    # 1) 样本登记：采集时间、采集人（初始保管人）、封签号
    s, r = call("POST", "/samples", {
        "sample_code": code,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-DEMO-1",
    })
    show("样本登记", s, r, "evaluation")

    # 2) 移交：必须由当前保管人发起，指定移交单号与接收人
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("移交（签收前=待补证）", s, r, "evaluation")

    # 3) 签收：单号/接收人/封签号三匹配，且晚于移交
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-R1-{run_id}", "event_type": "receive", "sample_code": code,
        "occurred_at": "2026-09-30T04:00:00Z",
        "transfer_no": "TR-DEMO-1", "receiver": "李运输", "seal_no": "SEAL-DEMO-1",
    })
    show("签收", s, r, "evaluation")

    # 4) 封签检查
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-C1-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T04:30:00Z",
        "checked_seal_no": "SEAL-DEMO-1", "seal_intact": True,
    })
    show("封签检查", s, r, "evaluation")

    # 5) 实验室入库（缺此节点结论为待补证）
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-S1-{run_id}", "event_type": "storage", "sample_code": code,
        "occurred_at": "2026-09-30T05:00:00Z",
        "custodian": "实验室", "storage_location": "冷库A-01",
    })
    show("实验室入库（此时有效）", s, r, "evaluation")

    # 6) 幂等重试：同事件号同内容 → 200
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号同内容重试（幂等）", s, r, "idempotent")

    # 7) 冲突：同事件号异内容 → 409
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-T1-{run_id}", "event_type": "transfer", "sample_code": code,
        "occurred_at": "2026-09-30T02:30:00Z",  # 时间被改动
        "transfer_no": "TR-DEMO-1", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    show("同事件号异内容（冲突）", s, r)

    # 8) 查询完整证据链（按发生时间排列）
    s, r = call("GET", f"/samples/{code}/chain")
    print(f"\n--- 证据链 [HTTP {s}] ---")
    print("结论:", r["status"], "| 当前保管人:", r["current_custodian"])
    for e in r["timeline"]:
        print(" ", e["occurred_at"], e["event_type"], e["event_id"])

    # 8.1) 纠错（替换）：用新事件号替换封签检查，结论按仅有效版本重算
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-C-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-C1-{run_id}",
        "action": "replace",
        "operator": "质控员",
        "reason": "示例：封签检查记录需补备注，整体替换",
        "new_event": {
            "event_id": f"API-EVT-C2-{run_id}",
            "event_type": "seal_check",
            "occurred_at": "2026-09-30T04:30:00Z",
            "checked_seal_no": "SEAL-DEMO-1",
            "seal_intact": True,
            "note": "纠错替换：复核无误",
        },
    })
    show("纠错-替换（同纠错号同内容重试幂等）", s, r, "correction")
    print("  替换后结论:", r["evaluation"]["status"], "| 保管人:", r["evaluation"]["current_custodian"])

    # 8.2) 同纠错号同内容重试 → 200 幂等；异内容 → 409（此处仅演示幂等）
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-C-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-C1-{run_id}",
        "action": "replace",
        "operator": "质控员",
        "reason": "示例：封签检查记录需补备注，整体替换",
        "new_event": {
            "event_id": f"API-EVT-C2-{run_id}",
            "event_type": "seal_check",
            "occurred_at": "2026-09-30T04:30:00Z",
            "checked_seal_no": "SEAL-DEMO-1",
            "seal_intact": True,
            "note": "纠错替换：复核无误",
        },
    })
    show("同纠错号同内容重试", s, r, "correction")

    # 8.3) 链查询可追溯被替换记录与纠错关系
    s, r = call("GET", f"/samples/{code}/chain")
    print(f"\n--- 纠错后证据链 [HTTP {s}] ---")
    print("有效时间线:", [e["event_id"] for e in r["timeline"]])
    print("被替换记录:", [e["event_id"] for e in r["superseded_events"]])
    print("纠错留痕:", [(c["correction_id"], c["action"], c["new_event_id"]) for c in r["corrections"]])

    # 9) 核定入库（仅 valid 可核定；核定后冻结，纠错也一并拒绝）
    s, r = call("POST", f"/samples/{code}/approve", {"approver": "质量主管", "note": "准予入库"})
    show("核定入库", s, r, "approval")

    # 9.1) 核定后纠错 → 409 history_locked
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-LATE-{run_id}",
        "sample_code": code,
        "target_event_id": f"API-EVT-T1-{run_id}",
        "action": "void",
        "operator": "质控员",
        "reason": "核定后试探作废",
    })
    show("核定后纠错（拒绝）", s, r)

    # 10) 核定后再写事件 → 409 history_locked
    s, r = call("POST", "/events", {
        "event_id": f"API-EVT-LATE-{run_id}", "event_type": "seal_check", "sample_code": code,
        "occurred_at": "2026-09-30T06:00:00Z", "checked_seal_no": "SEAL-DEMO-1",
    })
    show("核定后改写（拒绝）", s, r)

    # 11) 作废演示（独立样本）：作废后核验只纳入有效版本，结论即时重算
    code2 = f"API-VOID-{run_id}"
    call("POST", "/samples", {
        "sample_code": code2,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-DEMO-2",
    })
    call("POST", "/events", {
        "event_id": f"API-EVT2-T1-{run_id}", "event_type": "transfer", "sample_code": code2,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-DEMO-2", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    call("POST", "/events", {
        "event_id": f"API-EVT2-R1-{run_id}", "event_type": "receive", "sample_code": code2,
        "occurred_at": "2026-09-30T03:00:00Z",
        "transfer_no": "TR-DEMO-2", "receiver": "李运输", "seal_no": "WRONG-SEAL",
    })
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-V-{run_id}",
        "sample_code": code2,
        "target_event_id": f"API-EVT2-R1-{run_id}",
        "action": "void",
        "operator": "质控员",
        "reason": "误报，作废封签号错误的签收",
    })
    show("纠错-作废（原记录留痕、核验剔除）", s, r, "evaluation", "status")
    s, r = call("GET", f"/samples/{code2}/corrections")
    show("纠错审计查询", s, r)

    # 12) 批量移交：同一交接车次多份样本一次提交
    run_b = uuid.uuid4().hex[:8]
    b_codes = [f"API-BATCH-{run_b}-{i}" for i in (1, 2, 3)]
    for i, bc in enumerate(b_codes):
        call("POST", "/samples", {
            "sample_code": bc,
            "collected_at": "2026-09-30T01:00:00Z",
            "collector": "王采集",
            "seal_no": f"SEAL-BATCH-{run_b}-{i}",
        })

    batch_body = {
        "batch_id": f"API-BATCH-{run_b}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集",
        "to_custodian": "李运输",
        "items": [
            {"sample_code": b_codes[0], "transfer_no": f"TR-B-{run_b}-1",
             "event_id": f"API-BEVT-{run_b}-1"},
            {"sample_code": b_codes[1], "transfer_no": f"TR-B-{run_b}-2",
             "event_id": f"API-BEVT-{run_b}-2"},
            {"sample_code": b_codes[2], "transfer_no": f"TR-B-{run_b}-3",
             "event_id": f"API-BEVT-{run_b}-3"},
        ],
    }
    s, r = call("POST", "/transfer-batches", batch_body)
    show("批量移交提交（201，逐份核验结论）", s, r, "batch", "item_count")
    for it in r.get("batch", {}).get("items", []):
        print(f"    {it['sample_code']}: {it['evaluation']['status']}"
              f"（原事件有效={it['event_effective']}）")

    # 12.1) 同批次号同内容重试 → 200 幂等（顺序变化也识别为同内容）
    batch_body["items"] = list(reversed(batch_body["items"]))
    s, r = call("POST", "/transfer-batches", batch_body)
    show("同批次号同内容重试（200 幂等）", s, r, "idempotent")

    # 12.2) 同批次号异内容 → 409 batch_conflict
    bad = json.loads(json.dumps(batch_body, ensure_ascii=False))
    bad["items"][0]["transfer_no"] = "TR-CHANGED"
    s, r = call("POST", "/transfer-batches", bad)
    show("同批次号异内容（409 batch_conflict）", s, r)

    # 12.3) 整批失败示例：批内样本重复 / 事件号已占用 → 不留批次与事件
    bad2 = {
        "batch_id": f"API-BATCH-BAD-{run_b}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集", "to_custodian": "李运输",
        "items": [
            {"sample_code": b_codes[0], "transfer_no": "TR-X1",
             "event_id": f"API-BEVT-{run_b}-9"},
            {"sample_code": b_codes[0], "transfer_no": "TR-X2",
             "event_id": f"API-BEVT-{run_b}-1"},  # 批内样本重复 + 事件号已占用
        ],
    }
    s, r = call("POST", "/transfer-batches", bad2)
    show("整批失败（409/422 batch_rejected，逐项给出）", s, r)

    # 12.4) 批次查询：逐份当前结论与原事件是否仍有效
    s, r = call("GET", f"/transfer-batches/API-BATCH-{run_b}")
    show("批次查询", s, r, "batch", "item_count")

    # 12.5) 作废其中一份移交后再查：只改有效链与查询结果，批次原始归属仍在
    s, r = call("POST", "/corrections", {
        "correction_id": f"API-FIX-B-{run_b}",
        "sample_code": b_codes[1],
        "target_event_id": f"API-BEVT-{run_b}-2",
        "action": "void",
        "operator": "质控员",
        "reason": "该份误上车次，作废移交",
    })
    show("作废批内一份移交", s, r, "evaluation", "status")
    s, r = call("GET", f"/transfer-batches/API-BATCH-{run_b}")
    print(f"\n--- 纠错后批次查询 [HTTP {s}] ---")
    for it in r.get("batch", {}).get("items", []):
        print(f"    {it['sample_code']}: 原事件 {it['event_id']} 有效={it['event_effective']}"
              f" 当前有效事件={it['current_event_id']} 结论={it['evaluation']['status']}")

    # 12.6) 整批对账签收：接收方逐份提交覆盖原批次全部样本的对账清单
    # （收妥项=唯一事件号+实核封签号，落为既有 receive 事件；异常项=原因，不生成签收事件）
    run_r = uuid.uuid4().hex[:8]
    r_codes = [f"API-RECON-{run_r}-{i}" for i in (1, 2, 3)]
    for rc in r_codes:
        call("POST", "/samples", {
            "sample_code": rc,
            "collected_at": "2026-09-30T01:00:00Z",
            "collector": "王采集",
            "seal_no": rc.replace("API-RECON-", "SEAL-RECON-"),
        })
    recon_batch = {
        "batch_id": f"API-RECON-BATCH-{run_r}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集",
        "to_custodian": "李运输",
        "items": [
            {"sample_code": r_codes[0], "transfer_no": f"TR-R-{run_r}-1",
             "event_id": f"API-REVT-{run_r}-1"},
            {"sample_code": r_codes[1], "transfer_no": f"TR-R-{run_r}-2",
             "event_id": f"API-REVT-{run_r}-2"},
            {"sample_code": r_codes[2], "transfer_no": f"TR-R-{run_r}-3",
             "event_id": f"API-REVT-{run_r}-3"},
        ],
    }
    s, r = call("POST", "/transfer-batches", recon_batch)
    show("对账前置：批量移交提交", s, r, "batch", "item_count")

    recon_body = {
        "reconciliation_no": f"API-REC-{run_r}",
        "receiver": "李运输",
        "occurred_at": "2026-09-30T03:00:00Z",
        "items": [
            # 收妥项：唯一签收事件号 + 实核封签号
            {"sample_code": r_codes[0], "event_id": f"API-RECV-{run_r}-1",
             "verified_seal_no": r_codes[0].replace("API-RECON-", "SEAL-RECON-")},
            {"sample_code": r_codes[1], "event_id": f"API-RECV-{run_r}-2",
             "verified_seal_no": r_codes[1].replace("API-RECON-", "SEAL-RECON-")},
            # 异常项：仅填原因，不生成签收事件
            {"sample_code": r_codes[2], "reason": "到货封签破损，异常拒收待处理"},
        ],
    }
    s, r = call("POST", f"/transfer-batches/API-RECON-BATCH-{run_r}/reconciliation", recon_body)
    show("整批对账提交（201：2 收妥 / 1 异常）", s, r, "reconciliation", "reconciliation_no")
    for it in r.get("reconciliation", {}).get("items", []):
        print(f"    {it['sample_code']}: {it['outcome']} 事件={it['event_id']}"
              f" 收妥事件现有效={it['event_effective']} 结论={it['evaluation']['status']}"
              f" 保管人={it['evaluation']['current_custodian']}")

    # 同签收单号同内容重试（items 换序）→ 200 返回原结果
    recon_body["items"] = list(reversed(recon_body["items"]))
    s, r = call("POST", f"/transfer-batches/API-RECON-BATCH-{run_r}/reconciliation", recon_body)
    show("同单号同内容重试（200 幂等，返回原结果）", s, r, "idempotent")

    # 同单号异内容（实核封签改了）→ 409 reconciliation_conflict
    bad_rec = json.loads(json.dumps(recon_body, ensure_ascii=False))
    for it in bad_rec["items"]:
        if it.get("event_id") == f"API-RECV-{run_r}-1":
            it["verified_seal_no"] = "SEAL-TAMPERED"
    s, r = call("POST", f"/transfer-batches/API-RECON-BATCH-{run_r}/reconciliation", bad_rec)
    show("同单号异内容（409 reconciliation_conflict）", s, r)

    # 批次已对账，换新单号再提交 → 409 batch_already_reconciled
    other_rec = json.loads(json.dumps(recon_body, ensure_ascii=False))
    other_rec["reconciliation_no"] = f"API-REC-OTHER-{run_r}"
    s, r = call("POST", f"/transfer-batches/API-RECON-BATCH-{run_r}/reconciliation", other_rec)
    show("批次重复对账（409 batch_already_reconciled）", s, r)

    # 不合格收妥项（缺项）→ 422 整单拒绝且不留记录（用一个未对账的新批次演示）
    run_r2 = uuid.uuid4().hex[:8]
    rc2 = [f"API-RECON-{run_r2}-{i}" for i in (1, 2)]
    for x in rc2:
        call("POST", "/samples", {
            "sample_code": x, "collected_at": "2026-09-30T01:00:00Z",
            "collector": "王采集", "seal_no": x.replace("API-RECON-", "SEAL-RECON-")})
    call("POST", "/transfer-batches", {
        "batch_id": f"API-RECON-BATCH-{run_r2}",
        "occurred_at": "2026-09-30T02:00:00Z",
        "from_custodian": "王采集", "to_custodian": "李运输",
        "items": [
            {"sample_code": rc2[0], "transfer_no": f"TR-{run_r2}-1", "event_id": f"API-REVT-{run_r2}-1"},
            {"sample_code": rc2[1], "transfer_no": f"TR-{run_r2}-2", "event_id": f"API-REVT-{run_r2}-2"}]})
    s, r = call("POST", f"/transfer-batches/API-RECON-BATCH-{run_r2}/reconciliation", {
        "reconciliation_no": f"API-REC-{run_r2}", "receiver": "李运输",
        "occurred_at": "2026-09-30T03:00:00Z",
        "items": [{"sample_code": rc2[0], "event_id": f"API-RECV-{run_r2}-1",
                   "verified_seal_no": rc2[0].replace("API-RECON-", "SEAL-RECON-")}]})  # 缺第二份
    show("缺项整单拒绝（422 reconciliation_rejected，逐项给出，不留记录）", s, r)

    # 对账查询：当时的收妥/异常明细 + 各收妥事件现时有效性（纠错后反映有效链，不改写原对账）
    s, r = call("GET", f"/transfer-batches/API-RECON-BATCH-{run_r}/reconciliation")
    show("对账查询（收妥/异常明细 + 现时有效性）", s, r, "reconciliation", "reconciliation_no")

    # 13) 保管链版本复核（质控）：按样本编号 + 版本号查询当时的结论、原因、保管人与时间线
    print("\n=== 保管链版本复核 ===")
    s, r = call("GET", f"/samples/{code}/versions")
    print(f"--- 版本索引 [HTTP {s}] 当前最新版本: {r['current_version']}（共 {r['total']} 版）---")
    for it in r["items"]:
        print(f"    v{it['version']} [{it['change_type']}] {it['committed_at']}"
              f" 结论={it['status']} 保管人={it['current_custodian']} 触发号={it['change_ref']}")

    # 指定历史版本：复核核定（入库）当时的完整快照
    s, r = call("GET", f"/samples/{code}/versions/0")
    v0 = r["version_snapshot"]
    print(f"\n--- 版本 0（登记基线）[HTTP {s}] ---")
    print("结论:", v0["status"], "| 时间线事件数:", len(v0["timeline"]), "| 核定:", v0["approval"])

    # latest 便捷端点
    s, r = call("GET", f"/samples/{code}/versions/latest")
    latest = r["version_snapshot"]
    print(f"--- 当前最新版本 [HTTP {s}] v{latest['version']} [{latest['change_type']}] ---")
    print("结论:", latest["status"], "| 保管人:", latest["current_custodian"], "| 核定信息:", latest["approval"])
    print("当时有效时间线:", [(e["event_type"], e["event_id"]) for e in latest["timeline"]])
    print("纠错留痕:", [(c["correction_id"], c["action"]) for c in latest["corrections"]])
    print("作废/替换留痕:", [(e["event_id"], e["superseded_by"]) for e in latest["superseded_events"]])

    # 未知样本 / 不存在的版本明确报错
    s, r = call("GET", "/samples/NO-SUCH-SAMPLE/versions/0")
    print("\n未知样本查询:", s, r.get("error", {}).get("code"))
    s, r = call("GET", f"/samples/{code}/versions/9999")
    print("不存在的版本:", s, r.get("error", {}).get("code"),
          "| 当前最新版本:", r.get("error", {}).get("details", {}).get("current_version"))

    # 14) 案卷封存：同一抽检的多份样本保管链版本一次封存为不可变案卷
    print("\n=== 案卷封存 ===")
    # 14.1) 封存：code 指定历史版本 0（登记基线），code2 缺省取封存事务中的最新版本
    dossier_id = f"API-DOSSIER-{run_id}"
    dossier_body = {
        "dossier_id": dossier_id,
        "items": [
            {"sample_code": code2},                 # 未指定版本 → 取最新
            {"sample_code": code, "version": 0},    # 指定非负历史版本
        ],
    }
    s, r = call("POST", "/dossiers", dossier_body)
    d = r.get("dossier", {})
    print(f"--- 案卷封存 [HTTP {s}] 案卷号={d.get('dossier_id')} 份数={d.get('item_count')} ---")
    print("摘要:", d.get("digest"))
    for it in d.get("items", []):
        print(f"    [{it['index']}] {it['sample_code']} @ v{it['version']}"
              f" 结论={it['snapshot']['status']} 保管人={it['snapshot']['current_custodian']}")

    # 14.2) 查询案卷：清单（按样本编号排序）、各份完整快照与摘要
    s, r = call("GET", f"/dossiers/{dossier_id}")
    print(f"\n--- 案卷查询 [HTTP {s}] ---")
    print("清单:", [(it["sample_code"], it["version"]) for it in r["dossier"]["items"]])

    # 14.3) 核验：重算摘要并返回一致与否（只读，不改写）
    s, r = call("POST", f"/dossiers/{dossier_id}/verify")
    print(f"--- 案卷核验 [HTTP {s}] 一致={r.get('match')} ---")
    print("留存摘要:", r.get("stored_digest"))
    print("重算摘要:", r.get("recomputed_digest"))

    # 14.4) 同案卷号同一请求重试：即使样本已有新版本，仍返回原卷
    call("POST", "/events", {
        "event_id": f"API-EVT2-T2-{run_id}", "event_type": "transfer", "sample_code": code2,
        "occurred_at": "2026-09-30T06:00:00Z",
        "transfer_no": "TR-DEMO-2B", "from_custodian": "王采集", "to_custodian": "李运输",
    })
    s, r = call("POST", "/dossiers", dossier_body)
    print(f"\n--- 同案卷号同请求重试（样本已新增版本）[HTTP {s}] 幂等={r.get('idempotent')} ---")
    print("清单仍封存原版本:", [(it["sample_code"], it["version"]) for it in r["dossier"]["items"]])

    # 14.5) 同案卷号异内容 → 409 dossier_conflict
    s, r = call("POST", "/dossiers", {
        "dossier_id": dossier_id,
        "items": [{"sample_code": code, "version": 1}, {"sample_code": code2}],
    })
    print("同案卷号异内容:", s, r.get("error", {}).get("code"))

    # 14.6) 整卷失败：编号重复 / 版本不存在 → 不留案卷
    s, r = call("POST", "/dossiers", {
        "dossier_id": f"API-DOSSIER-BAD-{run_id}",
        "items": [{"sample_code": code}, {"sample_code": code}],
    })
    print("编号重复整卷失败:", s, r.get("error", {}).get("code"))
    s, r = call("POST", "/dossiers", {
        "dossier_id": f"API-DOSSIER-BAD2-{run_id}",
        "items": [{"sample_code": code, "version": 9999}, {"sample_code": code2}],
    })
    print("版本不存在整卷失败:", s, r.get("error", {}).get("code"),
          "| 当前最新版本:", r.get("error", {}).get("details", {}).get("current_version"))
    s, r = call("GET", f"/dossiers/API-DOSSIER-BAD-{run_id}")
    print("失败不留案卷（查询 404）:", s, r.get("error", {}).get("code"))

    # 15) 案卷双人独立复核 + 第三人裁决（只依据案卷封存版本快照）
    print("\n=== 案卷双人独立复核 ===")
    # 15.0) 为复核另封存一卷（复用上面已补齐完整链的 code / code2，取各自最新版本）
    review_dossier_id = f"API-REVIEW-DOS-{run_id}"
    s, r = call("POST", "/dossiers", {
        "dossier_id": review_dossier_id,
        "items": [{"sample_code": code}, {"sample_code": code2}],
    })
    digest = r["dossier"]["digest"]
    print(f"--- 复核专用案卷封存 [HTTP {s}] 摘要={digest} ---")

    s, r = call("GET", f"/dossiers/{review_dossier_id}/reviews")
    print(f"复核进度（未提交）[HTTP {s}] stage={r['stage']} review_count={r['review_count']}")

    # 15.1) 摘要不一致 → 422 review_digest_mismatch（拒绝写入，不占用复核单号）
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": f"API-RV-BAD-{run_id}", "reviewer": "质控员甲",
        "expected_digest": "0" * 64,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print("摘要不一致:", s, r.get("error", {}).get("code"))

    # 15.2) 复核人甲提交第一份完整清单：code 通过、code2 通过
    rv_no1 = f"API-RV-1-{run_id}"
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": rv_no1, "reviewer": "质控员甲",
        "expected_digest": digest,
        "items": [{"sample_code": code2, "passed": True},          # 顺序可与案卷清单不同
                  {"sample_code": code, "passed": True}]})
    print(f"第一份独立复核 [HTTP {s}] seq={r['submission']['seq']} stage={r['review']['stage']}")

    # 同一复核人再次提交 → 409 duplicate_reviewer（两名复核人必须不同）
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": f"API-RV-DUP-{run_id}", "reviewer": "质控员甲",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print("重复复核人:", s, r.get("error", {}).get("code"))

    # 15.3) 复核人乙第二份：code 判不通过（与甲分歧），code2 仍通过（一致终局）
    rv_no2 = f"API-RV-2-{run_id}"
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": rv_no2, "reviewer": "质控员乙",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": False, "reason": "封签号与登记不符"},
                  {"sample_code": code2, "passed": True}]})
    print(f"第二份独立复核 [HTTP {s}] stage={r['review']['stage']} 分歧={r['review']['disputed_samples']}")

    # 参与过前两次复核的人不得裁决
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": f"API-RV-ADJ-DUP-{run_id}", "reviewer": "质控员甲",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": True}]})
    print("前两名复核人试图裁决:", s, r.get("error", {}).get("code"))

    # 15.4) 第三人（质控主管）裁决分歧份项：维持不通过 → 案卷不通过
    rv_no3 = f"API-RV-3-{run_id}"
    adj_body = {
        "review_no": rv_no3, "reviewer": "质控主管丙",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": False, "reason": "复核确认：封签号与登记不符"}],
    }
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", adj_body)
    print(f"第三人裁决 [HTTP {s}] stage={r['review']['stage']}")
    print("案卷终局结论:", r["review"]["conclusion"])

    # 15.5) 同复核单号同内容重试 → 200 返回原结果（幂等）；异内容 → 409
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", adj_body)
    print(f"同号同内容重试 [HTTP {s}]（200=幂等，返回原结果）")
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": rv_no3, "reviewer": "质控主管丙",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": True}]})
    print("同复核单号异内容:", s, r.get("error", {}).get("code"))
    # 终局后越阶段提交
    s, r = call("POST", f"/dossiers/{review_dossier_id}/reviews", {
        "review_no": f"API-RV-LATE-{run_id}", "reviewer": "质控员丁",
        "expected_digest": digest,
        "items": [{"sample_code": code, "passed": True}]})
    print("案卷终局后再提交:", s, r.get("error", {}).get("code"))

    # 15.6) 查询：进度、原始意见、分歧与终局结论
    s, r = call("GET", f"/dossiers/{review_dossier_id}/reviews")
    print(f"\n--- 复核查询 [HTTP {s}] stage={r['stage']} ---")
    for it in r["items"]:
        print(f"  {it['sample_code']}: {it['status']} final={it['final']}")
        for o in it["opinions"]:
            print(f"      seq{o['seq']} {o['kind']:<12} {o['reviewer']}: "
                  f"{'通过' if o['passed'] else '不通过'} {o['reason'] or ''}")

    # 16) 人员标识与历史别名 + 复核利益冲突回避（防止参与保管的人评价自己的案卷）
    print("\n=== 人员标识与复核回避 ===")
    # 16.1) 登记人员标识：全局唯一主标识 + 可选初始历史别名
    qc_a = f"QC-A-{run_id}"
    s, r = call("POST", "/persons", {"person_id": qc_a, "aliases": ["王采集"]})
    show("登记人员（主标识 + 初始别名）", s, r, "person")
    revision = r["person"]["revision"]

    # 16.2) 增补历史别名：须携带所见修订号（GET 人员取得）；过期则拒绝
    s, r = call("POST", f"/persons/{qc_a}/aliases",
                {"alias": "采集老王", "expected_revision": revision})
    print(f"增补别名（携带修订号 {revision}）[HTTP {s}] 新修订号={r['person']['revision']}")
    s, r = call("POST", f"/persons/{qc_a}/aliases",
                {"alias": "老王", "expected_revision": revision})  # 修订号已过期
    print("过期修订号增补:", s, r.get("error", {}).get("code"),
          "| 当前修订号:", r.get("error", {}).get("details", {}).get("current_revision"))

    # 16.3) 别名不得归属多人/转移：另一人登记同一别名 → 409
    qc_b = f"QC-B-{run_id}"
    call("POST", "/persons", {"person_id": qc_b})
    s, r = call("POST", f"/persons/{qc_b}/aliases",
                {"alias": "王采集", "expected_revision": 1})
    print("别名归属他人:", s, r.get("error", {}).get("code"),
          "| 归属:", r.get("error", {}).get("details", {}).get("owner_person_id"))

    # 16.4) 另封存一卷演示回避：code/code2 的采集人、移交人均为“王采集”，code 的核定人为“质量主管”
    recusal_dossier_id = f"API-RECUSAL-DOS-{run_id}"
    s, r = call("POST", "/dossiers", {
        "dossier_id": recusal_dossier_id,
        "items": [{"sample_code": code}, {"sample_code": code2}],
    })
    digest2 = r["dossier"]["digest"]
    print(f"\n--- 回避演示案卷封存 [HTTP {s}] 摘要={digest2} ---")

    # 复核人 QC-A 的别名“王采集”是封存版本的采集人/移交人 → 整单拒绝并列出份项与命中角色
    s, r = call("POST", f"/dossiers/{recusal_dossier_id}/reviews", {
        "review_no": f"API-RV-REC-{run_id}", "reviewer": qc_a,
        "expected_digest": digest2,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print("复核人别名命中采集人/移交人:", s, r.get("error", {}).get("code"))
    for it in r.get("error", {}).get("details", {}).get("items", []):
        print(f"    命中份项 {it['sample_code']}: "
              + ", ".join(f"{h['role']}={h['participant']}" for h in it["hits"]))

    # 未登记姓名按原字符串比较：“质量主管”是 code 封存版本的核定人 → 同样拒绝
    s, r = call("POST", f"/dossiers/{recusal_dossier_id}/reviews", {
        "review_no": f"API-RV-REC2-{run_id}", "reviewer": "质量主管",
        "expected_digest": digest2,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print("复核人是核定人（未登记按原字符串命中）:", s, r.get("error", {}).get("code"))

    # 16.5) 回避失败不占复核单号：同一单号换无利益冲突的复核人可提交
    qc_c = f"QC-C-{run_id}"
    qc_d = f"QC-D-{run_id}"
    call("POST", "/persons", {"person_id": qc_c})
    call("POST", "/persons", {"person_id": qc_d})
    s, r = call("POST", f"/dossiers/{recusal_dossier_id}/reviews", {
        "review_no": f"API-RV-REC-{run_id}", "reviewer": qc_c,   # 复用被回避拒绝的单号
        "expected_digest": digest2,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print(f"回避失败不占复核单号，换人提交 [HTTP {s}] person_id={r['submission']['person_id']}")

    # 16.6) 第二人提交 → 案卷完成；意见记录提交时的身份归属供查询
    s, r = call("POST", f"/dossiers/{recusal_dossier_id}/reviews", {
        "review_no": f"API-RV-REC3-{run_id}", "reviewer": qc_d,
        "expected_digest": digest2,
        "items": [{"sample_code": code, "passed": True},
                  {"sample_code": code2, "passed": True}]})
    print(f"第二份独立复核 [HTTP {s}] stage={r['review']['stage']}")
    s, r = call("GET", f"/dossiers/{recusal_dossier_id}/reviews")
    print("复核人身份归属:", [(x["reviewer"], x["person_id"]) for x in r["reviewers"]])

    # 16.7) 人员查询与列表
    s, r = call("GET", f"/persons/{qc_a}")
    print(f"\n--- 人员查询 [HTTP {s}] ---")
    print(json.dumps(r["person"], ensure_ascii=False, indent=2))

    # 17) 运输测温复核（温度配置 → 读数 → 超温/缺测分段归属 → 纠错重算）
    print("\n=== 运输测温复核 ===")
    tcode = f"API-TEMP-{run_id}"
    call("POST", "/samples", {
        "sample_code": tcode,
        "collected_at": "2026-09-30T01:00:00Z",
        "collector": "王采集",
        "seal_no": "SEAL-T1",
    })
    # 保管链：02:00 移交在途（王采集→李运输），04:00 李运输签收，05:00 入库
    call("POST", "/events", {
        "event_id": f"API-TT1-{run_id}", "event_type": "transfer", "sample_code": tcode,
        "occurred_at": "2026-09-30T02:00:00Z",
        "transfer_no": "TR-T1", "from_custodian": "王采集", "to_custodian": "李运输"})
    call("POST", "/events", {
        "event_id": f"API-TR1-{run_id}", "event_type": "receive", "sample_code": tcode,
        "occurred_at": "2026-09-30T04:00:00Z",
        "transfer_no": "TR-T1", "receiver": "李运输", "seal_no": "SEAL-T1"})
    call("POST", "/events", {
        "event_id": f"API-TS1-{run_id}", "event_type": "storage", "sample_code": tcode,
        "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"})

    # 17.1) 一次性测温配置：允许 2~8℃ 闭区间，最大测温间隔 1 小时
    s, r = call("PUT", f"/samples/{tcode}/temperature-config",
                {"min_celsius": 2, "max_celsius": 8, "max_interval_seconds": 3600})
    show("一次性测温配置", s, r)
    s, r = call("PUT", f"/samples/{tcode}/temperature-config",
                {"min_celsius": 2, "max_celsius": 9, "max_interval_seconds": 3600})
    print("改配置（一次性，异内容）:", s, r.get("error", {}).get("code"))

    # 17.2) 读数上报（乱序补录；支持同号幂等/异内容冲突；采集前拒绝）
    call("POST", f"/samples/{tcode}/temperature-readings",
         {"reading_id": f"API-T-R01-{run_id}", "device_id": "TEMPLOGGER-7",
          "occurred_at": "2026-09-30T01:00:00Z", "celsius": 5.0})
    call("POST", f"/samples/{tcode}/temperature-readings",
         {"reading_id": f"API-T-R03-{run_id}", "device_id": "TEMPLOGGER-7",
          "occurred_at": "2026-09-30T03:00:00Z", "celsius": 9.4})     # 超温（在途段）
    call("POST", f"/samples/{tcode}/temperature-readings",
         {"reading_id": f"API-T-R04-{run_id}", "device_id": "TEMPLOGGER-7",
          "occurred_at": "2026-09-30T04:40:00Z", "celsius": 5.1})
    call("POST", f"/samples/{tcode}/temperature-readings",
         {"reading_id": f"API-T-R05-{run_id}", "device_id": "TEMPLOGGER-7",
          "occurred_at": "2026-09-30T06:00:00Z", "celsius": 5.0})
    s, r = call("POST", f"/samples/{tcode}/temperature-readings",
                {"reading_id": f"API-T-PRE-{run_id}", "device_id": "TEMPLOGGER-7",
                 "occurred_at": "2026-09-30T00:30:00Z", "celsius": 5.0})
    print("采集前读数:", s, r.get("error", {}).get("code"))
    s, r = call("POST", f"/samples/{tcode}/temperature-readings",
                {"reading_id": f"API-T-R01-{run_id}", "device_id": "TEMPLOGGER-7",
                 "occurred_at": "2026-09-30T01:00:00Z", "celsius": 5.0})
    print("同号同内容重试:", s, "idempotent =", r.get("idempotent"))

    # 17.3) 测温复核报告：超温点 + 跨移交/签收时刻拆分的缺测区间及逐段归属
    s, r = call("GET", f"/samples/{tcode}/temperature-report")
    rep = r["report"]
    print(f"\n--- 测温复核报告 [HTTP {s}] 超温 {rep['excursion_count']} 处、缺测 {rep['gap_count']} 段 ---")
    for e in rep["excursions"]:
        print(f"  超温: {e['occurred_at']} {e['celsius']}℃ ({e['direction']}) 归属={e['custody']['attribution']}")
    for g in rep["gaps"]:
        print(f"  缺测: {g['start_at']} → {g['end_at']}（{g['duration_seconds']}s，超限 {g['exceeds_by_seconds']}s）")
        for seg in g["segments"]:
            c_desc = (seg["custody"].get("custodian")
                      or f"在途 {seg['custody'].get('from_custodian')}→{seg['custody'].get('to_custodian')}"
                      or seg["custody"].get("attribution"))
            print(f"      分段 {seg['start_at']} → {seg['end_at']}（{seg['duration_seconds']}s）{c_desc}")

    # 17.4) 读数纠错：把误录的 9.4℃ 替换为 5.4℃（原始读数留痕，归属重算）
    s, r = call("POST", "/temperature-corrections", {
        "correction_id": f"API-TFIX1-{run_id}", "sample_code": tcode,
        "target_reading_id": f"API-T-R03-{run_id}", "action": "replace",
        "operator": "质控员", "reason": "设备导出小数点错误，9.4 应为 5.4",
        "new_reading": {"reading_id": f"API-T-R03B-{run_id}", "device_id": "TEMPLOGGER-7",
                        "occurred_at": "2026-09-30T03:00:00Z", "celsius": 5.4}})
    print("\n替换纠错:", s, "纠错后超温数 =", r.get("report", {}).get("excursion_count"))
    s, r = call("GET", f"/samples/{tcode}/temperature-readings")
    print(f"读数列表 [HTTP {s}] 总数={r['total']} 有效={r['effective']}（被替换原读数仍留痕、effective=false）")


if __name__ == "__main__":
    main()
