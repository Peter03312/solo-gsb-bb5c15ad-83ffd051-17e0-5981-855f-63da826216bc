# 样本交接链核验服务（Chain of Custody）

面向环境检测机构的样本保管链核验服务：登记样本采集信息，接收**移交 / 签收 / 封签检查 / 实验室入库**四类带唯一事件号的事件，自动按发生时间重算证据链，输出**当前保管人**与 **有效（valid）/ 待补证（pending）/ 无效（invalid）**结论及原因；同一交接车次的多份样本支持**批量移交一次提交**（全局唯一批次号、统一移交时间/交出人/接收人，整批原子、同号幂等）；核定入库前支持对当前有效事件提交**作废 / 替换纠错**（永久留痕、仅有效版本参与核验）；仅有效链可核定入库，核定后历史冻结；全部真正提交按先后形成**不可变版本**，质控可按样本编号 + 版本号复核任一历史版本的结论、原因、保管人、有效时间线、纠错与核定信息（版本 0 起，核定后旧版仍可查）；同一抽检的多份样本可**封存为案卷**（至少两份、可逐份指定历史版本、整卷原子、按样本编号排序清单 + 完整快照 + 可复算 SHA-256 摘要，封存后任何写入不改变旧案卷）；质控对已封存案卷作**双人独立复核**（提交须核对案卷摘要，摘要不一致拒绝写入；两名不同复核人各提交一次完整清单，意见不可改；逐份意见一致即终局，分歧份项只允许未参与前两次复核的第三人裁决，全部份项终局后形成案卷结论，任一份不通过则案卷不通过）；同一批量移交车次到达接收方后支持**整批对账签收**（以批次号、全局唯一签收单号、签收人和统一时间逐份提交覆盖原批次全部样本的对账清单：收妥项填写唯一事件号及实核封签号并作为既有 receive 事件与各样本新版本同事务提交，异常项仅填原因不生成签收事件；任一收妥项不合格或缺项/重复/外来项/事件号占用均整单拒绝不留记录；一个批次只能成功对账一次，同单号同内容重试返回原结果，查询呈现当时收妥/异常明细及纠错后各收妥事件的现时有效性）；支持**人员标识与历史别名登记**（别名全局唯一、不得转移归属，增补须携带所见修订号），复核提交时自动做**利益冲突回避**（复核人及其别名命中封存份项的采集/移交/签收/核定参与人即整单拒绝），成功意见记录当时身份归属供查询；运输期间支持**测温证据复核**（每份未核定样本一次性设定允许摄氏温度闭区间与正数最大测温间隔，按全局唯一读数号记录设备号/发生时间/有限数值，采集前、重复发生时刻或同号异内容拒绝；查询按发生时间列出超温点与相邻读数间超过限值的缺测区间，跨移交/签收时刻的缺测段自动拆分并依当前有效保管链标明每段当时保管人或在途移交，断链导致归属不能确定时标为待核；乱序补录与移交/签收纠错后即时重算归属，原始读数与旧纠错记录不改；核定后拒绝新配置与读数）。

技术栈：**Python 3.11 + FastAPI + SQLite（标准库 sqlite3）**，无外部服务依赖。

---

## 一、核验规则

| 场景 | 判定 |
|---|---|
| 样本登记 | 记录采集时间、采集人（=初始保管人）、封签号；样本编号唯一 |
| 移交（transfer） | 必须由**当前保管人**发起；**移交至签收期间不得再次移交**，否则无效 |
| 签收（receive） | 必须同时满足：匹配在途**移交单号**、**接收人**与移交单一致、**封签号**与登记一致、签收时间**晚于**移交时间 |
| 重复签收 | 同一移交单被签收两次 → 无效（无效事件不可被后续补录抵消） |
| 封签检查（seal_check） | 检查封签号与登记不符，或封签破损（`seal_intact=false`）→ 无效 |
| 缺签收 | 移交后没有合格签收 → **待补证** |
| 缺入库 | 没有实验室入库记录，或入库缺失前序移交-签收链 → **待补证** |
| 保管人断链 | 移交人不是当前保管人 / 签收无对应在途移交 → **无效** |
| 结论优先级 | **无效优先**：同时存在无效原因与待补证事项时，结论为无效（原因中附注待补证事项） |
| 乱序补录 | 事件可任意顺序上报，每次查询/核定都按 `occurred_at`（发生时间，非录入时间）整体重算 |
| 事件幂等 | 同一 `event_id` + 同内容（SHA-256 指纹）重试 → 幂等返回 HTTP 200；同 `event_id` 异内容 → HTTP 409 冲突（核定后亦保持此兼容行为） |
| 批量移交 | 同一交接车次多份样本一次提交：全局唯一**批次号** + 统一移交时间/交出人/接收人 + 每份的样本编号、移交单号、事件号；各份均作为既有 transfer 事件原子落库。批内样本编号/事件号重复、样本不存在或已核定、任一事件号已占用 → **整批失败**，不留下事件或批次。同批次号同内容重试幂等（200，样本事后已核定也成立，items 顺序与时区表示不敏感），异内容 → 409；并发提交按已提交顺序串行判定 |
| 批次查询 | 返回逐份样本的**当前核验结论**与**原事件是否仍有效**（作废/替换后反映有效链：`event_effective` / `current_event_id`）；批次头与明细的原始归属永久留痕，纠错不改变批次归属 |
| 整批对账签收 | 批量移交到达接收方后，以批次号、**全局唯一签收单号**、签收人与统一签收时间逐份提交**覆盖原批次全部样本**的对账清单：**收妥项**填唯一事件号 + 实核封签号，**异常项**仅填原因。收妥项须对应原批次该份**仍有效且处于该笔移交在途状态**的事件，且签收人=在途移交接收人、实核封签=登记封签、签收时间晚于移交时间（既有单份签收规则）。成功时各收妥项作为**既有 receive 事件**与各样本新版本**同事务提交**，异常项**仅留对账记录**（不生成签收事件、不产生版本） |
| 对账整单拒绝 | 批次不存在（404）、清单**缺项 / 批内重复 / 外来项**（422）、收妥事件号批内重复或**已占用**（409）、收妥项**不合格**（移交已失效 `transfer_not_effective` / 不在途 `transfer_not_in_transit` / 签收人不符 / 封签不符 / 签收时间不晚于移交或早于采集，422）、样本已核定（409）→ **整单拒绝且不留任何对账记录或事件**；`error.details.items` 逐项给出 `index/sample_code/event_id/code/message`，冲突优先（409） |
| 对账一次性/幂等/并发 | **一个批次只能成功对账一次**（新单号再提交 → 409 `batch_already_reconciled`）；同签收单号同内容重试 → 200 返回**原结果**（items 顺序与时区表示不敏感），异内容或跨批次复用单号 → 409 `reconciliation_conflict`；并发 `BEGIN IMMEDIATE` 串行，按已提交状态判定（批次唯一 + 单号唯一键兜底），失败单号修正后可复用 |
| 对账查询 | 呈现**当时**收妥/异常明细（原单号、原事件号、实核封签、异常原因永久留痕，**不改写原对账**）与各份当前核验结论；收妥项另给 `event_effective` / `superseded_by` / `current_event_id` 反映其 receive 事件经作废/替换纠错后的**现时有效性**；异常项这些字段为 `null`。批次查询附 `reconciliation` 摘要 |
| 核定前纠错 | 核定前可按样本编号 + 当前有效事件号提交**作废（void）/替换（replace）**，附全局唯一纠错号、操作人、原因；替换须给完整新事件内容与**未占用**的新事件号。原事件与纠错关系永久留痕，核验仅纳入有效版本并按发生时间重算结论/原因/当前保管人；被替换记录可经链查询追溯。纠错**不得跨样本**；同纠错号同内容重试幂等（200）、异内容冲突（409）；并发针对同一有效版本仅一次成功，失败不改链。核定入库后拒绝纠错 |
| 纠错与核定并发 | 写事务以 `BEGIN IMMEDIATE` 串行：先提交者生效，后到者看到其已提交结果——作废先提交则核定基于最终有效链（非 valid 则 422），核定先提交则纠错被 `history_locked` 拒绝 |
| 入库核定 | 仅 `valid` 可核定；核定后该样本登记与事件全部**拒绝改写**（HTTP 409），重复核定幂等 |
| 版本化复核 | 样本保管链按**提交先后**形成不可变版本：新登记样本从**版本 0**开始；升级前已有样本以**升级时状态**补建版本 0 基线，不推断更早历史。此后每次**真正提交**——单事件、批量移交中的该样本事件、作废/替换纠错、入库核定——各使受影响样本版本**递增一次**；同号同内容重试与失败操作**不产生版本**。补录事件的发生时间较早也**不改写旧版**；同批次各样本的版本与事件**一同提交或一同失败**。核定后旧版仍可查询 |
| 案卷封存 | 同一抽检的多份样本（**至少两份、编号不重复**）一次封存为案卷：全局唯一**案卷号**，可逐份指定**非负历史版本**，未指定者取**封存事务中的最新版本**，整卷选取来自**同一数据库一致视图**。任一样本/版本不存在、编号重复 → **整卷失败且不留案卷**。案卷永久保存所选版本完整快照与**按样本编号排序的清单**，并生成**可复算的 SHA-256 摘要**；后续补录、纠错、核定**不改变旧案卷**。同案卷号同内容重试幂等（200，即使最新版本已变化也返回原卷，items 顺序不敏感），异内容 → 409；并发提交按已提交结果判定 |
| 案卷双人复核 | 已封存案卷由**两名不同复核人**各提交一次**完整清单**（全局唯一**复核单号**、复核人标识、卷内每份样本的通过/不通过及不通过原因）。提交必须携带案卷封存**摘要**，与库存不一致 → **拒绝写入**（`review_digest_mismatch`）；复核只依据案卷封存的版本快照。清单须**恰好覆盖**卷内每份样本：缺项 / 重复份项 / 未知份项、重复复核人 → **拒绝且不留部分记录**（422/409）。两份意见逐份一致即该份**终局**（均不通过须原因文本也一致）；结论分歧或不通过原因分歧的份项 → 待裁决 |
| 第三人裁决 | 有分歧的份项**只允许未参与前两次复核的第三人**裁决：裁决人若是前两名复核人之一 → 409；裁决清单须**恰好覆盖**截止提交时的全部分歧份项（漏裁、重复、裁决非分歧份项 → 422 不留记录）。全部份项终局后才形成案卷结论：**任一份终局不通过则案卷不通过**。意见提交后**不可改**；案卷终局后再提交 → 409 越阶段 |
| 复核幂等/并发 | 同复核单号同内容重试 → 200 返回原结果（items 顺序不敏感）；同复核单号异内容（含跨案卷）→ 409 `review_conflict`。写事务 `BEGIN IMMEDIATE` 串行：并发提交按已提交状态判定（次序 seq、复核人资格、所处阶段均以已提交数据为准，唯一键兜底）；任何失败事务回滚，不留下复核案/提交/逐份意见的部分记录 |
| 人员标识与别名 | 人员以全局唯一**主标识**登记，可登记初始别名、事后**增补历史别名**；主标识与全部别名构成全局唯一命名空间——**别名不得归属多人、不得转移归属**（不提供删除/转移）；增补别名须携带**所见修订号**（登记为 1，每次成功增补 +1），过期（与当前不一致）→ 409 `identity_revision_stale`；同标识同内容登记幂等（200）、异内容冲突（409 `person_conflict`）、名称已占用（409 `alias_conflict`） |
| 复核利益冲突回避 | **两人复核按封存案卷全部份项、第三人裁决按分歧份项**，在提交事务中以**封存版本的采集人、有效事件中移交和签收的参与人、核定人**检查复核人及其别名；**命中任一份即整单拒绝**（409 `review_recusal`，明细列出份项和命中角色，**不占复核单号**、不留记录）；未登记姓名按原字符串比较；两人互异及第三人资格按**本次提交时**的别名归属判定；成功意见记录当时的身份归属（`person_id`）供查询，后续增补别名不追溯改变已提交意见，升级前旧意见不伪造身份记录（`person_id` 为 null） |
| 测温配置 | 每份**未核定**样本**一次性设定**允许摄氏温度闭区间 `[min_celsius, max_celsius]`（端点含在内）与**正数**最大测温间隔（秒）；设定后不可改：同内容重试幂等（200）、异内容冲突（409 `temp_config_conflict`）；核定后拒绝新配置（409 `history_locked`） |
| 测温读数 | 须先完成测温配置（否则 409 `temp_config_missing`）；按**全局唯一读数号**记录设备号、发生时间与**有限数值**（NaN/Infinity → 422）；**采集前**发生时刻（422 `reading_before_collection`）、同一样本**重复发生时刻**（409 `reading_time_duplicate`，时区归一后判定）、**同号异内容**（409 `reading_conflict`）拒绝；**同号同内容重试返回原记录**（200）；核定后拒绝新读数 |
| 测温复核查询 | 按发生时间列出**超温点**（读数落在闭区间之外，含方向与限值）与相邻读数间**超过最大测温间隔的缺测区间**（等于限值不报）；**跨移交/签收时刻的缺测段按当前有效链的移交、签收事件发生时刻拆分**（入库/封签检查不拆分），每段标明**当时保管人**（`custodian`）或**在途移交**（`in_transit`）；**断链**（移交人非当前保管人 / 在途期间再次移交 / 签收无对应在途移交）导致归属不能确定时，自该事件发生时刻起标为**待核**（`pending_review`）且其后不再恢复 |
| 测温归属重算 | 归属在**查询时**沿当前有效保管链即时重放计算：乱序补录读数、补录或作废/替换移交签收事件后再次查询即反映最新归属；**原始读数与旧纠错记录不改**；重复签收、签收要素不符（在途保持）、入库异常与封签检查不改变保管归属，不触发待核 |
| 测温和既有规则 | 测温配置与读数**不参与保管链核验结论、不产生保管链版本**（既有结论与版本规则保持兼容）；核定只冻结新写入，不影响测温复核查询 |

> 说明：任何事件发生时间早于样本采集时间，直接记为无效（采集前不可能发生保管动作）。

---

## 二、一键启动

### 方式 A：Docker Compose（推荐，自动初始化依赖、建表、写入演示数据）

```bash
docker compose up -d --build
docker compose logs -f          # 看到 uvicorn 启动日志即可
```

- 首次启动自动：安装 Python 依赖（镜像构建阶段）→ 建表 → 写入 3 条演示样本（有效 / 待补证 / 无效）。
- SQLite 数据持久化在宿主机 `./data/chain.db`，容器重建不丢数据；已有数据时演示数据自动跳过。
- 不需要演示数据：把 `docker-compose.yml` 中 `SEED_ON_START` 改为 `"false"`（只建空库）。

停止 / 清理：

```bash
docker compose down             # 停止（保留数据）
docker compose down && rm -f data/chain.db*   # 停止并清空数据
```

### 方式 B：本地 Python 运行

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

python -m app.seed              # 可选：初始化演示数据（幂等，可重复执行）
python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

---

## 三、服务地址与端口

| 用途 | 地址 |
|---|---|
| 服务根地址 | http://localhost:8080/ |
| 健康检查 | http://localhost:8080/health |
| Swagger 交互文档（可直接调试） | http://localhost:8080/docs |
| OpenAPI 规范 | http://localhost:8080/openapi.json |
| 业务 API 前缀 | `/api/v1` |

快速验证：

```bash
curl http://localhost:8080/health
curl http://localhost:8080/api/v1/samples        # 查看 3 条演示样本的结论
```

演示样本：

| 样本编号 | 结论 | 说明 |
|---|---|---|
| `DEMO-VALID-001` | valid | 移交→签收→封签检查→入库完整（事件故意乱序写入，演示自动重算） |
| `DEMO-PENDING-002` | pending | 已移交未签收、未入库；附运输测温演示（配置 2~8℃/30 分钟 + 4 条乱序读数，含超温点与跨移交时刻的缺测区间） |
| `DEMO-INVALID-003` | invalid | 封签检查封签号不符且破损（无效优先于入库事实） |

---

## 四、配置项（环境变量）

| 变量 | 默认值 | 说明 |
|---|---|---|
| `HOST` | `0.0.0.0` | 监听地址 |
| `PORT` | `8080` | 监听端口 |
| `DB_PATH` | Docker 内 `/data/chain.db`；本地 `./data/chain.db` | SQLite 文件路径 |
| `DATA_DIR` | Docker 内 `/data`；本地 `./data` | 数据目录（仅在未显式设置 `DB_PATH` 时用于拼默认路径） |
| `SEED_ON_START` | `true` | 启动时空库是否自动写入演示数据 |
| `LOCK_AFTER_APPROVAL` | `true` | 核定入库后是否冻结历史（按业务要求应始终为 true） |
| `DB_TIMEOUT` | `10` | SQLite 写锁等待秒数 |

---

## 五、HTTP API

所有时间均使用 ISO 8601（建议 UTC，如 `2026-09-30T02:00:00Z`；带时区偏移亦可，内部统一转 UTC）。

### 5.1 样本登记

`POST /api/v1/samples`

```json
{
  "sample_code": "S-001",
  "collected_at": "2026-09-30T01:00:00Z",
  "collector": "王采集",
  "seal_no": "SEAL-001"
}
```

- `201` 新建；同编号同内容重试 `200`（`idempotent: true`）；同编号异内容 `409 sample_conflict`。
- 响应中 `evaluation` 即时给出当前核验结论。

### 5.2 事件上报（统一入口）

`POST /api/v1/events`

事件类型与各自必填字段：

| event_type | 必填字段 | 含义 |
|---|---|---|
| `transfer` | `transfer_no`、`from_custodian`、`to_custodian` | 移交单号、交出方（须当前保管人）、接收方 |
| `receive` | `transfer_no`、`receiver`、`seal_no` | 匹配单号、签收人（须等于接收方）、签收封签号 |
| `seal_check` | `checked_seal_no`（`seal_intact` 默认 true） | 检查读取的封签号、是否完好 |
| `storage` | 无（建议 `custodian`、`storage_location`） | 实验室入库，`custodian` 默认“实验室” |

公共字段：`event_id`（全局唯一）、`sample_code`、`occurred_at`、可选 `note`（备注参与幂等指纹）。

```bash
# 移交
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-T1","event_type":"transfer","sample_code":"S-001",
  "occurred_at":"2026-09-30T02:00:00Z",
  "transfer_no":"TR-1","from_custodian":"王采集","to_custodian":"李运输"}'

# 签收（单号/接收人/封签号三匹配，且晚于移交）
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-R1","event_type":"receive","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:00:00Z",
  "transfer_no":"TR-1","receiver":"李运输","seal_no":"SEAL-001"}'

# 封签检查
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-C1","event_type":"seal_check","sample_code":"S-001",
  "occurred_at":"2026-09-30T04:30:00Z",
  "checked_seal_no":"SEAL-001","seal_intact":true}'

# 实验室入库
curl -X POST http://localhost:8080/api/v1/events -H 'Content-Type: application/json' -d '{
  "event_id":"EVT-S1","event_type":"storage","sample_code":"S-001",
  "occurred_at":"2026-09-30T05:00:00Z",
  "custodian":"实验室","storage_location":"冷库A-01"}'
```

- 新事件 `201`；同 `event_id` 同内容重试 `200`（`idempotent: true`）；异内容 `409 event_conflict`。
- 响应中 `evaluation.status` / `reasons` / `current_custodian` 为写入后重算结果。

### 5.3 查询

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/samples` | 全部样本核验摘要（结论、原因、当前保管人） |
| `GET /api/v1/samples/{sample_code}` | 单样本登记信息 + 核验摘要 |
| `GET /api/v1/samples/{sample_code}/chain` | **完整证据链**：按发生时间排列的事件时间线、当前保管人、在途移交、结论与全部原因 |

```bash
curl http://localhost:8080/api/v1/samples/S-001/chain
```

`chain` 响应要点：

```json
{
  "sample": {"sample_code": "S-001", "collector": "王采集", "seal_no": "SEAL-001", "...": "..."},
  "status": "valid",
  "reasons": ["保管链完整：采集 王采集 至当前保管人 实验室，封签一致，已入库"],
  "current_custodian": "实验室",
  "pending_transfer": null,
  "has_storage": true,
  "timeline": [
    {"event_id": "EVT-T1", "event_type": "transfer", "occurred_at": "...", "recorded_at": "...",
     "payload": {"...": "..."}, "replaces_event_id": null}
  ],
  "corrections": [
    {"correction_id": "FIX-1", "target_event_id": "EVT-C1", "action": "replace",
     "operator": "质控员", "reason": "……", "new_event_id": "EVT-C2", "created_at": "..."}
  ],
  "superseded_events": [
    {"event_id": "EVT-C1", "event_type": "seal_check", "occurred_at": "...", "payload": {"...": "..."},
     "superseded_by": "FIX-1", "replaces_event_id": null}
  ]
}
```

- `timeline` 仅含**当前有效版本**事件（`replaces_event_id` 标记该事件是否由替换产生、指向原事件号）；
- `superseded_events` 为被作废/替换的原始事件，仅用于追溯，不参与核验；
- `corrections` 为该样本全部纠错留痕（永久保留，核定后仍可查）。

### 5.4 入库核定

`POST /api/v1/samples/{sample_code}/approve`

```bash
curl -X POST http://localhost:8080/api/v1/samples/S-001/approve \
  -H 'Content-Type: application/json' -d '{"approver":"质量主管","note":"准予入库"}'
```

- `valid` → `200` 核定成功并冻结；`pending`/`invalid` → `422 approval_rejected`（响应体带回完整核验结果）。
- 核定后再上报事件或改写登记 → `409 history_locked`；重复核定幂等返回。
- 核定与纠错并发时，核定只可能基于**最终已提交的有效链**判定。

### 5.5 事件纠错（核定前：作废 / 替换）

`POST /api/v1/corrections`

公共字段：`correction_id`（全局唯一纠错号）、`sample_code`、`target_event_id`（被纠错的**当前有效**事件号）、`action`（`void` / `replace`）、`operator`、`reason`；`replace` 时必须提供 `new_event`（字段集与 5.2 事件完全一致，但**不含 `sample_code`**——纠错不得跨样本）。

```bash
# 作废：错误签收作废，核验随即只按剩余有效版本重算
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"FIX-0001","sample_code":"S-001","target_event_id":"EVT-R1",
  "action":"void","operator":"质控员","reason":"封签号录入错误，作废重报"}'

# 替换：给出完整新事件内容与未占用的新事件号
curl -X POST http://localhost:8080/api/v1/corrections -H 'Content-Type: application/json' -d '{
  "correction_id":"FIX-0002","sample_code":"S-001","target_event_id":"EVT-C1",
  "action":"replace","operator":"质控员","reason":"检查时扫码识别错误，整体替换",
  "new_event":{
    "event_id":"EVT-C2","event_type":"seal_check",
    "occurred_at":"2026-09-30T04:30:00Z",
    "checked_seal_no":"SEAL-001","seal_intact":true}}'
```

响应（`201` 新纠错；`200` 同纠错号同内容幂等重试）：

```json
{
  "correction": {
    "correction_id": "FIX-0002", "target_event_id": "EVT-C1", "action": "replace",
    "operator": "质控员", "reason": "检查时扫码识别错误，整体替换",
    "new_event_id": "EVT-C2", "created_at": "2026-09-30T05:10:00Z"
  },
  "evaluation": {"status": "valid", "reasons": ["..."], "current_custodian": "实验室", "...": "..."}
}
```

规则与语义：

| 规则 | 行为 |
|---|---|
| 准入 | 仅核定入库前可纠错；目标必须属于该样本且仍是当前有效版本 |
| 作废 `void` | 原事件标记失效（行不删除），不得携带 `new_event` |
| 替换 `replace` | 原事件标记失效，写入完整新事件；新事件号必须全局未占用（旧号永久留痕不可复用） |
| 重算 | 仅有效版本按 `occurred_at` 重算结论、原因、当前保管人；可连续替换（替换产生的新版本仍可再被纠错） |
| 幂等/冲突 | 同 `correction_id` 同内容重试 → `200`；异内容（含动作不同）→ `409 correction_conflict` |
| 并发 | 针对同一有效版本仅一次成功：落败方 `409 event_not_effective` 且事务回滚，链不变 |
| 核定后 | `409 history_locked`（包括与核定并发落败的纠错） |

纠错审计查询：

```bash
curl http://localhost:8080/api/v1/samples/S-001/corrections
# {"sample_code":"S-001","total":2,"items":[{"correction_id":"FIX-0002", ...}]}
```

被替换记录通过 `GET /api/v1/samples/{sample_code}/chain` 的 `superseded_events` / `corrections` 字段追溯（见 5.3）。

### 5.6 批量移交（同一交接车次多份样本）

`POST /api/v1/transfer-batches`

同一交接车次可能涉及多份样本：一次提交**全局唯一批次号**、**统一移交时间**、**交出人**、**接收人**，以及**每份样本的编号、移交单号和事件号**；批内各份均作为既有 `transfer` 事件在同一事务中落库，逐样本独立参与保管链核验。

```bash
curl -X POST http://localhost:8080/api/v1/transfer-batches \
  -H 'Content-Type: application/json' -d '{
    "batch_id":"BATCH-20260930-01",
    "occurred_at":"2026-09-30T02:00:00Z",
    "from_custodian":"王采集",
    "to_custodian":"李运输",
    "items":[
      {"sample_code":"S-001","transfer_no":"TR-20260930-01-01","event_id":"EVT-B01"},
      {"sample_code":"S-002","transfer_no":"TR-20260930-01-02","event_id":"EVT-B02"},
      {"sample_code":"S-003","transfer_no":"TR-20260930-01-03","event_id":"EVT-B03"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `batch_id` | 全局唯一批次号；同号同内容重试幂等、异内容冲突 |
| `occurred_at` | 批内各份统一移交时间（ISO 8601，内部归一 UTC） |
| `from_custodian` / `to_custodian` | 统一交出人 / 接收人；每份 transfer 均须满足“交出人=该样本当前保管人”（不满足不影响落库，但该份核验为断链 invalid） |
| `items[]` | 至少一份；每份含 `sample_code`、`transfer_no`、`event_id`（事件号全局唯一，落库为既有 transfer 事件） |

响应（`201` 新建；`200` 同批次号同内容幂等重试，带 `idempotent`）：

```json
{
  "batch": {
    "batch_id": "BATCH-20260930-01",
    "occurred_at": "2026-09-30T02:00:00Z",
    "from_custodian": "王采集",
    "to_custodian": "李运输",
    "item_count": 3,
    "created_at": "2026-09-30T01:59:00Z",
    "items": [
      {
        "index": 0,
        "sample_code": "S-001",
        "transfer_no": "TR-20260930-01-01",
        "event_id": "EVT-B01",
        "event_effective": true,
        "superseded_by": null,
        "current_event_id": "EVT-B01",
        "evaluation": {"status": "pending", "reasons": ["..."], "current_custodian": "王采集", "...": "..."}
      }
    ]
  },
  "idempotent": false
}
```

规则与语义：

| 规则 | 行为 |
|---|---|
| 整批成功 | 批内每份成为该样本的既有 `transfer` 事件（时间线 `batch_id` 标记批次归属），响应逐项给出提交后核验摘要 |
| 整批失败 | 批内样本编号重复 / 事件号重复 / 样本不存在（`422`），样本已核定 / 任一事件号已占用（`409`）→ 事务回滚，**不留下任何事件或批次**；`error.details.items` 逐项给出 `index/sample_code/event_id/code/message`；冲突与不合法项并存时冲突优先（409） |
| 幂等/冲突 | 同 `batch_id` 同内容 → `200`（对 items 提交顺序与时区表示不敏感；**样本事后已核定仍幂等**）；异内容（批次头任一字段或任一份内容变化）→ `409 batch_conflict`。校验失败不占用批次号，修正后可复用 |
| 批次与核定并发 | 写事务 `BEGIN IMMEDIATE` 串行，按已提交顺序判定：批次先提交则核定基于最终有效链（新增在途移交通常转为 pending → `422`）；核定先提交则批次因样本已核定整体 `409` 回滚 |
| 与单事件接口 | 批量事件号走既有单事件接口重试：同内容 `200`、异内容 `409 event_conflict`；纠错、核定接口行为不变 |
| 失败批次号 | `GET` 不存在的批次 → `404 batch_not_found` |

批次查询：

`GET /api/v1/transfer-batches/{batch_id}`

```bash
curl http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01
```

返回批次头与逐份当前状态。后续纠错只改变**有效链与查询结果**，批次原始归属可追溯：

| 字段 | 含义 |
|---|---|
| `items[].evaluation` | 该份样本**当前**核验结论（status/reasons/current_custodian 等） |
| `items[].event_effective` | 批内**原**移交事件是否仍是当前有效版本（未被作废/替换） |
| `items[].superseded_by` | 原事件被哪条纠错作废/替换（未纠错为 `null`） |
| `items[].current_event_id` | 沿替换链走到的当前有效事件号；被作废且无替换后继为 `null` |
| 批次头 / 明细 | `batch_id`、统一移交时间、交出人、接收人、份数与每份原始编号/单号/事件号**永久留痕**，纠错不增减、不改变 |

被作废/替换的批内原事件同样出现在对应样本 chain 的 `timeline.batch_id`（有效时）或 `superseded_events.batch_id`（失效后），可双向追溯。

### 5.7 整批对账签收（批量移交到达接收方）

同一批量移交车次到达接收方后，接收方以**批次号 + 全局唯一签收单号 + 签收人 + 统一签收时间**，逐份提交**恰好覆盖原批次全部样本**的对账清单。每份二选一：

- **收妥项**：`event_id`（该份唯一签收事件号，全局唯一）+ `verified_seal_no`（实核封签号），不得带 `reason`；
- **异常项**：仅填写非空 `reason`，不得带 `event_id` / `verified_seal_no`，**不生成签收事件**。

`POST /api/v1/transfer-batches/{batch_id}/reconciliation`

```bash
curl -X POST http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01/reconciliation \
  -H 'Content-Type: application/json' -d '{
    "reconciliation_no":"REC-20260930-01",
    "receiver":"李运输",
    "occurred_at":"2026-09-30T04:00:00Z",
    "items":[
      {"sample_code":"S-001","event_id":"EVT-REC-01","verified_seal_no":"SEAL-001"},
      {"sample_code":"S-002","event_id":"EVT-REC-02","verified_seal_no":"SEAL-002"},
      {"sample_code":"S-003","reason":"到货封签破损，异常拒收待处理"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `reconciliation_no` | 全局唯一签收单号；同号同内容重试幂等返回原结果，异内容（含跨批次复用）冲突 |
| `receiver` | 签收人；对每个**收妥项**须等于该份在途移交（批次原移交或其有效替换后继）的接收人 |
| `occurred_at` | 统一签收时间（ISO 8601，内部归一 UTC）；对每个收妥项须晚于该份移交时间、不早于采集时间 |
| `items[]` | 至少一份；**收妥项** = `sample_code` + `event_id` + `verified_seal_no`；**异常项** = `sample_code` + `reason` |

收妥项资格（**任一不合格即整单拒绝**，逐项返回）：

1. 原批次该份的移交事件**仍有效**——被作废且无替换后继 → `transfer_not_effective`（该份只能按异常提交）；
2. 有效移交（若经纠错替换则按替换后的新移交）**当前正处于在途状态**：未被任何合格签收（含既有单份签收）关闭 → 否则 `transfer_not_in_transit`；
3. 签收人 = 在途移交接收人（`receiver_mismatch`）、实核封签 = 登记封签（`seal_mismatch`）、签收时间晚于移交时间（`receive_time_invalid`）——即**既有单份签收规则**。

成功语义（`201` 新建；同号同内容重试 `200` 且 `idempotent: true`）：

- 各**收妥项**作为**既有 `receive` 事件**写入对应样本（`transfer_no` 自动取该份当前在途移交单号——批次原单号；原移交经替换则取替换后单号），并与各样本**新版本在同一事务提交**（每份收妥 +1 版本；异常项不产生版本）；
- **异常项仅留对账记录**：不生成 receive 事件、不关闭在途移交、不产生版本；
- 对账 receive 事件与普通签收行为一致（进入有效时间线、改变当前保管人、参与核定），可经既有 `/events` 接口同号幂等/冲突，也可被既有作废/替换纠错作用。

```json
{
  "reconciliation": {
    "reconciliation_no": "REC-20260930-01",
    "batch_id": "BATCH-20260930-01",
    "receiver": "李运输",
    "occurred_at": "2026-09-30T04:00:00Z",
    "item_count": 3, "received_count": 2, "exception_count": 1,
    "created_at": "2026-09-30T03:55:00Z",
    "items": [
      {"index": 0, "sample_code": "S-001", "transfer_no": "TR-20260930-01-01",
       "outcome": "received", "event_id": "EVT-REC-01", "verified_seal_no": "SEAL-001",
       "reason": null, "event_effective": true, "superseded_by": null,
       "current_event_id": "EVT-REC-01", "evaluation": {"...": "..."}},
      {"index": 2, "sample_code": "S-003", "transfer_no": "TR-20260930-01-03",
       "outcome": "exception", "event_id": null, "verified_seal_no": null,
       "reason": "到货封签破损，异常拒收待处理",
       "event_effective": null, "superseded_by": null, "current_event_id": null,
       "evaluation": {"...": "..."}}
    ]
  },
  "idempotent": false
}
```

规则与错误：

| 规则 | 行为 |
|---|---|
| 整单成功 | 收妥 receive 事件 + 各样本新版本 + 对账头/明细同一事务提交；异常项仅写明细 |
| 整单失败 | 缺项（`missing_sample_in_reconciliation`）/ 批内样本重复（`duplicate_sample_in_reconciliation`）/ 外来项（`unknown_sample_in_reconciliation`）/ 批内收妥事件号重复（`duplicate_event_in_reconciliation`）/ 收妥项不合格（上述 422 明细码）→ **422 `reconciliation_rejected`**；收妥事件号已占用（`event_id_occupied`）/ 样本已核定（`sample_approved`）→ **409 `reconciliation_rejected`**（冲突与不合法并存时 409 优先）。**事务回滚，不留对账、事件、版本或已占用单号** |
| 批次不存在 | `404 batch_not_found`（提交/查询均是） |
| 一次性 | 批次已成功对账，再以**任何单号**提交 → `409 batch_already_reconciled`（详情给原签收单号） |
| 幂等/冲突 | 同签收单号同内容（含 items 换序、时区表示归一）→ `200` 返回**原结果**；异内容（签收人/时间/任一份形态、事件号、封签、原因变化）或跨批次复用该单号 → `409 reconciliation_conflict`（详情给原批次号） |
| 并发 | 写事务 `BEGIN IMMEDIATE` 串行：同单号同内容一建一幂等；不同单号竞争同批次仅一单成功，落败方 409 整单回滚；批次唯一键与单号唯一键兜底，**按已提交状态判定** |
| 载荷校验 | 收妥项缺 `event_id`/`verified_seal_no`、异常项带事件字段、空原因、空清单/空单号 → 422（请求体字段校验，不占单号） |

> 说明：异常项不要求移交仍有效或在途（它只登记异常事实）；**收妥项**才要求“仍有效且在途”。全异常对账后在途移交依旧在途，此后仍可经既有单份签收接口补签，但该批次不能再次整批对账。已核定样本不得作为收妥项（`sample_approved` 409），按异常项提交不触及冻结历史。

对账查询：

`GET /api/v1/transfer-batches/{batch_id}/reconciliation`

```bash
curl http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01/reconciliation
```

- 返回**当时**的收妥/异常明细：`outcome`（`received`/`exception`）、原批次 `transfer_no`、收妥事件号、实核封签号、异常原因，以及份数统计与逐项**当前**核验摘要；
- 收妥项附该 receive 事件的**现时有效性**：`event_effective` / `superseded_by` / `current_event_id`——此后该签收被作废/替换纠错，查询反映有效链但**不改写原对账**（仍记原事件号与收妥结论）；异常项这三个字段为 `null`；
- 尚无成功对账 → `404 reconciliation_not_found`；批次查询 `GET /transfer-batches/{batch_id}` 另附 `reconciliation` 摘要（单号/签收人/时间/收妥与异常份数）。

### 5.8 保管链版本复核（质控）

质控人员按**样本编号 + 版本号**复核某份样本在指定提交版本的保管链：当时的核验结论、原因、当前保管人、有效事件时间线、纠错记录、被作废/替换留痕与核定信息；并可随时取得当前最新版本号。版本快照在提交时点固化、不可变，核定后的旧版仍可查询。

版本号规则：

- **版本 0**：新登记样本的登记基线；升级前已有样本在启动迁移时以**升级时状态**补建一条 `baseline` 版本 0（不推断更早历史）。
- **每次真正提交 +1**：单事件上报、批量移交中该样本的那份事件、**整批对账签收中该样本的收妥项**（作为既有 receive 事件，`change_type=event`、`change_ref` 为收妥事件号；异常项不产生版本）、作废/替换纠错成功、入库核定成功，各使受影响样本递增一个版本。
- **不产生版本**：同号同内容的幂等重试（样本/事件/批次/对账/纠错/核定）、以及任何失败操作（冲突 409、整批失败、整单拒绝、核定被拒 422、字段校验 422 等）。
- **按提交先后**：版本顺序是写入提交顺序，与事件 `occurred_at` 无关——乱序补录的更早事件只进入新版本，**旧版快照不变**。
- **批次原子**：同一批次各样本的版本与其事件在同一事务中，整批成功则每份 +1，整批失败则一份都不增。**整批对账同理：各收妥份版本与 receive 事件、对账记录同事务，整单拒绝则一份都不增。**

#### 版本索引

`GET /api/v1/samples/{sample_code}/versions`

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions
```

```json
{
  "sample_code": "S-001",
  "current_version": 6,
  "total": 7,
  "items": [
    {"version": 0, "change_type": "register", "change_ref": null,
     "committed_at": "2026-09-30T01:05:00Z", "status": "pending",
     "reasons": ["缺少实验室入库记录"], "current_custodian": "王采集",
     "has_storage": false, "event_count": 0, "approved_storage_at": null},
    {"version": 1, "change_type": "event", "change_ref": "EVT-T1", "...": "..."},
    {"version": 5, "change_type": "correction", "change_ref": "FIX-0002", "...": "..."},
    {"version": 6, "change_type": "approval", "change_ref": null,
     "committed_at": "2026-09-30T05:20:00Z", "status": "valid",
     "current_custodian": "实验室", "approved_storage_at": "2026-09-30T05:20:00Z"}
  ]
}
```

`items[].change_type` 取值：`register`（登记版本 0）/ `baseline`（升级基线版本 0）/ `event`（单事件）/ `batch_transfer`（批量移交中的该样本事件，`change_ref` 为批次号）/ `correction`（作废或替换纠错，`change_ref` 为纠错号）/ `approval`（入库核定）。

#### 指定版本完整快照

`GET /api/v1/samples/{sample_code}/versions/{version}`

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions/3
```

返回该版本**提交时点固化**的完整快照（结构与 5.3 的 `chain` 同源）：

```json
{
  "version_snapshot": {
    "sample": {"sample_code": "S-001", "collector": "王采集", "seal_no": "SEAL-001", "...": "..."},
    "version": 3,
    "change_type": "event",
    "change_ref": "EVT-R1",
    "committed_at": "2026-09-30T04:00:03Z",
    "status": "pending",
    "reasons": ["缺少实验室入库记录"],
    "current_custodian": "李运输",
    "pending_transfer": null,
    "has_storage": false,
    "event_count": 2,
    "timeline": [ {"event_id": "EVT-T1", "...": "..."}, {"event_id": "EVT-R1", "...": "..."} ],
    "corrections": [],
    "superseded_events": [],
    "approved_storage_at": null,
    "approved_by": null,
    "approval": null
  }
}
```

- `timeline` 为当时的**有效事件**时间线；`corrections` / `superseded_events` 为截至该版本的纠错与作废/替换留痕；
- `approval` 在核定版本及之后为 `{approved_at, approver, note}`，此前为 `null`；
- 快照一经写入不再随后续纠错/补录变化——对比不同版本即可复核保管链如何演变。

#### 当前最新版本

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions/latest
```

等价于先查索引取 `current_version` 再按号取快照。此外，样本列表/单样本摘要/证据链/事件与纠错写入响应/批次逐项 `evaluation` 中均附 `current_version` 字段（增量字段，不影响既有响应）。

#### 错误

| 情形 | HTTP | code |
|---|---|---|
| 样本编号未知（索引 / latest / 指定版本均是） | 404 | `sample_not_found` |
| 版本号不存在（含非整数、负数；详情带 `current_version` 便于定位） | 404 | `version_not_found` |

核定（冻结）只阻止新的写入，**不影响任何版本的只读查询**。

### 5.9 案卷封存（同一抽检多份样本）

质控将同一抽检的多份样本保管链封存为案卷：提交**全局唯一案卷号**与**至少两份不重复样本编号**，可逐份指定**非负历史版本**；未指定者取**封存事务中的最新版本**。整卷选取在同一事务（同一数据库一致视图）中完成；任一样本或版本不存在、编号重复 → **整卷失败且不留案卷**。案卷永久保存所选版本的完整快照与**按样本编号排序的清单**，并对封存内容生成**可复算的 SHA-256 摘要**；后续补录、纠错、核定均不改变旧案卷。

`POST /api/v1/dossiers`

```bash
curl -X POST http://localhost:8080/api/v1/dossiers \
  -H 'Content-Type: application/json' -d '{
    "dossier_id":"DOSSIER-20261001-01",
    "items":[
      {"sample_code":"S-001","version":3},
      {"sample_code":"S-002"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `dossier_id` | 全局唯一案卷号；同号同内容重试幂等、异内容冲突 |
| `items[]` | 至少两份；每份含 `sample_code` 与可选 `version`（非负整数，缺省取封存事务中最新版本） |

响应（`201` 新建；`200` 同案卷号同内容幂等重试，带 `idempotent`）：

```json
{
  "dossier": {
    "dossier_id": "DOSSIER-20261001-01",
    "item_count": 2,
    "digest": "9f2c…（64 位十六进制）",
    "created_at": "2026-10-01T02:00:00Z",
    "items": [
      {"index": 0, "sample_code": "S-001", "version": 3, "snapshot": {"version": 3, "status": "valid", "...": "..."}},
      {"index": 1, "sample_code": "S-002", "version": 6, "snapshot": {"version": 6, "status": "pending", "...": "..."}}
    ]
  },
  "idempotent": false
}
```

- `items` 无论提交顺序如何，清单恒**按样本编号升序**返回（`index` 0 起）；`snapshot` 即该样本该版本的完整保管链快照（结构同 5.8 指定版本快照）。
- 封存的是**版本快照副本**：此后样本补录、纠错、核定都不改变案卷内容。

规则与语义：

| 规则 | 行为 |
|---|---|
| 整卷成功 | 各份所选版本快照与清单一同事务落库；响应给出每份实际封存的版本号与完整快照 |
| 整卷失败 | 编号重复（`422 dossier_rejected`，明细 `duplicate_sample_in_dossier`）/ 样本不存在（`404 sample_not_found`）/ 版本不存在（`404 version_not_found`，详情附 `current_version`）→ 事务回滚，**不留下案卷**；失败不占用案卷号，修正后可复用 |
| 幂等/冲突 | 同 `dossier_id` 同内容 → `200`（对 items 提交顺序不敏感；**即使最新版本已变化也返回原卷**）；异内容（样本集合或任一份指定版本变化）→ `409 dossier_conflict` |
| 并发 | 写事务 `BEGIN IMMEDIATE` 串行：同号同内容一建一幂等；同号异内容先提交者生效、后到者 `409`，库中遵循已提交结果 |
| 份数/版本号校验 | 不足两份、版本号为负 → `422`（请求体字段校验） |

**摘要算法（可复算）**：对规范结构

```json
{"dossier_id": "案卷号",
 "items": [{"sample_code": "...", "version": 0, "snapshot": {...}}, "... 按样本编号升序"]}
```

以 `json.dumps(..., ensure_ascii=False, sort_keys=True, separators=(",", ":"))` 序列化后取 **SHA-256 十六进制**。任何人持有案卷号、清单与快照即可独立复算比对。

案卷查询：

`GET /api/v1/dossiers/{dossier_id}` —— 返回清单、各份完整快照与摘要；不存在 → `404 dossier_not_found`。

案卷核验：

```bash
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-20261001-01/verify
```

```json
{"dossier_id": "DOSSIER-20261001-01",
 "stored_digest": "9f2c…", "recomputed_digest": "9f2c…", "match": true}
```

核验按库存封存内容**重算摘要**并与留存摘要比对，返回一致与否（`match`）及两侧摘要；核验为只读操作，**不静默改写**任何存储内容——内容被篡改时报告 `match: false`，留存摘要与快照保持原样。

### 5.10 案卷双人独立复核与第三人裁决（质控）

质控人员对**已封存案卷**作双人独立复核：提交案卷号、全局唯一复核单号、复核人标识，以及卷内每份样本的通过/不通过结论和不通过原因。复核**只依据案卷封存的版本快照**——每次提交必须携带 `expected_digest`（案卷封存摘要，可先 `GET /dossiers/{id}` 取得），提交前与库存摘要核对，**不一致拒绝写入**。

流程：

1. 第一名复核人提交**完整清单**（覆盖卷内每份样本）→ 阶段 `first_reviewed`；
2. 第二名**不同**复核人再提交一次完整清单 → 两份逐份比对：
   - 意见一致（都通过；或都不通过且**原因文本一致**）→ 该份**终局**（`agreement`）；
   - 结论分歧（一过一不过，`pass_conflict`）或都不通过但**原因不同**（`reason_conflict`）→ 该份**待裁决**，阶段 `awaiting_adjudication`；
3. 有分歧时，由**未参与前两次复核的第三人**对分歧份项裁决（清单恰好覆盖全部分歧份项）→ 裁决份项终局（`adjudication`）；
4. **全部份项终局**后才在该次提交内形成案卷结论（阶段 `completed`）：**任一份终局不通过则案卷不通过**。

意见一旦提交**不可改**；无分歧时无需裁决，案卷在第二份提交时直接终局。

**利益冲突回避（防止参与样本保管的人评价自己的案卷）**：每次提交在事务内额外检查复核人（及其登记别名，见 5.11）是否参与了受审份项的保管——

- **检查范围**：两人复核（seq 1/2）按封存案卷**全部份项**；第三人裁决（seq 3）按**分歧份项**；
- **检查对象**：封存版本快照中的**采集人**（`collector`）、**有效事件**中移交的交出方/接收方（`transfer_from`/`transfer_to`）与签收的签收人（`receiver`）、**核定人**（`approver`）；
- **判定**：复核人姓名解析到已登记人员（主标识或别名）后与参与人按人员归属比较；**未登记姓名按原字符串比较**；
- **命中任一份即整单拒绝**：409 `review_recusal`，明细列出命中份项与角色，**不占复核单号**、不留任何记录（修正复核人后可复用同一单号）；
- 两名复核人互异、第三人资格均按**本次提交时**的别名归属判定（同一人换用主标识/别名提交同样拒绝）；
- 成功提交的意见记录**当时解析的人员归属**（`person_id`）供查询；后续增补别名不追溯改变已提交意见，升级前的旧意见不伪造身份记录（`person_id` 为 `null`）。

#### 提交复核 / 裁决

`POST /api/v1/dossiers/{dossier_id}/reviews`

```bash
# 第一步、第二步：两名不同复核人各提交一次完整清单
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-20261001-01/reviews \
  -H 'Content-Type: application/json' -d '{
    "review_no":"REV-20261001-001",
    "reviewer":"QC-1001 质控员甲",
    "expected_digest":"9f2c…（GET 案卷取得的 64 位摘要）",
    "items":[
      {"sample_code":"S-001","passed":true},
      {"sample_code":"S-002","passed":false,"reason":"封签号与登记不符"}
    ]
  }'

# 第三步（仅有分歧份项时）：未参与前两次复核的第三人，只裁决分歧份项
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-20261001-01/reviews \
  -H 'Content-Type: application/json' -d '{
    "review_no":"REV-20261001-003",
    "reviewer":"QC-2009 质控主管丙",
    "expected_digest":"9f2c…",
    "items":[
      {"sample_code":"S-002","passed":false,"reason":"复核确认封签号确有不符"}
    ]
  }'
```

请求字段：

| 字段 | 说明 |
|---|---|
| `review_no` | 全局唯一复核单号；同号同内容重试幂等、异内容（含跨案卷复用）冲突 |
| `reviewer` | 复核人标识；前两名必须不同，第三人不得是前两名之一 |
| `expected_digest` | 提交方持有的案卷封存 SHA-256 摘要；与库存不一致 → `422 review_digest_mismatch`，拒绝写入 |
| `items[]` | 逐份意见：`sample_code`、`passed`（通过/不通过）、`reason`（**不通过必填非空原因；通过必须为空**）。前两次须恰好覆盖案卷全部份项（顺序任意）；裁决须恰好覆盖全部分歧份项 |

响应（`201` 新提交；`200` 同复核单号同内容幂等重试，带原始 `submission` 与顶层 `idempotent: true`）：

```json
{
  "submission": {
    "review_no": "REV-20261001-001",
    "seq": 1,
    "kind": "review",
    "reviewer": "QC-1001 质控员甲",
    "person_id": "QC-1001",
    "submitted_at": "2026-10-01T03:00:00Z",
    "items": [
      {"sample_code": "S-001", "passed": true, "reason": null},
      {"sample_code": "S-002", "passed": false, "reason": "封签号与登记不符"}
    ]
  },
  "review": { "...完整复核进度（结构同 GET，见下）...": "" }
}
```

- 第三人裁决提交 `seq=3`、`kind=adjudication`，`items` 仅含本次裁决的分歧份项。
- `submission.person_id` 为本次提交时复核人解析到的人员标识（未登记为 `null`）；后续增补别名不改变该记录。
- 全部份项终局的那次提交，`review.stage=completed` 且 `review.conclusion` 固化；**终局结论在事务内一次形成，不随后续请求变化**。

#### 复核进度 / 原始意见 / 分歧 / 终局查询

`GET /api/v1/dossiers/{dossier_id}/reviews`

```bash
curl http://localhost:8080/api/v1/dossiers/DOSSIER-20261001-01/reviews
```

尚未有任何提交也可查（`stage=awaiting_review`）。返回：

```json
{
  "dossier_id": "DOSSIER-20261001-01",
  "digest": "9f2c…",
  "stage": "completed",
  "review_count": 3,
  "total_items": 2,
  "final_item_count": 2,
  "disputed_item_count": 0,
  "pending_item_count": 0,
  "all_items_final": true,
  "reviewers": [
    {"seq": 1, "kind": "review",       "reviewer": "QC-1001 质控员甲", "person_id": "QC-1001", "review_no": "REV-…-001", "submitted_at": "…"},
    {"seq": 2, "kind": "review",       "reviewer": "QC-1002 质控员乙", "person_id": "QC-1002", "review_no": "REV-…-002", "submitted_at": "…"},
    {"seq": 3, "kind": "adjudication", "reviewer": "QC-2009 质控主管丙", "person_id": "QC-2009", "review_no": "REV-…-003", "submitted_at": "…"}
  ],
  "items": [
    {
      "index": 0,
      "sample_code": "S-001",
      "version": 3,
      "status": "final",
      "opinions": [
        {"seq": 1, "kind": "review", "review_no": "REV-…-001", "reviewer": "QC-1001 质控员甲",
         "person_id": "QC-1001", "passed": true, "reason": null, "submitted_at": "…"},
        {"seq": 2, "kind": "review", "review_no": "REV-…-002", "reviewer": "QC-1002 质控员乙",
         "person_id": "QC-1002", "passed": true, "reason": null, "submitted_at": "…"}
      ],
      "disagreement": null,
      "final": {"passed": true, "reason": null, "source": "agreement"}
    },
    {
      "index": 1,
      "sample_code": "S-002",
      "version": 6,
      "status": "final",
      "opinions": [
        {"seq": 1, "kind": "review",       "reviewer": "QC-1001 质控员甲", "passed": true,  "reason": null},
        {"seq": 2, "kind": "review",       "reviewer": "QC-1002 质控员乙", "passed": false, "reason": "封签号与登记不符"},
        {"seq": 3, "kind": "adjudication", "reviewer": "QC-2009 质控主管丙", "passed": false, "reason": "复核确认封签号确有不符"}
      ],
      "disagreement": null,
      "final": {"passed": false, "reason": "复核确认封签号确有不符", "source": "adjudication"}
    }
  ],
  "disputed_samples": [],
  "conclusion": {
    "passed": false,
    "status": "rejected",
    "reason": "存在终局不通过份项，案卷不通过",
    "failed_samples": ["S-002"]
  },
  "finalized_at": "2026-10-01T03:10:00Z"
}
```

字段要点：

| 字段 | 含义 |
|---|---|
| `stage` | `awaiting_review`（无人提交）/ `first_reviewed`（仅第一份）/ `awaiting_adjudication`（两份齐、有分歧待裁决）/ `completed`（案卷结论已形成） |
| `items[].status` | 逐份状态：`pending`（两份未齐）/ `disputed`（有分歧待裁决）/ `final`（已终局） |
| `items[].opinions` | 该份全部**原始意见**（含 seq、kind、复核单号、复核人、提交时人员归属 `person_id`、通过与否、原因、提交时间）；一致份项两条，裁决份项三条，永久留痕 |
| `items[].disagreement` | 待裁决时给出分歧类型（`pass_conflict` 结论分歧 / `reason_conflict` 不通过原因分歧）与两人意见对照；终局后为 `null`（原始意见仍在 `opinions` 可追溯） |
| `items[].final.source` | 终局来源：`agreement`（两份一致；不通过时 `reason` 为共同原因）/ `adjudication`（第三人裁决；`reason` 为裁决原因） |
| `disputed_samples` | 当前待裁决份项编号列表 |
| `conclusion` | 案卷终局结论：`passed`/`status`（`passed`/`rejected`）、`failed_samples`、形成时间 `finalized_at`；未终局为 `null` |

规则与错误：

| 情形 | HTTP | code |
|---|---|---|
| 案卷不存在（提交/查询） | 404 | `dossier_not_found` |
| `expected_digest` 与封存摘要不一致 | 422 | `review_digest_mismatch`（详情给两侧摘要；失败不占用复核单号） |
| 前两次清单缺项 / 重复份项 / 未知份项 | 422 | `review_rejected`（明细 `missing_sample_in_review` / `duplicate_sample_in_review` / `unknown_sample_in_review`，逐项给出） |
| 通过带原因 / 不通过无原因 | 422 | 请求体字段校验 |
| 两名复核人相同（含按别名归属为同一人）；第三人是前两名之一（含别名） | 409 | `duplicate_reviewer` |
| 复核人（含其别名）命中受审份项的采集/移交/签收/核定参与人 | 409 | `review_recusal`（明细 `details.items` 列出份项与命中角色；不占复核单号） |
| 裁决清单漏裁 / 重复 / 裁决非分歧份项 | 422 | `review_rejected`（明细 `missing_adjudication` / `duplicate_sample_in_review` / `adjudication_not_allowed`） |
| 无分歧却提交裁决；案卷终局后再提交 | 409 | `review_stage_rejected`（越阶段） |
| 同复核单号异内容（含跨案卷复用该单号） | 409 | `review_conflict` |
| 并发冲突（同次序/同复核人/同单号竞争落败） | 409 | `review_conflict`（按已提交状态判定） |

- 所有失败均在写入前校验或整事务回滚，**不留下**复核案、提交记录或逐份意见的部分记录；失败的复核单号可复用。
- 复核只读取案卷封存的快照与摘要，不影响案卷本身（`verify` 仍 `match=true`），也不影响任何样本版本。

### 5.11 人员标识与历史别名（复核回避登记）

质控复核回避依赖统一的人员登记：每名人员以**全局唯一主标识**登记，可登记初始历史别名、事后**增补历史别名**。主标识与全部别名构成**全局唯一命名空间**——任何名称只归属一人，**别名不得归属多人、不得转移归属**（不提供删除/转移）。复核提交时把复核人与案卷参与人解析到人员归属后比较；**未登记姓名按原字符串比较**。

#### 登记人员

`POST /api/v1/persons`

```bash
curl -X POST http://localhost:8080/api/v1/persons \
  -H 'Content-Type: application/json' -d '{
    "person_id":"QC-1001",
    "aliases":["质控员甲","甲某"]
  }'
```

- `201` 新建（`revision=1`）；同主标识同内容（含初始别名集合，顺序不敏感）重试 `200`（`idempotent: true`）；同主标识异内容 `409 person_conflict`。
- 主标识或任一初始别名已被占用（他人主标识或别名）→ `409 alias_conflict`（明细给出归属人 `owner_person_id`）；初始别名重复或与主标识相同 → `422`（字段校验）。

响应：

```json
{
  "person": {
    "person_id": "QC-1001",
    "revision": 1,
    "aliases": ["质控员甲", "甲某"],
    "created_at": "2026-10-01T02:00:00Z"
  },
  "idempotent": false
}
```

#### 增补历史别名（须携带所见修订号）

`POST /api/v1/persons/{person_id}/aliases`

```bash
# 先 GET 取得当前修订号，再携带该修订号增补
REV=$(curl -s http://localhost:8080/api/v1/persons/QC-1001 | python3 -c 'import sys,json;print(json.load(sys.stdin)["person"]["revision"])')
curl -X POST http://localhost:8080/api/v1/persons/QC-1001/aliases \
  -H 'Content-Type: application/json' -d "{\"alias\":\"王甲\",\"expected_revision\":$REV}"
```

- 成功 `201`：别名落库且 `revision +1`（响应返回最新人员视图）。
- 所见修订号与当前不一致（过期，如并发增补后重试旧请求）→ `409 identity_revision_stale`（明细给 `expected_revision`/`current_revision`，重新查询后再试）。
- 别名已归属任何人（含本人、含任何主标识）→ `409 alias_conflict`；人员不存在 → `404 person_not_found`。
- 并发增补同一修订号：`BEGIN IMMEDIATE` 串行，按已提交修订号判定，仅一次成功。

#### 查询

| 方法与路径 | 说明 |
|---|---|
| `GET /api/v1/persons` | 全部已登记人员（主标识、当前修订号、全部别名） |
| `GET /api/v1/persons/{person_id}` | 单人员查询（增补别名前取 `revision`）；不存在 → `404 person_not_found` |

人员登记如何参与复核回避见 5.10「利益冲突回避」：复核人姓名命中任何已登记主标识/别名即按该人员归属判定；成功意见在 `reviewers[].person_id` 与 `items[].opinions[].person_id` 中记录提交时的归属供查询。

### 5.12 运输测温证据复核（未核定样本）

环境样本运输期间须复核测温证据：每份**未核定**样本**一次性设定**允许摄氏温度闭区间与正数最大测温间隔，随后按**全局唯一读数号**记录读数；复核查询按发生时间列出**超温点**与**缺测区间**，并依**当前有效保管链**标明每段当时保管人或在途移交。测温配置与读数**不参与保管链核验结论、不产生保管链版本**（既有结论与版本规则保持兼容）。

#### 一次性设定测温配置

`POST /api/v1/temperature-configs`

```bash
curl -X POST http://localhost:8080/api/v1/temperature-configs \
  -H 'Content-Type: application/json' -d '{
    "sample_code":"S-001","min_celsius":2.0,"max_celsius":8.0,"max_interval_seconds":1800}'
```

| 字段 | 说明 |
|---|---|
| `sample_code` | 样本编号（须已登记、未核定） |
| `min_celsius` / `max_celsius` | 允许摄氏温度闭区间端点（含端点；`min ≤ max`，须为有限数值） |
| `max_interval_seconds` | 正数最大测温间隔（秒，整数） |

- `201` 新建；同样本同内容重试 `200`（`idempotent: true`，核定后亦保持）；异内容 `409 temp_config_conflict`（一次性设定，不可更改）。
- 样本不存在 `404 sample_not_found`；已核定样本的新配置 `409 history_locked`；下限大于上限、间隔非正整数、温度非有限数值 → `422`（字段校验）。

#### 记录测温读数

`POST /api/v1/temperature-readings`

```bash
curl -X POST http://localhost:8080/api/v1/temperature-readings \
  -H 'Content-Type: application/json' -d '{
    "sample_code":"S-001","reading_id":"RD-0001","device_id":"DEV-COLD-01",
    "occurred_at":"2026-09-30T02:30:00Z","value_celsius":4.5}'
```

| 字段 | 说明 |
|---|---|
| `reading_id` | 全局唯一读数号；同号同内容重试幂等返回原记录，异内容冲突 |
| `device_id` | 测温设备号 |
| `occurred_at` | 读数发生时间（ISO 8601，内部归一 UTC；不得早于样本采集时间） |
| `value_celsius` | 摄氏温度读数（有限数值，NaN/Infinity → 422） |

- `201` 新建；同读数号同内容重试 `200` 返回**原记录**（核定后亦保持）；同号异内容 `409 reading_conflict`。
- 未设定测温配置 `409 temp_config_missing`；发生时刻早于采集 `422 reading_before_collection`；同一样本**重复发生时刻**（时区归一后判定）`409 reading_time_duplicate`；已核定样本的新读数 `409 history_locked`。
- 读数可**任意顺序补录**，复核查询恒按发生时间排列。

#### 测温证据复核查询

`GET /api/v1/samples/{sample_code}/temperature`

```bash
curl http://localhost:8080/api/v1/samples/S-001/temperature
```

```json
{
  "sample_code": "S-001",
  "config": {"sample_code": "S-001", "min_celsius": 2.0, "max_celsius": 8.0,
             "max_interval_seconds": 1800, "configured_at": "2026-09-30T01:20:00Z"},
  "reading_count": 4,
  "readings": [
    {"reading_id": "RD-0001", "device_id": "DEV-COLD-01", "occurred_at": "2026-09-30T01:30:00Z",
     "value_celsius": 4.2, "in_range": true, "recorded_at": "2026-09-30T02:00:00Z"}
  ],
  "out_of_range_points": [
    {"reading_id": "RD-0003", "device_id": "DEV-COLD-01", "occurred_at": "2026-09-30T03:40:00Z",
     "value_celsius": 9.6, "direction": "above_max", "limit_celsius": 8.0,
     "attribution": {"kind": "in_transit", "transfer_no": "TR-1",
                     "from_custodian": "王采集", "to_custodian": "李运输",
                     "transfer_event_id": "EVT-T1"}}
  ],
  "gap_intervals": [
    {"start": "2026-09-30T01:30:00Z", "end": "2026-09-30T02:30:00Z",
     "duration_seconds": 3600, "max_interval_seconds": 1800,
     "before_reading_id": "RD-0001", "after_reading_id": "RD-0002",
     "segments": [
       {"start": "2026-09-30T01:30:00Z", "end": "2026-09-30T02:00:00Z", "duration_seconds": 1800,
        "attribution": {"kind": "custodian", "custodian": "王采集"}},
       {"start": "2026-09-30T02:00:00Z", "end": "2026-09-30T02:30:00Z", "duration_seconds": 1800,
        "attribution": {"kind": "in_transit", "transfer_no": "TR-1",
                        "from_custodian": "王采集", "to_custodian": "李运输",
                        "transfer_event_id": "EVT-T1"}}
     ]}
  ]
}
```

要点：

- `out_of_range_points`（超温点）：读数落在允许闭区间**之外**（`direction` = `above_max` / `below_min`，`limit_celsius` 为被超出的限值端点；端点值本身在范围内）；按发生时间排列，附当时归属。
- `gap_intervals`（缺测区间）：相邻读数间隔**严格大于**最大测温间隔才列出（等于限值不报）；`before/after_reading_id` 为区间两侧读数号。
- **跨移交/签收时刻的缺测段须拆分**：区间内含当前有效链的移交、签收事件发生时刻时，在该时刻拆分为多段（`segments`，时长求和等于区间时长）；入库、封签检查时刻不拆分，段归属按段起始时刻判定。
- `attribution`（归属）三种形态：`{"kind":"custodian","custodian":...}` 当时保管人；`{"kind":"in_transit","transfer_no":...,"from_custodian":...,"to_custodian":...,"transfer_event_id":...}` 在途移交；`{"kind":"pending_review","detail":...}` **待核**——保管链断链（移交人非当前保管人 / 在途期间再次移交 / 签收无对应在途移交）导致归属不能确定，自该事件发生时刻起生效且其后不再恢复。重复签收、签收要素不符（在途保持）、入库异常与封签检查不改变保管归属，不触发待核。
- 归属在**查询时**沿当前有效链即时重算：乱序补录读数、补录或作废/替换移交签收事件后再次查询即反映最新归属；**原始读数与旧纠错记录不改**。读数/事件时刻对齐时按事件已发生处理。
- 未设定测温配置 → `404 temp_config_not_found`；样本不存在 → `404 sample_not_found`；核定后查询仍可用（只冻结新写入）。

### 错误响应统一格式

```json
{"error": {"code": "event_conflict", "message": "……", "details": {}}}
```

| HTTP | code | 触发 |
|---|---|---|
| 404 | `sample_not_found` / `event_not_found` | 样本不存在 / 纠错目标事件不存在 |
| 404 | `version_not_found` | 版本复核的版本号不存在（非整数/负数/超出现有版本；详情附 `current_version`） |
| 409 | `sample_conflict` / `event_conflict` | 同编号异内容 / 同事件号异内容 |
| 409 | `history_locked` | 已核定入库后改写历史或纠错 |
| 409 | `correction_conflict` | 同纠错号异内容（含动作 void↔replace 不一致） |
| 409 | `correction_cross_sample` | 纠错目标事件不属于提交的样本（禁止跨样本） |
| 409 | `event_not_effective` | 目标事件已被作废/替换，或并发下被抢先纠错（非当前有效版本） |
| 409 | `new_event_id_occupied` | 替换所用新事件号已被占用 |
| 404 | `batch_not_found` | 批次查询/对账提交/对账查询的批次号不存在 |
| 409 | `batch_conflict` | 同批次号异内容重试（含批次头或任一份样本内容变化） |
| 409/422 | `batch_rejected` | 整批失败：409=样本已核定（`sample_approved`）/事件号已占用（`event_id_occupied`）；422=批内样本重复（`duplicate_sample_in_batch`）/事件号重复（`duplicate_event_in_batch`）/样本不存在（`sample_not_found`）；逐项原因见 `details.items` |
| 404 | `reconciliation_not_found` | 批次尚无成功对账签收记录（对账查询） |
| 409/422 | `reconciliation_rejected` | 整批对账整单拒绝：409=收妥事件号已占用（`event_id_occupied`）/收妥样本已核定（`sample_approved`）；422=缺项（`missing_sample_in_reconciliation`）/批内重复（`duplicate_sample_in_reconciliation`）/外来项（`unknown_sample_in_reconciliation`）/收妥事件号批内重复（`duplicate_event_in_reconciliation`）/移交已失效（`transfer_not_effective`）/不在途（`transfer_not_in_transit`）/签收人不符（`receiver_mismatch`）/封签不符（`seal_mismatch`）/签收时间不晚于移交或早于采集（`receive_time_invalid`）；逐项见 `details.items`，冲突优先 409 |
| 409 | `reconciliation_conflict` | 同签收单号异内容重试（签收人/时间/任一份形态、事件号、封签、原因变化），或跨批次复用该单号（详情给原批次号） |
| 409 | `batch_already_reconciled` | 批次已成功对账一次后再以任何单号提交（详情给原签收单号）；并发竞争落败同码 |
| 422 | `approval_rejected` | 非有效链尝试核定（或 Pydantic 字段校验失败） |
| 404 | `dossier_not_found` | 案卷查询/核验/复核提交/复核查询的案卷号不存在 |
| 409 | `dossier_conflict` | 同案卷号异内容重试（样本集合或任一份指定版本变化） |
| 422 | `dossier_rejected` | 整卷失败：卷内样本编号重复（`duplicate_sample_in_dossier`，明细见 `details.items`）；样本/版本不存在分别按 `sample_not_found`/`version_not_found` 报 404，均不留案卷 |
| 422 | `review_digest_mismatch` | 复核提交携带的案卷摘要与封存摘要不一致，拒绝写入（详情给两侧摘要） |
| 422 | `review_rejected` | 复核/裁决清单不合法：缺项（`missing_sample_in_review`/`missing_adjudication`）、重复份项（`duplicate_sample_in_review`）、未知份项（`unknown_sample_in_review`）、裁决非分歧份项（`adjudication_not_allowed`）；逐项明细见 `details.items`，整单不留记录 |
| 409 | `duplicate_reviewer` | 两名独立复核人相同，或第三人曾参与前两次复核（均按提交时的人员标识/别名归属判定） |
| 409 | `review_recusal` | 利益冲突回避：复核人（含其别名）命中受审份项的采集/移交/签收/核定参与人，整单拒绝（明细 `details.items` 列出份项与命中角色、`reviewer_person_id` 给出复核人归属；不占复核单号、不留记录） |
| 409 | `review_stage_rejected` | 越阶段提交：无分歧时提交裁决、裁决/终局后再次提交（意见不可改） |
| 409 | `review_conflict` | 同复核单号异内容（含跨案卷占用该单号），或并发竞争落败（按已提交状态判定） |
| 404 | `person_not_found` | 人员查询/增补别名的人员标识不存在 |
| 409 | `person_conflict` | 同人员标识异内容登记（同标识登记内容不一致） |
| 409 | `alias_conflict` | 名称已归属他人（或本人）：别名不得归属多人、不得转移归属（明细给出 `owner_person_id`） |
| 409 | `identity_revision_stale` | 增补别名携带的所见修订号已过期（明细给 `expected_revision`/`current_revision`） |
| 409 | `temp_config_conflict` | 样本已设定测温配置且内容不一致（一次性设定，不可更改） |
| 404 | `temp_config_not_found` | 测温复核查询的样本尚未设定测温配置 |
| 409 | `temp_config_missing` | 未设定测温配置即记录读数 |
| 409 | `reading_conflict` | 同读数号异内容（含跨样本复用读数号） |
| 409 | `reading_time_duplicate` | 同一样本同一发生时刻已存在读数（时区归一后判定） |
| 422 | `reading_before_collection` | 读数发生时间早于样本采集时间 |

---

## 六、Python 调用示例

仓库内置一个**仅用标准库**（无需安装 requests）的端到端示例脚本：

```bash
python examples/call_api.py
```

脚本依次演示：登记 → 移交 → 签收 → 封签检查 → 入库 → 幂等重试 → 冲突 → 查询证据链 → **纠错替换 / 同号幂等 / 链追溯 / 作废演示 / 纠错审计查询** → 核定 → 核定后改写与纠错被拒 → **批量移交提交 / 同号幂等 / 异内容冲突 / 整批失败 / 批次查询 / 批内作废后查询变化** → **整批对账签收（收妥+异常混合提交、收妥项落为既有 receive 并升版本、同单号幂等/异内容冲突/批次一次性、缺项整单拒绝、对账查询）** → **保管链版本复核（版本索引、指定历史版本快照、当前最新版本、未知样本/版本报错）** → **案卷封存（指定/缺省版本封存、案卷查询、摘要核验、同号幂等重试、异内容冲突、整卷失败）** → **案卷双人独立复核（摘要核对、两份完整清单、重复复核人拒绝、分歧形成、第三人资格与清单校验、裁决终局、案卷不通过、同号幂等/异内容冲突、越阶段拒绝、复核查询）** → **人员标识与复核回避（登记人员与初始别名、携带修订号增补别名、过期修订号拒绝、别名归属冲突、复核人别名命中采集/移交人整单拒绝、未登记核定人按原字符串命中、回避失败不占复核单号、意见身份归属查询）** → **运输测温证据（一次性设定配置、同内容幂等/异内容冲突、乱序读数记录、同号幂等返回原记录、重复发生时刻/采集前拒绝、超温点与缺测区间复核查询、跨移交签收拆分与逐段归属、签收作废纠错后归属重算）**。也可直接在 http://localhost:8080/docs 的 Swagger UI 中点击调用。

版本复核也可用 curl 直接体验（完整脚本见下）：

```bash
curl http://localhost:8080/api/v1/samples/S-001/versions            # 版本索引 + current_version
curl http://localhost:8080/api/v1/samples/S-001/versions/3          # 指定版本完整快照
curl http://localhost:8080/api/v1/samples/S-001/versions/latest     # 当前最新版本快照
```

整批对账签收 curl 速览（清单须覆盖原批次全部样本；收妥项=事件号+实核封签，异常项=原因）：

```bash
# 提交对账（收妥 S-001/S-002，异常 S-003）
curl -X POST http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01/reconciliation \
  -H 'Content-Type: application/json' -d '{
    "reconciliation_no":"REC-20260930-01","receiver":"李运输",
    "occurred_at":"2026-09-30T04:00:00Z",
    "items":[{"sample_code":"S-001","event_id":"EVT-REC-01","verified_seal_no":"SEAL-001"},
             {"sample_code":"S-002","event_id":"EVT-REC-02","verified_seal_no":"SEAL-002"},
             {"sample_code":"S-003","reason":"封签破损，异常拒收"}]}'
# 同单号同内容重试 → 200 原结果；换新内容 → 409 reconciliation_conflict；
# 换新单号再提交 → 409 batch_already_reconciled（一批次仅一次）
curl http://localhost:8080/api/v1/transfer-batches/BATCH-20260930-01/reconciliation   # 当时明细 + 现时有效性
```

案卷封存 curl 速览：

```bash
curl -X POST http://localhost:8080/api/v1/dossiers -H 'Content-Type: application/json' -d '{
  "dossier_id":"DOSSIER-001",
  "items":[{"sample_code":"S-001","version":3},{"sample_code":"S-002"}]}'   # 封存（S-002 取最新版本）
curl http://localhost:8080/api/v1/dossiers/DOSSIER-001                     # 查询清单/快照/摘要
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/verify      # 核验：重算摘要比对
```

案卷双人复核 curl 速览（先取案卷摘要，再由两名不同复核人各提交完整清单；有分歧时第三人裁决）：

```bash
DIG=$(curl -s http://localhost:8080/api/v1/dossiers/DOSSIER-001 | python3 -c 'import sys,json;print(json.load(sys.stdin)["dossier"]["digest"])')
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews -H 'Content-Type: application/json' -d "{
  \"review_no\":\"REV-001\",\"reviewer\":\"QC-甲\",\"expected_digest\":\"$DIG\",
  \"items\":[{\"sample_code\":\"S-001\",\"passed\":true},{\"sample_code\":\"S-002\",\"passed\":true}]}"   # 第一份
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews -H 'Content-Type: application/json' -d "{
  \"review_no\":\"REV-002\",\"reviewer\":\"QC-乙\",\"expected_digest\":\"$DIG\",
  \"items\":[{\"sample_code\":\"S-001\",\"passed\":false,\"reason\":\"封签不符\"},{\"sample_code\":\"S-002\",\"passed\":true}]}"  # 第二份（S-001 分歧）
curl http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews             # 进度/原始意见/分歧/终局
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews -H 'Content-Type: application/json' -d "{
  \"review_no\":\"REV-003\",\"reviewer\":\"QC-丙\",\"expected_digest\":\"$DIG\",
  \"items\":[{\"sample_code\":\"S-001\",\"passed\":false,\"reason\":\"维持不通过\"}]}"   # 第三人只裁决分歧份项
```

人员标识与复核回避 curl 速览（登记人员 → 增补别名 → 命中回避 → 查询身份归属）：

```bash
# 登记人员（主标识 + 初始别名；同标识同内容重试幂等）
curl -X POST http://localhost:8080/api/v1/persons -H 'Content-Type: application/json' -d '{
  "person_id":"QC-1001","aliases":["质控员甲"]}'
# 增补别名须携带所见修订号（先 GET 取得；过期 409 identity_revision_stale）
REV=$(curl -s http://localhost:8080/api/v1/persons/QC-1001 | python3 -c 'import sys,json;print(json.load(sys.stdin)["person"]["revision"])')
curl -X POST http://localhost:8080/api/v1/persons/QC-1001/aliases -H 'Content-Type: application/json' -d "{
  \"alias\":\"王采集\",\"expected_revision\":$REV}"
# 复核人 QC-1001（别名“王采集”是封存版本的采集人/移交人）提交 → 409 review_recusal，明细列出份项与命中角色
curl -X POST http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews -H 'Content-Type: application/json' -d "{
  \"review_no\":\"REV-004\",\"reviewer\":\"QC-1001\",\"expected_digest\":\"$DIG\",
  \"items\":[{\"sample_code\":\"S-001\",\"passed\":true},{\"sample_code\":\"S-002\",\"passed\":true}]}"
# 换无利益冲突的复核人可复用同一复核单号；成功意见在 reviewers/opinions 中记录 person_id
curl http://localhost:8080/api/v1/dossiers/DOSSIER-001/reviews
```

运输测温证据 curl 速览（先一次性设定配置，再记录读数，最后复核查询）：

```bash
# 一次性设定：允许 2~8℃、最大测温间隔 1800 秒（同内容重试幂等，异内容 409）
curl -X POST http://localhost:8080/api/v1/temperature-configs -H 'Content-Type: application/json' -d '{
  "sample_code":"S-001","min_celsius":2.0,"max_celsius":8.0,"max_interval_seconds":1800}'
# 记录读数（全局唯一读数号；同号同内容重试返回原记录）
curl -X POST http://localhost:8080/api/v1/temperature-readings -H 'Content-Type: application/json' -d '{
  "sample_code":"S-001","reading_id":"RD-0001","device_id":"DEV-COLD-01",
  "occurred_at":"2026-09-30T02:30:00Z","value_celsius":4.5}'
# 复核查询：超温点 + 缺测区间（跨移交/签收时刻拆分，逐段标明保管人/在途/待核）
curl http://localhost:8080/api/v1/samples/S-001/temperature
```

---

## 七、自测

```bash
python -m tests.test_chain          # 既有规则：42 项断言
python -m tests.test_corrections    # 纠错：65 项断言（含并发）
python -m tests.test_batches        # 批量移交：83 项断言（含并发）
python -m tests.test_reconciliations # 整批对账签收：93 项断言（含并发）
python -m tests.test_versions       # 版本复核：94 项断言（含旧库迁移基线）
python -m tests.test_dossiers       # 案卷封存：61 项断言（含并发与篡改核验）
python -m tests.test_reviews        # 案卷双人复核：80 项断言（含并发）
python -m tests.test_identities     # 人员标识与复核回避：67 项断言（含并发）
python -m tests.test_temperature    # 运输测温证据：83 项断言（含并发）
```

- `test_chain` 覆盖 14 类场景：登记幂等/冲突、四类无效情形（非保管人移交、在途再移交、签收四要素不符、重复签收、封签不符/破损）、待补证两情形、乱序补录重算、核定准入与冻结、事件早于采集、无效优先等。
- `test_corrections` 覆盖：作废后仅有效版本重算（结论/原因/保管人）、替换生效与多跳替换、原事件与纠错关系留痕及链追溯、审计查询、同纠错号幂等/异内容冲突、跨样本拒绝、非有效版本拒绝、新事件号占用、replace/void 载荷校验、核定后拒绝纠错、同事件号 200/409 兼容、同有效版本并发仅一次成功、纠错与核定并发两种合法结局（可重复运行）。
- `test_batches` 覆盖：整批提交各份成为既有 transfer（逐份 pending/invalid 与在途信息、时间线批次归属）、整批失败四类原因（批内样本/事件号重复、样本不存在、已核定、事件号占用）且不留批次/事件/明细、冲突聚合与 409/422 优先、同批次号幂等（items 换序、时区归一、核定后仍 200）与异内容 409、批次查询随作废/替换更新（`event_effective`/`current_event_id`）与原始归属留痕、单事件接口对批量事件号 200/409、同批并发一建一幂等、异批事件号交叠一胜整批回滚、批次与核定并发按已提交顺序判定两种结局、载荷校验与失败批次号复用。
- `test_reconciliations` 覆盖：收妥+异常混合整批提交（收妥落为既有 receive 事件并升版本、异常仅留记录不升版本不关闭在途、统一签收时间/人、按原批次顺序呈现）、整单拒绝十类原因（批次不存在、缺项、批内重复、外来项、收妥事件号批内重复/已占用、移交已作废 `transfer_not_effective`、在途已被单签关闭、移交替换后接收人/单号按当前有效版本判定、签收人/封签/时间四类不合格、样本已核定 409）且不留对账/receive/版本、载荷形态校验（异常带事件字段、收妥缺字段、空原因、空清单）、同单号同内容幂等（换序/时区归一/核定后仍 200）与异内容（改人/时间/封签/收妥↔异常）及跨批次复用 409、批次一次性（新单号 409、失败单号修正后可复用）、已核定份仅可按异常提交、纠错后查询原对账不改写而 `event_effective`/`current_event_id` 反映作废与替换、对账 receive 走单事件接口 200/409 与核定冻结、并发两场景（同单号一建一幂等、异单号一胜一 `batch_already_reconciled` 落败回滚）、全收妥补齐可核定/全异常不关闭在途可补单签、数据库层终态一致性。
- `test_versions` 覆盖：登记即版本 0（快照字段）、单事件每次真正提交 +1 而同号重试/冲突失败不产生版本、快照不可变且补录较早事件不改旧版（版本按提交先后、时间线按发生时间重排）、批量移交各样本版本随事件同提交/整批失败全不增/幂等不增/失败批次号修正后复用、替换与作废纠错 +1 而幂等与对失效事件纠错不增、核定 +1 而核定被拒/重复核定/冻结后写入不增、核定后全部旧版仍可查、版本索引/`latest`/指定版本端点、未知样本与不存在/非数字/负数版本号报错、快照完整字段、旧库迁移为已有样本补建 `baseline` 版本 0 且幂等、迁移后版本从 1 继续。
- `test_dossiers` 覆盖：基本封存（指定/缺省版本、清单按样本编号排序、快照与版本复核一致、摘要可独立复算）、整卷失败五类（编号重复/样本不存在/版本不存在/份数不足/负数版本）且不留案卷与案卷号可复用、同号同内容（含换序）幂等且最新版本变化后仍返回原卷、异内容 409、封存后补录/纠错/核定不改变旧案卷、核验一致/篡改报不一致且不静默改写、不存在案卷查询与核验 404、同号同内容并发一建一幂等、同号异内容并发一建一冲突且遵循已提交结果、既有版本与写入接口行为保持。
- `test_reviews` 覆盖：未提交进度、摘要核对（不一致 422 且不留记录/不占复核单号）、两份完整清单校验（缺项/重复份项/未知份项 422 且不留部分记录、通过带原因/不通过无原因字段校验）、重复复核人 409（含换单号）、两份一致的通过/不通过终局（共同原因留痕、无分歧无需裁决）、结论分歧与不通过原因分歧识别、第三人资格（前两名禁止裁决）与裁决清单校验（漏裁/重复/裁决非分歧份项）、裁决终局与案卷不通过、原始意见/分歧对照/逐份终局/案卷结论查询、同复核单号同内容（含换序）200 幂等/异内容及跨案卷占用 409、终局后与无分歧时越阶段提交 409、不存在案卷 404、并发三场景（不同复核人各占一次序、同复核人仅一成、同号同内容一建一幂等）。
- `test_identities` 覆盖：人员登记（同内容幂等/异内容冲突/名称占用/初始别名字段校验）、增补别名（所见修订号成功与过期拒绝、别名归属本人/他人/主标识冲突、未知人员 404、并发同修订号仅一次成功）、复核回避（采集人/移交人/接收人/签收人/核定人各角色命中、多份命中列全、未登记姓名按原字符串比较、不占复核单号且不留记录、两人复核按全部份项、第三人裁决按分歧份项）、两人互异与第三人资格按提交时别名归属判定、成功意见记录身份归属（person_id）且后续增补别名不追溯、未登记/旧意见不伪造身份记录、既有复核单号/摘要/幂等规则保持。
- `test_temperature` 覆盖：测温配置一次性设定（同内容幂等/异内容冲突/区间与间隔及有限数值校验/未知样本 404）、读数记录（未配置 409、采集前 422、重复发生时刻 409 含时区归一、同号同内容返回原记录/同号异内容 409、非有限数值 422）、复核查询（超温点方向与限值、边界值在范围内、缺测区间严格大于限值才报、跨移交/签收拆分、入库不拆分、逐段归属保管人/在途）、断链待核三情形（非保管人移交/签收无在途/在途再移交）与不触发待核两情形（签收要素不符、重复签收）、乱序补录与移交/签收纠错后归属重算且原始读数与旧纠错记录不改、核定后拒绝新配置与读数（同号同内容重试仍幂等）、测温数据不影响核验结论且不产生版本、空读数与间隔边界、并发（同号同内容一建一幂等、同号异内容与同时刻一胜一败、配置并发一建一幂等/一建一冲突）。

---

## 八、目录结构

```
.
├── app/
│   ├── main.py       # FastAPI 路由、异常映射、启动初始化
│   ├── service.py    # 核心领域逻辑：幂等指纹、保管链重算、纠错（作废/替换）、核定冻结、批量移交、在途有效事件解析
│   ├── reconciliations.py # 整批对账签收：覆盖性清单校验、收妥项在途/签收资格、receive 与版本同事务、幂等/一次性/现时有效性查询
│   ├── versions.py   # 保管链版本：版本快照记录、版本 0 基线迁移、版本索引/指定版本查询
│   ├── dossiers.py   # 案卷封存：整卷原子封存、按编号排序清单、SHA-256 摘要与核验、同号幂等/冲突
│   ├── reviews.py    # 案卷双人独立复核：摘要核对、两份完整清单、分歧识别、第三人裁决、利益冲突回避、终局结论与查询
│   ├── identities.py # 人员标识与历史别名：全局唯一命名空间、修订号增补、姓名→人员归属解析
│   ├── temperature.py # 运输测温证据：一次性配置、读数记录（幂等/冲突/采集前/重复时刻）、超温点与缺测区间查询、保管归属重放与待核标注
│   ├── schemas.py    # 请求模型：事件、纠错（NewEventIn/CorrectionIn）、批量移交（TransferBatchIn）、整批对账（BatchReconciliationIn）、案卷（DossierIn）、复核（ReviewIn）、人员（PersonIn/AliasIn）、测温（TempConfigIn/TempReadingIn）按类型必填校验
│   ├── db.py         # SQLite 连接、建表与旧库迁移（批次/对账表、版本表与基线补建、案卷表、复核案/意见表、人员/别名表、测温配置/读数表）、IMMEDIATE 事务
│   ├── seed.py       # 演示数据初始化（幂等，含运输测温演示）
│   └── config.py     # 环境变量配置
├── examples/call_api.py   # 零依赖调用示例（含纠错、批量移交、整批对账签收、版本复核、案卷封存与双人复核、人员标识与复核回避、运输测温证据全流程）
├── tests/test_chain.py    # 既有规则端到端自测（42 项断言）
├── tests/test_corrections.py  # 纠错端到端自测（65 项断言，含并发）
├── tests/test_batches.py  # 批量移交端到端自测（83 项断言，含并发）
├── tests/test_reconciliations.py # 整批对账签收端到端自测（93 项断言，含并发）
├── tests/test_versions.py # 保管链版本复核端到端自测（94 项断言，含旧库迁移）
├── tests/test_dossiers.py # 案卷封存端到端自测（61 项断言，含并发与篡改核验）
├── tests/test_reviews.py  # 案卷双人复核端到端自测（80 项断言，含并发）
├── tests/test_identities.py # 人员标识与复核回避端到端自测（67 项断言，含并发）
├── tests/test_temperature.py # 运输测温证据端到端自测（83 项断言，含并发）
├── entrypoint.sh     # 容器入口：建表 → 演示数据 → 启动
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```
