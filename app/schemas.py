"""HTTP 请求体的 Pydantic 模型与按事件类型的必填字段校验。"""
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

EventType = Literal["transfer", "receive", "seal_check", "storage"]
CorrectionAction = Literal["void", "replace"]

# 各事件类型的必填字段（事件上报与纠错替换共用）
REQUIRED_FIELDS: dict[str, list[str]] = {
    "transfer": ["transfer_no", "from_custodian", "to_custodian"],
    "receive": ["transfer_no", "receiver", "seal_no"],
    "seal_check": ["checked_seal_no"],
    "storage": [],
}


class SampleIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号，业务唯一键")
    collected_at: datetime = Field(..., description="采集时间 ISO8601")
    collector: str = Field(..., min_length=1, description="采集人，即初始保管人")
    seal_no: str = Field(..., min_length=1, description="封签号")


class EventIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="全局唯一事件号，同号重试幂等")
    event_type: EventType
    sample_code: str = Field(..., min_length=1, description="样本编号")
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "EventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class NewEventIn(BaseModel):
    """纠错替换时提交的完整新事件内容（不含 sample_code —— 纠错不得跨样本）。

    字段集与校验规则与 EventIn 一致；新事件号必须全局唯一且未被占用。
    """

    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(..., min_length=1, description="新事件号，必须未被占用（全局唯一）")
    event_type: EventType
    occurred_at: datetime = Field(..., description="事件发生时间 ISO8601")

    # transfer（移交）
    transfer_no: str | None = Field(None, description="移交单号（transfer 必填）")
    from_custodian: str | None = Field(None, description="移交发起/交出方（transfer 必填，须为当前保管人）")
    to_custodian: str | None = Field(None, description="接收方（transfer 必填）")

    # receive（签收）
    receiver: str | None = Field(None, description="实际签收人（receive 必填，须等于移交的接收方）")
    seal_no: str | None = Field(None, description="签收核对封签号（receive 必填，须与登记一致）")

    # seal_check（封签检查）
    checked_seal_no: str | None = Field(None, description="检查时读取的封签号（seal_check 必填）")
    seal_intact: bool | None = Field(None, description="封签是否完好，默认 true；false 判无效")

    # storage（实验室入库）
    storage_location: str | None = Field(None, description="入库库位（storage 建议填写）")
    custodian: str | None = Field(None, description="入库后保管人，默认实验室")

    note: str | None = Field(None, description="备注，参与幂等指纹")

    @model_validator(mode="after")
    def _check_required_fields(self) -> "NewEventIn":
        missing = [f for f in REQUIRED_FIELDS[self.event_type] if not getattr(self, f)]
        if missing:
            raise ValueError(f"事件类型 {self.event_type} 缺少必填字段: {', '.join(missing)}")
        return self


class CorrectionIn(BaseModel):
    """核定前事件纠错：作废（void）或替换（replace）。

    * 仅核定入库前可纠错，纠错不得跨样本；
    * 同一纠错号同内容重试幂等（200），异内容冲突（409）；
    * replace 必须给出完整新事件内容与未占用的新事件号；void 不得携带新事件。
    """

    model_config = ConfigDict(extra="forbid")

    correction_id: str = Field(..., min_length=1, description="全局唯一纠错号，同号重试幂等")
    sample_code: str = Field(..., min_length=1, description="样本编号（纠错不得跨样本）")
    target_event_id: str = Field(..., min_length=1, description="被作废/替换的当前有效事件号")
    action: CorrectionAction = Field(..., description="void=作废，replace=替换")
    operator: str = Field(..., min_length=1, description="纠错操作人")
    reason: str = Field(..., min_length=1, description="纠错原因")
    new_event: NewEventIn | None = Field(None, description="替换时的完整新事件内容；作废时必须为空")

    @model_validator(mode="after")
    def _check_action_payload(self) -> "CorrectionIn":
        if self.action == "replace" and self.new_event is None:
            raise ValueError("action=replace 必须提供 new_event（完整新事件内容与未占用的新事件号）")
        if self.action == "void" and self.new_event is not None:
            raise ValueError("action=void 作废不得携带 new_event")
        return self


class ApproveIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    approver: str = Field(..., min_length=1, description="核定操作人")
    note: str | None = Field(None, description="核定备注")


class BatchTransferItemIn(BaseModel):
    """批量移交中的一份样本：样本编号、该份移交单号、该份既有 transfer 事件号。"""

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号")
    transfer_no: str = Field(..., min_length=1, description="该份样本的移交单号")
    event_id: str = Field(..., min_length=1, description="该份样本对应的（既有）transfer 事件号，全局唯一")


class TransferBatchIn(BaseModel):
    """同一交接车次多份样本的一次批量移交提交。

    * 批次号全局唯一；统一移交时间、交出人、接收人对批内各份样本生效；
    * 各份移交均作为既有 transfer 事件（event_id 全局唯一）落库；
    * 批内样本编号、事件号重复（在服务层按统一错误格式拒绝）；
    * 样本不存在/已核定、任一事件号已占用 → 整批失败，不留事件与批次。
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(..., min_length=1, description="全局唯一批次号，同号重试幂等")
    occurred_at: datetime = Field(..., description="统一移交时间 ISO8601")
    from_custodian: str = Field(..., min_length=1, description="交出人")
    to_custodian: str = Field(..., min_length=1, description="接收人")
    items: list[BatchTransferItemIn] = Field(..., min_length=1, description="批内各份样本（至少一份）")


class BatchReconciliationItemIn(BaseModel):
    """批量对账清单中的一份样本。

    二选一形态（由 reason 是否给出区分，模型层直接校验）：
      * 收妥项：填写唯一事件号 event_id 与实核封签号 verified_seal_no，不得填写 reason；
        须对应原批次该份样本仍有效且处于该笔移交在途状态的事件，
        签收人/封签/时间满足既有单份签收规则——服务层逐项核验。
      * 异常项：填写非空 reason，不得填写 event_id/verified_seal_no；不生成签收事件。
    """

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="原批次中的样本编号")
    event_id: str | None = Field(None, min_length=1, description="收妥项唯一签收事件号（全局唯一，落为既有 receive 事件）")
    verified_seal_no: str | None = Field(None, min_length=1, description="收妥项实核封签号（须与登记封签一致）")
    reason: str | None = Field(None, description="异常项原因；收妥项必须为空，异常项必填非空")

    @property
    def is_exception(self) -> bool:
        return self.reason is not None

    @model_validator(mode="after")
    def _check_shape(self) -> "BatchReconciliationItemIn":
        if self.reason is not None:
            if not self.reason.strip():
                raise ValueError("异常项必须填写非空异常原因（reason）")
            if self.event_id is not None or self.verified_seal_no is not None:
                raise ValueError("异常项仅填写原因，不得携带 event_id / verified_seal_no（不生成签收事件）")
        else:
            missing = [n for n in ("event_id", "verified_seal_no") if getattr(self, n) is None]
            if missing:
                raise ValueError(f"收妥项必须填写 {', '.join(missing)}（异常项则必须填写 reason）")
        return self


class BatchReconciliationIn(BaseModel):
    """同一批次到达接收方后的整批对账签收提交。

    * 签收单号（reconciliation_no）全局唯一：同号同内容重试幂等返回原结果，异内容冲突；
    * 统一签收人与统一签收时间；清单逐份覆盖原批次全部样本（缺项/重复/外来项整单拒绝）；
    * 收妥项（带 event_id + verified_seal_no）须对应仍有效且处于该笔移交在途状态的事件，
      签收人=在途移交接收人、实核封签=登记封签、签收时间晚于移交时间；
      成功时各收妥项作为既有 receive 事件与各样本新版本同事务提交；
    * 异常项（带 reason）仅留对账记录、不生成签收事件、不产生版本；
    * 一个批次只能成功对账一次；并发按已提交状态判定；失败整单拒绝且不留记录。
    """

    model_config = ConfigDict(extra="forbid")

    reconciliation_no: str = Field(..., min_length=1, description="全局唯一签收单号，同号重试幂等")
    receiver: str = Field(..., min_length=1, description="签收人（须匹配各收妥项在途移交的接收人）")
    occurred_at: datetime = Field(..., description="统一签收时间 ISO8601（须晚于各份移交时间）")
    items: list[BatchReconciliationItemIn] = Field(..., min_length=1, description="逐份对账清单（须恰好覆盖原批次全部样本）")


class DossierItemIn(BaseModel):
    """案卷中的一份样本：样本编号 + 可选的非负历史版本号（缺省取封存事务中的最新版本）。"""

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="样本编号")
    version: int | None = Field(None, ge=0, description="指定封存的非负历史版本号；缺省取最新版本")


class DossierIn(BaseModel):
    """同一抽检的多份样本保管链一次封存为案卷。

    * 案卷号全局唯一；至少两份不重复样本（重复/不存在/版本不存在 → 整卷失败不留案卷）；
    * 可逐份指定非负历史版本，未指定者取封存事务中的最新版本（同一数据库一致视图）；
    * 同案卷号同内容重试幂等（即使最新版本已变化也返回原卷），异内容冲突。
    """

    model_config = ConfigDict(extra="forbid")

    dossier_id: str = Field(..., min_length=1, description="全局唯一案卷号，同号重试幂等")
    items: list[DossierItemIn] = Field(..., min_length=2, description="卷内样本（至少两份，编号不重复）")


class ReviewItemIn(BaseModel):
    """案卷复核清单中的一份意见：卷内样本编号 + 通过/不通过 + 不通过原因。

    通过（passed=true）不得携带原因；不通过（passed=false）必须给出非空原因。
    """

    model_config = ConfigDict(extra="forbid")

    sample_code: str = Field(..., min_length=1, description="卷内样本编号，须命中案卷封存清单")
    passed: bool = Field(..., description="该份复核结论：true=通过，false=不通过")
    reason: str | None = Field(None, description="不通过原因；通过时必须为空，不通过时必填")

    @model_validator(mode="after")
    def _check_reason(self) -> "ReviewItemIn":
        if self.passed and self.reason is not None:
            raise ValueError("通过的份项不得填写不通过原因（reason 必须为空）")
        if not self.passed and not (self.reason and self.reason.strip()):
            raise ValueError("不通过的份项必须填写非空不通过原因（reason）")
        return self


class ReviewIn(BaseModel):
    """案卷复核意见提交（前两次为两名不同复核人的完整清单；第三次为第三人裁决）。

    * expected_digest 必须与案卷封存摘要一致，否则拒绝写入（复核只依据封存版本快照）；
    * 前两份意见由两名不同复核人各提交一次完整清单（覆盖卷内每份样本，不缺项/不重复份项）；
    * 前两份都到齐后，仅对有分歧的份项开放第三人裁决：裁决人不得是前两次复核人之一，
      裁决清单必须恰好覆盖全部分歧份项；全部份项终局后形成案卷结论；
    * 意见提交后不可改；同复核单号同内容重试幂等返回原结果，异内容冲突；
    * 越阶段提交（案卷终局后再提交、第二份未到就裁决、前两份缺漏时的裁决清单不全等）拒绝。
    """

    model_config = ConfigDict(extra="forbid")

    review_no: str = Field(..., min_length=1, description="全局唯一复核单号，同号重试幂等")
    reviewer: str = Field(..., min_length=1, description="复核人标识（工号/姓名等）")
    expected_digest: str = Field(
        ..., min_length=1,
        description="提交方持有的案卷封存摘要（SHA-256，64 位十六进制）；与库存不一致则拒绝写入",
    )
    items: list[ReviewItemIn] = Field(..., min_length=1, description="逐份样本意见（前两次须完整覆盖案卷清单）")


class PersonIn(BaseModel):
    """人员标识登记：全局唯一主标识 + 可选初始历史别名。

    * 主标识与全部别名构成全局唯一命名空间：任何名称只归属一人，
      别名不得归属多人、不得转移归属；
    * 登记成功后修订号为 1；事后增补别名须走别名接口并携带所见修订号；
    * 同主标识同内容（含初始别名集合，顺序不敏感）重试幂等，异内容冲突。
    """

    model_config = ConfigDict(extra="forbid")

    person_id: str = Field(..., min_length=1, description="人员标识（主标识，全局唯一）")
    aliases: list[str] = Field(default_factory=list, description="初始历史别名（可空，事后可经修订号增补）")

    @model_validator(mode="after")
    def _check_aliases(self) -> "PersonIn":
        for a in self.aliases:
            if not a or not a.strip():
                raise ValueError("初始别名不得为空或纯空白")
            if a == self.person_id:
                raise ValueError(f"初始别名 {a} 与人员主标识相同，无需重复登记")
        if len(set(self.aliases)) != len(self.aliases):
            raise ValueError("初始别名列表存在重复项")
        return self


class AliasIn(BaseModel):
    """增补历史别名：须携带所见修订号（GET 人员信息取得），过期（与当前修订号不一致）则拒绝。"""

    model_config = ConfigDict(extra="forbid")

    alias: str = Field(..., min_length=1, description="待增补的历史别名（全局唯一，不得归属多人或转移）")
    expected_revision: int = Field(..., ge=1, description="所见修订号；与人员当前修订号不一致（过期）则拒绝")
