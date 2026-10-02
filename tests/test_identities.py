"""端到端自测：人员标识与历史别名登记 + 案卷复核利益冲突回避。

覆盖：
  * 人员登记：201 / 同内容（别名换序）幂等 200 / 异内容 409 person_conflict；
    主标识或初始别名已被占用 → 409 alias_conflict；初始别名重复或与主标识相同 → 422；
  * 增补别名：携带所见修订号成功（修订号 +1）；过期修订号 → 409 identity_revision_stale
    （明细给两侧修订号）；别名已归属本人/他人或撞主标识 → 409 alias_conflict；
    未知人员 → 404 person_not_found；并发同修订号增补仅一次成功；
  * 复核回避：两人复核按封存案卷全部份项、第三人裁决按分歧份项，以封存版本的
    采集人 / 有效事件移交与签收参与人 / 核定人检查复核人及其别名；
    命中任一份 → 409 review_recusal 整单拒绝（列出份项与命中角色、不占复核单号、不留记录）；
    未登记姓名按原字符串比较（尾空格不命中）；
  * 两人互异与第三人资格按本次提交时的别名归属判定（同一人不同标识 → 409 duplicate_reviewer）；
  * 成功意见记录当时的人员归属（person_id）供查询；后续增补别名不追溯改变已提交意见；
    未登记 / 旧意见 person_id 为 null（不伪造身份记录）；
  * 既有复核单号、摘要与幂等规则保持。

运行：python -m tests.test_identities
"""
import os
import tempfile
import threading

_tmp = tempfile.mkdtemp()
os.environ["DB_PATH"] = os.path.join(_tmp, "test_identities.db")
os.environ["SEED_ON_START"] = "false"

from fastapi.testclient import TestClient  # noqa: E402

from app.db import db_session  # noqa: E402
from app.main import app  # noqa: E402

client = TestClient(app)
client.__enter__()  # 触发 lifespan（建表/启动钩子）
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


def build_sample(code, collector, to, approver=None):
    """登记并补齐有效链：collector 采集 → 移交 to → 签收 → 检查 → 入库（可选核定）。"""
    seal = f"SEAL-{code}"
    client.post(f"{API}/samples", json={
        "sample_code": code, "collected_at": "2026-09-30T01:00:00Z",
        "collector": collector, "seal_no": seal})
    for payload in [
        {"event_id": f"{code}-T1", "event_type": "transfer", "sample_code": code,
         "occurred_at": "2026-09-30T02:00:00Z", "transfer_no": f"TR-{code}",
         "from_custodian": collector, "to_custodian": to},
        {"event_id": f"{code}-R1", "event_type": "receive", "sample_code": code,
         "occurred_at": "2026-09-30T04:00:00Z", "transfer_no": f"TR-{code}",
         "receiver": to, "seal_no": seal},
        {"event_id": f"{code}-C1", "event_type": "seal_check", "sample_code": code,
         "occurred_at": "2026-09-30T04:30:00Z", "checked_seal_no": seal, "seal_intact": True},
        {"event_id": f"{code}-S1", "event_type": "storage", "sample_code": code,
         "occurred_at": "2026-09-30T05:00:00Z", "custodian": "实验室"},
    ]:
        client.post(f"{API}/events", json=payload)
    if approver:
        client.post(f"{API}/samples/{code}/approve", json={"approver": approver})


def make_dossier(did, codes):
    r = client.post(f"{API}/dossiers", json={
        "dossier_id": did, "items": [{"sample_code": c} for c in sorted(codes)]})
    assert r.status_code == 201, r.text
    return r.json()["dossier"]["digest"]


def submit(did, review_no, reviewer, digest, items):
    return client.post(f"{API}/dossiers/{did}/reviews", json={
        "review_no": review_no, "reviewer": reviewer,
        "expected_digest": digest, "items": items})


def progress(did):
    return client.get(f"{API}/dossiers/{did}/reviews").json()


def op(code, passed=True, reason=None):
    return {"sample_code": code, "passed": passed, "reason": reason}


def all_pass(codes):
    return [op(c, True) for c in codes]


def db_scalar(sql, args=()):
    with db_session() as conn:
        return conn.execute(sql, args).fetchone()[0]


def reg_person(pid, aliases=None):
    return client.post(f"{API}/persons", json={"person_id": pid, "aliases": aliases or []})


# ---------------------------------------------------------------- 1. 人员登记
print("1) 人员登记：唯一主标识 + 初始别名；同内容幂等、异内容与名称占用冲突")
r = reg_person("P-1001", ["甲", "小甲"])
check("登记人员 201", r.status_code == 201, r.text)
p = r.json()["person"]
check("初始修订号=1 且别名落库", p["revision"] == 1 and p["aliases"] == ["甲", "小甲"], str(p))
check("非幂等标记", r.json()["idempotent"] is False)

r = reg_person("P-1001", ["小甲", "甲"])
check("同标识同内容（别名换序）重试 200 幂等",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
check("幂等重试不推进修订号", r.json()["person"]["revision"] == 1)

r = reg_person("P-1001", ["甲"])
check("同标识异内容 409 person_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "person_conflict", r.text)

r = reg_person("甲")
check("主标识撞他人别名 409 alias_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "alias_conflict", r.text)
check("明细给出归属人", r.json()["error"]["details"]["owner_person_id"] == "P-1001", r.text)

r = reg_person("P-1002", ["P-1001"])
check("初始别名撞他人主标识 409 alias_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "alias_conflict", r.text)

r = reg_person("P-1002", ["乙", "乙"])
check("初始别名重复 422（字段校验）", r.status_code == 422, r.text)
r = reg_person("P-1002", ["P-1002"])
check("初始别名与主标识相同 422（字段校验）", r.status_code == 422, r.text)
check("冲突与校验失败均不留人员",
      db_scalar("SELECT COUNT(*) FROM person WHERE person_id IN ('P-1002','甲')") == 0)

r = client.get(f"{API}/persons/P-1001")
check("查询人员 200", r.status_code == 200 and r.json()["person"]["person_id"] == "P-1001", r.text)
r = client.get(f"{API}/persons/NO-SUCH")
check("未知人员 404 person_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "person_not_found", r.text)
r = client.get(f"{API}/persons")
check("人员列表返回全部已登记人员", r.status_code == 200 and r.json()["total"] == 1, r.text)

# ---------------------------------------------------------------- 2. 增补别名与修订号
print("2) 增补别名：须携带所见修订号，过期拒绝；别名全局唯一")
r = client.post(f"{API}/persons/P-1001/aliases", json={"alias": "老甲", "expected_revision": 1})
check("携带所见修订号增补 201", r.status_code == 201, r.text)
p = r.json()["person"]
check("增补后修订号 +1、别名落库", p["revision"] == 2 and "老甲" in p["aliases"], str(p))

r = client.post(f"{API}/persons/P-1001/aliases", json={"alias": "老甲", "expected_revision": 1})
check("过期修订号 409 identity_revision_stale",
      r.status_code == 409 and r.json()["error"]["code"] == "identity_revision_stale", r.text)
det = r.json()["error"]["details"]
check("明细给出两侧修订号",
      det["current_revision"] == 2 and det["expected_revision"] == 1, str(det))

r = client.post(f"{API}/persons/P-1001/aliases", json={"alias": "老甲", "expected_revision": 2})
check("别名已归属本人 409 alias_conflict（不得重复归属）",
      r.status_code == 409 and r.json()["error"]["code"] == "alias_conflict", r.text)

r = client.post(f"{API}/persons/P-1001/aliases", json={"alias": "P-1001", "expected_revision": 2})
check("别名撞本人主标识 409 alias_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "alias_conflict", r.text)

reg_person("P-1002")
r = client.post(f"{API}/persons/P-1002/aliases", json={"alias": "甲", "expected_revision": 1})
check("别名已归属他人 409 alias_conflict（不得归属多人/转移）",
      r.status_code == 409 and r.json()["error"]["code"] == "alias_conflict", r.text)
check("明细给出归属人", r.json()["error"]["details"]["owner_person_id"] == "P-1001", r.text)

r = client.post(f"{API}/persons/NO-SUCH/aliases", json={"alias": "x", "expected_revision": 1})
check("未知人员增补别名 404 person_not_found",
      r.status_code == 404 and r.json()["error"]["code"] == "person_not_found", r.text)

r = client.post(f"{API}/persons/P-1001/aliases", json={"alias": "甲某", "expected_revision": 2})
check("按最新修订号增补成功（修订号=3）",
      r.status_code == 201 and r.json()["person"]["revision"] == 3, r.text)
check("失败未改动别名集",
      client.get(f"{API}/persons/P-1001").json()["person"]["aliases"] == ["甲", "小甲", "老甲", "甲某"])

# ---------------------------------------------------------------- 3. 并发增补别名
print("3) 并发增补别名：同修订号并发仅一次成功（按已提交修订号判定）")
reg_person("P-2001")
results_c: dict[str, int] = {}
barrier = threading.Barrier(2)


def add_alias(tag, alias):
    barrier.wait()
    results_c[tag] = client.post(
        f"{API}/persons/P-2001/aliases",
        json={"alias": alias, "expected_revision": 1}).status_code


t1 = threading.Thread(target=add_alias, args=("A", "并发甲"))
t2 = threading.Thread(target=add_alias, args=("B", "并发乙"))
t1.start(); t2.start(); t1.join(); t2.join()
check("并发增补：一次 201 一次 409（过期）", sorted(results_c.values()) == [201, 409], str(results_c))
p = client.get(f"{API}/persons/P-2001").json()["person"]
check("仅一个别名落库、修订号=2", p["revision"] == 2 and len(p["aliases"]) == 1, str(p))

# ---------------------------------------------------------------- 4. 复核回避（未登记姓名按原字符串）
print("4) 复核回避：命中采集/移交/签收/核定任一角色即整单拒绝，不占复核单号")
build_sample("RC-1", collector="王采集", to="李运输", approver="质量主管")
build_sample("RC-2", collector="赵采集", to="李运输")
build_sample("RC-3", collector="王采集", to="钱运输")
DIG_A = make_dossier("DOS-RC-A", ["RC-1", "RC-2"])

# 复核人是封存版本的采集人 + 移交交出方（RC-1）
r = submit("DOS-RC-A", "REV-RC-01", "王采集", DIG_A, all_pass(["RC-1", "RC-2"]))
check("复核人是采集人/移交人 → 409 review_recusal",
      r.status_code == 409 and r.json()["error"]["code"] == "review_recusal", r.text)
det = r.json()["error"]["details"]
check("明细列出份项与命中角色（collector/transfer_from）",
      [i["sample_code"] for i in det["items"]] == ["RC-1"]
      and {h["role"] for h in det["items"][0]["hits"]} == {"collector", "transfer_from"}, str(det))
check("未登记复核人 reviewer_person_id 为 null", det["reviewer_person_id"] is None, str(det))

# 多份命中：李运输在 RC-1/RC-2 均为移交接收方与签收人
r = submit("DOS-RC-A", "REV-RC-02", "李运输", DIG_A, all_pass(["RC-1", "RC-2"]))
det = r.json()["error"]["details"]
check("命中多份 → 整单拒绝并列出全部份项",
      r.status_code == 409 and [i["sample_code"] for i in det["items"]] == ["RC-1", "RC-2"], str(det))
check("命中角色含 transfer_to 与 receiver",
      all({h["role"] for h in i["hits"]} == {"transfer_to", "receiver"} for i in det["items"]),
      str(det))

# 复核人是核定人（RC-1 已核定入库）
r = submit("DOS-RC-A", "REV-RC-03", "质量主管", DIG_A, all_pass(["RC-1", "RC-2"]))
det = r.json()["error"]["details"]
check("复核人是核定人 → 409，命中角色 approver",
      r.status_code == 409 and det["items"] == [
          {"sample_code": "RC-1", "hits": [{"role": "approver", "participant": "质量主管"}]}],
      str(det))

# 未登记姓名按原字符串比较：尾空格不命中
r = submit("DOS-RC-A", "REV-RC-04", "王采集 ", DIG_A, all_pass(["RC-1", "RC-2"]))
check("未登记姓名按原字符串比较（尾空格不命中）→ 201", r.status_code == 201, r.text)

# 回避失败不占复核单号：同一单号换人可提交
r = submit("DOS-RC-A", "REV-RC-01", "局外人甲", DIG_A, all_pass(["RC-1", "RC-2"]))
check("回避失败不占复核单号（同单号换人 201）", r.status_code == 201, r.text)
check("回避拒绝不留部分提交（该案卷仅两份成功提交）",
      db_scalar("SELECT COUNT(*) FROM review rv JOIN review_case rc ON rv.case_pk=rc.id"
                " JOIN dossier d ON rc.dossier_pk=d.id WHERE d.dossier_id='DOS-RC-A'") == 2)
rv = progress("DOS-RC-A")
check("未登记复核人意见 person_id 为 null（不伪造身份记录）",
      all(o["person_id"] is None for it in rv["items"] for o in it["opinions"]), str(rv["items"]))
check("案卷结论通过", rv["conclusion"]["status"] == "passed", str(rv["conclusion"]))

# ---------------------------------------------------------------- 5. 按别名归属判定
print("5) 回避与两人互异按提交时别名归属判定；成功意见记录身份归属")
reg_person("QC-1001", ["王采集"])
reg_person("QC-1002", ["李运输"])
reg_person("QC-2009", ["质控主管丙"])
reg_person("QC-3000")
DIG_B = make_dossier("DOS-RC-B", ["RC-1", "RC-3"])

r = submit("DOS-RC-B", "REV-RC-11", "QC-1001", DIG_B, all_pass(["RC-1", "RC-3"]))
det = r.json()["error"]["details"]
check("复核人别名是采集人/移交人 → 409 且命中两份",
      r.status_code == 409 and r.json()["error"]["code"] == "review_recusal"
      and [i["sample_code"] for i in det["items"]] == ["RC-1", "RC-3"], str(det))
check("明细记录复核人身份归属 QC-1001", det["reviewer_person_id"] == "QC-1001", str(det))

r = submit("DOS-RC-B", "REV-RC-12", "李运输", DIG_B, all_pass(["RC-1", "RC-3"]))
check("参与人姓名已登记为他人别名 → 仍按归属命中（RC-1 接收/签收）",
      r.status_code == 409
      and [i["sample_code"] for i in r.json()["error"]["details"]["items"]] == ["RC-1"], r.text)
check("两次回避拒绝均不留复核案",
      db_scalar("SELECT COUNT(*) FROM review_case rc JOIN dossier d ON rc.dossier_pk=d.id"
                " WHERE d.dossier_id='DOS-RC-B'") == 0)

r = submit("DOS-RC-B", "REV-RC-13", "QC-2009", DIG_B, all_pass(["RC-1", "RC-3"]))
check("无利益冲突复核人 201", r.status_code == 201, r.text)
check("成功提交记录当时身份归属（submission.person_id）",
      r.json()["submission"]["person_id"] == "QC-2009", r.text)

r = submit("DOS-RC-B", "REV-RC-14", "质控主管丙", DIG_B, all_pass(["RC-1", "RC-3"]))
check("第二人是第一人别名 → 409 duplicate_reviewer（按提交时归属判定）",
      r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer", r.text)
check("明细给出命中的既往复核人",
      r.json()["error"]["details"]["matched_reviewer"] == "QC-2009", r.text)

r = submit("DOS-RC-B", "REV-RC-15", "QC-3000", DIG_B, all_pass(["RC-1", "RC-3"]))
check("真正第二人 201 → 案卷完成",
      r.status_code == 201 and r.json()["review"]["stage"] == "completed", r.text)
rv = progress("DOS-RC-B")
check("复核人列表可见各自身份归属",
      [(x["seq"], x["person_id"]) for x in rv["reviewers"]] == [(1, "QC-2009"), (2, "QC-3000")],
      str(rv["reviewers"]))
check("逐份意见均携带提交时身份归属",
      all(o["person_id"] in ("QC-2009", "QC-3000") for it in rv["items"] for o in it["opinions"]))

# ---------------------------------------------------------------- 6. 第三人裁决按分歧份项回避；身份归属不追溯
print("6) 第三人裁决按分歧份项回避；成功意见身份归属不随后续增补别名追溯改变")
DIG_C = make_dossier("DOS-RC-C", ["RC-1", "RC-2"])
r = submit("DOS-RC-C", "REV-RC-21", "甲A", DIG_C,
           [op("RC-1", True), op("RC-2", False, "封签存疑")])
check("首份 201", r.status_code == 201, r.text)
r = submit("DOS-RC-C", "REV-RC-22", "乙B", DIG_C, all_pass(["RC-1", "RC-2"]))
check("第二份后 RC-2 分歧待裁决",
      r.status_code == 201 and r.json()["review"]["disputed_samples"] == ["RC-2"], r.text)

# 赵采集是 RC-2（分歧份项）的采集人 → 回避
r = submit("DOS-RC-C", "REV-RC-23", "赵采集", DIG_C, [op("RC-2", True)])
det = r.json()["error"]["details"]
check("裁决人命中分歧份项 → 409 review_recusal（仅列分歧份项）",
      r.status_code == 409 and r.json()["error"]["code"] == "review_recusal"
      and [i["sample_code"] for i in det["items"]] == ["RC-2"], str(det))

# 王采集只参与 RC-1（非分歧份项）→ 可裁决 RC-2
r = submit("DOS-RC-C", "REV-RC-24", "王采集", DIG_C, [op("RC-2", False, "维持不通过")])
check("裁决人仅参与非分歧份项 → 允许裁决（按分歧份项检查）", r.status_code == 201, r.text)
rv = r.json()["review"]
check("案卷终局不通过（RC-2）",
      rv["stage"] == "completed" and rv["conclusion"]["failed_samples"] == ["RC-2"],
      str(rv["conclusion"]))
ops = {o["reviewer"]: o for o in rv["items"][1]["opinions"]}
check("裁决意见记录当时身份归属（王采集=QC-1001 的别名）",
      ops["王采集"]["person_id"] == "QC-1001", str(ops))
check("未登记的甲A/乙B 意见 person_id 为 null",
      ops["甲A"]["person_id"] is None and ops["乙B"]["person_id"] is None, str(ops))

# 后续把 甲A 登记为别名 → 已提交意见不追溯改变
reg_person("QC-甲A", ["甲A"])
ops2 = {o["reviewer"]: o for o in progress("DOS-RC-C")["items"][1]["opinions"]}
check("后续增补别名不追溯改变已提交意见（甲A 仍为 null）",
      ops2["甲A"]["person_id"] is None, str(ops2["甲A"]))

# ---------------------------------------------------------------- 7. 第三人资格按别名归属判定
print("7) 第三人资格按提交时别名归属判定：裁决人是前两次复核人之一（含别名）→ 409")
reg_person("QC-丙", ["丙C"])
DIG_D = make_dossier("DOS-RC-D", ["RC-1", "RC-2"])
submit("DOS-RC-D", "REV-RC-31", "丙C", DIG_D, [op("RC-1", True), op("RC-2", False, "存疑")])
submit("DOS-RC-D", "REV-RC-32", "丁D", DIG_D, all_pass(["RC-1", "RC-2"]))
r = submit("DOS-RC-D", "REV-RC-33", "QC-丙", DIG_D, [op("RC-2", True)])
check("裁决人是第一复核人的主标识（同一人）→ 409 duplicate_reviewer",
      r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer", r.text)
r = submit("DOS-RC-D", "REV-RC-34", "丙C", DIG_D, [op("RC-2", True)])
check("裁决人即第一复核人本人 → 409 duplicate_reviewer",
      r.status_code == 409 and r.json()["error"]["code"] == "duplicate_reviewer", r.text)
r = submit("DOS-RC-D", "REV-RC-35", "戊E", DIG_D, [op("RC-2", True)])
check("真正第三人裁决 201", r.status_code == 201, r.text)
rv = progress("DOS-RC-D")
check("以别名提交的第一复核人意见记录归属 QC-丙",
      rv["items"][0]["opinions"][0]["person_id"] == "QC-丙", str(rv["items"][0]["opinions"]))

# ---------------------------------------------------------------- 8. 既有规则保持
print("8) 既有复核单号 / 摘要 / 幂等规则保持")
r = submit("DOS-RC-B", "REV-RC-15", "QC-3000", DIG_B, all_pass(["RC-3", "RC-1"]))
check("同复核单号同内容（换序）重试 200 幂等",
      r.status_code == 200 and r.json()["idempotent"] is True, r.text)
r = submit("DOS-RC-B", "REV-RC-15", "QC-3000", DIG_B,
           [op("RC-1", False, "改结论"), op("RC-3", True)])
check("同复核单号异内容 409 review_conflict",
      r.status_code == 409 and r.json()["error"]["code"] == "review_conflict", r.text)
r = submit("DOS-RC-B", "REV-RC-99", "QC-3000", "0" * 64, all_pass(["RC-1", "RC-3"]))
check("摘要不一致仍 422 review_digest_mismatch",
      r.status_code == 422 and r.json()["error"]["code"] == "review_digest_mismatch", r.text)
r = client.post(f"{API}/dossiers/DOS-RC-B/verify")
check("案卷核验仍 match=true（复核与登记不改封存内容）",
      r.status_code == 200 and r.json()["match"] is True, r.text)
check("健康检查可用", client.get("/health").json()["status"] == "ok")

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
raise SystemExit(1 if FAIL else 0)
